PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workbench_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Content-addressed research artifacts.  A changed file creates a new row;
-- historical hashes are never overwritten by a later run.
CREATE TABLE IF NOT EXISTS immutable_artifacts_v1 (
    artifact_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    content_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_type, artifact_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_immutable_artifacts_lookup
    ON immutable_artifacts_v1(artifact_type, artifact_id, created_at DESC);

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

CREATE TABLE IF NOT EXISTS strategy_versions_v1 (
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('stable', 'etf', 'cyclical')),
    loaded_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, strategy_version)
);

CREATE TABLE IF NOT EXISTS strategy_runs_v1 (
    strategy_run_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    facts_sha256 TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    screen_run_id TEXT NOT NULL REFERENCES screen_runs_v1(screen_run_id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed'))
);

CREATE TABLE IF NOT EXISTS watch_items_v1 (
    watch_item_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    review_condition TEXT,
    review_due_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_runs_v1_release
    ON strategy_runs_v1(release_id, strategy_id, created_at DESC);
