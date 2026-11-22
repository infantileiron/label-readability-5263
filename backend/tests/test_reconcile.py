"""验收测试：交付表导入对账与统计口径（纯 Python，不依赖 PostgreSQL）。

运行：PYTHONPATH=/tmp/pylibs python -m unittest discover -s backend/tests -v
覆盖需求：
- 旧展签 / 易读样张两份表按批次与观众合并成对，两段可隔天分批到达；
- 同一段取最高更正号；最高更正号有不同结果时整对暂不纳入统计；
- 中断重跑不重复计人；乱序导齐后结果一致；部分补传可复验；
- 缺测保留为缺测而非失败；历史手录批次照常复验。
"""
import random
import unittest

from src.services.reconcile import (ReconcileError, build_pairs, merge_batch,
                                    parse_sheet_rows, reduce_segments)
from src.services.statistics import analyze, summarize


def rec(code, segment, correction, order, outcome, batch_id=1, filename=None):
    return {'visitor_code': code, 'segment': segment, 'correction_no': correction,
            'reading_order': order, 'outcome': outcome, 'batch_id': batch_id,
            'filename': filename or f'{segment}-batch.csv'}


def pair_index(pairs):
    return {p['visitor_code']: p for p in pairs}


def _records_from_state(state):
    """把 reduce_segments 的暂存状态按最高签名还原成等价代表记录，用于无损重建配对。"""
    out = []
    for (code, segment), st in state.items():
        signatures = st.get('top_signatures') or [(st['outcome'], st['reading_order'])]
        filename = (st.get('sources') or [None])[0]
        for outcome, order in signatures:
            out.append({'visitor_code': code, 'segment': segment, 'correction_no': st['correction_no'],
                        'reading_order': order, 'outcome': outcome, 'batch_id': st.get('batch_id'),
                        'filename': filename})
    return out


# 配对级语义字段：溯源用的嵌套 segments/sources 不参与等价性判断。
PAIR_KEYS = ('visitor_code', 'reading_order', 'old_ok', 'new_ok', 'status', 'excluded',
             'exclude_reason', 'old_correction_no', 'new_correction_no', 'old_conflict', 'new_conflict')


def semantic(pairs):
    return [{key: pair[key] for key in PAIR_KEYS} for pair in pairs]


class MergeTwoSheets(unittest.TestCase):
    def test_segments_arrive_on_different_days_then_pair(self):
        # 第一天只有旧展签表，第二天易读样张飞表才到。
        day_one = [rec('PD-01', 'old', 0, 'old_first', True),
                   rec('PD-02', 'old', 0, 'new_first', False)]
        pairs, visitors = build_pairs(day_one)
        self.assertEqual(visitors, ['PD-01', 'PD-02'])
        for pair in pairs:
            self.assertEqual(pair['status'], 'pending')
            self.assertTrue(pair['excluded'])
            self.assertEqual(pair['exclude_reason'], '另一段测读尚未导齐')

        day_two = day_one + [rec('PD-01', 'new', 0, 'old_first', True),
                             rec('PD-02', 'new', 0, 'new_first', True)]
        pairs, _ = build_pairs(day_two)
        idx = pair_index(pairs)
        self.assertEqual(idx['PD-01']['status'], 'complete')
        self.assertFalse(idx['PD-01']['excluded'])
        self.assertEqual((idx['PD-01']['old_ok'], idx['PD-01']['new_ok']), (True, True))
        # PD-02：旧版未成功、样张成功 -> 样张独胜，且完整入组。
        self.assertEqual(idx['PD-02']['status'], 'complete')
        self.assertEqual((idx['PD-02']['old_ok'], idx['PD-02']['new_ok']), (False, True))


class LatestCorrection(unittest.TestCase):
    def test_highest_correction_wins(self):
        rows = [rec('PD-07', 'old', 0, 'old_first', True),
                rec('PD-07', 'old', 1, 'old_first', False),
                rec('PD-07', 'old', 2, 'old_first', True),
                rec('PD-07', 'new', 0, 'old_first', False)]
        state = reduce_segments(rows)
        self.assertEqual(state[('PD-07', 'old')]['outcome'], True)
        self.assertEqual(state[('PD-07', 'old')]['correction_no'], 2)
        self.assertFalse(state[('PD-07', 'old')]['conflict'])

    def test_same_correction_identical_is_deduped(self):
        rows = [rec('PD-08', 'new', 3, 'new_first', True),
                rec('PD-08', 'new', 3, 'new_first', True),
                rec('PD-08', 'old', 0, 'new_first', False)]
        pairs, _ = build_pairs(rows)
        pair = pair_index(pairs)['PD-08']
        self.assertFalse(pair['new_conflict'])
        self.assertEqual(pair['status'], 'complete')

    def test_tie_highest_correction_different_results_conflicts_and_excludes_pair(self):
        rows = [rec('PD-09', 'old', 0, 'old_first', True),
                rec('PD-09', 'new', 2, 'old_first', True),
                rec('PD-09', 'new', 2, 'old_first', False)]  # 同最高更正号、不同结果
        pairs, _ = build_pairs(rows)
        pair = pair_index(pairs)['PD-09']
        self.assertTrue(pair['new_conflict'])
        self.assertEqual(pair['status'], 'conflict')
        self.assertTrue(pair['excluded'])
        self.assertEqual(pair['exclude_reason'], '最高更正号存在不同结果，整对暂不纳入统计')
        # 冲突段结果不可信，落 None，绝不二选一参与统计。
        self.assertIsNone(pair['new_ok'])

    def test_conflict_resolved_by_later_correction(self):
        # 先有冲突，再补一个更高更正号的唯一结果，冲突解除并重新入组。
        rows = [rec('PD-10', 'old', 0, 'new_first', False),
                rec('PD-10', 'new', 1, 'new_first', True),
                rec('PD-10', 'new', 1, 'new_first', False),
                rec('PD-10', 'new', 2, 'new_first', True)]
        pair = pair_index(build_pairs(rows)[0])['PD-10']
        self.assertFalse(pair['new_conflict'])
        self.assertEqual(pair['status'], 'complete')
        self.assertEqual(pair['new_ok'], True)
        self.assertEqual(pair['new_correction_no'], 2)


class ConflictRoundTripIncremental(unittest.TestCase):
    def test_conflict_survives_incremental_merge_then_resolves(self):
        # 第一条新段记录入暂存；第二条同更正号不同结果制造冲突。
        state = merge_batch({}, [rec('PD-30', 'new', 1, 'old_first', True, batch_id=1)])
        state = merge_batch(state, [rec('PD-30', 'new', 1, 'old_first', False, batch_id=2)])
        self.assertTrue(state[('PD-30', 'new')]['conflict'])
        # 旧段同时到达，整对因新段冲突而排除。
        state = merge_batch(state, [rec('PD-30', 'old', 0, 'old_first', True, batch_id=3)])
        pairs, _ = build_pairs(_records_from_state(state))
        pair = pair_index(pairs)['PD-30']
        self.assertEqual(pair['status'], 'conflict')
        self.assertTrue(pair['excluded'])
        # 更高更正号的唯一结果到达，冲突解除，整对重新纳入统计。
        state = merge_batch(state, [rec('PD-30', 'new', 2, 'old_first', False, batch_id=4)])
        self.assertFalse(state[('PD-30', 'new')]['conflict'])
        pairs, _ = build_pairs(_records_from_state(state))
        pair = pair_index(pairs)['PD-30']
        self.assertEqual(pair['status'], 'complete')
        self.assertFalse(pair['excluded'])
        self.assertEqual(pair['new_ok'], False)
        self.assertEqual(pair['new_correction_no'], 2)

    def test_incremental_equals_full_even_with_conflict(self):
        full = [rec('PD-31', 'old', 0, 'new_first', True),
                rec('PD-31', 'new', 2, 'new_first', True),
                rec('PD-31', 'new', 2, 'new_first', False),
                rec('PD-32', 'old', 0, 'old_first', None),
                rec('PD-32', 'new', 0, 'old_first', True)]
        state = {}
        for row in full:
            state = merge_batch(state, [row])
        self.assertEqual(semantic(build_pairs(_records_from_state(state))[0]),
                         semantic(build_pairs(full)[0]))


class OrderIndependenceAndIdempotency(unittest.TestCase):
    def _dataset(self):
        return [
            rec('PD-01', 'old', 0, 'old_first', True), rec('PD-01', 'new', 0, 'old_first', True),
            rec('PD-02', 'old', 0, 'new_first', False), rec('PD-02', 'new', 0, 'new_first', True),
            rec('PD-03', 'old', 1, 'old_first', True), rec('PD-03', 'old', 0, 'old_first', False),
            rec('PD-03', 'new', 0, 'old_first', False),
            rec('PD-04', 'new', 2, 'new_first', False), rec('PD-04', 'old', 0, 'new_first', True),
            rec('PD-05', 'old', 0, 'old_first', None), rec('PD-05', 'new', 0, 'old_first', True),
        ]

    def test_shuffled_records_produce_identical_pairs(self):
        base = self._dataset()
        canonical = build_pairs(base)[0]
        rng = random.Random(5203)
        for _ in range(20):
            shuffled = base[:]
            rng.shuffle(shuffled)
            self.assertEqual(build_pairs(shuffled)[0], canonical)

    def test_duplicate_feed_does_not_double_count(self):
        base = self._dataset()
        once, visitors_once = build_pairs(base)
        twice, visitors_twice = build_pairs(base + base)  # 整批重跑
        self.assertEqual(visitors_twice, visitors_once)
        self.assertEqual(len(twice), len(once))
        self.assertEqual(twice, once)
        included = [p for p in twice if not p['excluded']]
        # PD-01..PD-04 完整入组，PD-05 缺测排除。
        self.assertEqual(len(included), 4)

    def test_partial_upload_then_completion_matches_full(self):
        base = self._dataset()
        # 切成三段，模拟部分补传，用增量接口合并。
        chunks = [base[0:4], base[4:8], base[8:]]
        state = {}
        for chunk in chunks:
            state = merge_batch(state, chunk)
        # 增量归约状态应等于一次性全量归约，与切分方式无关。
        self.assertEqual(state, reduce_segments(base))
        # 用增量状态重建配对，结果与全量配对一致（溯源元数据除外）。
        full_pairs, full_visitors = build_pairs(base)
        rebuilt_pairs, rebuilt_visitors = build_pairs(_records_from_state(state))
        self.assertEqual(rebuilt_visitors, full_visitors)
        self.assertEqual(semantic(rebuilt_pairs), semantic(full_pairs))

    def test_merge_batch_equivalent_to_full_reduce(self):
        base = self._dataset()
        state = {}
        for row in base:
            state = merge_batch(state, [row])  # 逐条增量
        self.assertEqual(state, reduce_segments(base))
        pairs, visitors = build_pairs(base)
        # 由增量状态重建出的配对与全量一致。
        rebuilt_pairs, _ = build_pairs(_records_from_state(state))
        for rebuilt in rebuilt_pairs:
            pair = pair_index(pairs)[rebuilt['visitor_code']]
            self.assertEqual(pair['old_ok'], rebuilt['old_ok'])
            self.assertEqual(pair['new_ok'], rebuilt['new_ok'])
            self.assertEqual(pair['old_conflict'], rebuilt['old_conflict'])
            self.assertEqual(pair['new_conflict'], rebuilt['new_conflict'])
            self.assertEqual(pair['status'], rebuilt['status'])


class MissingIsPreserved(unittest.TestCase):
    def test_none_outcome_is_missing_not_failure(self):
        rows = [rec('PD-11', 'old', 0, 'old_first', None),
                rec('PD-11', 'new', 0, 'old_first', True)]
        pair = pair_index(build_pairs(rows)[0])['PD-11']
        self.assertEqual(pair['status'], 'incomplete')
        self.assertTrue(pair['excluded'])
        self.assertIsNone(pair['old_ok'])  # 缺测保留 None，而非 False

    def test_both_sides_missing_still_excluded(self):
        rows = [rec('PD-12', 'old', 0, 'new_first', None),
                rec('PD-12', 'new', 0, 'new_first', None)]
        pair = pair_index(build_pairs(rows)[0])['PD-12']
        self.assertEqual(pair['status'], 'incomplete')
        self.assertTrue(pair['excluded'])


class SheetValidation(unittest.TestCase):
    def test_table_internal_duplicate_conflict_is_captured_not_raised(self):
        rows = [{'visitor_code': 'PD-20', 'correction_no': 1, 'reading_order': 'old_first', 'outcome': True},
                {'visitor_code': 'PD-20', 'correction_no': 1, 'reading_order': 'old_first', 'outcome': False}]
        # 解析不拒绝，保留两条原始记录交由对账判冲突。
        parsed = parse_sheet_rows('old', rows)
        self.assertEqual(len(parsed), 2)
        pair = pair_index(build_pairs(parsed + [rec('PD-20', 'new', 0, 'old_first', True)])[0])['PD-20']
        self.assertTrue(pair['old_conflict'])
        self.assertEqual(pair['status'], 'conflict')
        self.assertTrue(pair['excluded'])

    def test_bad_segment_and_outcome(self):
        with self.assertRaises(ReconcileError):
            parse_sheet_rows('middle', [])
        with self.assertRaises(ReconcileError):
            build_pairs([{'visitor_code': 'PD-21', 'segment': 'old', 'correction_no': 0,
                          'reading_order': 'old_first', 'outcome': 'maybe'}])

    def test_negative_correction_rejected(self):
        with self.assertRaises(ReconcileError):
            build_pairs([rec('PD-22', 'old', -1, 'old_first', True)])


class StatisticsExclusionAndManualLegacy(unittest.TestCase):
    def _pairs(self):
        # 导入配对（带 excluded 标记）+ 手录历史配对（无 excluded 键）混合。
        return [
            {'visitor_code': 'M-01', 'reading_order': 'old_first', 'old_ok': True, 'new_ok': True},  # 手录
            {'visitor_code': 'M-02', 'reading_order': 'new_first', 'old_ok': True, 'new_ok': False},  # 手录，旧版独胜
            {'visitor_code': 'I-01', 'reading_order': 'old_first', 'old_ok': False, 'new_ok': True,
             'excluded': False, 'exclude_reason': None},  # 导入完整，样张独胜
            {'visitor_code': 'I-02', 'reading_order': 'new_first', 'old_ok': None, 'new_ok': True,
             'excluded': True, 'exclude_reason': '存在缺测一侧，保留缺测但不入组'},  # 缺测排除
            {'visitor_code': 'I-03', 'reading_order': 'old_first', 'old_ok': None, 'new_ok': None,
             'excluded': True, 'exclude_reason': '最高更正号存在不同结果，整对暂不纳入统计'},  # 冲突排除
        ]

    def test_summary_counts_only_included(self):
        summary = summarize(self._pairs())
        self.assertEqual(summary['registered'], 5)
        self.assertEqual(summary['excluded'], 2)
        self.assertEqual(summary['total'], 3)       # 纳入统计的对数
        self.assertEqual(summary['complete'], 3)
        self.assertEqual(summary['both_ok'], 1)
        self.assertEqual(summary['old_only'], 1)
        self.assertEqual(summary['new_only'], 1)
        self.assertEqual(summary['neither'], 0)

    def test_analyze_excludes_conflicts_and_reports_them(self):
        result = analyze(self._pairs(), 95)
        self.assertEqual(result['complete'], 3)
        self.assertEqual(result['discordant'], 2)   # 旧版独胜 1 + 样张独胜 1
        self.assertEqual(len(result['excluded_pairs']), 2)
        reasons = {row['visitor_code']: row['reason'] for row in result['excluded_pairs']}
        self.assertIn('I-02', reasons)
        self.assertIn('I-03', reasons)
        # 读序诊断同样只看纳入的配对。
        self.assertEqual(result['reading_orders']['old_first']['total'], 2)  # M-01, I-01
        self.assertEqual(result['reading_orders']['new_first']['total'], 1)  # M-02

    def test_manual_legacy_pairs_without_flag_are_included(self):
        manual_only = [
            {'visitor_code': 'M-01', 'reading_order': 'old_first', 'old_ok': True, 'new_ok': True},
            {'visitor_code': 'M-02', 'reading_order': 'old_first', 'old_ok': False, 'new_ok': False},
        ]
        result = analyze(manual_only, 95)
        self.assertEqual(result['complete'], 2)
        self.assertEqual(result['excluded'], 0)
        self.assertIsNotNone(result['p_value'])

    def test_empty_and_all_excluded(self):
        empty = analyze([], 95)
        self.assertEqual(empty['complete'], 0)
        self.assertIsNone(empty['p_value'])
        self.assertIsNone(empty['delta_pp'])
        all_out = analyze([{'visitor_code': 'X-1', 'reading_order': 'old_first', 'old_ok': None,
                            'new_ok': None, 'excluded': True, 'exclude_reason': 'x'}], 95)
        self.assertEqual(all_out['complete'], 0)
        self.assertIsNone(all_out['p_value'])


class ReRunSameResult(unittest.TestCase):
    def test_rerun_after_more_data_keeps_deterministic_totals(self):
        # 模拟：先部分导入跑一次，补齐后再跑；统计只取决于当前完整配对集合。
        first = [rec('PD-01', 'old', 0, 'old_first', True), rec('PD-01', 'new', 0, 'old_first', True),
                 rec('PD-02', 'old', 0, 'new_first', True)]  # PD-02 还缺样张段
        first_pairs, _ = build_pairs(first)
        self.assertEqual(summarize(first_pairs)['complete'], 1)

        second = first + [rec('PD-02', 'new', 0, 'new_first', False),
                          rec('PD-03', 'old', 0, 'old_first', False),
                          rec('PD-03', 'new', 0, 'old_first', True)]
        second_pairs, _ = build_pairs(second)
        summary = summarize(second_pairs)
        self.assertEqual(summary['complete'], 3)
        self.assertEqual(summary['old_only'], 1)   # PD-02
        self.assertEqual(summary['new_only'], 1)   # PD-03
        self.assertEqual(summary['both_ok'], 1)    # PD-01


if __name__ == '__main__':
    unittest.main()
