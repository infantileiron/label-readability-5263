"""纯函数对账引擎：把旧展签 / 易读样张两份交付表按批次与观众合并成对。

设计目标（与持久层和传输方式解耦，可独立复验）：

* 同一观众的两段结果可能隔天才到，分批到达时各自暂存、补齐即配对。
* 同一段取「最高更正号」的结果；最高更正号出现多个不同结果时记冲突，整对暂不纳入统计。
* 记录乱序到达不影响最终结果：只按 (visitor, segment) 归约，与到达顺序无关。
* 缺测（outcome 为 None）是真实观测，必须保留，不能当成失败。
* 幂等：同一批记录重复喂入，归约结果不变，不重复计人。

输入 record 为 dict：
    visitor_code, segment('old'|'new'), correction_no(int>=0),
    reading_order('old_first'|'new_first'), outcome(bool|None),
    batch_id, filename(可选，仅用于回传溯源)
"""
from collections import defaultdict

SEGMENTS = ('old', 'new')
OLD, NEW = SEGMENTS


class ReconcileError(ValueError):
    """对账输入不合法（段名、结果值、更正号等结构性错误）。"""


def _check_record(row):
    code = row.get('visitor_code')
    if not isinstance(code, str) or not code.strip():
        raise ReconcileError('记录缺少观众编号')
    segment = row.get('segment')
    if segment not in SEGMENTS:
        raise ReconcileError(f'{code}: 段名必须是 old 或 new')
    correction = row.get('correction_no')
    if not isinstance(correction, int) or isinstance(correction, bool) or correction < 0:
        raise ReconcileError(f'{code}: 更正号必须是非负整数')
    order = row.get('reading_order')
    if order not in ('old_first', 'new_first'):
        raise ReconcileError(f'{code}: 读序必须是 old_first 或 new_first')
    outcome = row.get('outcome')
    if outcome is not None and not isinstance(outcome, bool):
        raise ReconcileError(f'{code}: 结果必须是 true / false / null(缺测)')
    return code, segment, correction, order, outcome


def reduce_segments(records):
    """把任意顺序的原始记录归约成每个 (观众, 段) 的暂存状态。

    返回 dict[(visitor, segment)] -> 状态 dict：
        reading_order, outcome, correction_no, conflict, batch_id, sources
    冲突时 outcome 为 None、conflict 为 True，但保留最高更正号与读序供排查。
    同一 (段, 更正号) 的重复记录若内容完全一致则去重，否则同样判冲突。
    """
    grouped = defaultdict(list)
    for row in records:
        code, segment, correction, order, outcome = _check_record(row)
        grouped[(code, segment)].append({
            'correction_no': correction, 'reading_order': order, 'outcome': outcome,
            'batch_id': row.get('batch_id'), 'filename': row.get('filename'),
        })

    state = {}
    for key, rows in grouped.items():
        highest = max(r['correction_no'] for r in rows)
        top = [r for r in rows if r['correction_no'] == highest]
        # 同更正号去重：完全相同的结果只算一条；出现不同结果即冲突。
        distinct = {(r['outcome'], r['reading_order']) for r in top}
        winner = top[-1]
        sources = sorted({r['filename'] for r in top if r.get('filename')})
        state[key] = {
            'reading_order': winner['reading_order'],
            'outcome': winner['outcome'] if len(distinct) == 1 else None,
            'correction_no': highest,
            'conflict': len(distinct) > 1,
            'batch_id': winner['batch_id'],
            'sources': sources,
            # 保留最高更正号的全部不同签名，使暂存状态可无损还原（含冲突）。
            'top_signatures': sorted((outcome, order) for outcome, order in distinct),
        }
    return state


def build_pairs(records):
    """归约并按观众合并成配对。乱序、分批、重复喂入均得到同一结果。

    返回 (pairs, visitors)：
      pairs: 每个观众一条，字段对齐 reading_pairs / 快照所需
        visitor_code, reading_order, old_ok, new_ok,
        status('complete'|'incomplete'|'conflict'|'pending'),
        excluded(bool), exclude_reason, old_correction_no, new_correction_no,
        old_conflict, new_conflict, segments(两段暂存溯源)
      visitors: 观众编号有序列表
    仅当两段都到齐、无冲突且两侧都有观测时才是 complete，纳入统计；
    缺测一侧为 incomplete，段冲突为 conflict，只有一段到达为 pending；后三类全部 excluded。
    """
    state = reduce_segments(records)
    visitors = sorted({key[0] for key in state})
    pairs = []
    for code in visitors:
        old = state.get((code, OLD))
        new = state.get((code, NEW))
        segments = {'old': old, 'new': new}
        reading_order = (old or new)['reading_order']
        conflict = bool((old and old['conflict']) or (new and new['conflict']))
        missing_segment = old is None or new is None

        if conflict:
            status, reason = 'conflict', '最高更正号存在不同结果，整对暂不纳入统计'
        elif missing_segment:
            status, reason = 'pending', '另一段测读尚未导齐'
        elif old['outcome'] is None or new['outcome'] is None:
            status, reason = 'incomplete', '存在缺测一侧，保留缺测但不入组'
        else:
            status, reason = 'complete', None

        pairs.append({
            'visitor_code': code,
            'reading_order': reading_order,
            'old_ok': old['outcome'] if old else None,
            'new_ok': new['outcome'] if new else None,
            'status': status,
            'excluded': status != 'complete',
            'exclude_reason': reason,
            'old_correction_no': old['correction_no'] if old else None,
            'new_correction_no': new['correction_no'] if new else None,
            'old_conflict': bool(old and old['conflict']),
            'new_conflict': bool(new and new['conflict']),
            'segments': segments,
        })
    return pairs, visitors


def merge_batch(existing_state, batch_records):
    """增量合并：把一批新记录并入已有暂存状态，支持部分补传与乱序。

    existing_state: 上次 reduce_segments 的结果（持久化后读回的同构 dict 亦可）。
    返回与 reduce_segments 同构的新状态，不修改入参。冲突状态同样可无损往返。
    """
    # 把暂存状态按最高更正号的不同签名还原成等价代表记录，再与新批次一起归约，
    # 从而保证增量与一次性全量计算严格一致（乱序导齐后结果相同）。
    synthetic = []
    for (code, segment), st in (existing_state or {}).items():
        signatures = st.get('top_signatures') or [(st['outcome'], st['reading_order'])]
        for outcome, order in signatures:
            synthetic.append({
                'visitor_code': code, 'segment': segment,
                'correction_no': st['correction_no'], 'reading_order': order,
                'outcome': outcome, 'batch_id': st.get('batch_id'),
                'filename': (st.get('sources') or [None])[0],
            })
    return reduce_segments([*synthetic, *batch_records])


def parse_sheet_rows(source, rows):
    """解析一份交付表的行（dict 列表）为标准 record 列表，并做结构性校验。

    source: 'old' / 'new'，对应旧展签表 / 易读样张飞表。
    每行约定键：visitor_code, correction_no, reading_order, outcome。
    outcome 接受布尔或空（缺测）。同表内同更正号的不同结果不在此拒绝，
    而是作为原始记录保留，由 reduce_segments 判冲突并让整对暂不纳入统计。
    """
    if source not in SEGMENTS:
        raise ReconcileError('交付表来源必须是 old 或 new')
    records = []
    for index, raw in enumerate(rows, 1):
        row = {
            'visitor_code': str(raw['visitor_code']).strip(),
            'segment': source,
            'correction_no': int(raw['correction_no']),
            'reading_order': raw['reading_order'],
            'outcome': raw.get('outcome'),
            'batch_id': raw.get('batch_id'),
            'filename': raw.get('filename'),
        }
        _check_record(row)  # 仅做结构性校验；冲突留给对账阶段。
        records.append(row)
    return records
