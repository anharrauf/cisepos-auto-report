"""Confirmed 14-column layout with synthetic records only."""
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openpyxl import Workbook
from analyze_purchase_report import CISEPOS_COLUMNS, analyze, summarize
from purchase_common import WorkflowError
from send_purchase_email import render_html

DAY = date(2026, 10, 17)


def header(no, when='17-10-2026', branch='North'):
    return ['Purchase', no, when, branch, None, None, None, 'Synthetic supplier', None, None, None, None, None, None]


def line(item='Apple', qty=2, rate=10, net=23, branch=None):
    return [None, None, None, branch, '00123', item, 'kg', None, qty, rate, qty*rate, 4, 1, net]


def total():
    return [None, None, None, None, None, 'Total', None, None, 99, None, 999, None, None, 999]


class ConfirmedSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        app_prefixes = ('PURCHASE_', 'REPORT_', 'DOWNLOAD_', 'CISEPOS_', 'SMTP_', 'EMAIL_')
        clean = {k:v for k,v in os.environ.items() if not k.startswith(app_prefixes)}
        self.env = patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = self.base / 'actual-layout.xlsx'

    def save(self, rows):
        book = Workbook()
        book.active.append(['Synthetic report title / date range / branch / timestamp'])
        book.active.append(list(CISEPOS_COLUMNS.values()))
        for row in rows:
            book.active.append(row)
        book.save(self.path)
        return self.path

    def test_confirmed_mapping_header_index_and_separate_rates(self):
        os.environ['PURCHASE_HEADER_ROW'] = '1'
        os.environ['PURCHASE_COLUMN_MAP'] = json.dumps(CISEPOS_COLUMNS)
        summary, folder = analyze(self.save([header('P1'), line(), total()]), DAY, self.base)
        row = summary['items'][0]
        self.assertTrue(summary['branch_aware'])
        self.assertEqual(row['current_purchase_rate'], 10)
        self.assertEqual(row['current_effective_net_rate'], 11.5)
        self.assertEqual(summary['total_transactions'], 1)
        self.assertEqual(summary['total_purchase_amount'], 23)
        saved = json.loads((folder/'records.json').read_text())
        self.assertEqual(saved['schema_version'], 2)
        self.assertEqual(saved['records'][0]['transaction_no'], 'P1')
        self.assertEqual(saved['records'][0]['tax_amount'], 4)

    def test_prior_purchase_rate_branch_and_effective_comparisons(self):
        analyze(self.save([header('OLD', '15-10-2026'), line(rate=8, net=18), total(),
                           header('SOUTH', '15-10-2026', 'South'), line(rate=50, net=104), total()]), date(2026,10,15), self.base)
        summary, _ = analyze(self.save([header('NEW'), line(), total()]), DAY, self.base)
        row = summary['items'][0]
        self.assertEqual(row['previous_purchase_rate'], 8)
        self.assertEqual(row['percent_difference'], 25)
        self.assertEqual(row['previous_effective_net_rate'], 9)
        self.assertEqual(row['effective_absolute_difference'], 2.5)
        html = render_html(summary)
        self.assertIn('PurchaseRate change', html)
        self.assertIn('Effective net rate', html)

    def test_repeated_complete_transactions_deduplicated_legitimate_lines_retained(self):
        block = [header('P1'), line(), line(), total()]
        summary, _ = analyze(self.save(block + block), DAY, self.base)
        self.assertEqual(summary['total_transactions'], 1)
        self.assertEqual(summary['total_rows'], 2)  # Two legitimate identical lines.
        self.assertEqual(summary['total_purchase_amount'], 46)
        self.assertEqual(summary['items'][0]['total_quantity'], 4)

    def test_conflicting_transaction_copies_fail(self):
        with self.assertRaises(WorkflowError):
            analyze(self.save([header('P1'), line(), total(), header('P1'), line(net=25), total()]), DAY, self.base)

    def test_empty_item_and_transaction_headers_are_not_purchases(self):
        blank_item = line(item=None)
        summary, _ = analyze(self.save([header('P1'), blank_item, line(), total(), [None]*14]), DAY, self.base)
        self.assertEqual(summary['total_rows'], 1)
        self.assertEqual(summary['total_transactions'], 1)

    def test_branch_transaction_number_reuse_not_deduplicated(self):
        summary, _ = analyze(self.save([header('P1'), line(), total(), header('P1', branch='South'), line(), total()]), DAY, self.base)
        self.assertEqual(summary['total_transactions'], 2)
        self.assertEqual(len(summary['items']), 2)

    def test_history_is_preserved_across_reruns(self):
        _, folder = analyze(self.save([header('OLD','15-10-2026'), line(),total()]),date(2026,10,15),self.base)
        before = (folder/'records.json').read_bytes()
        for _ in range(2):
            analyze(self.save([header('NEW'),line(rate=12),total()]),DAY,self.base)
        self.assertEqual((folder/'records.json').read_bytes(), before)

    def test_legacy_amount_never_masquerades_as_purchase_rate(self):
        row = dict(identity='code:a', item='A', branch='north', unit='kg', purchase_at='2026-10-17T00:00:00',
                   quantity=1, amount=12, net_amount=12, amount_basis='net', purchase_rate=10)
        old = dict(row, purchase_at='2026-10-16T00:00:00')
        old.pop('purchase_rate')
        summary, _ = summarize([row],[old],DAY,True)
        self.assertIsNone(summary['items'][0]['previous_purchase_rate'])
        self.assertEqual(summary['items'][0]['comparison_status'],'legacy_rate_unavailable')
        earlier = dict(row,purchase_at='2026-10-15T00:00:00',purchase_rate=8)
        summary, _ = summarize([row],[earlier,old],DAY,True)
        self.assertEqual(summary['items'][0]['previous_purchase_rate'],8)

    def test_metadata_not_forward_filled_across_transactions(self):
        broken = line()
        broken[1] = 'OTHER'
        with self.assertRaises(WorkflowError):
            analyze(self.save([header('P1'),line(),broken]),DAY,self.base)

    def test_actual_profile_cannot_accidentally_combine_branches(self):
        os.environ['PURCHASE_BRANCH_AWARE'] = 'false'
        with self.assertRaises(WorkflowError):
            analyze(self.save([header('P1'),line(),total()]),DAY,self.base)

    def test_missing_purchase_rate_is_not_inferred_from_net(self):
        broken = line()
        broken[9] = None
        with self.assertRaises(WorkflowError):
            analyze(self.save([header('P1'),broken]),DAY,self.base)


if __name__ == '__main__':
    unittest.main()
