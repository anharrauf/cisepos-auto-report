"""Compare explicit PurchaseRate and separate effective net rates using local history."""
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
    'item_code': ('barcode', 'item code', 'product code', 'sku', 'item id', 'product id'),
    'item': ('item', 'item name', 'product', 'product name', 'description'),
    'unit': ('default unit', 'unit', 'uom', 'unit name', 'unit of measure'),
    'quantity': ('quantity', 'qty', 'purchase qty', 'purchased quantity'),
    'net_amount': ('netamount', 'net amount', 'final amount', 'net total', 'amount after tax discount', 'net purchase amount'),
    'gross_amount': ('grossamount', 'gross amount', 'gross total', 'amount', 'total amount', 'purchase amount'),
    'purchase_date': ('purchase date', 'date', 'invoice date', 'transaction date'),
    'branch': ('branch', 'branch name', 'location'),
    'row_type': ('row type', 'record type'),
    'purchase_rate': ('purchaserate', 'purchase rate'),
    'transaction_no': ('transaction no',),
    'purchase_type': ('purchase type',),
    'supplier': ('supplier',),
    'tax_amount': ('taxamount', 'tax amount'),
    'discount_amount': ('discount amount',),
}
CISEPOS_COLUMNS = dict(zip(
    ('purchase_type', 'transaction_no', 'purchase_date', 'branch', 'item_code', 'item', 'unit',
     'supplier', 'quantity', 'purchase_rate', 'gross_amount', 'tax_amount', 'discount_amount', 'net_amount'),
    ('Purchase Type', 'Transaction No', 'Date', 'Branch', 'BarCode', 'Item', 'Default Unit',
     'Supplier', 'Quantity', 'PurchaseRate', 'GrossAmount', 'TaxAmount', 'Discount Amount', 'NetAmount')))
TOTAL = re.compile(r'^(?:(?:grand|sub|branch|net|overall)\s*)?totals?(?:\s*[:\-].*)?$', re.I)


def normalized(value):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', str(value)).strip().lower()).strip()


def text(value):
    if pd.isna(value):
        return ''
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value).strip()


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


def transaction_key(record):
    if not record.get('transaction_no'):
        return None
    return (record.get('purchase_type', ''), record['transaction_no'],
            record['branch'], record['purchase_at'][:10])


def deduplicate_blocks(records):
    """Deduplicate complete transaction blocks, preserving legitimate equal lines."""
    from itertools import groupby
    output, seen, duplicates = [], {}, 0
    for _, group in groupby(records, key=lambda r: r['_block']):
        rows = list(group)
        key = transaction_key(rows[0])
        for r in rows:
            r.pop('_block', None)
        signature = sorted(json.dumps(r, sort_keys=True) for r in rows)
        if key is not None and key in seen:
            if signature != seen[key]:
                raise WorkflowError('Conflicting repeated transaction blocks require review; no amounts were combined.')
            duplicates += len(rows)
            continue
        if key is not None:
            seen[key] = signature
        output.extend(rows)
    return output, duplicates


def normalize_records(path, day):
    frame, mapping = read_report(path)
    actual = 'transaction_no' in mapping
    branch_aware = flag('PURCHASE_BRANCH_AWARE', actual)
    if actual and not branch_aware:
        raise WorkflowError('The All Branches Purchasing profile requires PURCHASE_BRANCH_AWARE=true.')
    if actual and any(key not in mapping for key in CISEPOS_COLUMNS):
        raise WorkflowError('The CISEPOS Purchasing export requires all 14 confirmed columns.')
    if branch_aware and 'branch' not in mapping:
        raise WorkflowError('Branch-aware comparisons require a branch column.')
    if 'unit' not in mapping and not os.getenv('PURCHASE_DEFAULT_UNIT', '').strip():
        raise WorkflowError('A unit column or explicit PURCHASE_DEFAULT_UNIT is required.')
    records, excluded, context, block = [], 0, {}, 0
    metadata = ('transaction_no', 'purchase_type', 'purchase_date', 'branch', 'supplier')
    def get(row, key):
        return row[mapping[key]] if key in mapping else None
    previous_key = None
    for _, row in frame.iterrows():
        item, code = text(get(row, 'item')), text(get(row, 'item_code'))
        if (normalized(text(get(row, 'row_type'))) in ('total', 'subtotal', 'sub total', 'grand total')
                or any(TOTAL.fullmatch(text(get(row, field))) for field in
                       ('item', 'item_code', 'purchase_type', 'transaction_no', 'branch'))):
            excluded += 1
            context = {}
            block += 1
            continue
        header = not item or (actual and not code and not text(get(row, 'quantity'))
                              and not text(get(row, 'purchase_rate')) and not text(get(row, 'unit')))
        if header:
            # Only explicitly identified transaction headers seed metadata. Empty
            # item rows never become purchases even if they contain invoice totals.
            if actual and text(get(row, 'transaction_no')):
                context = {k: get(row, k) for k in metadata if text(get(row, k))}
                block += 1
            excluded += 1
            continue
        values = {k: get(row, k) if text(get(row, k)) else context.get(k) for k in metadata}
        # A newly numbered flat transaction cannot inherit the previous one's date/branch.
        own_no = text(get(row, 'transaction_no'))
        if own_no and own_no != text(context.get('transaction_no')):
            context = {k: get(row, k) for k in metadata if text(get(row, k))}
            values = {k: get(row, k) for k in metadata}
        qty = number(get(row, 'quantity'))
        if qty is None:
            raise WorkflowError('An item row has no purchase quantity.')
        if qty <= 0:
            excluded += 1
            continue
        net = number(get(row, 'net_amount'))
        gross = number(get(row, 'gross_amount'))
        amount = net if net is not None else gross
        rate = number(get(row, 'purchase_rate'))
        if amount is None or amount < 0 or (rate is not None and rate < 0):
            raise WorkflowError('A purchase row has no valid amount or PurchaseRate.')
        if actual and (rate is None or net is None):
            raise WorkflowError('CISEPOS item rows require PurchaseRate and NetAmount.')
        unit = normalized(text(get(row, 'unit')) or os.getenv('PURCHASE_DEFAULT_UNIT', ''))
        branch = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text(values['branch'])).casefold()).strip()
        if not unit or (branch_aware and not branch):
            raise WorkflowError('A comparison key has a missing unit or branch.')
        raw_date = values['purchase_date']
        if raw_date is None or pd.isna(raw_date) or not text(raw_date):
            if 'purchase_date' in mapping:
                raise WorkflowError('A purchase row is missing its transaction date.')
            timestamp = pd.Timestamp(day)
        else:
            timestamp = (pd.to_datetime(raw_date, unit='D', origin='1899-12-30')
                         if isinstance(raw_date, (int, float)) else pd.to_datetime(raw_date, format='mixed', dayfirst=True))
        if pd.isna(timestamp) or timestamp.tzinfo is not None:
            raise WorkflowError('Purchase timestamps must be valid local dates.')
        if timestamp.date() > day:
            continue
        identity_value = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', code or item).casefold()).strip()
        transaction = text(values['transaction_no'])
        if actual and (not transaction or not text(values['purchase_type'])):
            raise WorkflowError('An item row has no fully identified purchasing transaction.')
        record = {'identity': ('code:' if code else 'name:') + identity_value, 'item': item,
                  'unit': unit, 'branch': branch, 'purchase_at': timestamp.isoformat(),
                  'quantity': qty, 'amount': amount, 'net_amount': net,
                  'purchase_rate': rate, 'effective_net_rate': net / qty if net is not None else None,
                  'amount_basis': 'net' if net is not None else 'gross',
                  'transaction_no': transaction, 'purchase_type': text(values['purchase_type']),
                  'supplier': text(values['supplier']),
                  'tax_amount': number(get(row, 'tax_amount')),
                  'discount_amount': number(get(row, 'discount_amount'))}
        key = transaction_key(record)
        if key != previous_key:
            block += 1
        previous_key = key
        record['_block'] = block
        records.append(record)
    records, duplicate_rows = deduplicate_blocks(records)
    return records, excluded + duplicate_rows, branch_aware


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
    groups, previous, legacy = {}, {}, {}
    for row in current:
        groups.setdefault(comparison_key(row, branch_aware), []).append(row)
    for row in prior:
        key = comparison_key(row, branch_aware)
        when = row['purchase_at']
        if key not in legacy or when > legacy[key]['purchase_at']:
            legacy[key] = row
        if row.get('purchase_rate') is None:
            continue
        if key not in previous or when > previous[key][0]['purchase_at']:
            previous[key] = [row]
        elif when == previous[key][0]['purchase_at']:
            previous[key].append(row)
    results = []
    for key, rows in groups.items():
        qty, amount = sum(r['quantity'] for r in rows), sum(r['amount'] for r in rows)
        rate = (sum(r['purchase_rate'] * r['quantity'] for r in rows) / qty
                if all(r.get('purchase_rate') is not None for r in rows) else None)
        effective = (sum(r['net_amount'] for r in rows) / qty
                     if all(r.get('net_amount') is not None for r in rows) else None)
        old = previous.get(key, [])
        old_qty = sum(r['quantity'] for r in old)
        old_rate = (sum(r['purchase_rate'] * r['quantity'] for r in old) / old_qty
                    if old_qty and all(r.get('purchase_rate') is not None for r in old) else None)
        old_effective = (sum(r['net_amount'] for r in old) / old_qty
                         if old_qty and all(r.get('net_amount') is not None for r in old) else None)
        difference = rate - old_rate if rate is not None and old_rate is not None else None
        effective_difference = effective - old_effective if effective is not None and old_effective is not None else None
        results.append({'item': rows[0]['item'], 'identity': key[0], 'unit': key[1], 'branch': key[2],
                        'current_rate': rate, 'previous_rate': old_rate,
                        'current_purchase_rate': rate, 'previous_purchase_rate': old_rate,
                        'current_effective_net_rate': effective, 'previous_effective_net_rate': old_effective,
                        'effective_absolute_difference': effective_difference,
                        'effective_percent_difference': effective_difference / old_effective * 100 if old_effective and effective_difference is not None else None,
                        'comparison_status': ('comparable' if old else ('legacy_rate_unavailable' if key in legacy else 'new_item')),
                        'previous_purchase_at': old[0]['purchase_at'] if old else None,
                        'absolute_difference': difference,
                        'percent_difference': difference / old_rate * 100 if old_rate and difference is not None else None,
                        'total_quantity': qty, 'total_amount': amount,
                        'row_count': len(rows), 'amount_basis': ','.join(sorted({r['amount_basis'] for r in rows})),
                        'previous_amount_basis': ','.join(sorted({r['amount_basis'] for r in old})) if old else None})
    results.sort(key=lambda r: r['total_amount'], reverse=True)
    return {'report_date': day.isoformat(), 'total_purchase_amount': sum(r['amount'] for r in current),
            'total_unique_items': len({r['identity'] for r in current}), 'total_comparison_groups': len(results),
            'total_rows': len(current),
            'total_transactions': len({transaction_key(r) for r in current if transaction_key(r) is not None}),
            'total_tax_amount': sum(r['tax_amount'] for r in current) if all(r.get('tax_amount') is not None for r in current) else None,
            'total_discount_amount': sum(r['discount_amount'] for r in current) if all(r.get('discount_amount') is not None for r in current) else None,
            'rate_basis': 'quantity-weighted PurchaseRate; NetAmount/Quantity reported separately', 'history_scope': 'locally saved earlier snapshots and earlier rows in this workbook',
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
               'amount_basis', 'previous_amount_basis', 'current_purchase_rate', 'previous_purchase_rate',
               'current_effective_net_rate', 'previous_effective_net_rate', 'effective_absolute_difference',
               'effective_percent_difference', 'comparison_status']
    atomic_bytes(folder / 'comparison.csv', safe_csv(pd.DataFrame(summary['items'], columns=columns)))
    atomic_json(folder / 'summary.json', summary)
    atomic_json(folder / 'records.json', {'schema_version': 2, 'report_date': summary['report_date'], 'records': records})


def cli():
    parser = argparse.ArgumentParser(description='Analyze an already-downloaded XLSX, offline.')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--date', required=True, help='Report date YYYY-MM-DD (or today/yesterday)')
    args = parser.parse_args()
    analyze(args.input, report_date(args.date))


if __name__ == '__main__':
    raise SystemExit(run_cli(cli))
