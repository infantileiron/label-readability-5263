"""服务层端到端验收：用内存假库跑真实导入、对账与运行快照服务。

覆盖：隔天两段导入、幂等重传不重复计人、最高更正号冲突整对排除、
部分补传后结果确定、缺测保留、手录批次照常复验、快照只含当次纳入版本且可追溯。
"""
import unittest

from src.services.imports import import_sheet
from src.services.runs import enqueue, snapshot_digest
from src.services.statistics import analyze

from tests.fake_db import FakeDB


def rows(*items):
    return [{'visitor_code': code, 'correction_no': corr, 'reading_order': order, 'outcome': outcome}
            for code, corr, order, outcome in items]


class ImportServiceFlow(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.tables['studies'].append(
            {'id': 1, 'code': 'PD-1121', 'title': '执壶', 'station': '二厅', 'reading_task': '判定题',
             'old_id': 10, 'new_id': 11, 'revision': 0})
        self.db.tables['label_versions'] = [
            {'id': 10, 'code': 'YA-01', 'exhibit': '执壶', 'title': '旧', 'body': 'x' * 8,
             'type_pt': 16, 'line_height': 1.35},
            {'id': 11, 'code': 'YA-02', 'exhibit': '执壶', 'title': '新', 'body': 'y' * 8,
             'type_pt': 24, 'line_height': 1.6},
        ]

    def _pairs(self):
        return {r['visitor_code']: r for r in self.db.tables['reading_pairs']}

    def test_two_day_segments_then_pair_and_snapshot(self):
        # 第一天：旧展签表。
        import_sheet(self.db, 1, 'old', 'old-day1.csv',
                     rows(('PD-01', 0, 'old_first', True), ('PD-02', 0, 'new_first', False)), 'analyst')
        self.assertTrue(all(p['excluded'] for p in self._pairs().values()))  # 都还缺样张段

        # 第二天：易读样张飞表。
        import_sheet(self.db, 1, 'new', 'new-day2.csv',
                     rows(('PD-01', 0, 'old_first', True), ('PD-02', 0, 'new_first', True)), 'analyst')
        pairs = self._pairs()
        self.assertFalse(pairs['PD-01']['excluded'])
        self.assertFalse(pairs['PD-02']['excluded'])
        self.assertEqual(pairs['PD-02']['source'], 'import')
        # 两次导入各推进一次修订。
        self.assertEqual(self.db.tables['studies'][0]['revision'], 2)

        # 运行快照只含当次纳入统计的配对（这里两对都纳入）。
        run = enqueue(self.db, 1, 95, 'analyst')
        stored = self.db.tables['analysis_runs'][0]
        self.assertEqual(len(stored['snapshot']['pairs']), 2)
        self.assertEqual(snapshot_digest(stored['snapshot']), stored['checksum'])
        # 快照固化导入批次溯源。
        files = {b['filename'] for b in stored['snapshot']['import_batches']}
        self.assertEqual(files, {'old-day1.csv', 'new-day2.csv'})

    def test_idempotent_rerun_same_filename_does_not_duplicate(self):
        payload = rows(('PD-01', 0, 'old_first', True))
        first = import_sheet(self.db, 1, 'old', 'same.csv', payload, 'analyst')
        record_count_after_first = len(self.db.tables['import_records'])
        again = import_sheet(self.db, 1, 'old', 'same.csv', payload, 'analyst')
        self.assertTrue(again['idempotent'])
        self.assertEqual(again['batch_id'], first['batch_id'])
        self.assertEqual(len(self.db.tables['import_records']), record_count_after_first)
        # 观众仍只有一个，不重复计人。
        self.assertEqual(len(self._pairs()), 1)
        self.assertEqual(self.db.tables['studies'][0]['revision'], 1)  # 幂等重传不推进修订

    def test_highest_correction_conflict_excludes_pair_then_resolves(self):
        import_sheet(self.db, 1, 'old', 'old.csv',
                     rows(('PD-09', 0, 'old_first', True)), 'analyst')
        # 样张段同最高更正号两条不同结果 -> 冲突。
        import_sheet(self.db, 1, 'new', 'new-conflict.csv',
                     rows(('PD-09', 2, 'old_first', True), ('PD-09', 2, 'old_first', False)), 'analyst')
        self.assertTrue(self._pairs()['PD-09']['excluded'])
        self.assertIn('暂不纳入统计', self._pairs()['PD-09']['exclude_reason'])
        # 此时没有可纳入统计的配对，不允许提交运行。
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            enqueue(self.db, 1, 95, 'analyst')
        # 补一个更高更正号的唯一结果（新文件名）解除冲突。
        import_sheet(self.db, 1, 'new', 'new-fixed.csv',
                     rows(('PD-09', 3, 'old_first', False)), 'analyst')
        pair = self._pairs()['PD-09']
        self.assertFalse(pair['excluded'])
        self.assertEqual(pair['new_ok'], False)

    def test_missing_outcome_preserved_and_excluded(self):
        import_sheet(self.db, 1, 'old', 'old-m.csv', rows(('PD-05', 0, 'old_first', None)), 'analyst')
        import_sheet(self.db, 1, 'new', 'new-m.csv', rows(('PD-05', 0, 'old_first', True)), 'analyst')
        pair = self._pairs()['PD-05']
        self.assertIsNone(pair['old_ok'])      # 缺测不是失败
        self.assertTrue(pair['excluded'])
        result = analyze([dict(r) for r in self.db.tables['reading_pairs']], 95)
        self.assertEqual(result['complete'], 0)
        self.assertIsNone(result['p_value'])

    def test_partial_uploads_deterministic_and_snapshot_version_pinned(self):
        # 先到一对完整 + 一个只到旧段的观众。
        import_sheet(self.db, 1, 'old', 'o1.csv',
                     rows(('PD-01', 0, 'old_first', True), ('PD-02', 0, 'new_first', True)), 'analyst')
        import_sheet(self.db, 1, 'new', 'n1.csv', rows(('PD-01', 0, 'old_first', False)), 'analyst')
        run1 = enqueue(self.db, 1, 95, 'analyst')
        snap1_pairs = [p['visitor_code'] for p in self.db.tables['analysis_runs'][0]['snapshot']['pairs']]
        self.assertEqual(snap1_pairs, ['PD-01'])  # 当次只纳入已齐的 PD-01

        # 补齐 PD-02 样张段后重跑：新快照含两对，但旧快照仍可查、内容不变。
        import_sheet(self.db, 1, 'new', 'n2.csv', rows(('PD-02', 0, 'new_first', False)), 'analyst')
        run2 = enqueue(self.db, 1, 95, 'analyst')
        self.assertNotEqual(run1['id'], run2['id'])
        first_snapshot = self.db.tables['analysis_runs'][0]['snapshot']
        self.assertEqual([p['visitor_code'] for p in first_snapshot['pairs']], ['PD-01'])
        second = self.db.tables['analysis_runs'][1]['snapshot']
        self.assertEqual([p['visitor_code'] for p in second['pairs']], ['PD-01', 'PD-02'])
        # 旧快照校验值保持有效，可追溯原结果。
        self.assertEqual(snapshot_digest(first_snapshot), self.db.tables['analysis_runs'][0]['checksum'])

    def test_manual_legacy_pair_still_reviewed_alongside_imports(self):
        # 一条历史手录配对（默认纳入统计）。
        self.db.tables['reading_pairs'].append(
            {'study_id': 1, 'visitor_code': 'M-01', 'reading_order': 'old_first',
             'old_ok': True, 'new_ok': True, 'source': 'manual', 'excluded': False,
             'exclude_reason': None})
        import_sheet(self.db, 1, 'old', 'o.csv', rows(('PD-01', 0, 'new_first', False)), 'analyst')
        import_sheet(self.db, 1, 'new', 'n.csv', rows(('PD-01', 0, 'new_first', True)), 'analyst')
        codes = {r['visitor_code']: r for r in self.db.tables['reading_pairs']}
        self.assertIn('M-01', codes)  # 手录不被导入覆盖
        run = enqueue(self.db, 1, 95, 'analyst')
        snap_codes = {p['visitor_code'] for p in self.db.tables['analysis_runs'][0]['snapshot']['pairs']}
        self.assertEqual(snap_codes, {'M-01', 'PD-01'})  # 手录照常纳入复验
        result = analyze([dict(p) for p in self.db.tables['reading_pairs']], 95)
        self.assertEqual(result['complete'], 2)

    def test_snapshot_pins_excluded_reason_and_keeps_history_after_completion(self):
        # 一对可纳入 + 一对因冲突被排除，先跑一次并固定排除原因。
        import_sheet(self.db, 1, 'old', 'old.csv',
                     rows(('PD-01', 0, 'old_first', True), ('PD-09', 0, 'old_first', True)), 'analyst')
        import_sheet(self.db, 1, 'new', 'new.csv',
                     rows(('PD-01', 0, 'old_first', True),
                          ('PD-09', 2, 'old_first', True), ('PD-09', 2, 'old_first', False)), 'analyst')
        enqueue(self.db, 1, 95, 'analyst')
        first = self.db.tables['analysis_runs'][0]['snapshot']
        self.assertEqual([p['visitor_code'] for p in first['pairs']], ['PD-01'])
        self.assertEqual([p['visitor_code'] for p in first['excluded_pairs']], ['PD-09'])
        self.assertIn('暂不纳入统计', first['excluded_pairs'][0]['exclude_reason'])
        first_checksum = self.db.tables['analysis_runs'][0]['checksum']

        # 更高更正号补齐，冲突解除，整对恢复纳入；新快照纳入两对。
        import_sheet(self.db, 1, 'new', 'new-fixed.csv',
                     rows(('PD-09', 3, 'old_first', False)), 'analyst')
        enqueue(self.db, 1, 95, 'analyst')
        second = self.db.tables['analysis_runs'][1]['snapshot']
        self.assertEqual({p['visitor_code'] for p in second['pairs']}, {'PD-01', 'PD-09'})
        self.assertEqual(second['excluded_pairs'], [])
        # 旧快照与其校验值保持不变，原排除结论仍可追溯。
        self.assertEqual(self.db.tables['analysis_runs'][0]['snapshot'], first)
        self.assertEqual(snapshot_digest(first), first_checksum)

    def test_identical_input_reuses_run_changing_input_creates_new(self):
        import_sheet(self.db, 1, 'old', 'old.csv', rows(('PD-01', 0, 'old_first', True)), 'analyst')
        import_sheet(self.db, 1, 'new', 'new.csv', rows(('PD-01', 0, 'old_first', False)), 'analyst')
        first = enqueue(self.db, 1, 95, 'analyst')
        # 数据未变再次提交：命中相同校验值，复用运行，不新增队列行。
        again = enqueue(self.db, 1, 95, 'analyst')
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(len(self.db.tables['analysis_runs']), 1)
        # 置信水平变化 -> 校验值变化 -> 新运行。
        other = enqueue(self.db, 1, 99, 'analyst')
        self.assertNotEqual(other['id'], first['id'])
        self.assertEqual(len(self.db.tables['analysis_runs']), 2)


if __name__ == '__main__':
    unittest.main()
