"""Escaped HTML dashboards, local previews and TLS SMTP with delivery journaling."""
import argparse
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, getaddresses
from html import escape
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
from purchase_common import (WorkflowError, atomic_bytes, atomic_json,
                             diagnostic, integer, required, run_cli)
from download_purchase_report import validate_xlsx


def money(value):
    return '—' if value is None else f'{value:,.2f}'


def insights(summary):
    items = summary['items']
    up = sum(1 for r in items if (r['absolute_difference'] or 0) > 0)
    down = sum(1 for r in items if (r['absolute_difference'] or 0) < 0)
    new = sum(1 for r in items if r.get('comparison_status') == 'new_item')
    legacy = sum(1 for r in items if r.get('comparison_status') == 'legacy_rate_unavailable')
    return (f"Purchases totaled {money(summary['total_purchase_amount'])} across "
            f"{summary['total_unique_items']} unique items, {summary.get('total_transactions', 0)} purchasing transactions and {summary['total_rows']} item-level rows. "
            f"Compared with the latest locally available prior purchase, {up} item/unit groups increased "
            f"in PurchaseRate and {down} decreased; {new} have no prior history and {legacy} lack a recorded prior PurchaseRate. "
            f"Reported tax totaled {money(summary.get('total_tax_amount'))} and discounts totaled {money(summary.get('total_discount_amount'))}. "
            "PurchaseRate comparisons are quantity-weighted by item, unit and branch. Effective net rates (NetAmount / Quantity) are shown separately.")


def render_html(summary):
    items = summary['items']
    increases = sorted([r for r in items if (r['absolute_difference'] or 0) > 0],
                       key=lambda r: (r['percent_difference'] is not None, r['percent_difference'] or 0), reverse=True)
    decreases = sorted([r for r in items if (r['absolute_difference'] or 0) < 0],
                       key=lambda r: r['percent_difference'] if r['percent_difference'] is not None else 0)
    def table(title, rows):
        body = ''
        for row in rows[:10]:
            change = row['percent_difference']
            shade = '#b42318' if (change or 0) > 0 else '#067647'
            name = escape(str(row['item']))
            unit = escape(str(row['unit']))
            branch = escape(str(row.get('branch') or 'All branches'))
            effective = money(row.get('current_effective_net_rate'))
            old_effective = money(row.get('previous_effective_net_rate'))
            body += (f'<tr><td style="padding:9px;border-bottom:1px solid #eee">{name}<br><small>{unit} · {branch}</small></td>'
                     f'<td>{money(row["current_rate"])}</td><td>{money(row["previous_rate"])}</td>'
                     f'<td>{effective}<br><small>Prior: {old_effective}<br>Δ {money(row.get("effective_absolute_difference"))} ({money(row.get("effective_percent_difference"))}%)</small></td>'
                     f'<td style="color:{shade}">{money(row["absolute_difference"])}<br>{money(change)}%</td>'
                     f'<td>{money(row["total_quantity"])}</td><td>{money(row["total_amount"])}</td></tr>')
        if not body:
            body = '<tr><td colspan="7" style="padding:12px">No items in this category.</td></tr>'
        return (f'<h2 style="font-size:18px;margin-top:28px">{title}</h2>'
                '<table width="100%" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px;text-align:left">'
                '<tr style="background:#edf2f7"><th>Item / unit</th><th>Current PurchaseRate<br>(qty-weighted)</th><th>Previous PurchaseRate</th><th>Effective net rate</th><th>PurchaseRate change</th><th>Qty</th><th>Amount</th></tr>'
                + body + '</table>')
    return ('<!doctype html><html><body style="margin:0;background:#f2f4f7;font-family:Arial,sans-serif;color:#182230">'
            '<table width="100%" cellpadding="20"><tr><td align="center"><table width="860" style="max-width:100%;background:white;border-radius:12px" cellpadding="24"><tr><td>'
            f'<h1 style="color:#175cd3">Daily purchase dashboard</h1><p>{escape(summary["report_date"])}</p>'
            f'<table width="100%" cellpadding="14" style="background:#eff8ff"><tr><td>Total purchases<br><b>{money(summary["total_purchase_amount"])}</b></td>'
            f'<td>Unique items<br><b>{summary["total_unique_items"]}</b></td><td>Transactions<br><b>{summary.get("total_transactions", 0)}</b></td><td>Purchase rows<br><b>{summary["total_rows"]}</b></td></tr></table>'
            f'<p style="line-height:1.6">{escape(insights(summary))}</p>'
            + table('Biggest rate increases', increases) + table('Biggest rate decreases', decreases)
            + table('New items / no prior history', [r for r in items if r.get('comparison_status') == 'new_item'])
            + table('Legacy history missing PurchaseRate', [r for r in items if r.get('comparison_status') == 'legacy_rate_unavailable'])
            + table('Top purchases by total amount', sorted(items, key=lambda r: r['total_amount'], reverse=True))
            + '<p style="font-size:12px;color:#667085">Automated, data-derived narrative; no external AI service used. '
              'Comparisons cover available local history only. Date-only previous PurchaseRates are weighted across the latest purchase day. Legacy history without PurchaseRate is not treated as a PurchaseRate comparison. '
              'A missing percentage can mean no history or a zero previous rate. Figures use the export currency; returns and subtotal rows are excluded.</p>'
              '</td></tr></table></td></tr></table></body></html>')


def addresses(value):
    if '\r' in value or '\n' in value:
        raise WorkflowError('Email headers must not contain line breaks.')
    parsed = getaddresses([value])
    result = [address for _, address in parsed]
    if not result or any(not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', a) for a in result):
        raise WorkflowError('Email addresses must be valid comma-separated mailboxes.')
    return result


def smtp_settings():
    sender = addresses(required('EMAIL_FROM'))
    recipients = addresses(required('EMAIL_TO'))
    if len(sender) != 1:
        raise WorkflowError('Configure one sender mailbox.')
    mode = os.getenv('SMTP_SECURITY', 'starttls').lower()
    if mode not in ('starttls', 'ssl'):
        raise WorkflowError('SMTP_SECURITY must be starttls or ssl; plaintext delivery is disabled.')
    username, password = os.getenv('SMTP_USERNAME', ''), os.getenv('SMTP_PASSWORD', '')
    if bool(username) != bool(password):
        raise WorkflowError('Configure both SMTP credentials or neither for an authorized relay.')
    prefix = os.getenv('EMAIL_SUBJECT_PREFIX', 'CISEPOS Purchases')
    if '\r' in prefix or '\n' in prefix:
        raise WorkflowError('Email subject prefix must be a single line.')
    return {'host': required('SMTP_HOST'), 'port': integer('SMTP_PORT', 465 if mode == 'ssl' else 587, maximum=65535),
            'mode': mode, 'sender': sender[0], 'recipients': recipients, 'username': username,
            'password': password, 'prefix': prefix, 'timeout': integer('SMTP_TIMEOUT_SECONDS', 60, maximum=300)}


def build_message(summary, excel, csv_path, settings):
    excel = Path(excel)
    validate_xlsx(excel)
    files = [(excel, 'application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet')]
    if csv_path is not None:
        files.append((Path(csv_path), 'text', 'csv'))
    limit = integer('EMAIL_MAX_ATTACHMENT_MB', 20, maximum=100) * 1024 * 1024
    if sum(path.stat().st_size for path, _, _ in files) > limit:
        raise WorkflowError('Attachments exceed the configured limit.')
    message = EmailMessage()
    message['Subject'] = f'{settings["prefix"]} | {summary["report_date"]}'
    message['From'] = settings['sender']
    message['To'] = ', '.join(settings['recipients'])
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid()
    message.set_content(insights(summary))
    message.add_alternative(render_html(summary), subtype='html')
    for path, main, sub in files:
        message.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    return message


def deliver(summary, excel, folder, send=False, include_csv=True, allow_resend=False):
    folder = Path(folder)
    diagnostic('email-render-preview')
    atomic_bytes(folder / 'email_preview.html', render_html(summary).encode('utf-8'))
    if not send:
        return  # No SMTP configuration, connection, or email side effect.
    diagnostic('email-configuration')
    settings = smtp_settings()
    message = build_message(summary, excel, folder / 'comparison.csv' if include_csv else None, settings)
    diagnostic('email-tls-setup')
    tls = ssl.create_default_context()
    journal = folder / 'email_delivery.json'
    # Exclusive claim prevents two manual email invocations from delivering twice.
    if journal.exists():
        old = json.loads(journal.read_text(encoding='utf-8'))
        if not allow_resend or old.get('status') not in ('sent', 'pending'):
            raise WorkflowError('Delivery exists or is uncertain. Check SMTP before explicitly allowing a resend.')
        journal.unlink()
    try:
        with journal.open('x', encoding='utf-8') as stream:
            json.dump({'status': 'pending'}, stream)
    except FileExistsError:
        raise WorkflowError('Another email delivery already claimed this report.') from None
    diagnostic('email-smtp-delivery')
    # A pending journal is deliberately retained on any error, including uncertain
    # acceptance after a network disconnect. Automatic retries could duplicate mail.
    client_class = smtplib.SMTP_SSL if settings['mode'] == 'ssl' else smtplib.SMTP
    kwargs = {'context': tls} if settings['mode'] == 'ssl' else {}
    with client_class(settings['host'], settings['port'], timeout=settings['timeout'], **kwargs) as client:
        client.ehlo()
        if settings['mode'] == 'starttls':
            client.starttls(context=tls)
            client.ehlo()
        if settings['username']:
            client.login(settings['username'], settings['password'])
        refused = client.send_message(message, from_addr=settings['sender'], to_addrs=settings['recipients'])
        if refused:
            raise WorkflowError('Some recipients were refused; delivery needs manual review.')
    atomic_json(journal, {'status': 'sent'})


def cli():
    parser = argparse.ArgumentParser(description='Render email preview; send only with --send.')
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--excel', type=Path, required=True)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--no-csv', action='store_true')
    parser.add_argument('--allow-resend', action='store_true')
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding='utf-8'))
    deliver(summary, args.excel, args.summary.parent, args.send, not args.no_csv, args.allow_resend)


if __name__ == '__main__':
    raise SystemExit(run_cli(cli))
