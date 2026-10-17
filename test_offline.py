"""Offline Chromium tests: all browser requests are intercepted, no server login."""
import os
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
import login_test as app

FORM = '''<form onsubmit="event.preventDefault(); ACTION">
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

    def run_fixture(self, action, dashboard='<a href="/logout">Logout</a>', initial_status=200):
        with self.browser.new_context() as context:
            requests = []
            def route_handler(route):
                requests.append(route.request.url)
                is_login = route.request.url == app.URL
                route.fulfill(status=initial_status if is_login else 200,
                              content_type='text/html', body=FORM.replace('ACTION', action) if is_login else dashboard)
            context.route('**/*', route_handler)
            app.login(context.new_page(), 'offline-user', 'offline-password', 600, app.DEFAULTS)
            self.assertTrue(requests)

    def test_success_with_alert(self):
        self.run_fixture("alert('Welcome'); location.href='/dashboard';")

    def test_rejected_credentials(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture("document.body.insertAdjacentHTML('beforeend', '<div class=alert-danger>Rejected</div>');")

    def test_unverified_redirect(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture("location.href='/dashboard';", '<h1>Maintenance</h1>')

    def test_login_http_error(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture('', initial_status=503)

    def test_stalled_login(self):
        with self.assertRaises(app.LoginFailure):
            self.run_fixture('')

    def test_confirm_dismissed(self):
        self.run_fixture("if (!confirm('Continue?')) location.href='/dashboard';")

    def test_missing_control_timeout(self):
        selectors = dict(app.DEFAULTS, USERNAME='#missing')
        with self.browser.new_context() as context:
            context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=FORM.replace('ACTION', '')))
            with self.assertRaises(BrowserTimeout):
                app.login(context.new_page(), 'offline-user', 'offline-password', 200, selectors)

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
        self.assertTrue(app.authenticated_url('https://mkenter.express-cisepos.com/dashboard'))

if __name__ == '__main__':
    unittest.main()
