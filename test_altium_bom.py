"""Synthetic Altium BOMs: parse only, never fabricate or order these parts."""

import csv
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from preflight.bom import parse_bom

ROOT = Path(__file__).parent
HEADER = ['Designator', 'Comment', 'Footprint', 'Quantity',
          'Manufacturer Part Number', 'Manufacturer',
          'Supplier Part Number 1', 'Supplier 1']


def bom(supplier='LCSC', supplier_part='C123456'):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(HEADER)
    writer.writerow(['R1,R2', '330R', 'R0805', '2',
                     'RC0805FR-07330RL', 'Yageo', supplier_part, supplier])
    return output.getvalue()


class AltiumBomTest(unittest.TestCase):
    def test_numbered_supplier_headers_are_mapped(self):
        row = parse_bom(bom())[0]
        self.assertEqual(row.refs, ['R1', 'R2'])
        self.assertEqual(row.qty, 2)
        self.assertEqual(row.value, '330R')
        self.assertEqual(row.footprint, 'R0805')
        self.assertEqual(row.mpn, 'RC0805FR-07330RL')
        self.assertEqual(row.manufacturer, 'Yageo')
        self.assertEqual(row.lcsc, 'C123456')

    def test_supplier_part_is_kept_only_for_lcsc_rows(self):
        for supplier, expected in (('LCSC', 'C123456'),
                                   (' lcsc ', 'C123456'),
                                   ('Digi-Key', ''),
                                   ('Other Supplier', ''),
                                   ('', '')):
            with self.subTest(supplier=supplier):
                row = parse_bom(bom(supplier))[0]
                self.assertEqual(row.lcsc, expected)

    def test_empty_supplier_columns_are_safe(self):
        row = parse_bom(bom('', ''))[0]
        self.assertEqual(row.lcsc, '')
        self.assertEqual(row.mpn, 'RC0805FR-07330RL')

    def test_secondary_supplier_columns_are_not_aliased(self):
        # Only the primary (1-based) Altium supplier columns are mapped;
        # Supplier 2 / Supplier Part Number 2 stay unrecognised for now.
        text = ('Designator,Supplier Part Number 2,Supplier 2\n'
                'R1,C123456,LCSC\n')
        row = parse_bom(text)[0]
        self.assertEqual(row.lcsc, '')

    def test_sample_scans_standalone(self):
        result = subprocess.run(
            [sys.executable, '-m', 'preflight',
             str(ROOT / 'samples' / 'altium' / 'bom.csv'),
             '--format', 'json'],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual([finding['rule'] for finding in report['findings']],
                         ['BOM_ONLY_MODE'])


if __name__ == '__main__':
    unittest.main()
