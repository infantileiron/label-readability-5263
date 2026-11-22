import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile
from fastapi.encoders import jsonable_encoder

from src.constants.enums import Role
from src.controllers.auth import current_user, login, require_role
from src.controllers.studies import study_detail
from src.models.database import connect
from src.schemas.inputs import (Credentials, ImportSheetInput, PairInput, ReviewInput, RunInput,
                                StudyInput, VersionInput)
from src.services.csv_import import parse_sheet_csv
from src.services.exports import pairing_csv
from src.services.imports import import_sheet, list_batches, list_segments
from src.services.records import add_study, add_version, save_pair
from src.services.runs import detail, enqueue, export_data, review

api = APIRouter(prefix='/api')
analyst = require_role(Role.ANALYST)
curator = require_role(Role.CURATOR)


@api.post('/login')
def auth(data: Credentials):
    return login(data)


@api.get('/me')
def me(user=Depends(current_user)):
    return user


@api.get('/catalog')
def catalog(user=Depends(current_user)):
    with connect() as db:
        return {'versions': db.execute('SELECT * FROM label_versions ORDER BY id').fetchall(),
                'studies': db.execute('SELECT * FROM studies ORDER BY id').fetchall()}


@api.post('/versions', status_code=201)
def create_version(data: VersionInput, user=Depends(analyst)):
    with connect() as db:
        return add_version(db, data.model_dump(), user['username'])


@api.post('/studies', status_code=201)
def create_study(data: StudyInput, user=Depends(analyst)):
    with connect() as db:
        return add_study(db, data.model_dump(), user['username'])


@api.get('/studies/{study_id}')
def study(study_id: int, user=Depends(current_user)):
    with connect() as db:
        return study_detail(db, study_id, user['role'])


@api.put('/studies/{study_id}/pairs/{visitor_code}')
def put_pair(study_id: int, visitor_code: Annotated[str, Path(pattern=r'^[A-Z0-9][A-Z0-9-]{1,29}$')],
             revision: Annotated[int, Query(ge=0)], data: PairInput, user=Depends(analyst)):
    with connect() as db:
        return save_pair(db, study_id, visitor_code, data.model_dump(mode='json'), user['username'], revision)


@api.post('/studies/{study_id}/imports', status_code=201)
def import_json_sheet(study_id: int, data: ImportSheetInput, user=Depends(analyst)):
    """JSON 形式导入一份交付表（旧展签 old / 易读样张 new），供复验与自动化脚本调用。"""
    payload = data.model_dump()
    with connect() as db:
        return import_sheet(db, study_id, payload['source'], payload['filename'],
                            [{**row, 'filename': payload['filename']} for row in payload['rows']],
                            user['username'])


@api.post('/studies/{study_id}/imports/file', status_code=201)
async def import_csv_sheet(study_id: int, source: Annotated[str, Form(pattern=r'^(old|new)$')],
                           file: Annotated[UploadFile, File()], user=Depends(analyst)):
    """上传 CSV 交付表。文件名作为批次幂等键，重传同名文件不重复计人。"""
    content = await file.read()
    rows = parse_sheet_csv(content, source, file.filename)
    with connect() as db:
        return import_sheet(db, study_id, source, file.filename, rows, user['username'])


@api.get('/studies/{study_id}/imports')
def get_batches(study_id: int, user=Depends(current_user)):
    with connect() as db:
        return {'batches': list_batches(db, study_id), 'segments': list_segments(db, study_id)}


@api.post('/studies/{study_id}/runs', status_code=202)
def run(study_id: int, data: RunInput, user=Depends(analyst)):
    with connect() as db:
        return enqueue(db, study_id, data.confidence, user['username'])


@api.get('/runs/{run_id}')
def get_run(run_id: int, user=Depends(current_user)):
    with connect() as db:
        return detail(db, run_id, user['role'])


@api.post('/runs/{run_id}/review')
def signoff(run_id: int, data: ReviewInput, user=Depends(curator)):
    with connect() as db:
        return review(db, run_id, data.decision, data.note, user['username'])


@api.get('/runs/{run_id}/export.json')
def download_json(run_id: int, user=Depends(current_user)):
    with connect() as db:
        data = export_data(db, run_id, user['role'])
    return Response(json.dumps(jsonable_encoder(data), ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="proof-{run_id}.json"'})


@api.get('/runs/{run_id}/pairs.csv')
def download_csv(run_id: int, user=Depends(current_user)):
    with connect() as db:
        data = export_data(db, run_id, user['role'])
    return Response('\ufeff' + pairing_csv(data), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="proof-{run_id}-pairs.csv"'})
