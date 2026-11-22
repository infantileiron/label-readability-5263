"""导出验收：CSV 主表只含当次纳入配对，排除明细与批次溯源随快照固定。"""
import csv
import io
import unittest

from src.services.exports import pairing_csv
from src.services.runs import snapshot_digest


class ExportSnapshot(unittest.TestCase):
    def _export(self):
        snapshot = {
            'study_id': 1, 'revision': 3, 'pairs': [
                {'visitor_code': 'PD-01', 'reading_order': 'old_first', 'old_ok': True, 'new_ok': True,
                 'source': 'import', 'excluded': False},
                {'visitor_code': 'PD-02', 'reading_order': 'new_first', 'old_ok': False, 'new_ok': True,
                 'source': 'manual', 'excluded': False},
            ],
            'excluded_pairs': [
                {'visitor_code': 'PD-09', 'reading_order': 'old_first', 'old_ok': None, 'new_ok': None,
                 'source': 'import', 'exclude_reason': '最高更正号存在不同结果，整对暂不纳入统计'},
            ],
            'import_batches': [{'id': 1, 'filename': 'old.csv'}, {'id': 2, 'filename': 'new.csv'}],
        }
        return {'id': 7, 'checksum': snapshot_digest(snapshot), 'revision': 3, 'snapshot': snapshot}

    def test_csv_includes_included_then_excluded_section(self):
        text = pairing_csv(self._export())
        rows = list(csv.reader(io.StringIO(text)))
        header = rows[0]
        self.assertIn('included', header)
        # 前两条数据为纳入配对，included 列为 True。
        data = [r for r in rows[1:] if r and r[0] == '7' and r[-1] == 'True']
        self.assertEqual([r[3] for r in data], ['PD-01', 'PD-02'])
        # 排除段含原因说明。
        self.assertTrue(any('暂不纳入统计' in (r[-1] if r else '') for r in rows))
        self.assertTrue(any('PD-09' in (r[3] if len(r) > 3 else '') for r in rows))

    def test_snapshot_checksum_covers_excluded_and_batches(self):
        export = self._export()
        # 改动排除原因会改变校验值，证明排除版本确实进入固定快照。
        tampered = {**export['snapshot'],
                    'excluded_pairs': [{**export['snapshot']['excluded_pairs'][0], 'exclude_reason': 'changed'}]}
        self.assertNotEqual(snapshot_digest(tampered), export['checksum'])
        # 改动批次溯源同样改变校验值。
        tampered_batches = {**export['snapshot'], 'import_batches': [{'id': 9, 'filename': 'x.csv'}]}
        self.assertNotEqual(snapshot_digest(tampered_batches), export['checksum'])
        # 原始快照校验值保持稳定，可追溯。
        self.assertEqual(snapshot_digest(export['snapshot']), export['checksum'])


if __name__ == '__main__':
    unittest.main()
