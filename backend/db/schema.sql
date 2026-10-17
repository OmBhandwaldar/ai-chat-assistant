-- DuckDB schema: tables for wells, inspections, maintenance, overrides, memory.
-- See PLAN.md Phase 1.1 / TECH_STACK.md section 1.

-- ---------------------------------------------------------------------------
-- Fleet-wide reference data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS teams (
    team_id         VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    lead_user_id    VARCHAR
);

CREATE TABLE IF NOT EXISTS areas (
    area_id         VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    team_id         VARCHAR REFERENCES teams(team_id),
    lat             DOUBLE,
    lon             DOUBLE
);

CREATE TABLE IF NOT EXISTS users (
    user_id         VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    role            VARCHAR NOT NULL,
    team_id         VARCHAR REFERENCES teams(team_id)
);

-- Thresholds from the well-type manuals (T200 / T350). Seeded by ingest.py
-- from the manuals; overridable per well_type via `overrides`
-- (scope_type='well_type').
CREATE TABLE IF NOT EXISTS well_type_rules (
    well_type               VARCHAR PRIMARY KEY,
    producing_threshold_c   DOUBLE NOT NULL,   -- min (wellhead - ambient) to count as producing
    max_wellhead_c          DOUBLE NOT NULL,   -- temperature alarm ceiling
    rca_corrective_count    INTEGER NOT NULL DEFAULT 3,  -- corrective repairs on one equipment_kind in 12mo that trigger RCA
    rca_lookback_days       INTEGER NOT NULL DEFAULT 365
);

-- ---------------------------------------------------------------------------
-- Wells and equipment
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wells (
    asset_id            VARCHAR PRIMARY KEY,
    name                VARCHAR NOT NULL,
    well_type           VARCHAR NOT NULL REFERENCES well_type_rules(well_type),
    area_id             VARCHAR REFERENCES areas(area_id),
    team_id             VARCHAR REFERENCES teams(team_id),
    in_charge_user_id   VARCHAR,
    lat                 DOUBLE,
    lon                 DOUBLE,
    installed_on        DATE,
    recorded_status     VARCHAR,          -- 'active' | 'down' — may be stale, see resolver
    last_inspection_id  VARCHAR
);

CREATE TABLE IF NOT EXISTS well_equipment (
    asset_id        VARCHAR NOT NULL REFERENCES wells(asset_id),
    equipment_id    VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    model           VARCHAR,
    PRIMARY KEY (asset_id, equipment_id)
);

-- ---------------------------------------------------------------------------
-- Weekly inspections (raw, dataset-provided). Never mutated after ingest;
-- corrections/feedback live in context_notes / overrides and are applied
-- through views (see views.sql), keeping the original value intact.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id       VARCHAR PRIMARY KEY,
    asset_id            VARCHAR NOT NULL REFERENCES wells(asset_id),
    inspected_at         TIMESTAMP NOT NULL,
    inspector_user_id   VARCHAR,
    method               VARCHAR,          -- 'drone' | 'manual'
    ambient_temp_c       DOUBLE,
    wellhead_temp_c      DOUBLE,
    spill_detected       BOOLEAN,
    spill_area_pct       DOUBLE,
    spill_est_volume_bbl DOUBLE,
    notes                 VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_inspections_asset ON inspections(asset_id);
CREATE INDEX IF NOT EXISTS idx_inspections_time ON inspections(inspected_at);

-- ---------------------------------------------------------------------------
-- Maintenance / repairs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS maintenance (
    maintenance_id          VARCHAR PRIMARY KEY,
    asset_id                VARCHAR NOT NULL REFERENCES wells(asset_id),
    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    maintenance_type        VARCHAR,       -- 'scheduled' | 'corrective'
    equipment_kind           VARCHAR,
    issue                    VARCHAR,
    action_taken              VARCHAR,
    technician_user_id       VARCHAR,
    contractor                VARCHAR,
    downtime_hours            DOUBLE,       -- duration of THIS job only — not well downtime
    cost_labor                DOUBLE,
    cost_parts                 DOUBLE,
    cost_total                  DOUBLE,
    triggered_by_inspection_id VARCHAR,
    report_file                 VARCHAR     -- relative path, may be NULL
);

CREATE INDEX IF NOT EXISTS idx_maintenance_asset ON maintenance(asset_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_started ON maintenance(started_at);

-- Free-text repair reports, chunked for retrieval (Phase 3.2 populates
-- report_chunks; this table holds the raw body + asset/maintenance join keys).
CREATE TABLE IF NOT EXISTS maintenance_reports (
    report_path     VARCHAR PRIMARY KEY,
    asset_id        VARCHAR NOT NULL REFERENCES wells(asset_id),
    maintenance_id  VARCHAR REFERENCES maintenance(maintenance_id),
    area_id         VARCHAR,
    started_at      TIMESTAMP,
    body            VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS report_chunks (
    chunk_id        VARCHAR PRIMARY KEY,
    report_path     VARCHAR NOT NULL REFERENCES maintenance_reports(report_path),
    asset_id        VARCHAR NOT NULL,
    maintenance_id  VARCHAR,
    area_id         VARCHAR,
    started_at      TIMESTAMP,
    chunk_index     INTEGER,
    text            VARCHAR NOT NULL
    -- dense vectors live in ChromaDB, keyed by chunk_id, not duplicated here
);

-- ---------------------------------------------------------------------------
-- Regulator events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS regulatory_events (
    event_id                VARCHAR PRIMARY KEY,
    asset_id                 VARCHAR NOT NULL REFERENCES wells(asset_id),
    date                      DATE,
    agency                     VARCHAR,
    event_type                  VARCHAR,
    protocol_id                  VARCHAR,
    related_inspection_id        VARCHAR,
    hours_to_report                DOUBLE,
    detail                          VARCHAR,
    fine_usd                         DOUBLE,
    status                             VARCHAR   -- 'open' | 'closed'
);

CREATE INDEX IF NOT EXISTS idx_reg_events_asset ON regulatory_events(asset_id);
CREATE INDEX IF NOT EXISTS idx_reg_events_date ON regulatory_events(date);

-- ---------------------------------------------------------------------------
-- Human notes (dataset-provided) + runtime shared notes, same table.
-- `source` distinguishes dataset-loaded notes from notes written live by
-- users through record_shared_note() (Q16: shared knowledge about a well).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS context_notes (
    note_id             VARCHAR PRIMARY KEY,
    asset_id            VARCHAR NOT NULL REFERENCES wells(asset_id),
    created_at          TIMESTAMP NOT NULL,
    author_user_id      VARCHAR,
    note_type           VARCHAR NOT NULL,  -- 'operator_feedback' | 'operator_note' | 'correction'
    inspection_id       VARCHAR,           -- set only on note_type='correction'
    text                VARCHAR NOT NULL,
    valid_from          DATE,
    valid_to            DATE,
    source              VARCHAR NOT NULL DEFAULT 'dataset'  -- 'dataset' | 'runtime'
);

CREATE INDEX IF NOT EXISTS idx_context_notes_asset ON context_notes(asset_id);
CREATE INDEX IF NOT EXISTS idx_context_notes_inspection ON context_notes(inspection_id);

-- ---------------------------------------------------------------------------
-- Overrides: every feedback/correction, at an explicit scope.
-- scope_type='inspection' -> scope_id=inspection_id (Q12, Q13)
-- scope_type='well'       -> scope_id=asset_id
-- scope_type='well_type'  -> scope_id='T200'|'T350' (Q14)
-- scope_type='global'     -> scope_id='*'
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS overrides_id_seq START 1;

CREATE TABLE IF NOT EXISTS overrides (
    id              BIGINT PRIMARY KEY DEFAULT nextval('overrides_id_seq'),
    scope_type      VARCHAR NOT NULL,   -- 'inspection' | 'well' | 'well_type' | 'global'
    scope_id        VARCHAR NOT NULL,
    field           VARCHAR NOT NULL,   -- e.g. 'wellhead_temp_c', 'spill_est_volume_bbl', 'rca_corrective_count'
    original_value  JSON,
    new_value       JSON NOT NULL,
    author_user_id  VARCHAR NOT NULL,
    reason          VARCHAR,
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_overrides_scope ON overrides(scope_type, scope_id);

-- ---------------------------------------------------------------------------
-- Claims: user statements that may contradict the data (Q17, Q18). Kept
-- 'unconfirmed' unless promoted to a formal override by the recording user.
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS claims_id_seq START 1;

CREATE TABLE IF NOT EXISTS shared_claims (
    id              BIGINT PRIMARY KEY DEFAULT nextval('claims_id_seq'),
    user_id         VARCHAR NOT NULL,
    scope_type      VARCHAR NOT NULL,   -- 'well' (claims are always well-scoped in this dataset)
    scope_id        VARCHAR NOT NULL,   -- asset_id
    text            VARCHAR NOT NULL,
    status          VARCHAR NOT NULL DEFAULT 'unconfirmed',  -- 'unconfirmed' | 'confirmed' | 'rejected'
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_claims_scope ON shared_claims(scope_type, scope_id);

-- ---------------------------------------------------------------------------
-- Private per-user memory
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS conversations_id_seq START 1;

CREATE TABLE IF NOT EXISTS conversations (
    id              BIGINT PRIMARY KEY DEFAULT nextval('conversations_id_seq'),
    user_id         VARCHAR NOT NULL,
    conversation_id VARCHAR NOT NULL,
    turn_id         INTEGER NOT NULL,
    role            VARCHAR NOT NULL,   -- 'user' | 'assistant'
    text            VARCHAR NOT NULL,
    ts              TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_conv ON conversations(conversation_id);

CREATE TABLE IF NOT EXISTS turn_summaries (
    user_id         VARCHAR PRIMARY KEY,
    summary         VARCHAR NOT NULL DEFAULT '',
    updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);
