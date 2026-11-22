"""CSV 交付表解析验收：UTF-8/BOM、缺测留空、布尔词表、表头与坏值拒绝。"""
import unittest

from fastapi import HTTPException

from src.services.csv_import import parse_sheet_csv


def csv_bytes(text):
    return text.encode('utf-8')


HEADER = 'visitor_code,correction_no,reading_order,outcome\n'


class CsvImportParsing(unittest.TestCase):
    def test_bom_and_missing_and_booleans(self):
        body = '﻿' + HEADER + (
            'PD-01,0,old_first,true\n'
            'PD-02,1,new_first,FALSE\n'
            'PD-03,0,old_first,\n'            # 空 = 缺测
            'PD-04,2,new_first,缺测\n'         # 中文缺测标记
            'PD-05,0,old_first,成功\n'
        )
        rows = parse_sheet_csv(csv_bytes(body), 'old', 'old.csv')
        idx = {r['visitor_code']: r for r in rows}
        self.assertEqual(idx['PD-01']['outcome'], True)
        self.assertEqual(idx['PD-02']['outcome'], False)
        self.assertIsNone(idx['PD-03']['outcome'])
        self.assertIsNone(idx['PD-04']['outcome'])
        self.assertEqual(idx['PD-05']['outcome'], True)
        self.assertTrue(all(r['filename'] == 'old.csv' for r in rows))

    def test_missing_header_column_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            parse_sheet_csv(csv_bytes('visitor_code,correction_no,reading_order\nPD-01,0,old_first\n'),
                            'new', 'x.csv')
        self.assertEqual(ctx.exception.status_code, 422)

    def test_bad_correction_and_outcome(self):
        with self.assertRaises(HTTPException):
            parse_sheet_csv(csv_bytes(HEADER + 'PD-01,x,old_first,true\n'), 'old', 'x.csv')
        with self.assertRaises(HTTPException):
            parse_sheet_csv(csv_bytes(HEADER + 'PD-01,0,old_first,maybe\n'), 'old', 'x.csv')

    def test_non_utf8_rejected(self):
        with self.assertRaises(HTTPException):
            parse_sheet_csv(b'\xff\xfe' + HEADER.encode('utf-16-le'), 'old', 'x.csv')

    def test_empty_sheet_rejected(self):
        with self.assertRaises(HTTPException):
            parse_sheet_csv(csv_bytes('﻿' + HEADER), 'old', 'x.csv')


if __name__ == '__main__':
    unittest.main()
