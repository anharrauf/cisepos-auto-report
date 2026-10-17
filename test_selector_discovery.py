"""Offline manual-picker safety regressions. No login or real report page."""
import os
import unittest
from playwright.sync_api import sync_playwright
from discover_purchase_selectors import arm_picker, collect_selector, CONTROLS

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
            page.goto('https://offline.invalid/report')
            ids = dict(START='start', END='end', TYPE='type', GENERATE='generate', LENGTH='length', TABLE='cell', EXCEL='excel')
            for key, _, allowed in CONTROLS:
                arm_picker(page, allowed)
                if key == 'GENERATE':
                    page.locator('#generate').focus()
                    page.keyboard.press('Enter')
                bounds = page.locator('#'+ids[key]).bounding_box()
                page.mouse.click(bounds['x'] + bounds['width']/2, bounds['y'] + bounds['height']/2)
                selector = collect_selector(page)
                self.assertNotIn('PRIVATE',selector)
                self.assertNotIn('#',selector)
                self.assertNotIn('[',selector)
                self.assertEqual(page.locator(selector).count(),1)
                if key=='TABLE':
                    self.assertTrue(selector.endswith('table:nth-of-type(1)'))
            self.assertFalse(page.evaluate('Boolean(window.generated || window.exported || window.submitted)'))

    def test_tampered_selector_is_never_accepted(self):
        with self.browser.new_context() as context:
            page = context.new_page()
            page.set_content(HTML)
            arm_picker(page,'input')
            page.evaluate('window.__purchasePick = "input[value=PRIVATE_VALUE]"')
            with self.assertRaises(ValueError):
                collect_selector(page)


if __name__ == '__main__':
    unittest.main()
