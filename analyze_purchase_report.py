"""Normalize XLSX purchases and compare weighted rates with locally known history."""
import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import unicodedata
import pandas as pd
from purchase_common import (WorkflowError, atomic_bytes, atomic_json, base_directory,
                             diagnostic, flag, report_date, run_cli)

ALIASES = {
    'item_code': ('item code', 'product code', 'sku', 'item id', 'product id'),
    'item': ('item', 'item name', 'product', 'product name', 'description'),
    'unit': ('unit', 'uom', 'unit name', 'unit of measure'),
    'quantity': ('quantity', 'qty', 'purchase qty', 'purchased quantity'),
    'net_amount': ('net amount', 'final amount', 'net total', 'amount after tax discount', 'net purchase amount'),
    'gross_amount': ('gross amount', 'gross total', 'amount', 'total amount', 'purchase amount'),
    'purchase_date': ('purchase date', 'date', 'invoice date', 'transaction date'),
    'branch': ('branch', 'branch name', 'location'),
    'row_type': ('row type', 'record type'),
}
TOTAL = re.compile(r'^(?:(?:grand|sub|branch|net|overall)\s*)?totals?(?:\s*[:\-].*)?$', re.I)


def normalized(value):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', str(value)).strip().lower()).strip()


def text(value):
    return '' if pd.isna(value) else str(value).strip()


def number(value):
    if pd.isna(value) or str(value).strip() in ('', '-', '—'):
        return None
    if isinstance(value, bool):
        raise WorkflowError('Unexpected boolean in numeric purchase data.')
    raw = str(value).strip()
    if raw.startswith('(') and raw.endswith(')'):
        raw = '-' + raw[1:-1]
    if ',' in raw and not re.fullmatch(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?', raw):
        raise WorkflowError('Ambiguous numeric separators in purchase data.')
    raw = raw.replace(',', '')
    try:
        result = float(raw)
    except ValueError:
        raise WorkflowError('Unrecognized numeric format; configure the export or mappings.') from None
    if not math.isfinite(result):
        raise WorkflowError('Non-finite purchase value encountered.')
    return result


def mapping_for(columns):
    overrides = json.loads(os.getenv('PURCHASE_COLUMN_MAP', '{}'))
    if not isinstance(overrides, dict) or any(k not in ALIASES or not isinstance(v, str) for k, v in overrides.items()):
        raise WorkflowError('PURCHASE_COLUMN_MAP must map canonical fields to column names.')
    mapping = {}
    for key, aliases in ALIASES.items():
        choices = [col for col in columns if normalized(col) in
                   ({normalized(overrides[key])} if key in overrides else set(aliases))]
        if len(choices) > 1:
            raise WorkflowError('Ambiguous columns; set PURCHASE_COLUMN_MAP explicitly.')
        if choices:
            mapping[key] = choices[0]
        elif key in overrides:
            raise WorkflowError('A configured column was not found.')
    return mapping


def read_report(path):
    # Header may follow a title/date banner. Never print workbook contents.
    sheet = os.getenv('PURCHASE_SHEET', '0')
    sheet = int(sheet) if sheet.isdigit() else sheet
    raw = pd.read_excel(path, sheet_name=sheet, header=None, engine='openpyxl', dtype=object)
    explicit = os.getenv('PURCHASE_HEADER_ROW', '').strip()
    candidates = [int(explicit)] if explicit else range(min(40, len(raw)))
    for index in candidates:
        if not 0 <= index < len(raw):
            raise WorkflowError('Configured header row is outside the workbook.')
        columns = [text(value) for value in raw.iloc[index]]
        # Automatic detection ignores unrelated title rows; strict ambiguity checks
        # run only after a plausible item/quantity header is found.
        seen = {normalized(col) for col in columns}
        overrides = json.loads(os.getenv('PURCHASE_COLUMN_MAP', '{}'))
        item_names = {normalized(overrides.get(k, a)) for k in ('item', 'item_code') for a in ALIASES[k]}
        qty_names = {normalized(overrides.get('quantity', a)) for a in ALIASES['quantity']}
        if not explicit and not (seen & item_names and seen & qty_names):
            continue
        mapping = mapping_for(columns)
        if 'quantity' not in mapping or not ({'item', 'item_code'} & mapping.keys()) or not ({'net_amount', 'gross_amount'} & mapping.keys()):
            if explicit:
                break
            continue
        if len([c for c in columns if c]) != len(set(c for c in columns if c)):
            raise WorkflowError('Duplicate column names require a corrected export.')
        frame = raw.iloc[index + 1:].copy()
        frame.columns = columns
        return frame, mapping
    raise WorkflowError('Purchase header not recognized; set header row and column mappings.')


def normalize_records(path, day):
    frame, mapping = read_report(path)
    branch_aware = flag('PURCHASE_BRANCH_AWARE', False)
    if branch_aware and 'branch' not in mapping:
        raise WorkflowError('Branch-aware comparisons require a branch column.')
    if 'unit' not in mapping and not os.getenv('PURCHASE_DEFAULT_UNIT', '').strip():
        raise WorkflowError('A unit column or explicit PURCHASE_DEFAULT_UNIT is required.')
    records, excluded = [], 0
    def get(row, key):
        return row[mapping[key]] if key in mapping else None
    for _, row in frame.iterrows():
        item, code = text(get(row, 'item')), text(get(row, 'item_code'))
        row_type = normalized(text(get(row, 'row_type')))
        if row_type in ('total', 'subtotal', 'grand total') or TOTAL.fullmatch(item) or TOTAL.fullmatch(code):
            excluded += 1
            continue
        if not item and not code:
            excluded += 1
            continue
        qty = number(get(row, 'quantity'))
        if qty is None:
            # Item-labelled rows without quantity can be malformed exports, not
            # silently ignored purchases. Fully empty rows were handled above.
            raise WorkflowError('An item row has no purchase quantity.')
        if qty <= 0:
            # Returns/zero-quantity adjustments are not positive purchases.
            excluded += 1
            continue
        net = number(get(row, 'net_amount'))
        amount = net if net is not None else number(get(row, 'gross_amount'))
        if amount is None or amount < 0:
            raise WorkflowError('A purchase row has no valid amount.')
        unit = normalized(text(get(row, 'unit')) or os.getenv('PURCHASE_DEFAULT_UNIT', ''))
        branch = normalized(text(get(row, 'branch')))
        if not unit or (branch_aware and not branch):
            raise WorkflowError('A comparison key has a missing unit or branch.')
        raw_date = get(row, 'purchase_date')
        if raw_date is None or pd.isna(raw_date) or not text(raw_date):
            if 'purchase_date' in mapping:
                raise WorkflowError('A purchase row is missing its transaction date.')
            timestamp = pd.Timestamp(day)
        else:
            # Excel serial dates must use the Excel epoch rather than Unix ns.
            timestamp = (pd.to_datetime(raw_date, unit='D', origin='1899-12-30')
                         if isinstance(raw_date, (int, float)) else pd.to_datetime(raw_date, format='mixed', dayfirst=True))
        if pd.isna(timestamp) or timestamp.tzinfo is not None:
            raise WorkflowError('Purchase timestamps must be valid local dates.')
        if timestamp.date() > day:
            continue
        identity_value = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', code or item).casefold()).strip()
        identity = ('code:' if code else 'name:') + identity_value
        records.append({'identity': identity, 'item': item or code, 'unit': unit, 'branch': branch,
                        'purchase_at': timestamp.isoformat(), 'quantity': qty, 'amount': amount,
                        'amount_basis': 'net' if net is not None else 'gross'})
    return records, excluded, branch_aware


def comparison_key(record, branch_aware):
    return (record['identity'], record['unit'], record['branch'] if branch_aware else '')


def history_records(base, day):
    history = []
    for path in sorted((Path(base) / 'processed').glob('*/records.json')):
        try:
            folder_date = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        if folder_date < day:
            saved = json.loads(path.read_text(encoding='utf-8'))
            # A snapshot includes only that snapshot's day, avoiding duplicate
            # rows when imported workbooks contain overlapping date ranges.
            history.extend(saved['records'])
    return history


def summarize(records, history, day, branch_aware):
    current = [r for r in records if date.fromisoformat(r['purchase_at'][:10]) == day]
    # Prefer the current workbook for dates it covers; never double-count overlapping imports.
    workbook_days = {r['purchase_at'][:10] for r in records}
    known = [r for r in history if r['purchase_at'][:10] not in workbook_days] + records
    prior = [r for r in known if r['purchase_at'][:10] < day.isoformat()]
    groups, previous = {}, {}
    for row in current:
        groups.setdefault(comparison_key(row, branch_aware), []).append(row)
    for row in prior:
        key = comparison_key(row, branch_aware)
        when = row['purchase_at']
        if key not in previous or when > previous[key][0]['purchase_at']:
            previous[key] = [row]
        elif when == previous[key][0]['purchase_at']:
            previous[key].append(row)
    results = []
    for key, rows in groups.items():
        qty, amount = sum(r['quantity'] for r in rows), sum(r['amount'] for r in rows)
        rate = amount / qty
        old = previous.get(key, [])
        old_qty = sum(r['quantity'] for r in old)
        old_rate = sum(r['amount'] for r in old) / old_qty if old_qty else None
        difference = rate - old_rate if old_rate is not None else None
        results.append({'item': rows[0]['item'], 'identity': key[0], 'unit': key[1], 'branch': key[2],
                        'current_rate': rate, 'previous_rate': old_rate,
                        'previous_purchase_at': old[0]['purchase_at'] if old else None,
                        'absolute_difference': difference,
                        'percent_difference': difference / old_rate * 100 if old_rate else None,
                        'total_quantity': qty, 'total_amount': amount,
                        'row_count': len(rows), 'amount_basis': ','.join(sorted({r['amount_basis'] for r in rows})),
                        'previous_amount_basis': ','.join(sorted({r['amount_basis'] for r in old})) if old else None})
    results.sort(key=lambda r: r['total_amount'], reverse=True)
    return {'report_date': day.isoformat(), 'total_purchase_amount': sum(r['amount'] for r in current),
            'total_unique_items': len({r['identity'] for r in current}), 'total_comparison_groups': len(results),
            'total_rows': len(current), 'history_scope': 'locally saved earlier snapshots and earlier rows in this workbook',
            'branch_aware': branch_aware, 'items': results}, current


def safe_csv(frame):
    # Protect spreadsheet users from formula injection in text columns.
    def protect(value):
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
            return "'" + value
        return value
    return frame.map(protect).to_csv(index=False).encode('utf-8-sig')


def analyze(path, day, base=None):
    diagnostic('analysis-read-normalize')
    base = Path(base) if base is not None else base_directory()
    records, excluded, branch_aware = normalize_records(path, day)
    diagnostic('analysis-compare-history')
    summary, current = summarize(records, history_records(base, day), day, branch_aware)
    summary['excluded_nonpurchase_rows'] = excluded
    folder = base / 'processed' / day.isoformat()
    save_analysis(summary, current, folder)
    return summary, folder


def save_analysis(summary, records, folder):
    diagnostic('analysis-save')
    columns = ['item', 'identity', 'unit', 'branch', 'current_rate', 'previous_rate', 'previous_purchase_at',
               'absolute_difference', 'percent_difference', 'total_quantity', 'total_amount', 'row_count',
               'amount_basis', 'previous_amount_basis']
    atomic_bytes(folder / 'comparison.csv', safe_csv(pd.DataFrame(summary['items'], columns=columns)))
    atomic_json(folder / 'summary.json', summary)
    atomic_json(folder / 'records.json', {'report_date': summary['report_date'], 'records': records})


def cli():
    parser = argparse.ArgumentParser(description='Analyze an already-downloaded XLSX, offline.')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--date', required=True, help='Report date YYYY-MM-DD (or today/yesterday)')
    args = parser.parse_args()
    analyze(args.input, report_date(args.date))


if __name__ == '__main__':
    raise SystemExit(run_cli(cli))
