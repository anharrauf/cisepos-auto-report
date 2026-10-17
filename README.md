# CISEPOS login automation

Python 3.10+ and Playwright Chromium. Opens https://mkenter.express-cisepos.com/public/login, fills credentials from environment variables, submits once, and verifies authentication. Headless by default. No report downloads, stored sessions, screenshots, traces, or credentials in source.

## Ubuntu VPS setup

Run the automation as a regular user. Installation of Ubuntu system packages needs sudo.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
cd /path/to/cisepos-login
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

The final command may request sudo for browser system libraries. A headless VPS does not need a desktop or X server. Ensure the VPS can access the CISEPOS hostname over HTTPS and Playwright's browser download servers during installation. Keep TLS certificate verification enabled.

## Configure credentials

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` locally and supply `CISEPOS_USERNAME` and `CISEPOS_PASSWORD`. Quote values containing spaces or `#`. Dotenv interpolation is disabled so literal `$` characters are preserved. Alternatively inject both variables with your deployment's secret manager. Exported environment values take precedence over `.env`. Do not place credentials in command arguments, shell history, source, issue trackers, or logs. `.gitignore` excludes local environment files and session artifacts; `.env.example` contains no secrets.

Optional settings:

| Variable | Default / use |
| --- | --- |
| `CISEPOS_HEADLESS` | `true`; use `false` only with a display for manual debugging |
| `CISEPOS_TIMEOUT_MS` | `30000`; positive integer up to `300000`, per action and verification phase |
| `CISEPOS_USERNAME_SELECTOR` | Visible username/email/autocomplete username input |
| `CISEPOS_PASSWORD_SELECTOR` | Visible password input |
| `CISEPOS_SUBMIT_SELECTOR` | Visible submit button/input |
| `CISEPOS_SUCCESS_SELECTOR` | **Required**: a confirmed authenticated-only dashboard element; no default |
| `CISEPOS_ERROR_SELECTOR` | Visible error alert or validation message |
| `CISEPOS_CHROMIUM_PATH` | Optional system Chromium path; normally use Playwright's installed Chromium |

## Confirm selectors before the first live run

The live CISEPOS DOM could not be inspected here because the environment's network allowlist excludes the domain. Default selectors are conventional candidates, not confirmed CISEPOS selectors. Inspect the login page manually in browser developer tools and override selectors in `.env` as needed. Username/password/submit selectors should each match exactly one visible element. Set `CISEPOS_SUCCESS_SELECTOR` to exactly one visible element you have manually confirmed is available only in an authenticated dashboard session. No logout selector is assumed. This setting is required before any credentials are submitted. Do not choose a generic heading or navigation container.

Verification requires the exact HTTPS origin and `/public/dashboard` path (an optional trailing slash or query is allowed), together with one visible authenticated-only UI element. The URL is checked again after reading the element to catch intervening redirects. Redirecting alone never counts as success. If this deployment keeps the login URL after authentication, or opens authentication in a new window, adapt the verification to the observed application before use. Browser popups are closed; JS alerts are accepted and confirmations/prompts dismissed. HTML error dialogs are detected by the error selector; unrelated HTML modals need a page-specific handler. MFA and CAPTCHA are not bypassed.

## Run a live login

```bash
source .venv/bin/activate
python login_test.py
```

A successful run prints `SUCCESS: CISEPOS login verified.` and closes the browser, discarding the session. There are no automatic retries that could lock an account. Failures print a sanitized reason; raw Playwright logs and server messages are suppressed because they can contain credentials. Do not enable `DEBUG=pw:api`, tracing or screenshots with real credentials without reviewing their secret exposure.

Exit codes: `0` verified, `1` login failure/unverified result, `2` configuration error, `3` navigation/control timeout, `4` browser/selector/runtime error, `130` cancellation. A login result timeout returns `1`. Browser contexts and browsers close on success and failure.

## Safe tests without credentials

```bash
python -m unittest -v test_offline.py
python -m py_compile login_test.py test_offline.py
```

Offline tests use dummy credentials and intercept all browser requests with local HTML fixtures. They do not contact CISEPOS or load `.env`. They cover configuration validation, same-origin verification, success with an alert, confirmation dismissal, credential rejection, HTTP failure, stalled login, missing controls, and redirects without authenticated evidence.

Validation performed in the supplied environment: all 21 tests passed using system Chromium with Playwright 1.62.0 and python-dotenv 1.2.2. Syntax compilation and missing-credential exit behavior were also checked. Real credentials and live CISEPOS authentication were not tested. The initial sandbox blocked Chromium startup; the tests passed with approved browser execution permissions.

## Windows Server 2022 (Python 3.13, Playwright 1.62)

From PowerShell in the cloned repository:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m unittest -v test_offline.py
```

Edit `.env` locally, including a manually confirmed `CISEPOS_SUCCESS_SELECTOR`. Restrict the file ACL to the Windows service account/administrators; `chmod` is Linux-specific. Existing exported environment values still override `.env`. Leave `CISEPOS_CHROMIUM_PATH` unset when using Playwright-installed Chromium. After configuration, a single real attempt can be run with `.\.venv\Scripts\python.exe login_test.py`. No live attempts were made while developing this fix.

## Diagnosing intermittent exit code 4

Fixed stage labels identify `navigation`, `fill-username`, `fill-password`, `submission`, `verification`, browser/context/page creation, and context/browser cleanup. For example, `BROWSER FAILURE: stage=verification category=browser-or-selector-error` identifies the phase without exposing the selector, exception, URL, username, password, cookies, tokens, or page content. Runtime startup/shutdown errors use `playwright-runtime`. A timeout at an action returns `3`; failure to confirm login returns `1`. No raw exceptions are printed.

A dashboard redirect could previously interrupt a locator read with an execution-context error. The verifier now retries only known navigation-context interruptions within its deadline and waits for the configured marker after redirects or delayed rendering. Invalid/ambiguous selectors and closed pages are not silently retried. The old password-disappearance check is removed: a confirmed authenticated marker and exact dashboard URL are the evidence. Submission uses Playwright's normal action/navigation waiting, once only. Cleanup failures have their own stage and cannot overwrite an earlier login failure.

These are plausible causes identified from the code; the original Windows failure was not reproduced against CISEPOS. Regression coverage includes a dashboard reload during verification, delayed UI without logout controls, duplicate/invalid selectors, exact dashboard matching, navigation-context interruption, closed pages, cleanup precedence, and sanitized output. Tests ran on Linux Chromium; Windows Server 2022/Python 3.13 execution remains to be confirmed on that host.
