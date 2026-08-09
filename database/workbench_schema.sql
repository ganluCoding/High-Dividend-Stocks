PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workbench_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    rule_sha256 TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, rule_version)
);

CREATE TABLE IF NOT EXISTS screen_runs_v1 (
    screen_run_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    facts_sha256 TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    available_cutoff TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed'))
);

CREATE TABLE IF NOT EXISTS screen_results_v1 (
    screen_run_id TEXT NOT NULL REFERENCES screen_runs_v1(screen_run_id),
    instrument_id TEXT NOT NULL,
    research_state TEXT NOT NULL CHECK (research_state IN ('资料足以研究', '继续观察', '不纳入本模板', '资料不足')),
    reason_codes_json TEXT NOT NULL,
    research_priority INTEGER,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (screen_run_id, instrument_id)
);

CREATE INDEX IF NOT EXISTS idx_screen_results_v1_state
    ON screen_results_v1(screen_run_id, research_state, research_priority);
