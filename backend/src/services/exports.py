import csv
import io


def pairing_csv(export):
    """当次快照的样本 CSV：主表只含纳入统计的配对，排除明细附在其后。"""
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    snapshot = export['snapshot']
    # 快照里的 pairs 已是当次纳入统计的配对版本；保留来源列以区分导入与手录。
    writer.writerow(['run_id', 'checksum', 'revision', 'visitor_code', 'reading_order',
                     'old_ok', 'new_ok', 'source', 'included'])
    for row in snapshot['pairs']:
        writer.writerow([export['id'], export['checksum'], export['revision'], row['visitor_code'],
                         row['reading_order'], row['old_ok'], row['new_ok'],
                         row.get('source', 'manual'), True])
    excluded = snapshot.get('excluded_pairs', [])
    if excluded:
        writer.writerow([])
        writer.writerow(['# 暂不纳入统计的配对（缺测 / 最高更正号冲突 / 段未齐）'])
        writer.writerow(['run_id', 'checksum', 'revision', 'visitor_code', 'reading_order',
                         'old_ok', 'new_ok', 'source', 'exclude_reason'])
        for row in excluded:
            writer.writerow([export['id'], export['checksum'], export['revision'], row['visitor_code'],
                             row['reading_order'], row['old_ok'], row['new_ok'],
                             row.get('source', 'manual'), row.get('exclude_reason') or '未纳入统计'])
    return output.getvalue()
