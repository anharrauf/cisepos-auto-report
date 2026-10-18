"""Manual selector discovery with fixed diagnostics and structural-only output."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout, Error
from urllib.parse import urlsplit
from download_purchase_report import REPORT_URL
from login_test import URL, DASHBOARD_URL

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


class DiscoveryFailure(Exception):
    def __init__(self, stage, category):
        self.stage, self.category = stage, category


class DiscoveryStateError(Exception):
    pass


@contextmanager
def discovery_stage(name):
    # Labels and categories are developer constants; no exception content is output.
    print(f'STAGE: discovery-{name}', flush=True)
    try:
        yield
    except DiscoveryFailure:
        raise
    except BrowserTimeout:
        raise DiscoveryFailure(name, 'timeout') from None
    except EOFError:
        raise DiscoveryFailure(name, 'interactive-console-unavailable') from None
    except DiscoveryStateError:
        raise DiscoveryFailure(name, 'unexpected-page-or-http-error') from None
    except KeyboardInterrupt:
        raise DiscoveryFailure(name, 'cancelled') from None
    except Error:
        raise DiscoveryFailure(name, 'browser-error') from None
    except Exception:
        raise DiscoveryFailure(name, 'invalid-state-or-local-error') from None


# Readiness returns booleans only. It classifies the report controls using known
# captions/labels and semantic field identifiers; it never reads input values,
# options, table cells or page HTML, or returns those attributes/captions to Python.
READINESS_HELPERS = r"""
    const norm = s => (s || '').toLowerCase().replace(/[\s_:\-*]+/g, '');
    const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const caption = root => [...root.childNodes].map(n => {
        if (n.nodeType === Node.TEXT_NODE) return n.textContent;
        if (n.nodeType !== Node.ELEMENT_NODE || ['INPUT','SELECT','TEXTAREA','TABLE','SCRIPT','STYLE'].includes(n.tagName)) return '';
        return caption(n);
    }).join(' ').slice(0,160);
    const captions = e => [e.id, e.name, e.getAttribute('aria-label'), e.getAttribute('placeholder'),
        ...[...(e.labels || [])].map(caption),
        ...[...(e.parentElement?.querySelectorAll('input:not([type=hidden]),select').length === 1 ? e.parentElement.querySelectorAll(':scope > label') : [])].map(caption)].map(norm);
    const named = (e, names) => captions(e).some(c => names.includes(c));
"""
FORM_READY = '() => {' + READINESS_HELPERS + r"""
    const fields = [...document.querySelectorAll('input,select')].filter(e =>
        visible(e) && !['hidden','password','submit','button','checkbox','radio'].includes(e.type));
    const starts = fields.filter(e => e.tagName === 'INPUT' && named(e,['start','startdate','fromdate','datefrom']));
    const ends = fields.filter(e => e.tagName === 'INPUT' && named(e,['end','enddate','todate','dateto']));
    const type = fields.some(e => e.tagName === 'SELECT' && named(e,['type','reporttype']));
    const generate = [...document.querySelectorAll('button,a,input[type=submit],input[type=button]')].some(e =>
        visible(e) && (['generate','generatereport'].includes(norm(caption(e))) || named(e,['generate','generatereport'])));
    return starts.some(s => ends.some(e => e !== s)) && type && generate;
}"""
RESULTS_READY = '() => {' + READINESS_HELPERS + r"""
    const length = [...document.querySelectorAll('select')].some(e => visible(e) &&
        (e.matches('.dataTables_length select,.dt-length select,select[name$="_length"]') ||
         named(e,['length','show','showentries','entries'])));
    const table = [...document.querySelectorAll('table')].some(visible);
    const excel = [...document.querySelectorAll('button,a,input[type=button]')].some(e => visible(e) &&
        (e.matches('.buttons-excel') || ['excel','exportexcel','excelexport'].includes(norm(caption(e))) || named(e,['excel','exportexcel','excelexport'])));
    return length && table && excel;
}"""


def same_page(actual, expected):
    a, b = urlsplit(actual), urlsplit(expected)
    return (a.scheme, a.netloc, a.path.rstrip('/')) == (b.scheme, b.netloc, b.path.rstrip('/'))


def prepare_report(page, *, login_timeout=300000, report_timeout=60000, form_timeout=None):
    """Manual authentication only; reuse this page/context for report navigation."""
    with discovery_stage('login-navigation'):
        response = page.goto(URL, wait_until='domcontentloaded', timeout=report_timeout)
        if response is None or response.status >= 400:
            raise DiscoveryStateError()
    with discovery_stage('manual-login-wait'):
        print('Log in manually in Chromium. Discovery will detect the dashboard and open the report form automatically.', flush=True)
        page.wait_for_url(lambda url: same_page(url, DASHBOARD_URL), wait_until='domcontentloaded', timeout=login_timeout)
    with discovery_stage('report-navigation'):
        response = page.goto(REPORT_URL, wait_until='domcontentloaded', timeout=report_timeout)
        if response is None or response.status >= 400:
            raise DiscoveryStateError()
        require_report_page(page)
    with discovery_stage('form-readiness'):
        page.wait_for_function(FORM_READY, timeout=report_timeout if form_timeout is None else form_timeout)


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
    if not same_page(page.url, REPORT_URL):
        raise DiscoveryStateError()


def pick_control(page, control):
    key, label, allowed = control
    print(f'Click only the {label} control in Chromium. Do not type or press Enter in the page.')
    with discovery_stage('pick-' + key.lower()):
        require_report_page(page)
        arm_picker(page, allowed)
        candidate = collect_selector(page)
        require_report_page(page)
        return candidate


def discover_controls(page, *, prompt=input, picker=pick_control, form_ready=False, timeout=60000):
    # Two manual stages: the report form exists before generation; result controls
    # appear afterward. There is never an automated Generate click here.
    if not form_ready:
        with discovery_stage('form-readiness'):
            require_report_page(page)
            page.wait_for_function(FORM_READY, timeout=timeout)
    print('Stage 1: pick the four report form controls. No console confirmation is required.', flush=True)
    candidates = {control[0]: picker(page, control) for control in FORM_CONTROLS}
    # Remove every capture handler/overlay before asking the user to use the form.
    with discovery_stage('manual-generation'):
        page.evaluate('window.__purchaseStopPick?.()')
        print('Stage 1 complete. The report form is unlocked.')
        prompt('Stage 2: manually set the dates/type and click Generate Report in Chromium. Wait for the table/Show Entries/Excel controls, then press Enter in this console. ')
    with discovery_stage('results-readiness'):
        require_report_page(page)
        page.wait_for_function(RESULTS_READY, timeout=timeout)
    candidates.update({control[0]: picker(page, control) for control in RESULT_CONTROLS})
    with discovery_stage('validate-candidates'):
        if candidates['START'] == candidates['END'] or candidates['GENERATE'] == candidates['EXCEL']:
            raise ValueError('Different controls must have different selectors.')
    return candidates


def main():
    # Disable diagnostic modes that could otherwise log browser interaction.
    os.environ.pop('DEBUG', None)
    os.environ.pop('PWDEBUG', None)
    with discovery_stage('runtime'):
        with sync_playwright() as p:
            with discovery_stage('browser-launch'):
                browser = p.chromium.launch(headless=False)
            try:
                with discovery_stage('context-creation'):
                    context = browser.new_context(accept_downloads=False)
                    page = context.new_page()
                prepare_report(page)
                candidates = discover_controls(page, form_ready=True)
                with discovery_stage('save-candidates'):
                    output = Path(__file__).resolve().with_name('selector_candidates.env')
                    output.write_text('# Structural candidates only; verify before copying into .env.\n' +
                                      '\n'.join(f'CISEPOS_REPORT_{key}_SELECTOR={value}' for key, value in candidates.items()) + '\n', encoding='utf-8')
                    print('Saved seven structural candidates in selector_candidates.env. No credentials or report values were collected.')
            finally:
                failing = sys.exc_info()[0] is not None
                try:
                    with discovery_stage('cleanup'):
                        browser.close()
                except DiscoveryFailure:
                    if not failing:
                        raise
                    print('WARNING: discovery cleanup failed; earlier failure retained.', file=sys.stderr)


def cli():
    try:
        main()
        return 0
    except DiscoveryFailure as exc:
        print(f'DISCOVERY FAILED: stage={exc.stage} category={exc.category}. Sensitive details suppressed.', file=sys.stderr)
        return 130 if exc.category == 'cancelled' else 1
    except (Exception, KeyboardInterrupt):
        print('DISCOVERY FAILED: stage=runtime category=unexpected-error. Sensitive details suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(cli())
