"""Offline manual-picker safety regressions. No login or real report page."""
import os
import unittest
from playwright.sync_api import sync_playwright
from discover_purchase_selectors import arm_picker, collect_selector, discover_controls, CONTROLS
from download_purchase_report import REPORT_URL
from offline_test_support import ACTION_TIMEOUT_MS

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
                if len(prompts)==2:
                    self.assertEqual(captured,['START','END','TYPE','GENERATE'])
                    self.assertFalse(page.evaluate('Boolean(window.generated)'))
                    # Simulates the user's one manual click during the unlocked pause.
                    page.locator('#generate').click()
                    page.locator('table').wait_for(state='visible',timeout=ACTION_TIMEOUT_MS)
                return ''
            result = discover_controls(page,prompt=prompt,picker=picker)
            self.assertEqual(len(result),7)
            self.assertEqual(len(prompts),2)
            self.assertEqual(page.evaluate('window.generated'),1)
            self.assertFalse(page.evaluate('Boolean(window.exported)'))
            self.assertNotIn('PRIVATE',str(result))

    def test_tampered_selector_is_never_accepted(self):
        with self.browser.new_context() as context:
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
