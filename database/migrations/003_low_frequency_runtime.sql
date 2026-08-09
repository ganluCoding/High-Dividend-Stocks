CREATE TABLE IF NOT EXISTS low_frequency_batches (
    run_id TEXT PRIMARY KEY REFERENCES ingestion_runs(run_id),
    as_of_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'published', 'quarantined')),
    stock_symbols_attempted INTEGER NOT NULL DEFAULT 0,
    stock_dividend_symbols_succeeded INTEGER NOT NULL DEFAULT 0,
    financial_symbols_succeeded INTEGER NOT NULL DEFAULT 0,
    etf_symbols_attempted INTEGER NOT NULL DEFAULT 0,
    etf_distribution_symbols_succeeded INTEGER NOT NULL DEFAULT 0,
    dividend_events_inserted INTEGER NOT NULL DEFAULT 0,
    etf_events_inserted INTEGER NOT NULL DEFAULT 0,
    financial_observations_inserted INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_low_frequency_batches_date
    ON low_frequency_batches(as_of_date DESC);
