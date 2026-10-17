"""Offline generated workbooks and mocked SMTP; no live requests or credentials."""
from datetime import date
from email import policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from contextlib import redirect_stderr, redirect_stdout
import pandas as pd
from openpyxl import Workbook
import analyze_purchase_report as analysis
import send_purchase_email as emailer
import run_daily_purchase_report as daily
from purchase_common import WorkflowError, job_lock, report_date, run_cli
import login_test

DAY = date(2026, 10, 17)
HEADERS = ['Item Code', 'Item Name', 'Unit', 'Quantity', 'Net Amount', 'Gross Amount', 'Purchase Date', 'Branch']


def workbook(path, rows, headers=HEADERS, banner=True):
    book = Workbook()
    sheet = book.active
    if banner:
        sheet.append(['Purchase report'])
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = self.base / 'report.xlsx'

    def sample(self):
        return workbook(self.path, [
            ['A-1', '<script>alert(1)</script>', 'kg', 2, 24, 20, '17-10-2026', 'North'],
            ['A-1', '<script>alert(1)</script>', 'kg', 3, 36, 30, '17-10-2026', 'North'],
            ['B', 'New item', 'box', 1, 0, 99, '17-10-2026', 'South'],
            [None, 'Grand Total', None, 6, 60, 149, None, None],
            ['RET', 'Return', 'kg', -1, -10, -10, '17-10-2026', 'North']])

    def test_net_weighted_rate_totals_and_zero_amount(self):
        summary, folder = analysis.analyze(self.sample(), DAY, self.base)
        self.assertEqual(summary['total_rows'], 3)
        self.assertEqual(summary['total_unique_items'], 2)
        self.assertEqual(summary['total_purchase_amount'], 60)
        self.assertEqual(summary['items'][0]['current_rate'], 12)
        self.assertEqual(summary['items'][1]['current_rate'], 0)
        self.assertEqual(summary['excluded_nonpurchase_rows'], 2)
        self.assertTrue((folder / 'comparison.csv').exists())
        json.loads((folder / 'summary.json').read_text())

    def test_most_recent_available_history_same_unit(self):
        workbook(self.path, [['A', 'Apple', 'kg', 2, 16, 14, '10-10-2026', 'North']])
        analysis.analyze(self.path, date(2026, 10, 10), self.base)
        workbook(self.path, [['A', 'Apple', 'kg', 1, 10, 9, '15-10-2026', 'North'],
                             ['A', 'Apple', 'box', 1, 99, 99, '15-10-2026', 'North']])
        analysis.analyze(self.path, date(2026, 10, 15), self.base)
        workbook(self.path, [['A', 'Apple', 'kg', 3, 36, 33, '17-10-2026', 'North']])
        summary, _ = analysis.analyze(self.path, DAY, self.base)
        row = summary['items'][0]
        self.assertEqual(row['previous_rate'], 10)
        self.assertEqual(row['absolute_difference'], 2)
        self.assertEqual(row['percent_difference'], 20)

    def test_latest_timestamp_not_average_entire_history(self):
        records = [dict(identity='code:a', item='Apple', unit='kg', branch='', quantity=1,
                        amount=12, amount_basis='net', purchase_at='2026-10-17T00:00:00')]
        history = [dict(records[0], amount=5, purchase_at='2026-10-15T08:00:00'),
                   dict(records[0], amount=10, purchase_at='2026-10-15T14:00:00')]
        result, _ = analysis.summarize(records, history, DAY, False)
        self.assertEqual(result['items'][0]['previous_rate'], 10)

    def test_overlapping_range_does_not_double_count_prior_history(self):
        # A current range export is authoritative for the dates it covers.
        row = dict(identity='code:a', item='Apple', unit='kg', branch='', quantity=1,
                   amount=12, amount_basis='net', purchase_at='2026-10-17T00:00:00')
        old = dict(row, amount=10, purchase_at='2026-10-15T00:00:00')
        stale = dict(old, amount=1)
        summary, _ = analysis.summarize([row, old], [stale], DAY, False)
        self.assertEqual(summary['items'][0]['previous_rate'], 10)

    def test_ambiguous_numeric_separator_rejected(self):
        with self.assertRaises(WorkflowError):
            analysis.number('1,25')
        self.assertEqual(analysis.number('(1,250.50)'), -1250.5)

    def test_empty_export_produces_zero_summary(self):
        workbook(self.path, [])
        summary, folder = analysis.analyze(self.path, DAY, self.base)
        self.assertEqual(summary['total_purchase_amount'], 0)
        self.assertEqual(summary['items'], [])
        self.assertTrue((folder / 'comparison.csv').read_text(encoding='utf-8-sig').startswith('item,'))

    def test_daily_sent_job_skips_browser_and_email(self):
        self.smtp_env()
        os.environ['DOWNLOAD_BASE_DIR'] = str(self.base)
        folder = self.base / 'processed' / DAY.isoformat()
        folder.mkdir(parents=True)
        (folder / 'email_delivery.json').write_text('{"status":"sent"}')
        with patch.object(daily, 'download_report') as download, patch.object(daily, 'deliver') as deliver:
            daily.run(DAY)
            download.assert_not_called()
            deliver.assert_not_called()

    def test_optional_branch_aware(self):
        os.environ['PURCHASE_BRANCH_AWARE'] = 'true'
        workbook(self.path, [['A', 'Apple', 'kg', 1, 10, 10, '15-10-2026', 'North'],
                             ['A', 'Apple', 'kg', 1, 12, 12, '17-10-2026', 'South']])
        summary, _ = analysis.analyze(self.path, DAY, self.base)
        self.assertIsNone(summary['items'][0]['previous_rate'])

    def test_gross_fallback(self):
        workbook(self.path, [['A', 'Apple', 'kg', 2, None, 20, '17-10-2026', 'North']])
        summary, _ = analysis.analyze(self.path, DAY, self.base)
        self.assertEqual(summary['items'][0]['current_rate'], 10)
        self.assertEqual(summary['items'][0]['amount_basis'], 'gross')

    def test_ambiguous_columns_fail_closed(self):
        workbook(self.path, [['Apple', 'Apple', 1, 2, 'kg']],
                 ['Item', 'Item Name', 'Qty', 'Amount', 'Unit'])
        with self.assertRaises(WorkflowError):
            analysis.analyze(self.path, DAY, self.base)
        os.environ['PURCHASE_COLUMN_MAP'] = '{"item":"Item Name"}'
        summary, _ = analysis.analyze(self.path, DAY, self.base)
        self.assertEqual(summary['total_rows'], 1)

    def test_missing_unit_requires_explicit_default(self):
        workbook(self.path, [['Apple', 1, 2]], ['Item', 'Qty', 'Amount'])
        with self.assertRaises(WorkflowError):
            analysis.analyze(self.path, DAY, self.base)
        os.environ['PURCHASE_DEFAULT_UNIT'] = 'each'
        summary, _ = analysis.analyze(self.path, DAY, self.base)
        self.assertEqual(summary['items'][0]['unit'], 'each')

    def test_malformed_quantity_not_silently_skipped(self):
        workbook(self.path, [['A', 'Apple', 'kg', 'unknown', 10, 10, '17-10-2026', 'North']])
        with self.assertRaises(WorkflowError):
            analysis.analyze(self.path, DAY, self.base)

    def test_zero_previous_rate_has_no_percentage(self):
        row = dict(identity='a', item='A', unit='kg', branch='', quantity=1, amount=2,
                   amount_basis='net', purchase_at='2026-10-17T00:00:00')
        summary, _ = analysis.summarize([row], [dict(row, amount=0, purchase_at='2026-10-16T00:00:00')], DAY, False)
        self.assertEqual(summary['items'][0]['absolute_difference'], 2)
        self.assertIsNone(summary['items'][0]['percent_difference'])

    def test_html_escaped_and_csv_formula_safe(self):
        summary, _ = analysis.analyze(self.sample(), DAY, self.base)
        html = emailer.render_html(summary)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        csv = analysis.safe_csv(pd.DataFrame({'item': ['=CMD()', '  +formula']})).decode('utf-8-sig')
        self.assertIn("'=CMD()", csv)
        self.assertIn("'  +formula", csv)

    def test_dry_run_no_login_no_smtp_and_isolated_outputs(self):
        self.sample()
        os.environ['DOWNLOAD_BASE_DIR'] = str(self.base)
        with patch.object(daily, 'download_report') as download, patch.object(emailer.smtplib, 'SMTP') as smtp:
            daily.run(DAY, self.path, dry_run=True)
            download.assert_not_called()
            smtp.assert_not_called()
        self.assertTrue((self.base / 'dry-run' / 'processed' / DAY.isoformat() / 'email_preview.html').exists())
        self.assertFalse((self.base / 'processed').exists())

    def smtp_env(self):
        os.environ.update(SMTP_HOST='smtp.invalid', EMAIL_FROM='sender@example.test',
                          EMAIL_TO='one@example.test,two@example.test', SMTP_USERNAME='dummy', SMTP_PASSWORD='dummy')

    def test_tls_attachment_multiple_recipients_and_idempotency(self):
        summary, folder = analysis.analyze(self.sample(), DAY, self.base)
        self.smtp_env()
        with patch.object(emailer.smtplib, 'SMTP') as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            emailer.deliver(summary, self.path, folder, send=True)
            client.starttls.assert_called_once()
            message = client.send_message.call_args.args[0]
            parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
            self.assertEqual(len(list(parsed.iter_attachments())), 2)
            self.assertEqual(client.send_message.call_args.kwargs['to_addrs'], ['one@example.test', 'two@example.test'])
            with self.assertRaises(WorkflowError):
                emailer.deliver(summary, self.path, folder, send=True)
            self.assertEqual(client.send_message.call_count, 1)
        self.assertEqual(json.loads((folder / 'email_delivery.json').read_text())['status'], 'sent')

    def test_smtp_uncertain_failure_retains_pending(self):
        summary, folder = analysis.analyze(self.sample(), DAY, self.base)
        self.smtp_env()
        with patch.object(emailer.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.side_effect = OSError('private server details')
            with self.assertRaises(OSError):
                emailer.deliver(summary, self.path, folder, send=True)
        self.assertEqual(json.loads((folder / 'email_delivery.json').read_text())['status'], 'pending')

    def test_header_injection_and_plain_smtp_rejected(self):
        self.smtp_env()
        os.environ['EMAIL_TO'] = 'ok@example.test\r\nBcc: wrong@example.test'
        with self.assertRaises(WorkflowError):
            emailer.smtp_settings()
        os.environ['EMAIL_TO'] = 'ok@example.test'
        os.environ['SMTP_SECURITY'] = 'none'
        with self.assertRaises(WorkflowError):
            emailer.smtp_settings()

    def test_safe_cli_no_exception_contents(self):
        captured = io.StringIO()
        def fail():
            raise ValueError('private credential token item data')
        with redirect_stdout(captured), redirect_stderr(captured):
            self.assertEqual(run_cli(fail), 1)
        self.assertNotIn('private credential', captured.getvalue())

    def test_lock_prevents_overlap_and_cleans_up(self):
        with job_lock(self.base, DAY):
            with self.assertRaises(WorkflowError):
                with job_lock(self.base, DAY):
                    pass
        self.assertFalse((self.base / 'state' / f'{DAY}.lock').exists())

    def test_date_modes(self):
        self.assertEqual(report_date('2026-10-17'), DAY)
        self.assertEqual((report_date('today') - report_date('yesterday')).days, 1)

    def test_submit_click_fix_is_preserved(self):
        page = MagicMock()
        page.url = login_test.URL
        page.goto.return_value.status = 200
        selectors = dict(login_test.DEFAULTS, SUCCESS='#confirmed')
        controls = {name: MagicMock() for name in selectors.values()}
        page.locator.side_effect = controls.__getitem__
        with patch.object(login_test, 'verify_login'):
            login_test.login(page, 'dummy', 'dummy', 1000, selectors)
        controls[selectors['SUBMIT']].click.assert_called_once_with(no_wait_after=True)



if __name__ == '__main__':
    unittest.main()
