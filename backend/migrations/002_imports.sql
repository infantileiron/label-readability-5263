-- 导入批次：一份交付表（旧展签或易读样张）的一次上传，幂等键为来源文件名。
CREATE TABLE import_batches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    study_id bigint NOT NULL REFERENCES studies(id),
    source text NOT NULL CHECK (source IN ('old', 'new')),
    filename text NOT NULL,
    sheet_kind text NOT NULL CHECK (sheet_kind IN ('old_label', 'easy_proof')),
    row_count int NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    status text NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reconciled')),
    imported_by text NOT NULL REFERENCES accounts(username),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (study_id, filename)
);
CREATE INDEX import_batches_study ON import_batches(study_id, id);

-- 交付表原始记录。同一批次内同一观众的更正号必须唯一；
-- 跨批次重传同一文件名时整批先作废再重收，保证重跑不重复计人。
CREATE TABLE import_records (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id bigint NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    study_id bigint NOT NULL REFERENCES studies(id),
    visitor_code text NOT NULL,
    segment text NOT NULL CHECK (segment IN ('old', 'new')),
    correction_no int NOT NULL CHECK (correction_no >= 0),
    reading_order text NOT NULL CHECK (reading_order IN ('old_first', 'new_first')),
    outcome boolean,
    active boolean NOT NULL DEFAULT true,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, visitor_code, correction_no)
);
CREATE INDEX import_records_lookup ON import_records(study_id, visitor_code, segment);
CREATE INDEX import_records_batch ON import_records(batch_id) WHERE active;

-- 每个观众每一段的对账暂存：最高更正号若有多个不同结果则记冲突，整对暂不纳入统计。
CREATE TABLE pair_segments (
    study_id bigint NOT NULL REFERENCES studies(id),
    visitor_code text NOT NULL,
    segment text NOT NULL CHECK (segment IN ('old', 'new')),
    reading_order text NOT NULL CHECK (reading_order IN ('old_first', 'new_first')),
    outcome boolean,
    correction_no int,
    batch_id bigint REFERENCES import_batches(id),
    conflict boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (study_id, visitor_code, segment)
);
CREATE INDEX pair_segments_study ON pair_segments(study_id, visitor_code);

-- reading_pairs 增加来源与排除标记。手录历史数据默认 manual、纳入统计。
ALTER TABLE reading_pairs ADD COLUMN source text NOT NULL DEFAULT 'manual'
    CHECK (source IN ('manual', 'import'));
ALTER TABLE reading_pairs ADD COLUMN excluded boolean NOT NULL DEFAULT false;
ALTER TABLE reading_pairs ADD COLUMN exclude_reason text;
CREATE INDEX reading_pairs_study_active
    ON reading_pairs(study_id) WHERE NOT excluded;
