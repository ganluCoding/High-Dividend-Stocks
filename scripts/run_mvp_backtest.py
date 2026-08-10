#!/usr/bin/env python3
"""Run a transparent, static-basket MVP backtest on a published release.

This is deliberately not a point-in-time strategy engine.  It evaluates the
current research snapshots as fixed baskets, using unadjusted closes and cash
dividends on ex-date.  The output is useful for data/implementation checks;
it must not be read as a live recommendation or an unbiased historical test.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "backtest_mvp.json"


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def annualized_return(start_value: float, end_value: float, days: int) -> float | None:
    if start_value <= 0 or end_value <= 0 or days <= 0:
        return None
    return (end_value / start_value) ** (365.25 / days) - 1.0


def load_price_rows(connection: sqlite3.Connection, instrument_ids: list[str], start_date: str, end_date: str) -> dict[str, dict[str, float]]:
    placeholders = ",".join("?" for _ in instrument_ids)
    rows = connection.execute(
        f"""SELECT instrument_id, trade_date, close, observed_at, run_id
            FROM price_daily
            WHERE adjustment='unadjusted' AND instrument_id IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
            ORDER BY instrument_id, trade_date, observed_at DESC, run_id DESC""",
        (*instrument_ids, start_date, end_date),
    ).fetchall()
    prices: dict[str, dict[str, float]] = defaultdict(dict)
    for instrument_id, trade_date, close, _observed_at, _run_id in rows:
        # The release retains repeated observations from independent sources.
        # Keep the first row after deterministic ordering (latest observation).
        prices[str(instrument_id)].setdefault(str(trade_date), float(close))
    return dict(prices)


def load_dividends(connection: sqlite3.Connection, instrument_ids: list[str], start_date: str, end_date: str) -> dict[str, dict[str, float]]:
    placeholders = ",".join("?" for _ in instrument_ids)
    rows = connection.execute(
        f"""SELECT instrument_id, ex_date, cash_dps_cny
            FROM stock_dividend_events
            WHERE status='implemented' AND ex_date IS NOT NULL
              AND instrument_id IN ({placeholders})
              AND ex_date BETWEEN ? AND ?""",
        (*instrument_ids, start_date, end_date),
    ).fetchall()
    dividends: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for instrument_id, ex_date, dps in rows:
        if dps is not None and float(dps) > 0:
            dividends[str(instrument_id)][str(ex_date)] += float(dps)
    return {instrument_id: dict(values) for instrument_id, values in dividends.items()}


def first_trade_dates(prices: dict[str, dict[str, float]]) -> list[str]:
    return sorted({trade_date for values in prices.values() for trade_date in values})


def simulate(
    connection: sqlite3.Connection,
    instrument_ids: list[str],
    start_date: str,
    end_date: str,
    principal: float,
    cost_bps: float,
    rebalance: str = "annual",
) -> dict[str, Any]:
    prices = load_price_rows(connection, instrument_ids, start_date, end_date)
    dividends = load_dividends(connection, instrument_ids, start_date, end_date)
    timeline = first_trade_dates(prices)
    if not timeline:
        raise ValueError("No price history for basket")

    names = {
        row[0]: {"ticker": row[1], "name": row[2]}
        for row in connection.execute(
            f"SELECT instrument_id,ticker,name FROM instruments WHERE instrument_id IN ({','.join('?' for _ in instrument_ids)})",
            instrument_ids,
        )
    }
    shares = {instrument_id: 0.0 for instrument_id in instrument_ids}
    cash = float(principal)
    last_prices: dict[str, float] = {}
    values: list[tuple[str, float]] = []
    rebalance_dates: list[str] = []
    total_dividends = 0.0
    total_turnover = 0.0
    total_cost = 0.0
    trade_count = 0
    previous_year: int | None = None

    def portfolio_value() -> float:
        return cash + sum(shares[i] * last_prices[i] for i in instrument_ids if i in last_prices)

    for trade_date in timeline:
        current_day = parse_date(trade_date)
        for instrument_id, values_for_instrument in prices.items():
            if trade_date in values_for_instrument:
                last_prices[instrument_id] = values_for_instrument[trade_date]

        for instrument_id in instrument_ids:
            dps = dividends.get(instrument_id, {}).get(trade_date, 0.0)
            if dps and shares[instrument_id] > 0:
                cash += shares[instrument_id] * dps
                total_dividends += shares[instrument_id] * dps

        should_rebalance = previous_year is None or (rebalance == "annual" and current_day.year != previous_year)
        if should_rebalance:
            active = [i for i in instrument_ids if i in last_prices]
            if active:
                current_value = portfolio_value()
                turnover_value = sum(abs(shares[i] * last_prices[i] - current_value / len(active)) for i in active)
                cost = turnover_value * cost_bps / 10000.0
                investable = max(0.0, current_value - cost)
                target = investable / len(active)
                shares = {i: (target / last_prices[i] if i in active else 0.0) for i in instrument_ids}
                cash = 0.0
                total_turnover += turnover_value
                total_cost += cost
                trade_count += len(active)
                rebalance_dates.append(trade_date)
            previous_year = current_day.year

        values.append((trade_date, portfolio_value()))

    start_value = values[0][1]
    end_value = values[-1][1]
    daily_returns = [values[index][1] / values[index - 1][1] - 1.0 for index in range(1, len(values)) if values[index - 1][1] > 0]
    running_max = 0.0
    max_drawdown = 0.0
    for _trade_date, value in values:
        running_max = max(running_max, value)
        if running_max > 0:
            max_drawdown = min(max_drawdown, value / running_max - 1.0)
    volatility = pstdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else None
    sharpe = mean(daily_returns) / pstdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 and pstdev(daily_returns) > 0 else None
    calendar_days = (parse_date(values[-1][0]) - parse_date(values[0][0])).days
    cagr = annualized_return(principal, end_value, calendar_days)
    return {
        "start_date": values[0][0],
        "end_date": values[-1][0],
        "observations": len(values),
        "principal_cny": round(principal, 2),
        "initial_value_after_entry_cost_cny": round(start_value, 2),
        "final_value_cny": round(end_value, 2),
        "cumulative_return_on_principal": round(end_value / principal - 1.0, 8),
        "annualized_return_on_principal": round(cagr, 8) if cagr is not None else None,
        "annualized_volatility": round(volatility, 8) if volatility is not None else None,
        "sharpe_zero_rf": round(sharpe, 8) if sharpe is not None else None,
        "max_drawdown": round(max_drawdown, 8),
        "cash_dividends_received_cny": round(total_dividends, 2),
        "turnover_cny": round(total_turnover, 2),
        "transaction_cost_cny": round(total_cost, 2),
        "rebalance_count": len(rebalance_dates),
        "trade_count_proxy": trade_count,
        "price_instruments": sum(1 for i in instrument_ids if prices.get(i)),
        "dividend_instruments": sum(1 for i in instrument_ids if dividends.get(i)),
        "dividend_events_used": sum(len(values_for_instrument) for values_for_instrument in dividends.values()),
        "basket": [names.get(i, {"ticker": i, "name": i}) for i in instrument_ids],
        "equity_curve": [{"trade_date": trade_date, "value_cny": round(value, 2)} for trade_date, value in values],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--release-id")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("data/backtests"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    runtime = args.runtime_root.expanduser()
    release_id = args.release_id or config["release_id"]
    release = runtime / "releases" / release_id
    facts = release / "facts.sqlite"
    if not facts.is_file():
        raise SystemExit(f"Published release not found: {facts}")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_json = output_dir / f"mvp_backtest_{release_id}_{stamp}.json"
    output_csv = output_dir / f"mvp_backtest_{release_id}_{stamp}.csv"
    with sqlite3.connect(facts) as connection:
        connection.execute("PRAGMA query_only=ON")
        results: dict[str, Any] = {}
        for basket_name, instrument_ids in config["baskets"].items():
            by_cost: dict[str, Any] = {}
            for cost_bps in (0.0, 10.0, 20.0):
                by_cost[str(int(cost_bps))] = simulate(
                    connection,
                    instrument_ids,
                    config["window_start"],
                    config["window_end"],
                    float(config["principal_cny"]),
                    cost_bps,
                )
            results[basket_name] = by_cost
    payload = {
        "schema_version": "mvp-backtest-result-v1",
        "status": "exploratory_static_basket",
        "release_id": release_id,
        "config": config,
        "method": {
            "rebalance": "annual_equal_weight",
            "price_field": "unadjusted_close",
            "dividend_field": "cash_dps_cny_on_ex_date",
            "dividend_reinvestment": "cash_held_until_next_rebalance",
            "cost_sensitivity_bps": [0, 10, 20],
            "risk_free_rate": 0,
            "formulas": {
                "cumulative_return": "final_value / initial_value - 1",
                "cagr": "(final_value / initial_value) ** (365.25 / calendar_days) - 1",
                "volatility": "stdev(daily_returns) * sqrt(252)",
                "max_drawdown": "min(value / running_max - 1)",
            },
        },
        "results": results,
        "limitations": [
            "Baskets are current screening snapshots and are not reconstructed point-in-time; look-ahead and survivor bias remain.",
            "Only 31 core stock dividend histories are available; ETF product facts and ETF distributions are not used.",
            "No delisted securities, historical universe membership, PIT financials, tax, stamp duty, slippage, limit-up/down or execution constraints.",
            "Price release contains repeated observations from independent sources; the backtest keeps the latest observed row per instrument/date.",
            "This is an MVP-derived diagnostic, not an investable performance claim or an allocation recommendation.",
        ],
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["basket", "cost_bps", "start_date", "end_date", "principal_cny", "initial_value_after_entry_cost_cny", "final_value_cny", "cumulative_return_on_principal", "annualized_return_on_principal", "annualized_volatility", "sharpe_zero_rf", "max_drawdown", "cash_dividends_received_cny", "turnover_cny", "transaction_cost_cny", "rebalance_count"])
        writer.writeheader()
        for basket_name, by_cost in results.items():
            for cost_bps, result in by_cost.items():
                writer.writerow({"basket": basket_name, "cost_bps": cost_bps, **{key: result[key] for key in writer.fieldnames if key not in ("basket", "cost_bps")}})
    print(json.dumps({"status": payload["status"], "release_id": release_id, "json": str(output_json), "csv": str(output_csv), "baskets": list(results)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
