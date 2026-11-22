"""导入批次与对账持久化。

一次请求在单事务内完成：幂等登记批次 -> 落原始记录 -> 重算该研究全部暂存状态
-> 写回 reading_pairs（含 excluded 标记）-> 推进样本修订并写审计。
中断（事务回滚）不留半成品；重跑同一文件名命中幂等直接回上次结果，不重复计人。
"""
from fastapi import HTTPException

from src.constants.enums import PAIR_LIMIT
from src.services.audit import record
from src.services.reconcile import SEGMENTS, build_pairs, parse_sheet_rows
from src.services.records import get_study

SHEET_KIND = {'old': 'old_label', 'new': 'easy_proof'}
PAIR_FIELDS = 'visitor_code, reading_order, old_ok, new_ok, source, excluded, exclude_reason'


def list_batches(db, study_id):
    return db.execute('''SELECT b.id, b.source, b.sheet_kind, b.filename, b.row_count, b.status,
                                b.imported_by, b.created_at,
                                count(r.*) FILTER (WHERE r.active) AS active_rows
                         FROM import_batches b
                         LEFT JOIN import_records r ON r.batch_id = b.id
                         WHERE b.study_id = %s
                         GROUP BY b.id ORDER BY b.id''', (study_id,)).fetchall()


def list_segments(db, study_id):
    return db.execute('''SELECT visitor_code, segment, reading_order, outcome, correction_no,
                                conflict, batch_id, updated_at
                         FROM pair_segments WHERE study_id = %s ORDER BY visitor_code, segment''',
                      (study_id,)).fetchall()


def _rebuild_pairs(db, study_id):
    """用全部 active 原始记录重算配对，结果与到达顺序、批次切分无关。"""
    raw = db.execute('''SELECT visitor_code, segment, correction_no, reading_order, outcome, batch_id
                        FROM import_records WHERE study_id = %s AND active
                        ORDER BY id''', (study_id,)).fetchall()
    pairs, visitors = build_pairs([dict(row) for row in raw])

    # 暂存段全量重写，保证乱序补齐后状态一致。
    db.execute('DELETE FROM pair_segments WHERE study_id = %s', (study_id,))
    for pair in pairs:
        for segment in SEGMENTS:
            st = pair['segments'].get(segment)
            if not st:
                continue
            db.execute('''INSERT INTO pair_segments(study_id, visitor_code, segment, reading_order, outcome,
                                                     correction_no, batch_id, conflict)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (study_id, pair['visitor_code'], segment, st['reading_order'], st['outcome'],
                        st['correction_no'], st['batch_id'], st['conflict']))

    # 仅对账导入来源的配对；手录行不在导入记录内，保持原样照常复验。
    imported_codes = set(visitors)
    if imported_codes:
        db.execute('DELETE FROM reading_pairs WHERE study_id = %s AND visitor_code = ANY(%s) AND source = %s',
                   (study_id, list(imported_codes), 'import'))
    existing_manual = {
        row['visitor_code'] for row in db.execute(
            'SELECT visitor_code FROM reading_pairs WHERE study_id = %s AND source = %s',
            (study_id, 'manual')).fetchall()
    }
    for pair in pairs:
        if pair['visitor_code'] in existing_manual:
            # 手录与导入撞号时保留手录并提示，不静默覆盖会签依据。
            continue
        db.execute('''INSERT INTO reading_pairs(study_id, visitor_code, reading_order, old_ok, new_ok,
                                                source, excluded, exclude_reason, updated_at)
                      VALUES (%(study_id)s,%(visitor_code)s,%(reading_order)s,%(old_ok)s,%(new_ok)s,
                              'import',%(excluded)s,%(exclude_reason)s,now())''',
                   {'study_id': study_id, 'visitor_code': pair['visitor_code'],
                    'reading_order': pair['reading_order'], 'old_ok': pair['old_ok'],
                    'new_ok': pair['new_ok'], 'excluded': pair['excluded'],
                    'exclude_reason': pair['exclude_reason']})

    total = db.execute('SELECT count(*) AS n FROM reading_pairs WHERE study_id = %s',
                       (study_id,)).fetchone()['n']
    if total > PAIR_LIMIT:
        raise HTTPException(409, f'每批最多登记 {PAIR_LIMIT} 对，导入后为 {total} 对')
    return pairs


def import_sheet(db, study_id, source, filename, rows, actor):
    """幂等导入一份交付表。重传同名文件返回上次批次，不重复计人。"""
    if source not in SEGMENTS:
        raise HTTPException(422, '交付表来源必须是 old 或 new')
    study = get_study(db, study_id, lock=True)

    previous = db.execute('SELECT id, row_count FROM import_batches WHERE study_id = %s AND filename = %s',
                          (study_id, filename)).fetchone()
    if previous:
        return {'idempotent': True, 'batch_id': previous['id'], 'row_count': previous['row_count'],
                'pairs': _pair_view(db, study_id)}

    records = parse_sheet_rows(source, rows)
    if not records:
        raise HTTPException(422, '交付表没有可导入的记录行')

    batch = db.execute('''INSERT INTO import_batches(study_id, source, filename, sheet_kind, row_count, imported_by)
                          VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
                       (study_id, source, filename, SHEET_KIND[source], len(records), actor)).fetchone()
    db.executemany('''INSERT INTO import_records(batch_id, study_id, visitor_code, segment, correction_no,
                                                 reading_order, outcome)
                      VALUES (%(batch_id)s,%(study_id)s,%(visitor_code)s,%(segment)s,%(correction_no)s,
                              %(reading_order)s,%(outcome)s)''',
                   [{'batch_id': batch['id'], 'study_id': study_id, **row} for row in records])

    before_revision = study['revision']
    pairs = _rebuild_pairs(db, study_id)
    db.execute("UPDATE import_batches SET status = 'reconciled' WHERE id = %s", (batch['id'],))
    revision = db.execute('UPDATE studies SET revision = revision + 1 WHERE id = %s RETURNING revision',
                          (study_id,)).fetchone()['revision']
    record(db, actor, 'import.sheet', f'{study["code"]}/{filename}',
           {'batch_id': batch['id'], 'source': source, 'rows': len(records),
            'revision_before': before_revision, 'revision_after': revision,
            'excluded': sum(1 for p in pairs if p['excluded'])})
    return {'idempotent': False, 'batch_id': batch['id'], 'row_count': len(records),
            'revision': revision, 'pairs': _pair_view(db, study_id)}


def _pair_view(db, study_id):
    return db.execute(f'''SELECT {PAIR_FIELDS} FROM reading_pairs WHERE study_id = %s
                          ORDER BY visitor_code''', (study_id,)).fetchall()
