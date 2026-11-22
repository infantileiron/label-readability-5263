"""内存假数据库：实现服务层实际用到的 psycopg 子集，供服务层端到端验收。

不追求完整 SQL 引擎，只按语句前缀与参数复现关系语义，覆盖：
导入幂等、对账重算、excluded 标记、修订自增、手录共存、运行快照固定与复用。
"""
from copy import deepcopy

try:  # 测试环境若装有 psycopg，解包 Jsonb；否则定义同名占位。
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover
    class Jsonb:  # type: ignore
        def __init__(self, value):
            self.obj = value


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None

    def execute(self, sql, params=None):
        self._result = self.db.run(sql, params or ())
        return self

    def executemany(self, sql, seq):
        for params in seq:
            self.db.run(sql, params)
        self._result = None
        return self

    def fetchone(self):
        if not self._result:
            return None
        return deepcopy(self._result[0])

    def fetchall(self):
        return deepcopy(self._result or [])


class FakeDB:
    def __init__(self):
        self.tables = {name: [] for name in (
            'accounts', 'label_versions', 'studies', 'reading_pairs', 'analysis_runs',
            'signoffs', 'audit_events', 'import_batches', 'import_records', 'pair_segments')}
        self._seq = {}
        self.conn = self

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        return FakeCursor(self).execute(sql, params)

    def executemany(self, sql, seq):
        return FakeCursor(self).executemany(sql, seq)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _next_id(self, table):
        self._seq[table] = self._seq.get(table, 0) + 1
        return self._seq[table]

    def _run_view(self, run):
        """把 analysis_runs 行扩展成 RUN_COLUMNS + result/current_revision 视图。"""
        if run is None:
            return None
        signoff = next((s for s in self.tables['signoffs'] if s['run_id'] == run['id']), None)
        study = next((t for t in self.tables['studies'] if t['id'] == run['study_id']), None)
        view = {key: run.get(key) for key in (
            'id', 'study_id', 'revision', 'confidence', 'method', 'checksum', 'status', 'error',
            'requested_by', 'created_at', 'finished_at', 'result')}
        view.update({
            'signed_at': signoff and signoff['signed_at'], 'signed_by': signoff and signoff['signed_by'],
            'decision': signoff and signoff['decision'], 'note': signoff and signoff['note'],
            'current_revision': study and study['revision'],
        })
        return view

    # -- 辅助：条件匹配 -------------------------------------------------
    @staticmethod
    def _matches(row, criteria):
        return all(row.get(k) == v for k, v in criteria.items())

    def _where(self, table, criteria):
        return [r for r in self.tables[table] if self._matches(r, criteria)]

    def run(self, sql, params):
        s = ' '.join(sql.split())
        if isinstance(params, dict):
            params = {k: (v.obj if isinstance(v, Jsonb) else v) for k, v in params.items()}
        else:
            params = tuple((v.obj if isinstance(v, Jsonb) else v) for v in params)
        p = params

        if s.startswith('SELECT pg_advisory_xact_lock') or s.startswith('SELECT 1 FROM schema_migrations'):
            return [{'?': 1}]

        # 读研究（含锁）
        if s.startswith('SELECT * FROM studies WHERE id ='):
            rows = self._where('studies', {'id': p[0]})
            return rows
        if s.startswith('SELECT id FROM studies WHERE id='):
            return [{'id': p[0]}] if self._where('studies', {'id': p[0]}) else []

        # reading_pairs 列查询
        if 'FROM reading_pairs WHERE study_id = %s AND NOT excluded' in s:
            return [r for r in self._where('reading_pairs', {'study_id': p[0]}) if not r['excluded']]
        if 'FROM reading_pairs WHERE study_id = %s ORDER BY visitor_code' in s:
            return self._where('reading_pairs', {'study_id': p[0]})
        if 'FROM reading_pairs WHERE study_id=%s AND visitor_code=%s' in s:
            return self._where('reading_pairs', {'study_id': p[0], 'visitor_code': p[1]})
        if s.startswith('SELECT count(*) AS n FROM reading_pairs WHERE study_id = %s'):
            return [{'n': len(self._where('reading_pairs', {'study_id': p[0]}))}]
        if 'FROM reading_pairs WHERE study_id = %s AND source = %s' in s:
            return [{'visitor_code': r['visitor_code']}
                    for r in self._where('reading_pairs', {'study_id': p[0], 'source': p[1]})]
        if 'DELETE FROM reading_pairs WHERE study_id = %s AND visitor_code = ANY(%s) AND source = %s' in s:
            study_id, codes, source = p
            self.tables['reading_pairs'] = [
                r for r in self.tables['reading_pairs']
                if not (r['study_id'] == study_id and r['visitor_code'] in set(codes) and r['source'] == source)]
            return []

        # reading_pairs 写
        if s.startswith('INSERT INTO reading_pairs'):
            if isinstance(p, dict):
                row = {'study_id': p['study_id'], 'visitor_code': p['visitor_code'],
                       'reading_order': p['reading_order'], 'old_ok': p['old_ok'], 'new_ok': p['new_ok'],
                       'source': 'import' if "'import'" in s else p.get('source', 'manual'),
                       'excluded': p.get('excluded', False),
                       'exclude_reason': p.get('exclude_reason')}
                existing = next((r for r in self.tables['reading_pairs']
                                 if r['study_id'] == row['study_id'] and r['visitor_code'] == row['visitor_code']), None)
                if 'ON CONFLICT' in s and existing:
                    existing.update(row)
                elif existing is None:
                    self.tables['reading_pairs'].append(row)
            return []

        # studies 修订
        if s.startswith('UPDATE studies SET revision=revision+1') or s.startswith('UPDATE studies SET revision = revision + 1'):
            study = self._where('studies', {'id': p[0]})[0]
            study['revision'] += 1
            return [{'revision': study['revision']}]

        # label_versions
        if 'FROM label_versions WHERE id IN' in s:
            ids = {p[0], p[1]}
            return [r for r in self.tables['label_versions'] if r['id'] in ids]

        # import_batches
        if s.startswith('SELECT id, row_count FROM import_batches WHERE study_id = %s AND filename = %s'):
            return [{'id': r['id'], 'row_count': r['row_count']}
                    for r in self.tables['import_batches']
                    if r['study_id'] == p[0] and r['filename'] == p[1]]
        if s.startswith('INSERT INTO import_batches'):
            row = {'id': self._next_id('import_batches'), 'study_id': p[0], 'source': p[1],
                   'filename': p[2], 'sheet_kind': p[3], 'row_count': p[4], 'status': 'received',
                   'imported_by': p[5]}
            self.tables['import_batches'].append(row)
            return [{'id': row['id']}]
        if s.startswith("UPDATE import_batches SET status = 'reconciled'"):
            self._where('import_batches', {'id': p[0]})[0]['status'] = 'reconciled'
            return []
        if 'FROM import_batches b LEFT JOIN import_records r' in s:
            out = []
            for b in self._where('import_batches', {'study_id': p[0]}):
                active = sum(1 for r in self.tables['import_records']
                             if r['batch_id'] == b['id'] and r['active'])
                out.append({**b, 'active_rows': active})
            return out
        if 'FROM import_batches b WHERE b.study_id = %s ORDER BY b.id' in s:
            return self._where('import_batches', {'study_id': p[0]})

        # import_records
        if 'FROM import_records WHERE study_id = %s AND active' in s:
            return [r for r in self._where('import_records', {'study_id': p[0]}) if r['active']]
        if s.startswith('INSERT INTO import_records'):
            if isinstance(p, dict):
                row = {'id': self._next_id('import_records'), **{k: p[k] for k in (
                    'batch_id', 'study_id', 'visitor_code', 'segment', 'correction_no',
                    'reading_order', 'outcome')}, 'active': True}
                self.tables['import_records'].append(row)
            return []
        # pair_segments
        if s.startswith('DELETE FROM pair_segments WHERE study_id = %s'):
            self.tables['pair_segments'] = [r for r in self.tables['pair_segments']
                                            if r['study_id'] != p[0]]
            return []
        if s.startswith('INSERT INTO pair_segments'):
            self.tables['pair_segments'].append({
                'study_id': p[0], 'visitor_code': p[1], 'segment': p[2], 'reading_order': p[3],
                'outcome': p[4], 'correction_no': p[5], 'batch_id': p[6], 'conflict': p[7]})
            return []
        if 'FROM pair_segments WHERE study_id = %s ORDER BY visitor_code, segment' in s:
            return self._where('pair_segments', {'study_id': p[0]})

        # analysis_runs
        if 'FROM analysis_runs r LEFT JOIN signoffs s' in s and 'JOIN studies t' in s:
            # run 详情：附带当前研究修订号。
            run = next((r for r in self.tables['analysis_runs'] if r['id'] == p[0]), None)
            return [self._run_view(run)] if run else []
        if 'FROM analysis_runs r LEFT JOIN signoffs s' in s and 'WHERE r.study_id' in s:
            return [self._run_view(r) for r in self.tables['analysis_runs']
                    if r['study_id'] == p[0]][::-1]
        if "FROM analysis_runs WHERE status='queued'" in s:
            return [r for r in self.tables['analysis_runs'] if r['status'] == 'queued'][:1]
        if 'SELECT id FROM analysis_runs WHERE study_id = %s AND checksum = %s' in s:
            return [{'id': r['id']} for r in self.tables['analysis_runs']
                    if r['study_id'] == p[0] and r['checksum'] == p[1] and r['status'] != 'failed']
        if s.startswith('INSERT INTO analysis_runs'):
            row = {'id': self._next_id('analysis_runs'), **p, 'status': 'queued', 'result': None,
                   'error': None, 'finished_at': None}
            self.tables['analysis_runs'].append(row)
            return [{'id': row['id']}]
        if s.startswith("UPDATE analysis_runs SET status='succeeded'"):
            run = next(r for r in self.tables['analysis_runs'] if r['id'] == p[1])
            run.update(status='succeeded', result=p[0], finished_at='now')
            return []
        if s.startswith("UPDATE analysis_runs SET status='failed'"):
            run = next(r for r in self.tables['analysis_runs'] if r['id'] == p[1])
            run.update(status='failed', error=p[0], finished_at='now')
            return []
        if 'SELECT snapshot FROM analysis_runs WHERE id = %s' in s:
            return [{'snapshot': next(r for r in self.tables['analysis_runs'] if r['id'] == p[0])['snapshot']}]

        # audit
        if s.startswith('INSERT INTO audit_events'):
            self.tables['audit_events'].append({'actor': p[0], 'action': p[1], 'target': p[2]})
            return []

        # 通用 SELECT count
        if s.startswith('SELECT count(*)'):
            return [{'n': 0}]

        # signoffs
        if s.startswith('INSERT INTO signoffs'):
            run_id, decision, note, signed_by = p
            self.tables['signoffs'].append(
                {'run_id': run_id, 'decision': decision, 'note': note, 'signed_by': signed_by,
                 'signed_at': 'now'})
            return []
        if 'FROM signoffs s JOIN analysis_runs r' in s:
            study_id, revision = p
            ids = {r['id'] for r in self.tables['analysis_runs']
                   if r['study_id'] == study_id and r['revision'] == revision}
            return [{'1': 1} for s in self.tables['signoffs']
                    if s['run_id'] in ids and s['decision'] == 'publish'][:1]

        raise AssertionError(f'FakeDB 未实现的 SQL: {s}')
