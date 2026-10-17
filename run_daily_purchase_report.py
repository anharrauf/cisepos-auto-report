"""Daily purchase export -> local rate analysis -> HTML/SMTP delivery."""
import argparse
import json
from pathlib import Path
from analyze_purchase_report import analyze, normalize_records, history_records, summarize, save_analysis
from download_purchase_report import download_report
from send_purchase_email import deliver, smtp_settings
from purchase_common import (WorkflowError, base_directory, diagnostic, job_lock,
                             report_date, run_cli)


def run(day, input_path=None, dry_run=False, allow_resend=False):
    base = base_directory()
    # Dry runs never touch production history or delivery state.
    work_base = base / 'dry-run' if dry_run else base
    if dry_run and input_path is None:
        raise WorkflowError('Dry-run requires --input with an already-downloaded workbook.')
    if not dry_run:
        diagnostic('configuration-email')
        smtp_settings()  # Validate required delivery settings before attempting login.
    with job_lock(work_base, day):
        production_folder = base / 'processed' / day.isoformat()
        journal = production_folder / 'email_delivery.json'
        if not dry_run and journal.exists() and not allow_resend:
            if json.loads(journal.read_text(encoding='utf-8')).get('status') == 'sent':
                diagnostic('daily-already-delivered-skip')
                return
            raise WorkflowError('Previous email delivery is uncertain; inspect before retrying.')
        path = Path(input_path) if input_path is not None else download_report(day, base)
        # Reuse production history during dry-run, but write outputs only to dry-run.
        if dry_run:
            diagnostic('dry-run-analysis')
            records, excluded, branch_aware = normalize_records(path, day)
            summary, current = summarize(records, history_records(base, day), day, branch_aware)
            summary['excluded_nonpurchase_rows'] = excluded
            folder = work_base / 'processed' / day.isoformat()
            save_analysis(summary, current, folder)
        else:
            summary, folder = analyze(path, day, base)
        deliver(summary, path, folder, send=not dry_run, allow_resend=allow_resend)


def cli():
    parser = argparse.ArgumentParser(description='Daily purchase workflow, yesterday by default.')
    parser.add_argument('--date', help='yesterday, today or YYYY-MM-DD')
    parser.add_argument('--input', type=Path, help='Use local XLSX instead of logging in or downloading')
    parser.add_argument('--dry-run', action='store_true', help='Local analysis and HTML preview only; requires --input')
    parser.add_argument('--allow-resend', action='store_true', help='Only after checking any prior delivery manually')
    args = parser.parse_args()
    run(report_date(args.date), args.input, args.dry_run, args.allow_resend)


if __name__ == '__main__':
    raise SystemExit(run_cli(cli))
