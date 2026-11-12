"""Synthetic Pro BOMs: parse only, never fabricate or order these parts."""

import csv
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from preflight.bom import parse_bom

ROOT = Path(__file__).parent
HEADER = ['No.', 'Quantity', 'Comment', 'Designator', 'Footprint', 'Value',
          'Manufacturer Part', 'Manufacturer', 'Supplier Part', 'Supplier']


def bom(supplier='LCSC', extra_headers=(), extra_cells=(), delimiter=','):
    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(HEADER + list(extra_headers))
    writer.writerow(['1', '2', '330R', 'R1,R2', 'R0805', '330R', '', '',
                     'C990000001', supplier] + list(extra_cells))
    return output.getvalue()


class EasyEdaBomTest(unittest.TestCase):
    def test_supplier_is_required_per_row(self):
        for supplier in ('LCSC', ' lcsc ', 'Other Supplier', '', 'LCSC Electronics'):
            with self.subTest(supplier=supplier):
                row = parse_bom(bom(supplier))[0]
                self.assertEqual(row.refs, ['R1', 'R2'])
                self.assertEqual(row.qty, 2)
                self.assertEqual(row.value, '330R')
                self.assertEqual(row.footprint, 'R0805')
                self.assertEqual(row.lcsc, 'C990000001' if supplier.strip().lower() == 'lcsc' else '')

    def test_documented_tab_separated_pro_headers(self):
        row = parse_bom(bom(delimiter='\t'))[0]
        self.assertEqual(row.refs, ['R1', 'R2'])
        self.assertEqual(row.lcsc, 'C990000001')

    def test_explicit_lcsc_column_keeps_precedence_even_when_empty(self):
        for delimiter in (',', '\t'):
            for explicit in ('C990000099', ''):
                with self.subTest(delimiter=delimiter, explicit=explicit):
                    row = parse_bom(bom(extra_headers=['LCSC Part #'], extra_cells=[explicit], delimiter=delimiter))[0]
                    self.assertEqual(row.lcsc, explicit)

    def test_supplier_part_without_supplier_is_not_an_lcsc_alias(self):
        row = parse_bom('Designator,Quantity,Value,Footprint,Supplier Part\nR1,1,330R,R0805,C990000001\n')[0]
        self.assertEqual(row.lcsc, '')

    def test_short_rows_and_missing_supplier_part_are_safe(self):
        row = parse_bom(','.join(HEADER) + '\n1,1,330R,R1,R0805\n')[0]
        self.assertEqual(row.lcsc, '')
        row = parse_bom('Designator,Quantity,Value,Footprint,Supplier\nR1,1,330R,R0805,LCSC\n')[0]
        self.assertEqual(row.lcsc, '')

    def test_generic_tab_separated_and_legacy_headers_are_unchanged(self):
        for text in ('Designator\tValue\nR1\t330R\n',
                     'Components,Name\nR1,330R\n'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, 'no recognisable reference'):
                    parse_bom(text)

    def test_tabs_inside_comma_separated_fields_stay_literal(self):
        row = parse_bom('Designator,Value,Description\nR1,"330R\tprecision",note\n')[0]
        self.assertEqual(row.value, '330R\tprecision')

    def test_synthetic_good_and_broken_samples_have_exact_findings(self):
        for name, verdict, code, rules in (
            ('good', 'PASS', 0, ['BOM_ONLY_MODE']),
            ('broken', 'CAUTION', 1, ['BOM_MISSING_MPN', 'BOM_ONLY_MODE']),
        ):
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, '-m', 'preflight', str(ROOT / 'samples' / 'easyeda-pro' / f'{name}.csv'), '--format', 'json'],
                    cwd=ROOT, capture_output=True, text=True,
                )
                report = json.loads(result.stdout)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(report['verdict'], verdict)
                self.assertEqual([finding['rule'] for finding in report['findings']], rules)


if __name__ == '__main__':
    unittest.main()
