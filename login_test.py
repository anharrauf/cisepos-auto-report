"""CISEPOS login-only smoke test; no persisted session or report operations."""
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from dotenv import load_dotenv
from playwright.sync_api import Error, TimeoutError as BrowserTimeout, sync_playwright

URL = 'https://mkenter.express-cisepos.com/public/login'
DEFAULTS = {
    'USERNAME': 'input[name="username"]:visible, input[name="email"]:visible, input[autocomplete="username"]:visible',
    'PASSWORD': 'input[type="password"]:visible',
    'SUBMIT': 'button[type="submit"]:visible, input[type="submit"]:visible',
    'SUCCESS': 'a[href*="logout"]:visible, form[action*="logout"]:visible, [data-testid="logout"]:visible',
    'ERROR': '.alert-danger:visible, .invalid-feedback:visible, [role="alert"]:visible, .swal2-popup.swal2-icon-error:visible',
}

class ConfigurationError(Exception):
    pass

class LoginFailure(Exception):
    pass


def configuration():
    load_dotenv(Path(__file__).resolve().with_name('.env'), override=False, interpolate=False)
    username, password = os.getenv('CISEPOS_USERNAME', ''), os.getenv('CISEPOS_PASSWORD', '')
    if not username.strip() or not password:
        raise ConfigurationError('Set nonempty CISEPOS_USERNAME and CISEPOS_PASSWORD.')
    headless = os.getenv('CISEPOS_HEADLESS', 'true').strip().lower()
    if headless not in ('true', 'false'):
        raise ConfigurationError('CISEPOS_HEADLESS must be true or false.')
    try:
        timeout = int(os.getenv('CISEPOS_TIMEOUT_MS', '30000'))
    except ValueError:
        raise ConfigurationError('CISEPOS_TIMEOUT_MS must be an integer.') from None
    if not 1 <= timeout <= 300000:
        raise ConfigurationError('CISEPOS_TIMEOUT_MS must be between 1 and 300000.')
    selectors = {key: os.getenv(f'CISEPOS_{key}_SELECTOR', default) for key, default in DEFAULTS.items()}
    if any(not value.strip() for value in selectors.values()):
        raise ConfigurationError('Selectors must not be empty.')
    return username, password, headless == 'true', timeout, selectors


def authenticated_url(url):
    actual, expected = urlsplit(url), urlsplit(URL)
    return ((actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)
            and actual.path.rstrip('/') not in ('', expected.path.rstrip('/')))


def login(page, username, password, timeout, selectors):
    """Single attempt. Page contents and Playwright exceptions must not be logged."""
    def dialog_handler(dialog):
        if dialog.type == 'alert':
            dialog.accept()
        else:
            dialog.dismiss()
    page.on('dialog', dialog_handler)
    page.on('popup', lambda popup: popup.close())
    page.set_default_timeout(timeout)
    page.set_default_navigation_timeout(timeout)
    response = page.goto(URL, wait_until='domcontentloaded')
    if response is None or response.status >= 400:
        raise LoginFailure('Login page returned an HTTP error or no response.')
    if (urlsplit(page.url).scheme, urlsplit(page.url).netloc) != ('https', urlsplit(URL).netloc):
        raise LoginFailure('Login page redirected to an unexpected origin.')
    page.locator(selectors['USERNAME']).fill(username)
    page.locator(selectors['PASSWORD']).fill(password)
    page.locator(selectors['SUBMIT']).click(no_wait_after=True)
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if page.is_closed():
            raise LoginFailure('Login page closed before verification.')
        if page.locator(selectors['ERROR']).first.is_visible():
            raise LoginFailure('Page displayed a login error. Check credentials and account status manually.')
        if (authenticated_url(page.url)
                and page.locator(selectors['SUCCESS']).first.is_visible()
                and not page.locator(selectors['PASSWORD']).first.is_visible()):
            return
        page.wait_for_timeout(100)
    raise LoginFailure('Login not verified before timeout. Check selectors, credentials, MFA or CAPTCHA manually.')


def main():
    try:
        username, password, headless, timeout, selectors = configuration()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, executable_path=os.getenv('CISEPOS_CHROMIUM_PATH') or None)
            try:
                context = browser.new_context(accept_downloads=False)
                try:
                    login(context.new_page(), username, password, timeout, selectors)
                finally:
                    context.close()
            finally:
                browser.close()
        print('SUCCESS: CISEPOS login verified.')
        return 0
    except ConfigurationError as exc:
        print(f'CONFIGURATION ERROR: {exc}', file=sys.stderr)
        return 2
    except LoginFailure as exc:
        print(f'LOGIN FAILED: {exc}', file=sys.stderr)
        return 1
    except BrowserTimeout:
        print('TIMEOUT: check connectivity and form selectors.', file=sys.stderr)
        return 3
    except Error:
        # Raw call logs can include credentials from fill().
        print('BROWSER ERROR: check Chromium, connectivity and selectors. Raw details suppressed to protect secrets.', file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130

if __name__ == '__main__':
    sys.exit(main())
