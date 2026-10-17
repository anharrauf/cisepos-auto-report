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

The login behavior works on the VPS. The 14-column Excel layout is now confirmed; report-control CSS selectors still need one-time local discovery. No live requests were made for this feature. Confirm the following manually before scheduling. The code refuses to guess missing controls and does not retry logins.

- Preserve `CISEPOS_USERNAME`, `CISEPOS_PASSWORD`, and any working login-selector overrides. Set `CISEPOS_SUCCESS_SELECTOR` to exactly one visible authenticated-only dashboard element. Login verification requires the exact `/public/dashboard` path plus that element.
- Confirm CSS selectors for `CISEPOS_REPORT_START_SELECTOR`, `END_SELECTOR`, `TYPE_SELECTOR`, `GENERATE_SELECTOR`, `TABLE_SELECTOR`, `LENGTH_SELECTOR`, and `EXCEL_SELECTOR` (each prefixed `CISEPOS_REPORT_`). Use unique selectors; a table ID is ideal. Report selectors must be standard CSS because the redraw observer uses the same selectors in the DOM. No report controls have guessed defaults.
- The start/end inputs must accept text in `dd-mm-yyyy`, as specified for this deployment. The report type selector is a native `<select>` with visible option label `WithTax&Disc`. The page-length selector is a native `<select>` with an `All` option. A custom widget requires a page-specific adapter, not an invented selector.
- Set `CISEPOS_PURCHASE_REPORT_URL` to the provided report URL (already in `.env.example`). Only the existing CISEPOS HTTPS origin is accepted. `CISEPOS_REPORT_TIMEOUT_MS=120000` controls report actions/export; the login retains `CISEPOS_TIMEOUT_MS`.
- Generate must redraw the table, trigger a DataTables draw, or load a new document. The script waits for that signal and for any DataTables processing indicator to disappear; an old visible table alone is insufficient. After selecting All it waits again. For initialized DataTables it checks loaded row count against filtered record count and refuses known partial exports. Verify once that the resulting Excel covers all branches/items; custom server-side export implementations can need an adapter.
- The Excel button must produce a real `.xlsx` download. Legacy `.xls`, HTML disguised as Excel, or a button that opens a new window instead of downloading are not supported. Such exports fail rather than being silently renamed.

## Confirmed Purchasing workbook layout

Row 1 is a report banner (title, dates, branch, print timestamp). Row 2 contains the headings; `PURCHASE_HEADER_ROW=1` is the zero-based header index. `.env.example` now supplies the exact column map and enables `PURCHASE_BRANCH_AWARE=true`. Keep these settings when updating your existing `.env`.

| Exact heading | Canonical field |
| --- | --- |
| Purchase Type | purchase_type |
| Transaction No | transaction_no |
| Date | purchase_date |
| Branch | branch |
| BarCode | item_code |
| Item | item |
| Default Unit | unit |
| Supplier | supplier |
| Quantity | quantity |
| PurchaseRate | purchase_rate |
| GrossAmount | gross_amount |
| TaxAmount | tax_amount |
| Discount Amount | discount_amount |
| NetAmount | net_amount |

`PURCHASE_SHEET=0` selects the first worksheet. All 14 headings are required for this CISEPOS layout. Numbers use decimal dots and optional comma thousands separators; native Excel dates, serial dates and day-first date text are supported. Preserve barcodes as text in the export where possible, including leading zeroes. No currency or unit conversion is inferred.

### Item rows, transactions and history

- Empty Item rows, transaction header rows and Total/subtotal rows are excluded from purchase-line amounts. Explicit transaction headers supply Transaction No, Purchase Type, Date and Branch to following item rows in that transaction. Totals end that metadata context; a different transaction number cannot inherit an earlier transaction's metadata. Missing required metadata fails validation.
- Positive-quantity item rows must contain PurchaseRate and NetAmount. Returns and zero-quantity adjustments remain excluded. Transaction header invoice totals are never added to line totals.
- Transaction identity is Purchase Type + Transaction No + Branch + date. The email counts distinct transactions separately from item rows. Repeated complete transaction blocks with identical lines are included once; conflicting copies stop analysis for review. Multiple identical legitimate item lines inside one transaction remain intact. Without an export line ID, identical adjacent flat lines cannot safely be classified as accidental duplicates; the tool does not guess by deleting item lines.
- Item identity prefers BarCode, then Item name. Comparison keys also include Default Unit and Branch for this confirmed layout (disabling branch mode is rejected). No kg/box conversion or cross-branch price matching is performed. A branch's transaction number may be reused in another branch without being deduplicated.

### PurchaseRate and effective net rate are separate

The main rate-change fields (`current_purchase_rate`, `previous_purchase_rate`, `absolute_difference`, `percent_difference`) use the exported **PurchaseRate**, never NetAmount/Quantity or GrossAmount/Quantity. When an item has multiple purchases today, the displayed current PurchaseRate is quantity-weighted across today's lines. Previous PurchaseRate uses the most recent earlier available timestamp for the same item/unit/branch; if the export has dates only, ties on the latest purchase day are quantity-weighted. Daily volume totals still include all of today's positive purchase lines.

Tax/discount-adjusted **effective net rate** is separately calculated as sum(NetAmount)/sum(Quantity), with `current_effective_net_rate`, `previous_effective_net_rate`, `effective_absolute_difference` and `effective_percent_difference`. The previous effective rate corresponds to the records selected for the previous PurchaseRate. The HTML email labels both measures and shows both changes. A prior zero rate has an absolute difference but no percentage.

Generic older exports can still be analyzed with custom mappings; missing PurchaseRate is left unavailable rather than being inferred from amounts. The confirmed CISEPOS layout always requires the explicit rate and net amount. Total purchase amount uses NetAmount for the confirmed layout; generic exports may use gross when net is absent.

History remains in `data/processed/YYYY-MM-DD/records.json`. Version 2 snapshots preserve each item's explicit PurchaseRate, NetAmount, transaction identity, unit, branch, quantity and timestamp. Existing historical dates are not modified when another day is analyzed; rerunning a date replaces that day's snapshot rather than appending duplicates. Earlier rows in a range workbook can supply history, and overlapping imports are not double-counted. Future records/snapshots do not influence a selected day.

Legacy version 1 snapshots are retained, but amount-derived rates in them are **not** reinterpreted as PurchaseRate. The comparison searches for the latest earlier record with a real PurchaseRate; if none exists, the email marks legacy rate history as unavailable. Reanalyze the original historical XLSX files using the new confirmed mapping to populate actual rates. New items and legacy rate-unavailable items are shown separately. Back up history and keep item/unit/branch mappings consistent across dates.

## One-time safe selector discovery on the Windows VPS

Run locally in an interactive desktop/RDP session (not from Codex or Task Scheduler):

```powershell
.\discover_purchase_selectors.ps1
```

The wrapper uses `.venv\Scripts\python.exe`; you can also run `.\.venv\Scripts\python.exe discover_purchase_selectors.py`. It opens an ephemeral headed Chromium session and asks you to log in **manually in the browser**. It never loads credentials from `.env`, fills login fields or stores browser state. Discovery now has two stages. First navigate manually to the purchase-report form **before** generating. Press Enter in the PowerShell window and pick Start Date, End Date, Report Type and Generate Report. The overlay is then removed so you can manually fill dates/type and click Generate Report once. Wait for the results to appear, press Enter in PowerShell again, then pick Show Entries, the report table and Excel. The script never clicks Generate itself.

Across the two stages the script prompts you to click exactly seven controls: Start Date, End Date, Report Type, Generate Report, Show Entries, the table, and Excel. While picking, a transparent overlay receives mouse clicks instead of the controls underneath; capture handlers additionally suppress pointer/form/button actions and keyboard activation. The script does not generate/export reports, inspect field/option values, read labels/text/HTML/table records, collect DOM attributes or take screenshots. It computes only standard HTML tag paths and sibling positions; candidate output is validated against a strict structural-selector grammar so IDs/names/tokens cannot appear in the output. Browser-debug modes are disabled. The browser and session close afterward.

Seven candidates are saved locally to ignored `selector_candidates.env`. These are positional candidates, not stable application IDs: reopen the report in a fresh manual session and confirm each resolves to the intended single control before copying into `.env`. Dynamic page layouts can require different selectors. Do not copy credentials or purchasing data into the candidate file or share page dumps. Discovery is designed for native date/select controls as documented above; unsupported custom widgets require an adapter.

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

Tests generate synthetic workbooks, intercept browser requests and mock SMTP. They cover login regression, dd-mm-yyyy dates, report type selection, redraw waiting, All entries, validated XLSX download, historical rate calculation, separate PurchaseRate/net-rate calculations, transaction deduplication, selector-discovery safety, totals/returns exclusion, units/branches, missing history, escaped HTML/CSV, delivery journals and dry-run isolation. No test requires CISEPOS or SMTP credentials. `CISEPOS_CHROMIUM_PATH` can select a system Chromium for offline tests. CI is configured to run the suite on Windows Server 2022/Python 3.13 and Ubuntu.

Previous schema/discovery validation: all 64 original offline tests passed locally on Linux Chromium, syntax compilation and Git whitespace checks passed, and a synthetic command-line dry run produced CSV/JSON/HTML without a delivery journal; live report selectors, a real workbook import, SMTP delivery and execution on your Windows VPS still require a one-time deployment check. Keep `.env`, credentials, virtual environments, raw/processed business data and ZIP artifacts out of source control.

### Offline timeouts on a Windows VPS

The former login fixture supplied a **1200ms deadline for every production login action**, including submit-click actionability and any navigation work. A local synthetic button that remains disabled for 1800ms reproduces `BrowserFailure('submission', 'timeout', 3)` with the old budget and passes with the new budget. This confirms that the fixture budget can cause the reported failure; it does not establish which actionability/navigation condition or host scheduling delay caused the four failures on your VPS. The real site's successful login is not evidence of a credential issue in these synthetic tests.

Production `login_test.py` is unchanged, including `click(no_wait_after=True)` and production timeout configuration. Normal offline login/report actions now use a **15000ms condition-based deadline**. This is an upper bound, not a fixed delay. Tests retain the real Playwright click and login verification. Intentionally missing-control, stale-table and unsuccessful-verification tests use short deadlines only for their expected failing operation; they assert the failure stage/result, so an earlier action failure is not accepted. There are no added retries, skips or blanket exception catches.

After checking out this PR branch, verify from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m unittest discover -v
$LASTEXITCODE  # Expected: 0
```

For an unusually slow/shared VPS, increase only the test budget (5000–120000ms allowed):

```powershell
$env:CISEPOS_OFFLINE_TIMEOUT_MS = '30000'
.\.venv\Scripts\python.exe -m unittest discover -v
$LASTEXITCODE
Remove-Item Env:CISEPOS_OFFLINE_TIMEOUT_MS
```

The test-only module is never imported by production scripts. Tests do not load `.env` credentials, log in to CISEPOS, download a real report or send email. The staged-discovery regression starts with no results, confirms the form is unlocked for a simulated manual Generate, and then picks the newly created table/Excel controls without activating them.
