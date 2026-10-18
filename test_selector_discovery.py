"""Offline manual-picker safety regressions. No login or real report page."""
import os
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
import unittest
from playwright.sync_api import sync_playwright, Error
from discover_purchase_selectors import arm_picker, collect_selector, discover_controls, CONTROLS
from download_purchase_report import REPORT_URL
from offline_test_support import ACTION_TIMEOUT_MS, EXPECTED_FAILURE_TIMEOUT_MS
import discover_purchase_selectors as discovery
from login_test import URL, DASHBOARD_URL

HTML = '''<main><form onsubmit="window.submitted=true; return false">
<input id="start" value="PRIVATE_DATE"><input id="end">
<select id="type"><option>PRIVATE_OPTION</option></select>
<button id="generate" onclick="window.generated=true">Generate</button></form>
<select id="length"><option>All</option></select>
<table id="table"><tbody><tr><td id="cell">PRIVATE_RECORD</td></tr></tbody></table>
<button id="excel" onclick="window.exported=true">Excel</button></main>'''


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True, executable_path=os.getenv('CISEPOS_CHROMIUM_PATH') or None)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def test_seven_structural_selectors_no_action_or_private_values(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            context.route('**/*',lambda route: route.fulfill(content_type='text/html',body=HTML))
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(ACTION_TIMEOUT_MS)
            page.goto('https://offline.invalid/report')
            ids = dict(START='start', END='end', TYPE='type', GENERATE='generate', LENGTH='length', TABLE='cell', EXCEL='excel')
            for key, _, allowed in CONTROLS:
                arm_picker(page, allowed)
                if key == 'GENERATE':
                    page.locator('#generate').focus()
                    page.keyboard.press('Enter')
                bounds = page.locator('#'+ids[key]).bounding_box()
                page.mouse.click(bounds['x'] + bounds['width']/2, bounds['y'] + bounds['height']/2)
                selector = collect_selector(page, timeout=ACTION_TIMEOUT_MS)
                self.assertNotIn('PRIVATE',selector)
                self.assertNotIn('#',selector)
                self.assertNotIn('[',selector)
                self.assertEqual(page.locator(selector).count(),1)
                if key=='TABLE':
                    self.assertTrue(selector.endswith('table:nth-of-type(1)'))
            self.assertFalse(page.evaluate('Boolean(window.generated || window.exported || window.submitted)'))

    def test_two_stages_allow_manual_generate_before_result_controls_exist(self):
        # The real layout has no results until the user generates the report.
        form = """<main><form onsubmit="event.preventDefault()">
        <input id=start><input id=end><select id=type><option>Type</option></select>
        <button id=generate type=button onclick="window.generated=(window.generated||0)+1; document.querySelector('main').insertAdjacentHTML('beforeend', '&lt;select id=length&gt;&lt;option&gt;All&lt;/option&gt;&lt;/select&gt;&lt;table&gt;&lt;tbody&gt;&lt;tr&gt;&lt;td id=cell&gt;PRIVATE_RECORD&lt;/td&gt;&lt;/tr&gt;&lt;/tbody&gt;&lt;/table&gt;&lt;button id=excel type=button onclick=window.exported=true&gt;Excel&lt;/button&gt;')">Generate</button>
        </form></main>"""
        with self.browser.new_context() as context:
            context.set_offline(True)
            context.route('**/*',lambda route: route.fulfill(content_type='text/html',body=form))
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(ACTION_TIMEOUT_MS)
            page.goto(REPORT_URL)  # Fully intercepted, no CISEPOS request.
            captured = []
            prompts = []
            ids = dict(START='start',END='end',TYPE='type',GENERATE='generate',LENGTH='length',TABLE='cell',EXCEL='excel')
            def picker(page, control):
                key, _, allowed = control
                if key in ('START','END','TYPE','GENERATE'):
                    self.assertEqual(page.locator('table').count(),0)
                arm_picker(page,allowed)
                bounds = page.locator('#'+ids[key]).bounding_box()
                page.mouse.click(bounds['x']+bounds['width']/2,bounds['y']+bounds['height']/2)
                candidate = collect_selector(page,timeout=ACTION_TIMEOUT_MS)
                captured.append(key)
                return candidate
            def prompt(_):
                prompts.append(True)
                if len(prompts)==1:
                    self.assertEqual(captured,['START','END','TYPE','GENERATE'])
                    self.assertFalse(page.evaluate('Boolean(window.generated)'))
                    # Simulates the user's one manual click during the unlocked pause.
                    page.locator('#generate').click()
                    page.locator('table').wait_for(state='visible',timeout=ACTION_TIMEOUT_MS)
                return ''
            result = discover_controls(page,prompt=prompt,picker=picker,timeout=ACTION_TIMEOUT_MS)
            self.assertEqual(len(result),7)
            self.assertEqual(len(prompts),1)
            self.assertEqual(page.evaluate('window.generated'),1)
            self.assertFalse(page.evaluate('Boolean(window.exported)'))
            self.assertNotIn('PRIVATE',str(result))

    def fixture_session(self, context, *, finish_login=True, report_status=200, expire=False, show_form=True, native_button=False):
        requests, cookies = [], []
        form = """<form id=form style="display:none" onsubmit="window.generated=true;return false">
        <label for=f1>Start Date *</label><input id=f1 value=PRIVATE_VALUE>
        <label for=f2>End Date</label><input id=f2>
        <label>Report Type <select><option>PRIVATE_OPTION</option></select></label>
        <button type=submit>Generate Report</button></form>
        <script>setTimeout(()=>{document.querySelector('#form').style.display='block';window.ready=true},250)</script>"""
        if native_button:
            form = form.replace('<button type=submit>Generate Report</button>', '<input type=submit name=submit value="Generate Report">')
        def route_handler(route):
            url = route.request.url
            requests.append(url)
            if url == URL:
                auto = finish_login and requests.count(URL) == 1
                body = ("<script>setTimeout(()=>{document.cookie='offline_session=present;path=/';location.href='/public/dashboard'},50)</script>" if auto else '<p>Offline manual login page</p>')
                route.fulfill(content_type='text/html',body=body)
            elif url.startswith(DASHBOARD_URL):
                route.fulfill(content_type='text/html',body='<p>PRIVATE_DASHBOARD_DATA</p>')
            elif url == REPORT_URL:
                cookies.append('offline_session=present' in route.request.headers.get('cookie',''))
                if expire:
                    # Client-side redirect keeps every navigation inside the
                    # interception layer; HTTP redirect following may bypass it.
                    route.fulfill(content_type='text/html',body="<script>location.replace('/public/login')</script>")
                else:
                    route.fulfill(status=report_status,content_type='text/html',body=form if show_form else '<p>PRIVATE_RECORD</p>')
            else:
                route.abort()
        context.route('**/*',route_handler)
        return requests,cookies

    def test_manual_dashboard_detection_auto_navigation_same_session_and_form_wait(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            requests,cookies = self.fixture_session(context,native_button=True)
            page = context.new_page()
            capture = io.StringIO()
            # This only simulates completion of manual login on a local fixture.
            with redirect_stdout(capture):
                discovery.prepare_report(page,login_timeout=ACTION_TIMEOUT_MS,report_timeout=ACTION_TIMEOUT_MS)
            self.assertEqual(page.url,REPORT_URL)
            self.assertEqual(requests[:3],[URL,DASHBOARD_URL,REPORT_URL])
            self.assertEqual(cookies,[True])
            self.assertTrue(page.evaluate('window.ready'))
            self.assertFalse(page.evaluate('Boolean(window.generated)'))
            self.assertEqual(page.locator('table').count(),0)
            self.assertNotIn('PRIVATE',capture.getvalue())
            self.assertNotIn('offline_session',capture.getvalue())

    def test_manual_login_timeout_does_not_open_report(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            requests,_ = self.fixture_session(context,finish_login=False)
            with self.assertRaises(discovery.DiscoveryFailure) as caught:
                discovery.prepare_report(context.new_page(),login_timeout=EXPECTED_FAILURE_TIMEOUT_MS,report_timeout=ACTION_TIMEOUT_MS)
            self.assertEqual((caught.exception.stage,caught.exception.category),('manual-login-wait','timeout'))
            self.assertNotIn(REPORT_URL,requests)

    def test_expired_session_is_diagnosed_at_report_navigation(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            self.fixture_session(context,expire=True)
            with self.assertRaises(discovery.DiscoveryFailure) as caught:
                discovery.prepare_report(context.new_page(),login_timeout=ACTION_TIMEOUT_MS,report_timeout=ACTION_TIMEOUT_MS)
            self.assertEqual((caught.exception.stage,caught.exception.category),('report-navigation','unexpected-page-or-http-error'))

    def test_http_error_is_diagnosed_at_report_navigation(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            self.fixture_session(context,report_status=403)
            with self.assertRaises(discovery.DiscoveryFailure) as caught:
                discovery.prepare_report(context.new_page(),login_timeout=ACTION_TIMEOUT_MS,report_timeout=ACTION_TIMEOUT_MS)
            self.assertEqual(caught.exception.stage,'report-navigation')

    def test_missing_form_controls_do_not_start_picker(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            self.fixture_session(context,show_form=False)
            with self.assertRaises(discovery.DiscoveryFailure) as caught:
                discovery.prepare_report(context.new_page(),login_timeout=ACTION_TIMEOUT_MS,report_timeout=ACTION_TIMEOUT_MS,form_timeout=EXPECTED_FAILURE_TIMEOUT_MS)
            self.assertEqual((caught.exception.stage,caught.exception.category),('form-readiness','timeout'))

    def test_stage_diagnostics_suppress_raw_error_values(self):
        capture = io.StringIO()
        def fail():
            with discovery.discovery_stage('report-navigation'):
                raise Error('PRIVATE_PASSWORD PRIVATE_TOKEN PRIVATE_COOKIE PRIVATE_RECORD')
        with patch.object(discovery,'main',side_effect=fail),redirect_stdout(capture),redirect_stderr(capture):
            self.assertEqual(discovery.cli(),1)
        self.assertIn('stage=report-navigation category=browser-error',capture.getvalue())
        self.assertNotIn('PRIVATE',capture.getvalue())

    def test_stage_two_eof_has_actionable_safe_stage(self):
        def fail():
            with discovery.discovery_stage('manual-generation'):
                raise EOFError('PRIVATE_CONSOLE_DATA')
        capture=io.StringIO()
        with patch.object(discovery,'main',side_effect=fail),redirect_stdout(capture),redirect_stderr(capture):
            self.assertEqual(discovery.cli(),1)
        self.assertIn('stage=manual-generation category=interactive-console-unavailable',capture.getvalue())
        self.assertNotIn('PRIVATE',capture.getvalue())

    def test_picker_rejects_leaving_report_without_reading_login_fields(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<input type=password value=PRIVATE>'))
            page=context.new_page()
            page.goto(URL)
            with self.assertRaises(discovery.DiscoveryFailure) as caught:
                discovery.pick_control(page,discovery.FORM_CONTROLS[0])
            self.assertEqual((caught.exception.stage,caught.exception.category),('pick-start','unexpected-page-or-http-error'))

    def test_dashboard_url_match_requires_exact_https_origin_and_path(self):
        self.assertTrue(discovery.same_page(DASHBOARD_URL+'/?session=PRIVATE',DASHBOARD_URL))
        for url in ['https://other.invalid/public/dashboard',URL,DASHBOARD_URL+'-other',DASHBOARD_URL.replace('https:','http:')]:
            self.assertFalse(discovery.same_page(url,DASHBOARD_URL))

    def test_tampered_selector_is_never_accepted(self):
        with self.browser.new_context() as context:
            context.set_offline(True)
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(ACTION_TIMEOUT_MS)
            page.set_content(HTML)
            arm_picker(page,'input')
            page.evaluate('window.__purchasePick = "input[value=PRIVATE_VALUE]"')
            with self.assertRaises(ValueError):
                collect_selector(page, timeout=ACTION_TIMEOUT_MS)


if __name__ == '__main__':
    unittest.main()
