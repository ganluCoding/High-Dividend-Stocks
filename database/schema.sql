-- High-dividend research database, SQLite schema v1.
-- Raw CSV snapshots stay immutable in data/curated/; this database is the
-- query layer used later by the strategy library and desktop application.

PRAGMA foreign_keys = ON;

CREATE TABLE app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE data_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    research_as_of TEXT,
    principal_cny REAL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    deterministic_content_hash TEXT,
    status TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE data_sources (
    source_id TEXT PRIMARY KEY,
    publisher TEXT,
    source_tier TEXT NOT NULL CHECK (source_tier IN ('primary', 'convenience', 'secondary_locator', 'unknown')),
    url TEXT,
    use_scope TEXT,
    notes TEXT
);

CREATE TABLE instruments (
    instrument_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'etf', 'index')),
    exchange TEXT,
    benchmark_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE candidate_universe_memberships (
    universe_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    category TEXT NOT NULL,
    dividend_style TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    data_status TEXT NOT NULL,
    PRIMARY KEY (universe_id, instrument_id)
);

CREATE TABLE price_daily (
    run_id TEXT NOT NULL REFERENCES data_runs(run_id),
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    trade_date TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES data_sources(source_id),
    source_raw_sha256 TEXT,
    observed_at TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume REAL,
    turnover_cny REAL,
    amplitude_pct REAL,
    change_pct REAL,
    change_cny REAL,
    turnover_rate_pct REAL,
    PRIMARY KEY (run_id, instrument_id, trade_date, adjustment, source_id)
);

CREATE TABLE stock_dividend_events (
    run_id TEXT NOT NULL REFERENCES data_runs(run_id),
    version_id TEXT NOT NULL,
    dividend_event_id TEXT NOT NULL,
    supersedes_version_id TEXT,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    profit_period_label TEXT,
    distribution_type TEXT,
    installment_no INTEGER,
    status TEXT NOT NULL,
    cash_per_10_shares_cny REAL,
    cash_dps_cny REAL,
    published_at_date TEXT,
    record_date TEXT,
    ex_date TEXT,
    payment_date TEXT,
    description TEXT,
    source_id TEXT NOT NULL REFERENCES data_sources(source_id),
    source_raw_sha256 TEXT,
    observed_at TEXT,
    PRIMARY KEY (run_id, version_id)
);

CREATE TABLE etf_distribution_events (
    run_id TEXT NOT NULL REFERENCES data_runs(run_id),
    version_id TEXT NOT NULL,
    dividend_event_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    status TEXT NOT NULL,
    cash_per_unit_cny REAL NOT NULL,
    cumulative_distribution_cny REAL,
    ex_date TEXT,
    date_semantics TEXT,
    source_id TEXT NOT NULL REFERENCES data_sources(source_id),
    source_raw_sha256 TEXT,
    observed_at TEXT,
    PRIMARY KEY (run_id, version_id)
);

CREATE TABLE financial_observations (
    run_id TEXT NOT NULL REFERENCES data_runs(run_id),
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    statement_type TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    currency TEXT,
    report_date TEXT NOT NULL,
    published_at_date TEXT,
    updated_at_date TEXT,
    source_id TEXT NOT NULL REFERENCES data_sources(source_id),
    source_raw_sha256 TEXT,
    observed_at TEXT,
    PRIMARY KEY (run_id, instrument_id, statement_type, metric, report_date, published_at_date, source_id)
);

CREATE INDEX idx_price_daily_instrument_date ON price_daily (instrument_id, trade_date DESC);
CREATE INDEX idx_stock_dividend_instrument_date ON stock_dividend_events (instrument_id, ex_date DESC);
CREATE INDEX idx_etf_distribution_instrument_date ON etf_distribution_events (instrument_id, ex_date DESC);
CREATE INDEX idx_financial_instrument_metric_date ON financial_observations (instrument_id, metric, report_date DESC);
CREATE INDEX idx_candidate_universe_category ON candidate_universe_memberships (universe_id, category);

-- One current unadjusted close per instrument. The data run remains visible
-- so UI and strategies can show exactly which frozen snapshot supplied it.
CREATE VIEW v_latest_unadjusted_close AS
WITH ranked AS (
    SELECT
        p.*,
        ROW_NUMBER() OVER (
            PARTITION BY p.instrument_id
            ORDER BY p.trade_date DESC, p.observed_at DESC, p.run_id DESC
        ) AS rn
    FROM price_daily AS p
    WHERE p.adjustment = 'unadjusted'
)
SELECT
    r.run_id,
    r.instrument_id,
    i.ticker,
    i.name,
    i.asset_type,
    r.trade_date,
    r.close,
    r.source_id
FROM ranked AS r
JOIN instruments AS i ON i.instrument_id = r.instrument_id
WHERE r.rn = 1;

-- Historical, pre-tax static cash yield. It deliberately uses implemented
-- events only and is not a forward dividend forecast or total return.
CREATE VIEW v_static_cash_yield AS
WITH latest AS (
    SELECT * FROM v_latest_unadjusted_close
),
cash_events AS (
    SELECT instrument_id, ex_date, cash_dps_cny AS cash_per_unit_cny, source_id, 'stock' AS event_kind
    FROM (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY dividend_event_id
                ORDER BY observed_at DESC, run_id DESC
            ) AS rn
        FROM stock_dividend_events
        WHERE status = 'implemented' AND ex_date IS NOT NULL
    )
    WHERE rn = 1
    UNION ALL
    SELECT instrument_id, ex_date, cash_per_unit_cny, source_id, 'etf' AS event_kind
    FROM (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY dividend_event_id
                ORDER BY observed_at DESC, run_id DESC
            ) AS rn
        FROM etf_distribution_events
        WHERE status = 'implemented' AND ex_date IS NOT NULL
    )
    WHERE rn = 1
),
window_cash AS (
    SELECT
        l.instrument_id,
        COALESCE(SUM(e.cash_per_unit_cny), 0.0) AS trailing_12m_cash_per_unit_cny,
        COUNT(e.ex_date) AS trailing_12m_event_count
    FROM latest AS l
    LEFT JOIN cash_events AS e
        ON e.instrument_id = l.instrument_id
       AND e.ex_date > date(l.trade_date, '-12 months')
       AND e.ex_date <= l.trade_date
    GROUP BY l.instrument_id
)
SELECT
    l.run_id,
    l.instrument_id,
    l.ticker,
    l.name,
    l.asset_type,
    l.trade_date AS price_date,
    l.close AS latest_unadjusted_close_cny,
    w.trailing_12m_cash_per_unit_cny,
    w.trailing_12m_event_count,
    CASE
        WHEN l.close > 0 AND w.trailing_12m_event_count > 0
            THEN w.trailing_12m_cash_per_unit_cny / l.close
    END AS trailing_12m_static_cash_yield_pre_tax,
    CASE
        WHEN l.asset_type = 'etf' THEN 'ETF分配记录仍须逐条公告核验；收益率不是基金总回报。'
        ELSE '已实施现金分红/未复权价格；不是前瞻股息率或总回报。'
    END AS interpretation_boundary
FROM latest AS l
JOIN window_cash AS w ON w.instrument_id = l.instrument_id;

-- UI-friendly candidate list. Null yield means the candidate has not yet
-- completed the required price-and-distribution collection; it never means 0%.
CREATE VIEW v_candidate_universe AS
SELECT
    m.universe_id,
    i.ticker,
    i.name,
    i.asset_type,
    m.category,
    m.dividend_style,
    m.risk_level,
    m.data_status,
    y.price_date,
    y.latest_unadjusted_close_cny,
    y.trailing_12m_cash_per_unit_cny,
    y.trailing_12m_static_cash_yield_pre_tax
FROM candidate_universe_memberships AS m
JOIN instruments AS i ON i.instrument_id = m.instrument_id
LEFT JOIN v_static_cash_yield AS y ON y.instrument_id = m.instrument_id;

CREATE VIEW v_database_summary AS
SELECT 'instruments' AS entity, COUNT(*) AS row_count FROM instruments
UNION ALL SELECT 'price_daily', COUNT(*) FROM price_daily
UNION ALL SELECT 'stock_dividend_events', COUNT(*) FROM stock_dividend_events
UNION ALL SELECT 'etf_distribution_events', COUNT(*) FROM etf_distribution_events
UNION ALL SELECT 'financial_observations', COUNT(*) FROM financial_observations;
