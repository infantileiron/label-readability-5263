/** @typedef {{visitor_code: string, reading_order: 'old_first'|'new_first', old_ok: boolean|null, new_ok: boolean|null, source?: 'manual'|'import', excluded?: boolean, exclude_reason?: string|null}} ReadingPair */
/** @typedef {{id: number, code: string, title: string, exhibit: string, body: string, type_pt: number, line_height: number}} LabelVersion */
/** @typedef {{id: number, revision: number, status: 'queued'|'succeeded'|'failed', decision: 'publish'|'revise'|null, checksum: string}} ProofRun */
/** @typedef {{id: number, source: 'old'|'new', sheet_kind: string, filename: string, row_count: number, status: string, imported_by: string, created_at: string}} ImportBatch */
export const completePair = ({ old_ok, new_ok }) => old_ok !== null && new_ok !== null;
export const includedPair = row => !row.excluded;
export const SOURCE = { manual: '手录', import: '交付表导入' };
export const SHEET_SOURCE = { old: '旧展签表', new: '易读样张飞表' };
