"""HTTP 层集成验收：用 TestClient + 假库验证路由、鉴权、校验与 multipart 上传。"""
import unittest

import tests.bootstrap_env  # noqa: F401  必须在 src 应用模块导入前设置占位环境
from fastapi.testclient import TestClient

from src.app import app
from src.controllers.auth import current_user
from src.controllers import studies as studies_controller
from src.routes import api as api_module
from src.models import database as db_module
from src import worker as worker_module

from tests.fake_db import FakeDB

ANALYST = {'username': 'analyst', 'display_name': '沈砚', 'role': 'analyst'}


class _StubCache:
    def exists(self, key):
        return True

    def ping(self):
        return True


class HttpImportFlow(unittest.TestCase):
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
        self._orig_connect = api_module.connect
        self._orig_worker_connect = worker_module.connect
        self._orig_caches = (db_module.cache, worker_module.cache, studies_controller.cache)
        api_module.connect = lambda: self.db
        worker_module.connect = lambda: self.db
        db_module.cache = worker_module.cache = studies_controller.cache = _StubCache()
        app.dependency_overrides[current_user] = lambda: ANALYST
        self.client = TestClient(app)

    def tearDown(self):
        api_module.connect = self._orig_connect
        worker_module.connect = self._orig_worker_connect
        db_module.cache, worker_module.cache, studies_controller.cache = self._orig_caches
        app.dependency_overrides.clear()

    def test_json_import_then_study_detail_and_run(self):
        payload = {'source': 'old', 'filename': 'old-a.csv', 'rows': [
            {'visitor_code': 'PD-01', 'correction_no': 0, 'reading_order': 'old_first', 'outcome': True},
            {'visitor_code': 'PD-02', 'correction_no': 1, 'reading_order': 'new_first', 'outcome': None},
        ]}
        res = self.client.post('/api/studies/1/imports', json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertFalse(body['idempotent'])
        self.assertEqual(body['row_count'], 2)

        # 幂等：同名文件重传不新增。
        again = self.client.post('/api/studies/1/imports', json=payload)
        self.assertEqual(again.status_code, 201)
        self.assertTrue(again.json()['idempotent'])
        self.assertEqual(len(self.db.tables['import_batches']), 1)

        # 研究详情含导入批次与摘要。
        detail = self.client.get('/api/studies/1').json()
        self.assertEqual(detail['imports']['batches'][0]['filename'], 'old-a.csv')
        self.assertEqual(detail['summary']['registered'], 2)
        self.assertEqual(detail['summary']['excluded'], 2)  # 都还缺样张段

    def test_csv_multipart_upload_and_conflict_exclusion(self):
        old_csv = ('visitor_code,correction_no,reading_order,outcome\n'
                   'PD-09,0,old_first,true\n')
        r1 = self.client.post('/api/studies/1/imports/file',
                              data={'source': 'old'},
                              files={'file': ('old.csv', old_csv, 'text/csv')})
        self.assertEqual(r1.status_code, 201, r1.text)
        new_csv = ('visitor_code,correction_no,reading_order,outcome\n'
                   'PD-09,2,old_first,true\n'
                   'PD-09,2,old_first,false\n')
        r2 = self.client.post('/api/studies/1/imports/file',
                              data={'source': 'new'},
                              files={'file': ('new.csv', new_csv, 'text/csv')})
        self.assertEqual(r2.status_code, 201, r2.text)
        pair = next(p for p in self.db.tables['reading_pairs'] if p['visitor_code'] == 'PD-09')
        self.assertTrue(pair['excluded'])
        # 冲突未解除前无可纳入配对，提交运行应 422。
        run = self.client.post('/api/studies/1/runs', json={'confidence': 95})
        self.assertEqual(run.status_code, 422)

    def test_validation_errors(self):
        bad_outcome = self.client.post('/api/studies/1/imports', json={
            'source': 'old', 'filename': 'b.csv',
            'rows': [{'visitor_code': 'PD-01', 'correction_no': 0,
                      'reading_order': 'old_first', 'outcome': 'maybe'}]})
        self.assertEqual(bad_outcome.status_code, 422)
        bad_source = self.client.post('/api/studies/1/imports', json={
            'source': 'middle', 'filename': 'c.csv',
            'rows': [{'visitor_code': 'PD-01', 'correction_no': 0,
                      'reading_order': 'old_first', 'outcome': True}]})
        self.assertEqual(bad_source.status_code, 422)
        empty_rows = self.client.post('/api/studies/1/imports', json={
            'source': 'old', 'filename': 'd.csv', 'rows': []})
        self.assertEqual(empty_rows.status_code, 422)

    def test_full_pair_then_run_and_export_endpoints(self):
        self.client.post('/api/studies/1/imports', json={'source': 'old', 'filename': 'o.csv', 'rows': [
            {'visitor_code': 'PD-01', 'correction_no': 0, 'reading_order': 'old_first', 'outcome': True}]})
        self.client.post('/api/studies/1/imports', json={'source': 'new', 'filename': 'n.csv', 'rows': [
            {'visitor_code': 'PD-01', 'correction_no': 0, 'reading_order': 'old_first', 'outcome': False}]})
        run = self.client.post('/api/studies/1/runs', json={'confidence': 95})
        self.assertEqual(run.status_code, 202, run.text)
        run_id = run.json()['id']
        # 队列中的运行尚不能导出（worker 未消费），返回 409。
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/export.json').status_code, 409)
        # 模拟 worker 完成。
        from src.worker import execute_one
        self.assertTrue(execute_one())
        export = self.client.get(f'/api/runs/{run_id}/export.json')
        self.assertEqual(export.status_code, 200)
        self.assertIn('"checksum"', export.text)
        csv_res = self.client.get(f'/api/runs/{run_id}/pairs.csv')
        self.assertEqual(csv_res.status_code, 200)
        self.assertIn('PD-01', csv_res.text)


if __name__ == '__main__':
    unittest.main()
