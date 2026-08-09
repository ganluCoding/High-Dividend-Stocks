CREATE TABLE IF NOT EXISTS coverage_matrix (
    instrument_id TEXT NOT NULL REFERENCES security_master(instrument_id),
    dataset TEXT NOT NULL,
    required_history_window TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    available_cutoff TEXT NOT NULL,
    latest_effective_date TEXT,
    latest_available_date TEXT,
    collection_status TEXT NOT NULL CHECK (collection_status IN ('collected', 'missing', 'not_applicable')),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('approved', 'unverified', 'not_applicable')),
    completeness_ratio REAL NOT NULL CHECK (completeness_ratio >= 0 AND completeness_ratio <= 1),
    freshness_status TEXT NOT NULL CHECK (freshness_status IN ('fresh', 'stale', 'unknown', 'not_applicable')),
    source_tier TEXT NOT NULL,
    release_id TEXT NOT NULL,
    source_run_ids_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, dataset, required_history_window)
);

CREATE INDEX IF NOT EXISTS idx_coverage_matrix_dataset
    ON coverage_matrix(dataset, collection_status, verification_status);

CREATE TABLE IF NOT EXISTS release_fact_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
