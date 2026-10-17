"""One-time local, manual selector picker. Never reads attributes, values or text."""
import os
from pathlib import Path
import re
import sys
from playwright.sync_api import sync_playwright
from urllib.parse import urlsplit
from download_purchase_report import REPORT_URL
from login_test import URL

FORM_CONTROLS = [('START', 'Start Date', 'input'), ('END', 'End Date', 'input'),
            ('TYPE', 'Report Type', 'select'), ('GENERATE', 'Generate Report', 'button,a,input')]
RESULT_CONTROLS = [('LENGTH', 'Show Entries', 'select'), ('TABLE', 'report table (click inside it)', 'table'),
            ('EXCEL', 'Excel export button', 'button,a,input')]
CONTROLS = FORM_CONTROLS + RESULT_CONTROLS
# Finite HTML tags plus sibling positions only. Never output DOM attributes,
# option labels, input values, text, URLs, table cells or server-generated IDs.
TAGS = 'div|form|section|main|article|aside|header|footer|nav|fieldset|label|span|p|ul|li|ol|table|thead|tbody|tfoot|tr|td|th|input|select|button|a|center|h1|h2|h3|h4|details|summary'
STRUCTURAL = re.compile(r'body(?: > (?:' + TAGS + r'):nth-of-type\([1-9][0-9]{0,3}\)){1,30}')

PICKER = '''allowed => {
    window.__purchasePick = null;
    const overlay = document.createElement('div');
    Object.assign(overlay.style, {position:'fixed',inset:'0',zIndex:'2147483647',background:'transparent',cursor:'crosshair'});
    document.body.appendChild(overlay);
    const blocker = e => { e.preventDefault(); e.stopImmediatePropagation(); };
    const click = e => {
        blocker(e);
        const underneath = document.elementsFromPoint(e.clientX, e.clientY).find(n => n !== overlay);
        const node = underneath?.closest(allowed);
        if (!node || node === document.body) return;
        let current = node, path = [];
        while (current && current !== document.body) {
            const tag = current.tagName.toLowerCase();
            const siblings = [...current.parentElement.children].filter(n => n.tagName === current.tagName);
            path.unshift(tag + ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')');
            current = current.parentElement;
        }
        if (current !== document.body) return;
        window.__purchasePick = 'body > ' + path.join(' > ');
    };
    const key = e => blocker(e);
    document.addEventListener('click', click, true);
    document.addEventListener('pointerdown', blocker, true);
    document.addEventListener('mousedown', blocker, true);
    document.addEventListener('submit', blocker, true);
    document.addEventListener('keydown', key, true);
    window.__purchaseStopPick = () => {
        overlay.remove();
        document.removeEventListener('click', click, true);
        document.removeEventListener('pointerdown', blocker, true);
        document.removeEventListener('mousedown', blocker, true);
        document.removeEventListener('submit', blocker, true);
        document.removeEventListener('keydown', key, true);
    };
}'''


def arm_picker(page, allowed):
    page.evaluate(PICKER, allowed)


def collect_selector(page, *, timeout=180000):
    page.wait_for_function('window.__purchasePick !== null', timeout=timeout)
    candidate = page.evaluate('window.__purchasePick')
    page.evaluate('window.__purchaseStopPick()')
    if not isinstance(candidate, str) or not STRUCTURAL.fullmatch(candidate):
        raise ValueError('Unsafe selector candidate rejected.')
    if page.locator(candidate).count() != 1:
        raise ValueError('Selector is not unique.')
    return candidate


def require_report_page(page):
    if urlsplit(page.url)._replace(query='', fragment='') != urlsplit(REPORT_URL):
        raise ValueError('Expected report page is not open.')


def pick_control(page, control):
    key, label, allowed = control
    print(f'Click only the {label} control in Chromium. Do not type or press Enter in the page.')
    arm_picker(page, allowed)
    return collect_selector(page)


def discover_controls(page, *, prompt=input, picker=pick_control):
    # Two manual stages: the report form exists before generation; result controls
    # appear afterward. There is never an automated Generate click here.
    prompt('Stage 1: log in manually and open the report FORM. Do not generate yet. Press Enter in this console when ready. ')
    require_report_page(page)
    candidates = {control[0]: picker(page, control) for control in FORM_CONTROLS}
    # Remove every capture handler/overlay before asking the user to use the form.
    page.evaluate('window.__purchaseStopPick?.()')
    print('Stage 1 complete. The report form is unlocked.')
    prompt('Stage 2: manually set the dates/type and click Generate Report in Chromium. Wait for the table/Show Entries/Excel controls, then press Enter in this console. ')
    require_report_page(page)
    candidates.update({control[0]: picker(page, control) for control in RESULT_CONTROLS})
    if candidates['START'] == candidates['END'] or candidates['GENERATE'] == candidates['EXCEL']:
        raise ValueError('Different controls must have different selectors.')
    return candidates


def main():
    # Disable diagnostic modes that could otherwise log browser interaction.
    os.environ.pop('DEBUG', None)
    os.environ.pop('PWDEBUG', None)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            page.goto(URL, wait_until='domcontentloaded')
            candidates = discover_controls(page)
            output = Path(__file__).resolve().with_name('selector_candidates.env')
            output.write_text('# Structural candidates only; verify after reopening the page before copying into .env.\n' +
                              '\n'.join(f'CISEPOS_REPORT_{key}_SELECTOR={value}' for key, value in candidates.items()) + '\n', encoding='utf-8')
            print('Saved seven structural candidates in selector_candidates.env. No credentials or report values were collected.')
        finally:
            browser.close()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        print('Selector discovery stopped. No raw exception details are displayed.', file=sys.stderr)
        sys.exit(1)
