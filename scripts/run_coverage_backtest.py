#!/usr/bin/env python3
"""Run a broad, coverage-limited stock backtest on one published release.

This is intentionally a diagnostic, not a formal point-in-time strategy test:
the eligible universe is derived from the current release and therefore has
survivorship/look-ahead bias.  It exists to answer how results change when the
sample expands beyond the 31-name MVP basket.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from run_mvp_backtest import simulate


DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"


def select_universe(connection: sqlite3.Connection, minimum_history_days: int, start_date: str, end_date: str) -> list[str]:
    rows = connection.execute(
        """
        WITH price_counts AS (
          SELECT instrument_id, COUNT(DISTINCT trade_date) AS history_days
          FROM price_daily
          WHERE adjustment='unadjusted' AND trade_date BETWEEN ? AND ?
          GROUP BY instrument_id
        ), dividend_positive AS (
          SELECT DISTINCT instrument_id
          FROM stock_dividend_events
          WHERE status='implemented' AND ex_date IS NOT NULL
            AND cash_dps_cny > 0 AND ex_date <= ?
        )
        SELECT i.instrument_id
        FROM instruments i
        JOIN price_counts p ON p.instrument_id=i.instrument_id
        JOIN dividend_positive d ON d.instrument_id=i.instrument_id
        WHERE i.asset_type='stock' AND i.active=1 AND p.history_days >= ?
        ORDER BY i.instrument_id
        """,
        (start_date, end_date, end_date, minimum_history_days),
    ).fetchall()
    return [str(row[0]) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--window-start", default="2019-01-02")
    parser.add_argument("--window-end", default="2026-08-07")
    parser.add_argument("--minimum-history-days", type=int, default=1250)
    parser.add_argument("--principal-cny", type=float, default=200000.0)
    parser.add_argument("--output-dir", type=Path, default=Path("data/backtests"))
    args = parser.parse_args()

    facts = args.runtime_root.expanduser() / "releases" / args.release_id / "facts.sqlite"
    if not facts.is_file():
        raise SystemExit(f"Published release not found: {facts}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(facts) as connection:
        connection.execute("PRAGMA query_only=ON")
        instrument_ids = select_universe(connection, args.minimum_history_days, args.window_start, args.window_end)
        if not instrument_ids:
            raise SystemExit("No stock instruments meet the coverage filter")
        results: dict[str, Any] = {}
        for cost_bps in (0.0, 10.0, 20.0):
            results[str(int(cost_bps))] = simulate(
                connection,
                instrument_ids,
                args.window_start,
                args.window_end,
                args.principal_cny,
                cost_bps,
            )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_json = args.output_dir / f"coverage_backtest_{args.release_id}_{stamp}.json"
    output_csv = args.output_dir / f"coverage_backtest_{args.release_id}_{stamp}.csv"
    coverage = {
        "eligible_instruments": len(instrument_ids),
        "stock_universe_denominator": 5422,
        "coverage_ratio": round(len(instrument_ids) / 5422, 6),
        "minimum_history_days": args.minimum_history_days,
        "requires_positive_implemented_dividend": True,
    }
    payload = {
        "schema_version": "coverage-backtest-result-v1",
        "status": "insufficient_evidence_current_snapshot_diagnostic",
        "release_id": args.release_id,
        "coverage": coverage,
        "config": {"principal_cny": args.principal_cny, "window_start": args.window_start, "window_end": args.window_end},
        "method": {
            "universe": "current-release stocks with >= minimum_history_days and at least one implemented positive cash dividend",
            "rebalance": "annual_equal_weight",
            "price_field": "unadjusted_close",
            "dividend_field": "cash_dps_cny_on_ex_date",
            "cost_sensitivity_bps": [0, 10, 20],
        },
        "results": results,
        "limitations": [
            "Current-release universe is not reconstructed point-in-time; look-ahead and survivorship bias remain.",
            "ETF facts are excluded because ETF product/distribution coverage is insufficient.",
            "No delisted securities, PIT announcement cutoffs, tax, stamp duty, slippage, limit-up/down or execution constraints.",
            "This diagnostic must not be read as a full-market performance claim or allocation recommendation.",
        ],
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        fields = ["cost_bps", "start_date", "end_date", "principal_cny", "final_value_cny", "cumulative_return_on_principal", "annualized_return_on_principal", "annualized_volatility", "sharpe_zero_rf", "max_drawdown", "cash_dividends_received_cny", "price_instruments", "dividend_instruments"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for cost_bps, result in results.items():
            writer.writerow({"cost_bps": cost_bps, **{key: result[key] for key in fields if key != "cost_bps"}})
    print(json.dumps({"status": payload["status"], "release_id": args.release_id, "eligible_instruments": len(instrument_ids), "json": str(output_json), "csv": str(output_csv)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
