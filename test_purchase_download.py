"""Offline Chromium report page with a generated XLSX download; no CISEPOS calls."""
from datetime import date
import io
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from openpyxl import Workbook
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
from download_purchase_report import export_report, validate_xlsx, report_settings
from purchase_common import WorkflowError
from offline_test_support import ACTION_TIMEOUT_MS, EXPECTED_FAILURE_TIMEOUT_MS
import download_purchase_report as download_module

SELECTORS = dict(START='#start', END='#end', TYPE='#type', GENERATE='#generate',
                 TABLE='#report', LENGTH='#length', EXCEL='#excel')


def fake_page(redraw=True, already_all=False, valid_file=True, full_navigation=False):
    generate = "setTimeout(() => document.querySelector('#report tbody').innerHTML='<tr><td>Generated</td></tr>', 50)" if redraw else ''
    if full_navigation:
        generate = "location.href='/generated?start='+document.querySelector('#start').value+'&amp;end='+document.querySelector('#end').value+'&amp;type='+document.querySelector('#type').value"
    selected = 'selected' if already_all else ''
    return f'''<input id="start"><input id="end">
    <select id="type"><option>Other</option><option value="taxdisc">WithTax&amp;Disc</option></select>
    <button id="generate" onclick="{generate}">Generate</button>
    <select id="length" onchange="document.querySelector('#report tbody').innerHTML='<tr><td>All rows</td></tr>'">
    <option value="10">10</option><option value="-1" {selected}>All</option></select>
    <table id="report"><tbody><tr><td>Old report</td></tr></tbody></table>
    <button id="excel" onclick="location.href='/export.xlsx'">Excel</button>'''



class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True, executable_path=os.getenv('CISEPOS_CHROMIUM_PATH') or None)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def exercise(self, **options):
        with tempfile.TemporaryDirectory() as temp, self.browser.new_context(accept_downloads=True) as context:
            book = Workbook()
            book.active.append(['Item', 'Qty', 'Amount', 'Unit'])
            book.active.append(['Offline item', 1, 12, 'kg'])
            stream = io.BytesIO()
            book.save(stream)
            def route_handler(route):
                if route.request.url.endswith('/export.xlsx'):
                    route.fulfill(body=stream.getvalue() if options.get('valid_file', True) else b'<html>Not XLSX</html>',
                                  headers={'Content-Disposition': 'attachment; filename=server-file.xlsx'},
                                  content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                else:
                    route.fulfill(content_type='text/html', body=fake_page(**dict(options, already_all=True)) if urlsplit(route.request.url).path == '/generated' else fake_page(**options))
            context.route('**/*', route_handler)
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(ACTION_TIMEOUT_MS)
            page.goto('https://offline.invalid/report')
            target = Path(temp) / 'raw' / '2026-10-17' / 'purchase_report_2026-10-17.xlsx'
            export_report(page, date(2026, 10, 17), date(2026, 10, 17), target, SELECTORS, 'WithTax&Disc', ACTION_TIMEOUT_MS)
            if options.get('full_navigation'):
                submitted = parse_qs(urlsplit(page.url).query)
                self.assertEqual(submitted, {'start':['17-10-2026'], 'end':['17-10-2026'], 'type':['taxdisc']})
            else:
                self.assertEqual(page.locator('#start').input_value(), '17-10-2026')
                self.assertEqual(page.locator('#end').input_value(), '17-10-2026')
                self.assertEqual(page.locator('#type').input_value(), 'taxdisc')
            self.assertEqual(page.locator('#length').input_value(), '-1')
            validate_xlsx(target)
            self.assertFalse(target.with_suffix('.partial.xlsx').exists())

    def test_generate_all_and_download(self):
        self.exercise()

    def test_all_already_selected(self):
        self.exercise(already_all=True)

    def test_stale_table_is_not_exported(self):
        wait = download_module.wait_draw
        with patch.object(download_module, 'wait_draw', side_effect=lambda page, selectors, timeout: wait(page, selectors, EXPECTED_FAILURE_TIMEOUT_MS)) as wait_for_draw:
            with self.assertRaises(BrowserTimeout):
                self.exercise(redraw=False)
            wait_for_draw.assert_called_once()

    def test_invalid_excel_is_rejected(self):
        from zipfile import BadZipFile
        with self.assertRaises(BadZipFile):
            self.exercise(valid_file=False)

    def test_full_document_generation(self):
        self.exercise(full_navigation=True)

    def test_datatables_partial_page_is_not_exported(self):
        from download_purchase_report import export_report
        with tempfile.TemporaryDirectory() as temp, self.browser.new_context(accept_downloads=True) as context:
            context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=fake_page()))
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(ACTION_TIMEOUT_MS)
            page.goto('https://offline.invalid/report')
            page.evaluate("""() => {
                window.jQuery = () => ({off(){return this}, on(){return this}, DataTable(){
                    return {page:{info:()=>({recordsDisplay:100})},rows:()=>({count:()=>10})};
                }});
                window.jQuery.fn={dataTable:{isDataTable:()=>true}};
            }""")
            with self.assertRaises(WorkflowError):
                export_report(page, date(2026,10,17), date(2026,10,17), Path(temp)/'out.xlsx', SELECTORS, 'WithTax&Disc', ACTION_TIMEOUT_MS)
            self.assertFalse((Path(temp)/'out.xlsx').exists())

    def test_missing_controls_fail_before_login(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(WorkflowError):
            report_settings()

    def test_untrusted_report_host_rejected(self):
        with patch.dict(os.environ, {'CISEPOS_PURCHASE_REPORT_URL': 'https://other.invalid/report'}, clear=True):
            with self.assertRaises(WorkflowError):
                report_settings()


if __name__ == '__main__':
    unittest.main()
