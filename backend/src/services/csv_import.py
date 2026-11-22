import csv
import io

from fastapi import HTTPException

from src.services.reconcile import ReconcileError

TRUE_TOKENS = {'true', '1', 'yes', '成功', '是'}
FALSE_TOKENS = {'false', '0', 'no', '未成功', '否'}
NULL_TOKENS = {'', 'null', 'none', '缺测', 'na', 'n/a'}
REQUIRED_COLUMNS = ('visitor_code', 'correction_no', 'reading_order', 'outcome')


def parse_sheet_csv(content: bytes, source: str, filename: str):
    """把上传的 CSV 字节解析成 import_sheet 所需 rows（dict 列表）。

    缺测 outcome 留空或写「缺测」。表头必须含四个约定列，多余列忽略。
    解析失败统一转 422，避免把脏数据带进对账事务。
    """
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise HTTPException(422, 'CSV 须为 UTF-8 编码') from None
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if not reader.fieldnames or not all(col in reader.fieldnames for col in REQUIRED_COLUMNS):
        raise HTTPException(422, f'CSV 表头须包含 {", ".join(REQUIRED_COLUMNS)}')
    rows = []
    for index, raw in enumerate(reader, 1):
        if not any((value or '').strip() for value in raw.values() if value):
            continue
        try:
            correction = int(str(raw['correction_no']).strip())
        except (TypeError, ValueError):
            raise HTTPException(422, f'第 {index} 行更正号不是整数') from None
        outcome = _parse_outcome(raw.get('outcome'), index)
        rows.append({
            'visitor_code': str(raw['visitor_code']).strip(),
            'correction_no': correction,
            'reading_order': str(raw['reading_order']).strip(),
            'outcome': outcome, 'filename': filename,
        })
    if not rows:
        raise HTTPException(422, 'CSV 没有可导入的数据行')
    try:
        from src.services.reconcile import parse_sheet_rows
        parse_sheet_rows(source, rows)
    except ReconcileError as exc:
        raise HTTPException(422, str(exc)) from None
    return rows


def _parse_outcome(value, index):
    token = '' if value is None else str(value).strip().lower()
    if token in NULL_TOKENS:
        return None
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    raise HTTPException(422, f'第 {index} 行结果无法识别：{token or "（空）"}')
