# CISEPOS daily purchase reports

Python 3.13 / Playwright 1.62 automation for Windows Server 2022 (also supports headless Ubuntu). One login → All Branches Purchase Report → XLSX → historical rate analysis → HTML dashboard → TLS SMTP email with Excel and optional CSV attachments. The existing login remains usable separately, including the VPS-confirmed `click(no_wait_after=True)` submit behavior.

## Windows VPS setup

In PowerShell, from your cloned repository:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
```

Keep your existing `.env` rather than overwriting it on upgrades; copy only the new fields from `.env.example`. Edit locally. Restrict its Windows ACL to the service account and administrators. Never paste credentials into chat or commit them. Exported environment variables override `.env`; dotenv interpolation is disabled. Use Playwright-installed Chromium unless `CISEPOS_CHROMIUM_PATH` intentionally selects another installation. `tzdata` supports IANA time zones on Windows.

Ubuntu alternative:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
cp .env.example .env
chmod 600 .env
```

Use the virtual environment interpreter in all commands. No display is required with `CISEPOS_HEADLESS=true`. Allow HTTPS access to CISEPOS and the configured SMTP destination/port. TLS verification remains enabled.

## One-time deployment configuration

The login behavior works on the VPS, but the report's actual control selectors and Excel schema were not available during development. No live requests were made for this feature. Confirm the following manually before scheduling. The code refuses to guess missing controls and does not retry logins.

- Preserve `CISEPOS_USERNAME`, `CISEPOS_PASSWORD`, and any working login-selector overrides. Set `CISEPOS_SUCCESS_SELECTOR` to exactly one visible authenticated-only dashboard element. Login verification requires the exact `/public/dashboard` path plus that element.
- Confirm CSS selectors for `CISEPOS_REPORT_START_SELECTOR`, `END_SELECTOR`, `TYPE_SELECTOR`, `GENERATE_SELECTOR`, `TABLE_SELECTOR`, `LENGTH_SELECTOR`, and `EXCEL_SELECTOR` (each prefixed `CISEPOS_REPORT_`). Use unique selectors; a table ID is ideal. Report selectors must be standard CSS because the redraw observer uses the same selectors in the DOM. No report controls have guessed defaults.
- The start/end inputs must accept text in `dd-mm-yyyy`, as specified for this deployment. The report type selector is a native `<select>` with visible option label `WithTax&Disc`. The page-length selector is a native `<select>` with an `All` option. A custom widget requires a page-specific adapter, not an invented selector.
- Set `CISEPOS_PURCHASE_REPORT_URL` to the provided report URL (already in `.env.example`). Only the existing CISEPOS HTTPS origin is accepted. `CISEPOS_REPORT_TIMEOUT_MS=120000` controls report actions/export; the login retains `CISEPOS_TIMEOUT_MS`.
- Generate must redraw the table, trigger a DataTables draw, or load a new document. The script waits for that signal and for any DataTables processing indicator to disappear; an old visible table alone is insufficient. After selecting All it waits again. For initialized DataTables it checks loaded row count against filtered record count and refuses known partial exports. Verify once that the resulting Excel covers all branches/items; custom server-side export implementations can need an adapter.
- The Excel button must produce a real `.xlsx` download. Legacy `.xls`, HTML disguised as Excel, or a button that opens a new window instead of downloading are not supported. Such exports fail rather than being silently renamed.

## Excel schema and calculations

An `.xlsx` file is read by pandas/openpyxl. Header auto-detection scans the first 40 rows; `PURCHASE_HEADER_ROW` optionally supplies a zero-based row index. `PURCHASE_SHEET=0` selects the first sheet; a sheet name is also accepted. Column names are Unicode-normalized and matched to common headings. Ambiguity or malformed item data fails rather than producing misleading totals.

Configure `PURCHASE_COLUMN_MAP` when headings differ or there are multiple candidate columns. For example, in `.env`:

```dotenv
PURCHASE_COLUMN_MAP='{"item_code":"SKU","item":"Product Name","unit":"UOM","quantity":"Qty","net_amount":"Final Amount","gross_amount":"Amount","purchase_date":"Invoice Date","branch":"Branch"}'
```

Canonical fields: `item_code`, `item`, `unit`, `quantity`, `net_amount`, `gross_amount`, `purchase_date`, `branch`, `row_type`. Map only fields present in your export. A unique item code is preferred; item name is the fallback identity. Confirm that amounts are **item-line totals**, not repeated invoice-level totals, and that the workbook uses one currency. Currency symbols are not guessed or converted. Numbers use decimal dots and optional comma thousands separators. Dates are interpreted day-first; native Excel dates and Excel serial dates are supported.

A unit column is required unless `PURCHASE_DEFAULT_UNIT` explicitly supplies the one unit used by every row. Units are normalized for case/spacing, but no kg/gram/box conversion is attempted. `PURCHASE_BRANCH_AWARE=true` additionally keys comparisons by branch and requires a branch column; otherwise quantities and amounts across branches are combined.

For each item/unit (and optional branch):

1. Exclude subtotal/total rows, empty rows, and nonpositive quantities/returns. An item with malformed quantity or amount fails validation. Adjustments and returns are not included in positive purchase totals.
2. Prefer the row's final/net amount, including zero. Use gross amount only when net is absent. The selected basis is recorded in outputs and the email; compare differing net/gross bases with care.
3. Current rate = sum of selected amounts / sum of positive quantities on the requested report date. Current totals are quantity-weighted, not an average of rates.
4. Find the latest earlier locally available purchase timestamp for the same comparison key. When only dates exist, use the weighted rate of all rows on the latest purchase day. A date gap does not prevent comparison. A zero prior rate has an absolute difference but no percentage.
5. Emit current/previous rates, prior timestamp, absolute/percent differences, total quantity/amount and item-line row count. `total_rows` is **item-level purchase rows**, not distinct invoices.

History comes from earlier `data/processed/YYYY-MM-DD/records.json` snapshots and earlier dated rows in the current workbook. Overlapping imports are not double-counted. Only the requested day's rows are saved in its snapshot. Future snapshots/rows never influence comparisons. Re-running a date replaces its analysis snapshot; it does not append duplicates. If a workbook lacks a purchase-date column, all its rows are assigned the requested date; use only single-day exports in that case.

This system cannot infer unseen CISEPOS history. Bootstrap it by analyzing historical downloads in chronological order. The first known purchase is shown as “no prior comparison,” not a zero rate. Preserve and back up normalized history on the VPS. Do not change item codes, unit mappings or branch mode mid-history without rebuilding affected snapshots.

## Run each component

Commands below are PowerShell; replace the example dates and paths locally.

Login smoke test (one attempt, no reports):

```powershell
.\.venv\Scripts\python.exe login_test.py
```

Download only (one login, no analysis or email):

```powershell
.\.venv\Scripts\python.exe download_purchase_report.py --date 2026-10-17
```

It sets both dates to that day. Optional `--start-date 2026-10-01 --end-date 2026-10-17` downloads a range for history import; `--date` labels the saved folder/file. Analyze each desired report day with the same workbook if importing a range that has a purchase-date column.

Analysis only (fully offline; no login/SMTP):

```powershell
.\.venv\Scripts\python.exe analyze_purchase_report.py --input data\raw\2026-10-17\purchase_report_2026-10-17.xlsx --date 2026-10-17
```

Email preview only (default; no SMTP connection):

```powershell
.\.venv\Scripts\python.exe send_purchase_email.py --summary data\processed\2026-10-17\summary.json --excel data\raw\2026-10-17\purchase_report_2026-10-17.xlsx
```

Send the prepared email with Excel and CSV attachments:

```powershell
.\.venv\Scripts\python.exe send_purchase_email.py --summary data\processed\2026-10-17\summary.json --excel data\raw\2026-10-17\purchase_report_2026-10-17.xlsx --send
```

Use `--no-csv` to omit CSV. The HTML dashboard includes totals, unique items, purchase rows, increases/decreases, new items and top purchases. Names are HTML-escaped; CSV text is protected against spreadsheet formula injection. The plain-English paragraph is generated deterministically from computed figures: it is an AI-style data narrative, **not an external LLM call**. Purchase data is not sent to an AI provider. No images or JavaScript are needed in the email.

SMTP configuration: `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY=starttls` (usually 587) or `ssl` (usually 465), `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, comma-separated `EMAIL_TO`, and `EMAIL_SUBJECT_PREFIX`. Both SMTP credentials may be empty only for an authorized TLS relay. Plain SMTP is disabled. `EMAIL_MAX_ATTACHMENT_MB` limits raw attachment bytes (default 20 MB; allow for MIME overhead and your provider's limits). `SMTP_TIMEOUT_SECONDS` defaults to 60. No SMTP debug output is enabled.

## Full workflow and dry run

```powershell
# Safe first test: existing file, no login, no network or email
.\.venv\Scripts\python.exe run_daily_purchase_report.py --date 2026-10-17 --input C:\Reports\purchase.xlsx --dry-run

# Full automatic workflow: downloads and SENDS email
.\.venv\Scripts\python.exe run_daily_purchase_report.py

# Explicit modes
.\.venv\Scripts\python.exe run_daily_purchase_report.py --date yesterday
.\.venv\Scripts\python.exe run_daily_purchase_report.py --date today
.\.venv\Scripts\python.exe run_daily_purchase_report.py --date 2026-10-17
```

`REPORT_DATE_MODE=yesterday` is the default. `REPORT_TIMEZONE=Asia/Karachi` determines today/yesterday independently of the Windows machine's time zone. `DOWNLOAD_BASE_DIR=data` is resolved relative to the project, not Task Scheduler's working directory; absolute paths are accepted. A full run with `--input` skips the browser but still **sends** email unless `--dry-run` is present.

Local outputs:

```text
data/raw/YYYY-MM-DD/purchase_report_YYYY-MM-DD.xlsx
data/processed/YYYY-MM-DD/comparison.csv
data/processed/YYYY-MM-DD/summary.json
data/processed/YYYY-MM-DD/records.json
data/processed/YYYY-MM-DD/email_preview.html
data/processed/YYYY-MM-DD/email_delivery.json
data/state/YYYY-MM-DD.lock/
```

Dry runs use production history read-only and write their outputs under `data/dry-run/processed/YYYY-MM-DD/`; they cannot mark production mail as delivered. Protect all these files as business data. They are intentionally excluded from Git. File writes use a temporary file plus atomic rename; `.partial.xlsx` is never treated as a completed download.

## Scheduling later with Windows Task Scheduler

After validating selectors, columns, report completeness and one delivery on the VPS:

1. Open **Task Scheduler → Create Task**. Use a dedicated account with permission to the repository, `.env`, Chromium cache and data directory. Choose “Run whether user is logged on or not.” Install the Playwright browser as that same account.
2. Set a daily trigger after the previous day's purchasing is complete, using your desired local schedule.
3. Action **Start a program**: Program `C:\path\cisepos-auto-report\.venv\Scripts\python.exe`; Arguments `"C:\path\cisepos-auto-report\run_daily_purchase_report.py"`; Start in `C:\path\cisepos-auto-report`.
4. In Settings choose **Do not start a new instance** if already running. Set a suitable execution limit longer than the configured browser/SMTP timeouts. Review Last Run Result (`0` means completed or already delivered; feature scripts return `1` for sanitized failure and `130` for cancellation).

A per-date lock also blocks overlapping full runs. If forcibly killed, a lock may remain: confirm no process is running before removing that day's `.lock` directory. Do not run standalone download/analysis commands concurrently with a full job for the same date. Do not add automatic retry loops around logins or uncertain deliveries.

A delivery journal is written as `pending` before SMTP and `sent` after confirmed completion. An already-sent full run is skipped. A disconnect/partial-recipient refusal leaves `pending`; this prevents an automatic duplicate even when the mail server may have accepted it. SMTP cannot guarantee exactly-once delivery. Check the mail server/recipients first, then use `--allow-resend` only for an intentional resend. This flag must not be used by a scheduled task.

## Safe diagnostics and offline tests

Only fixed stage labels/status messages are printed. Feature CLI wrappers suppress raw exceptions, server responses, selectors and private values. Do not enable `DEBUG=pw:api`, SMTP debug, screenshots, traces or page dumps with real credentials. Existing login diagnostics retain their safe stage labels. A timeout, bad column mapping, invalid export, expired session, partial table, SMTP error or missing configuration stops the workflow. There are no repeated login attempts, security bypasses or silently guessed report schema.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile login_test.py purchase_common.py download_purchase_report.py analyze_purchase_report.py send_purchase_email.py run_daily_purchase_report.py
```

Tests generate synthetic workbooks, intercept browser requests and mock SMTP. They cover login regression, dd-mm-yyyy dates, report type selection, redraw waiting, All entries, validated XLSX download, historical rate calculation, tax/discount/net preference, totals/returns exclusion, units/branches, missing history, escaped HTML/CSV, delivery journals and dry-run isolation. No test requires CISEPOS or SMTP credentials. `CISEPOS_CHROMIUM_PATH` can select a system Chromium for offline tests. CI is configured to run the suite on Windows Server 2022/Python 3.13 and Ubuntu.

Development validation: all 51 offline tests passed on Linux Chromium, syntax compilation and Git whitespace checks passed, and a synthetic command-line dry run produced CSV/JSON/HTML without a delivery journal; live report selectors, the actual export schema, SMTP delivery and execution on your Windows VPS still require a one-time deployment check. Keep `.env`, credentials, virtual environments, raw/processed business data and ZIP artifacts out of source control.
