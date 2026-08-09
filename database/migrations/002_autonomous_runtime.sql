CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    file_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    trigger_kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    target_trade_date TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'published', 'quarantined', 'skipped', 'failed')),
    message TEXT,
    raw_run_path TEXT,
    release_id TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_failures (
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    source_id TEXT,
    dataset TEXT,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    source_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    rows_received INTEGER,
    observed_at TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (run_id, source_id, dataset)
);

CREATE TABLE IF NOT EXISTS quality_checks (
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    check_name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    details TEXT,
    PRIMARY KEY (run_id, check_name)
);

CREATE TABLE IF NOT EXISTS ingestion_watermarks (
    dataset TEXT PRIMARY KEY,
    last_success_trade_date TEXT,
    last_success_run_id TEXT REFERENCES ingestion_runs(run_id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS database_releases (
    release_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    published_at TEXT NOT NULL,
    database_path TEXT NOT NULL,
    database_sha256 TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_master (
    instrument_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'etf')),
    exchange TEXT NOT NULL,
    listing_status TEXT NOT NULL DEFAULT 'active',
    source_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id TEXT REFERENCES ingestion_runs(run_id)
);

CREATE TABLE IF NOT EXISTS market_daily_prices (
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    instrument_id TEXT NOT NULL REFERENCES security_master(instrument_id),
    trade_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    close REAL NOT NULL CHECK (close > 0),
    open REAL,
    high REAL,
    low REAL,
    volume REAL,
    turnover_cny REAL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('approved', 'quarantined')),
    PRIMARY KEY (run_id, instrument_id, source_id)
);

CREATE TABLE IF NOT EXISTS universe_definitions (
    universe_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    description TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS screening_runs (
    screening_run_id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL REFERENCES universe_definitions(universe_id),
    source_run_id TEXT REFERENCES ingestion_runs(run_id),
    as_of_date TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_results (
    screening_run_id TEXT NOT NULL REFERENCES screening_runs(screening_run_id),
    instrument_id TEXT NOT NULL REFERENCES security_master(instrument_id),
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    reason_code TEXT NOT NULL,
    score REAL,
    PRIMARY KEY (screening_run_id, instrument_id)
);

CREATE INDEX IF NOT EXISTS idx_market_daily_prices_instrument_date ON market_daily_prices (instrument_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_security_master_type ON security_master (asset_type, listing_status);

CREATE VIEW IF NOT EXISTS v_latest_market_eod_price AS
WITH ranked AS (
    SELECT p.*, ROW_NUMBER() OVER (
        PARTITION BY p.instrument_id
        ORDER BY p.trade_date DESC, p.run_id DESC
    ) AS rn
    FROM market_daily_prices AS p
    WHERE p.validation_status = 'approved'
)
SELECT r.instrument_id, s.ticker, s.name, s.asset_type, s.exchange, r.trade_date, r.close, r.source_id, r.run_id
FROM ranked AS r
JOIN security_master AS s ON s.instrument_id = r.instrument_id
WHERE r.rn = 1;
