"""CISEPOS login-only smoke test; no persisted session or report operations."""
from contextlib import contextmanager
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
    'SUCCESS': '',  # Must be confirmed against this deployment; no guessed logout selector.
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
        raise ConfigurationError('Set all selectors, including CISEPOS_SUCCESS_SELECTOR to a confirmed authenticated-only element.')
    return username, password, headless == 'true', timeout, selectors


DASHBOARD_URL = 'https://mkenter.express-cisepos.com/public/dashboard'


class BrowserFailure(Exception):
    def __init__(self, stage, category, code=4):
        self.stage, self.category, self.code = stage, category, code


@contextmanager
def stage(name):
    # Only developer-controlled stage names and fixed categories are emitted.
    print(f'STAGE: {name}', flush=True)
    try:
        yield
    except BrowserTimeout:
        raise BrowserFailure(name, 'timeout', 3) from None
    except Error:
        raise BrowserFailure(name, 'browser-or-selector-error') from None


def authenticated_url(url):
    actual, expected = urlsplit(url), urlsplit(DASHBOARD_URL)
    return ((actual.scheme, actual.netloc, actual.path.rstrip('/'))
            == (expected.scheme, expected.netloc, expected.path))


def navigation_interrupted(exc):
    # Inspect only to classify; never emit exception text, even in debug output.
    return any(text in str(exc) for text in (
        'Execution context was destroyed', 'Cannot find context with specified id'))


def verify_login(page, timeout, selectors):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if page.is_closed():
            raise LoginFailure('Login verification failed: page closed.')
        try:
            if page.locator(selectors['ERROR']).first.is_visible():
                raise LoginFailure('Login verification failed: visible error indicator.')
            # Hydration and redirects can occur between locator calls. Recheck the
            # URL after reading the marker; only transient execution-context errors
            # are retried, never malformed/ambiguous selectors or closed targets.
            if (authenticated_url(page.url)
                    and page.locator(selectors['SUCCESS']).is_visible()
                    and authenticated_url(page.url)):
                return
        except Error as exc:
            if not navigation_interrupted(exc):
                raise
        page.wait_for_timeout(100)
    raise LoginFailure('Login verification timed out: dashboard URL and authenticated UI were not both confirmed.')


def login(page, username, password, timeout, selectors):
    """One submission only. All diagnostics use fixed, non-sensitive labels."""
    def dialog_handler(dialog):
        try:
            if dialog.type == 'alert':
                dialog.accept()
            else:
                dialog.dismiss()
        except Error:
            print('WARNING: dialog handling interrupted; verification will determine outcome.', file=sys.stderr)

    def popup_handler(popup):
        try:
            popup.close()
        except Error:
            print('WARNING: popup cleanup interrupted.', file=sys.stderr)

    page.on('dialog', dialog_handler)
    page.on('popup', popup_handler)
    page.set_default_timeout(timeout)
    page.set_default_navigation_timeout(timeout)
    with stage('navigation'):
        response = page.goto(URL, wait_until='domcontentloaded')
        if response is None or response.status >= 400:
            raise LoginFailure('Navigation failed: HTTP error or missing response.')
        if (urlsplit(page.url).scheme, urlsplit(page.url).netloc) != ('https', urlsplit(URL).netloc):
            raise LoginFailure('Navigation failed: unexpected origin.')
    with stage('fill-username'):
        page.locator(selectors['USERNAME']).fill(username)
    with stage('fill-password'):
        page.locator(selectors['PASSWORD']).fill(password)
    with stage('submission'):
        # Playwright owns actionability and navigation waiting. Never click twice.
        page.locator(selectors['SUBMIT']).click()
    with stage('verification'):
        verify_login(page, timeout, selectors)


def close_resource(resource, name):
    # A cleanup exception must not overwrite the original failure and its stage.
    failing = sys.exc_info()[0] is not None
    try:
        with stage(name):
            resource.close()
    except BrowserFailure:
        if not failing:
            raise
        print(f'WARNING: {name} failed while handling an earlier failure.', file=sys.stderr)


def main():
    try:
        username, password, headless, timeout, selectors = configuration()
        with sync_playwright() as p:
            with stage('browser-launch'):
                browser = p.chromium.launch(headless=headless, executable_path=os.getenv('CISEPOS_CHROMIUM_PATH') or None)
            try:
                with stage('context-creation'):
                    context = browser.new_context(accept_downloads=False)
                try:
                    with stage('page-creation'):
                        page = context.new_page()
                    login(page, username, password, timeout, selectors)
                finally:
                    close_resource(context, 'context-cleanup')
            finally:
                close_resource(browser, 'browser-cleanup')
        print('SUCCESS: CISEPOS login verified.')
        return 0
    except ConfigurationError as exc:
        print(f'CONFIGURATION ERROR: {exc}', file=sys.stderr)
        return 2
    except LoginFailure as exc:
        print(f'LOGIN FAILED: {exc}', file=sys.stderr)
        return 1
    except BrowserFailure as exc:
        print(f'BROWSER FAILURE: stage={exc.stage} category={exc.category}. Sensitive details suppressed.', file=sys.stderr)
        return exc.code
    except BrowserTimeout:
        print('BROWSER FAILURE: stage=playwright-runtime category=timeout.', file=sys.stderr)
        return 3
    except Error:
        # Raw call logs can include credentials from fill().
        print('BROWSER FAILURE: stage=playwright-runtime category=browser-error. Sensitive details suppressed.', file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130

if __name__ == '__main__':
    sys.exit(main())
