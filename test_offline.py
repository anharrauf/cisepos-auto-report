"""Offline Chromium tests: all browser requests are intercepted, no server login."""
import os
import io
from contextlib import redirect_stdout, redirect_stderr, nullcontext
import unittest
from unittest.mock import patch, MagicMock
from playwright.sync_api import sync_playwright, Error
import login_test as app
from offline_test_support import ACTION_TIMEOUT_MS, EXPECTED_FAILURE_TIMEOUT_MS

FORM = '''<form onsubmit="event.preventDefault(); window.__offlineSubmitted=true; ACTION">
<input name="username"><input type="password"><button type="submit">Login</button></form>'''

class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True, executable_path=os.getenv('CISEPOS_CHROMIUM_PATH') or None)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def run_fixture(self, action, dashboard='<div data-testid="authenticated-dashboard">Ready</div>', initial_status=200, *, action_timeout=ACTION_TIMEOUT_MS, verification_timeout=None, verification_url=None, delayed_button=False):
        with self.browser.new_context() as context:
            requests = []
            def route_handler(route):
                requests.append(route.request.url)
                is_login = route.request.url == app.URL
                body = FORM.replace('ACTION', action)
                if is_login and delayed_button:
                    body = body.replace('<button type=', '<button disabled type=') + (
                        "<script>document.querySelector('input[type=password]').addEventListener('input',"
                        "()=>setTimeout(()=>document.querySelector('button').disabled=false,1800))</script>")
                route.fulfill(status=initial_status if is_login else 200,
                              content_type='text/html', body=body if is_login else dashboard)
            context.route('**/*', route_handler)
            page = context.new_page()
            real_verify = app.verify_login
            def verify(page, timeout, selectors):
                # Only intentionally unsuccessful verification gets a short budget;
                # navigation, fill and click keep the normal readiness deadline.
                if verification_url is not None:
                    page.wait_for_url(verification_url, wait_until='domcontentloaded', timeout=ACTION_TIMEOUT_MS)
                else:
                    page.wait_for_function('window.__offlineSubmitted === true', timeout=ACTION_TIMEOUT_MS)
                return real_verify(page, verification_timeout, selectors)
            verification = patch.object(app, 'verify_login', side_effect=verify) if verification_timeout is not None else nullcontext()
            with verification:
                app.login(page, 'offline-user', 'offline-password', action_timeout, dict(app.DEFAULTS, SUCCESS='[data-testid=authenticated-dashboard]'))
            self.assertTrue(requests)

    def test_success_with_alert(self):
        self.run_fixture("alert('Welcome'); location.href='/public/dashboard';")

    def test_delayed_authenticated_ui_without_logout(self):
        self.run_fixture("location.href='/public/dashboard';", "<script>setTimeout(() => {document.body.innerHTML='<div data-testid=authenticated-dashboard>Ready</div>'}, 200)</script>")

    def test_dashboard_reload_during_verification(self):
        self.run_fixture("location.href='/public/dashboard';", "<script>if(!sessionStorage.reloaded){sessionStorage.reloaded='yes';setTimeout(() => location.reload(), 10)}else{document.write('<div data-testid=authenticated-dashboard>Ready</div>')}</script>")

    def test_duplicate_authenticated_selector_fails_safely(self):
        with self.assertRaises(app.BrowserFailure) as caught:
            self.run_fixture("location.href='/public/dashboard';", '<div data-testid=authenticated-dashboard></div><div data-testid=authenticated-dashboard></div>')
        self.assertEqual(caught.exception.stage, 'verification')

    def test_wrong_path_with_marker_is_rejected(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture("location.href='/public/maintenance';", verification_timeout=EXPECTED_FAILURE_TIMEOUT_MS, verification_url=app.URL.replace('/login', '/maintenance'))

    def test_rejected_credentials(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture("document.body.insertAdjacentHTML('beforeend', '<div class=alert-danger>Rejected</div>');")

    def test_unverified_redirect(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture("location.href='/public/dashboard';", '<h1>Maintenance</h1>', verification_timeout=EXPECTED_FAILURE_TIMEOUT_MS, verification_url=app.DASHBOARD_URL)

    def test_login_http_error(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture('', initial_status=503)

    def test_stalled_login(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture('', verification_timeout=EXPECTED_FAILURE_TIMEOUT_MS)

    def test_confirm_dismissed(self):
        self.run_fixture("if (!confirm('Continue?')) location.href='/public/dashboard';")

    def test_missing_control_timeout(self):
        selectors = dict(app.DEFAULTS, USERNAME='#missing', SUCCESS='[data-testid=authenticated-dashboard]')
        with self.browser.new_context() as context:
            context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=FORM.replace('ACTION', '')))
            page = context.new_page()
            real_locator = page.locator
            missing = real_locator('#missing')
            real_fill = missing.fill
            # The absent control alone has a short, intentional deadline. Slow
            # navigation cannot masquerade as the missing-control failure.
            with patch.object(missing, 'fill', side_effect=lambda value: real_fill(value, timeout=EXPECTED_FAILURE_TIMEOUT_MS)), patch.object(page, 'locator', side_effect=lambda selector: missing if selector == '#missing' else real_locator(selector)):
                with self.assertRaises(app.BrowserFailure) as caught:
                    app.login(page, 'offline-user', 'offline-password', ACTION_TIMEOUT_MS, selectors)
            self.assertEqual((caught.exception.stage, caught.exception.code), ('fill-username', 3))

    def test_delayed_button_succeeds_with_normal_readiness_budget(self):
        # Deliberately exceeds the old 1.2s budget, but asserts the functional
        # result rather than timing an expected failure on a loaded Windows VPS.
        self.run_fixture("location.href='/public/dashboard';", delayed_button=True)

    def test_configuration_validation(self):
        with patch.object(app, 'load_dotenv'), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(app.ConfigurationError):
                app.configuration()
        with patch.object(app, 'load_dotenv'), patch.dict(os.environ, {
            'CISEPOS_USERNAME': 'offline-user', 'CISEPOS_PASSWORD': 'offline-password',
            'CISEPOS_TIMEOUT_MS': '-1'}, clear=True):
            with self.assertRaises(app.ConfigurationError):
                app.configuration()

    def test_origin_validation(self):
        self.assertFalse(app.authenticated_url('https://evil.example/dashboard'))
        self.assertFalse(app.authenticated_url(app.URL + '?error=1'))
        self.assertFalse(app.authenticated_url('https://mkenter.express-cisepos.com/dashboard'))
        self.assertTrue(app.authenticated_url(app.DASHBOARD_URL + '?view=home'))

class RegressionTests(unittest.TestCase):
    def test_navigation_context_race_retried(self):
        page = MagicMock()
        page.url = app.DASHBOARD_URL
        page.is_closed.return_value = False
        error = MagicMock()
        error.first.is_visible.side_effect = [Error('Execution context was destroyed: secret'), False]
        success = MagicMock()
        success.is_visible.return_value = True
        page.locator.side_effect = lambda value: error if value == 'error' else success
        app.verify_login(page, 1000, {'ERROR': 'error', 'SUCCESS': 'success'})
        page.wait_for_timeout.assert_called_once()

    def test_invalid_selector_not_retried(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.locator.side_effect = Error('Invalid selector secret username password cookie token')
        with self.assertRaises(app.BrowserFailure) as caught:
            with app.stage('verification'):
                app.verify_login(page, 1000, {'ERROR': 'bad', 'SUCCESS': 'marker'})
        self.assertEqual(caught.exception.stage, 'verification')
        page.wait_for_timeout.assert_not_called()

    def test_diagnostics_never_emit_raw_error(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                with app.stage('submission'):
                    raise Error('private-username private-password cookie token page-content')
            except app.BrowserFailure as exc:
                self.assertEqual(exc.code, 4)
        self.assertEqual(output.getvalue(), 'STAGE: submission\n')

    def test_cleanup_preserves_primary_failure(self):
        resource = MagicMock()
        resource.close.side_effect = Error('sensitive cleanup details')
        with self.assertRaises(app.LoginFailure):
            try:
                raise app.LoginFailure('Verification failed.')
            finally:
                app.close_resource(resource, 'context-cleanup')

    def test_cleanup_failure_has_own_stage(self):
        resource = MagicMock()
        resource.close.side_effect = Error('sensitive cleanup details')
        with self.assertRaises(app.BrowserFailure) as caught:
            app.close_resource(resource, 'browser-cleanup')
        self.assertEqual(caught.exception.stage, 'browser-cleanup')

    def test_closed_page_not_retried(self):
        page = MagicMock()
        page.is_closed.return_value = True
        with self.assertRaises(app.LoginFailure):
            app.verify_login(page, 1000, {'ERROR': 'error', 'SUCCESS': 'marker'})
        page.locator.assert_not_called()

    def test_main_sanitizes_browser_failure(self):
        fake = MagicMock()
        fake.__enter__.return_value.chromium.launch.side_effect = Error('secret-user secret-pass cookie token content')
        output = io.StringIO()
        with patch.object(app, 'configuration', return_value=('dummy', 'dummy', True, 1000, app.DEFAULTS)), patch.object(app, 'sync_playwright', return_value=fake), redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(app.main(), 4)
        self.assertIn('stage=browser-launch', output.getvalue())
        for secret in ('secret-user', 'secret-pass', 'cookie', 'token', 'content'):
            self.assertNotIn(secret, output.getvalue())

    def test_authenticated_element_required(self):
        with patch.object(app, 'load_dotenv'), patch.dict(os.environ, {
            'CISEPOS_USERNAME': 'dummy', 'CISEPOS_PASSWORD': 'dummy'}, clear=True):
            with self.assertRaises(app.ConfigurationError):
                app.configuration()

if __name__ == '__main__':
    unittest.main()
