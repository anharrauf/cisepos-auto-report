"""Single-login purchase export. Report controls must be confirmed on the VPS."""
import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile
from playwright.sync_api import sync_playwright
import login_test
from purchase_common import (WorkflowError, base_directory, diagnostic, integer,
                             report_date, required, run_cli)

REPORT_URL = 'https://mkenter.express-cisepos.com/public/reports/AllBranchesPurchaseReport'
CONTROL_NAMES = ('START', 'END', 'TYPE', 'GENERATE', 'TABLE', 'LENGTH', 'EXCEL')


def report_settings():
    url = os.getenv('CISEPOS_PURCHASE_REPORT_URL', REPORT_URL)
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.netloc) != ('https', urlsplit(login_test.URL).netloc):
        raise WorkflowError('Report URL must use the existing CISEPOS HTTPS origin.')
    selectors = {name: required(f'CISEPOS_REPORT_{name}_SELECTOR') for name in CONTROL_NAMES}
    value = os.getenv('CISEPOS_REPORT_TYPE', 'WithTax&Disc')
    if value != 'WithTax&Disc':
        raise WorkflowError('This workflow requires report type WithTax&Disc.')
    return url, selectors, value


def validate_xlsx(path):
    # DataTables may export HTML or legacy XLS. Do not disguise these as XLSX.
    with ZipFile(path) as archive:
        if not {'[Content_Types].xml', 'xl/workbook.xml'} <= set(archive.namelist()):
            raise WorkflowError('Export is not an XLSX workbook.')


def arm_draw(page, table_selector):
    # Both DataTables draw and table DOM changes are observed before clicking.
    # Configured report selectors are CSS, so the same selector works in the DOM.
    page.evaluate('''selector => {
        window.__purchaseObserver?.disconnect();
        window.__purchaseDraw = false;
        window.__purchaseArmed = true;
        const observer = new MutationObserver(records => {
            const current = document.querySelector(selector);
            if (!current) return;
            const changed = records.some(r => {
                if (r.target === current || current.contains(r.target)) return true;
                return [...r.addedNodes].some(n => n === current || n.contains?.(current));
            });
            if (changed) window.__purchaseDraw = true;
        });
        observer.observe(document.body, {childList:true, subtree:true, characterData:true});
        window.__purchaseObserver = observer;
        if (window.jQuery) window.jQuery(document).off('draw.dt.purchaseExport')
            .on('draw.dt.purchaseExport', selector, () => window.__purchaseDraw = true);
    }''', table_selector)


def wait_draw(page, selectors, timeout):
    page.wait_for_function('window.__purchaseDraw === true || window.__purchaseArmed !== true', timeout=timeout)
    page.wait_for_load_state('domcontentloaded', timeout=timeout)
    table = page.locator(selectors['TABLE'])
    table.wait_for(state='visible', timeout=timeout)
    page.wait_for_function('''selector => {
        const t = document.querySelector(selector);
        if (!t) return false;
        const wrapper = t.closest('.dataTables_wrapper, .dt-container') || t.parentElement;
        const busy = wrapper.querySelectorAll('.dataTables_processing, .dt-processing');
        return [...busy].every(e => !e.getClientRects().length || getComputedStyle(e).display === 'none');
    }''', arg=selectors['TABLE'], timeout=timeout)


def export_report(page, start, end, destination, selectors, report_type, timeout):
    diagnostic('report-dates')
    page.locator(selectors['START']).fill(start.strftime('%d-%m-%Y'))
    page.locator(selectors['END']).fill(end.strftime('%d-%m-%Y'))
    diagnostic('report-type')
    # Select by visible label; the option's value may differ from its label.
    page.locator(selectors['TYPE']).select_option(label=report_type)
    diagnostic('report-generation')
    arm_draw(page, selectors['TABLE'])
    page.locator(selectors['GENERATE']).click(no_wait_after=True)
    wait_draw(page, selectors, timeout)
    diagnostic('report-all-entries')
    length = page.locator(selectors['LENGTH'])
    all_option = length.locator('option').filter(has_text=re.compile(r'^\s*All\s*$', re.I))
    if all_option.count() != 1:
        raise WorkflowError('A unique All entries option was not found.')
    all_value = all_option.get_attribute('value')
    if length.input_value() != all_value:
        arm_draw(page, selectors['TABLE'])
        length.select_option(value=all_value)
        wait_draw(page, selectors, timeout)
    # Server-side DataTables can cap an All request. Refuse a known partial export.
    complete = page.evaluate("""selector => {
        const jq = window.jQuery;
        const table = document.querySelector(selector);
        if (!jq?.fn?.dataTable?.isDataTable(table)) return true;
        const api = jq(table).DataTable();
        const info = api.page.info();
        return api.rows().count() >= info.recordsDisplay;
    }""", selectors['TABLE'])
    if not complete:
        raise WorkflowError('The report still contains only a partial page; export was not attempted.')
    diagnostic('report-export')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.partial.xlsx')
    try:
        with page.expect_download(timeout=timeout) as pending:
            page.locator(selectors['EXCEL']).click(no_wait_after=True)
        download = pending.value
        if download.failure():
            raise WorkflowError('Excel download did not complete.')
        download.save_as(temporary)
        if not temporary.exists() or not temporary.stat().st_size:
            raise WorkflowError('Excel export is empty.')
        validate_xlsx(temporary)
        # Never use the server-supplied filename as a local path.
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def download_report(day, base=None, start=None, end=None):
    settings = report_settings()  # Fail before a live login if controls are unset.
    username, password, headless, timeout, login_selectors = login_test.configuration()
    login_timeout = timeout
    timeout = integer('CISEPOS_REPORT_TIMEOUT_MS', max(timeout, 120000), maximum=900000)
    start, end = start or day, end or day
    if start > end:
        raise WorkflowError('Start date must precede end date.')
    base = Path(base) if base is not None else base_directory()
    destination = base / 'raw' / day.isoformat() / f'purchase_report_{day.isoformat()}.xlsx'
    diagnostic('download-browser-launch')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, executable_path=os.getenv('CISEPOS_CHROMIUM_PATH') or None)
        try:
            context = browser.new_context(accept_downloads=True)
            try:
                page = context.new_page()
                login_test.login(page, username, password, login_timeout, login_selectors)
                page.set_default_timeout(timeout)
                page.set_default_navigation_timeout(timeout)
                diagnostic('report-navigation')
                response = page.goto(settings[0], wait_until='domcontentloaded')
                if response is None or response.status >= 400 or (urlsplit(page.url).scheme, urlsplit(page.url).netloc, urlsplit(page.url).path) != (urlsplit(settings[0]).scheme, urlsplit(settings[0]).netloc, urlsplit(settings[0]).path):
                    raise WorkflowError('Report navigation failed or session expired.')
                return export_report(page, start, end, destination, settings[1], settings[2], timeout)
            finally:
                login_test.close_resource(context, 'download-context-cleanup')
        finally:
            login_test.close_resource(browser, 'download-browser-cleanup')


def cli():
    parser = argparse.ArgumentParser(description='Download one CISEPOS purchase report.')
    parser.add_argument('--date', help='today, yesterday, or YYYY-MM-DD; default from .env')
    parser.add_argument('--start-date', type=report_date)
    parser.add_argument('--end-date', type=report_date)
    args = parser.parse_args()
    download_report(report_date(args.date), start=args.start_date, end=args.end_date)


if __name__ == '__main__':
    raise SystemExit(run_cli(cli))
