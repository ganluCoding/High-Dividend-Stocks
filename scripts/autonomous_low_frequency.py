#!/usr/bin/env python3
"""Bounded, full-market dividend and financial refresh.

The market snapshot is daily.  This job rotates through the active market in
small daily batches, stages all responses first, applies coverage gates, then
publishes one SQLite transaction.  Missing symbols never delete the last
approved observation.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import akshare as ak
import baostock as bs

from autonomous_update import (
    PROJECT_ROOT,
    backup_database,
    canonical_database_sha256,
    sha256_file,
    utc_now,
    write_health,
    write_raw,
)
from collect_spike_data import normalize_etf_dividends, normalize_stock_dividends
from apply_migrations import apply


SH_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "database" / "high_dividend.db"
DATE_COLUMN = re.compile(r"^\d{8}$")
EMPTY_STOCK_DIVIDEND_COLUMNS = [
    "实施方案公告日期", "分红类型", "送股比例", "转增比例", "派息比例",
    "股权登记日", "除权日", "派息日", "股份到账日", "实施方案分红说明", "报告时间",
]


def source_call(adapter: str, params: dict[str, str]) -> pd.DataFrame:
    functions = {
        "stock_dividend_cninfo": lambda: ak.stock_dividend_cninfo(symbol=params["symbol"]),
        "stock_financial_abstract": lambda: ak.stock_financial_abstract(symbol=params["symbol"]),
        "fund_etf_dividend_sina": lambda: ak.fund_etf_dividend_sina(symbol=params["symbol"]),
    }
    try:
        frame = functions[adapter]()
    except KeyError as exc:
        # AKShare raises this when CNInfo has no dividend table for a security,
        # rather than returning an empty DataFrame.  That is an empty factual
        # result, not a source outage.
        if adapter == "stock_dividend_cninfo" and "实施方案公告日期" in str(exc):
            return pd.DataFrame(columns=EMPTY_STOCK_DIVIDEND_COLUMNS)
        raise
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{adapter} returned {type(frame).__name__}, expected DataFrame")
    return frame


def collect_baostock_financials(instruments: list[dict[str, Any]], year: int, quarter: int) -> tuple[list[dict[str, Any]], set[str], list[dict[str, str]]]:
    """Collect a compact, cross-source financial fact set in one BaoStock session."""
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    metric_specs = {
        "profit": {"netProfit": "PARENT_NETPROFIT", "MBRevenue": "OPERATE_INCOME", "roeAvg": "ROE", "epsTTM": "EPS_TTM", "gpMargin": "GROSS_MARGIN"},
        "balance": {"liabilityToAsset": "ASSET_LIABILITY_RATIO", "currentRatio": "CURRENT_RATIO", "quickRatio": "QUICK_RATIO"},
        "cash_flow": {"CFOToOR": "CFO_TO_OPERATE_INCOME", "CFOToNP": "CFO_TO_NETPROFIT"},
        "growth": {"YOYNI": "NETPROFIT_GROWTH", "YOYEquity": "EQUITY_GROWTH", "YOYAsset": "ASSET_GROWTH"},
    }
    records: list[dict[str, Any]] = []
    responded: set[str] = set()
    failures: list[dict[str, str]] = []
    try:
        for instrument in instruments:
            bs_code = f"{instrument['provider_symbol'][:2].lower()}.{instrument['code']}"
            completed = True
            for statement_type, metric_map in metric_specs.items():
                try:
                    function = getattr(bs, f"query_{statement_type}_data")
                    query = function(code=bs_code, year=year, quarter=quarter)
                    if query.error_code != "0":
                        failures.append({"source": "baostock_financial", "symbol": instrument["code"], "dataset": statement_type, "message": f"provider error {query.error_code}: {query.error_msg}"})
                        completed = False
                        break
                    responded.add(instrument["code"])
                    if not query.next():
                        continue
                    row = dict(zip(query.fields, query.get_row_data()))
                    report_date = row.get("statDate")
                    if not report_date:
                        continue
                    for provider_metric, metric in metric_map.items():
                        value = pd.to_numeric(pd.Series([row.get(provider_metric)]), errors="coerce").iloc[0]
                        if pd.isna(value):
                            continue
                        records.append({
                            "instrument_id": instrument["instrument_id"],
                            "statement_type": statement_type,
                            "metric": metric,
                            "value": float(value),
                            "currency": "CNY",
                            "report_date": str(report_date),
                            "published_at_date": row.get("pubDate"),
                            "updated_at_date": None,
                            "source_id": "baostock_financial",
                            "source_raw_sha256": None,
                            "observed_at": None,
                        })
                except Exception as exc:  # noqa: BLE001
                    failures.append({"source": "baostock_financial", "symbol": instrument["code"], "dataset": statement_type, "message": f"{type(exc).__name__}: {exc}"})
                    completed = False
                    break
            if completed:
                # A successful empty statement is normal for some newly listed
                # or reorganized companies.  Source health measures successful
                # provider responses, not the presence of every optional fact.
                responded.add(instrument["code"])
    finally:
        bs.logout()
    return records, responded, failures


def lock_job() -> Any:
    path = PROJECT_ROOT / "data" / "runtime" / "low-frequency-update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"pid={__import__('os').getpid()} started_at={utc_now()}\n")
    handle.flush()
    return handle


def open_database(database: Path) -> sqlite3.Connection:
    """Open SQLite with a bounded wait for the concurrent history writer."""
    connection = sqlite3.connect(database, timeout=180)
    connection.execute("PRAGMA busy_timeout=180000")
    return connection


def financial_records(frame: pd.DataFrame, instrument: dict[str, Any], observed_at: str, raw_sha256: str, as_of: pd.Timestamp) -> list[dict[str, Any]]:
    metric_map = {
        "归母净利润": "PARENT_NETPROFIT",
        "营业总收入": "OPERATE_INCOME",
        "经营现金流量净额": "NETCASH_OPERATE",
        "基本每股收益": "BASIC_EPS",
        "每股净资产": "NET_ASSET_PER_SHARE",
        "净资产收益率(ROE)": "ROE",
        "资产负债率": "ASSET_LIABILITY_RATIO",
    }
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        metric = metric_map.get(str(row.get("指标") or "").strip())
        if metric is None:
            continue
        for column, value in row.items():
            if not DATE_COLUMN.match(str(column)):
                continue
            number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
            if pd.isna(number):
                continue
            report_date = pd.Timestamp(str(column)).date().isoformat()
            if pd.Timestamp(report_date) > as_of:
                continue
            key = (instrument["instrument_id"], metric, report_date)
            records.setdefault(key, {
                "instrument_id": instrument["instrument_id"],
                "statement_type": "abstract",
                "metric": metric,
                "value": float(number),
                "currency": "CNY",
                "report_date": report_date,
                "published_at_date": None,
                "updated_at_date": None,
                "source_id": "akshare_eastmoney_financial_abstract",
                "source_raw_sha256": raw_sha256,
                "observed_at": observed_at,
            })
    return list(records.values())


def load_market_batch(connection: sqlite3.Connection, asset_type: str, limit: int) -> list[dict[str, Any]]:
    """Return the next active instruments after a persisted ticker cursor.

    A cursor gives each active stock/ETF a turn even when a provider returns no
    distribution event, which is common for ETFs.  It also avoids repeatedly
    spending the free-source quota on the small original research core.
    """
    if limit <= 0:
        return []
    dataset = f"low_frequency_{asset_type}_cursor"
    row = connection.execute(
        "SELECT last_success_trade_date FROM ingestion_watermarks WHERE dataset=?", (dataset,)
    ).fetchone()
    cursor = str(row[0]) if row and row[0] else ""
    after_cursor_query = """
        SELECT instrument_id, ticker, name, asset_type, exchange
        FROM instruments
        WHERE active=1 AND asset_type=? AND ticker>?
        ORDER BY ticker
        LIMIT ?
    """
    rows = connection.execute(after_cursor_query, (asset_type, cursor, limit)).fetchall()
    if len(rows) < limit:
        wrap_query = """
            SELECT instrument_id, ticker, name, asset_type, exchange
            FROM instruments
            WHERE active=1 AND asset_type=? AND ticker<=?
            ORDER BY ticker
            LIMIT ?
        """
        rows += connection.execute(wrap_query, (asset_type, cursor, limit - len(rows))).fetchall()
    instruments: list[dict[str, Any]] = []
    for instrument_id, ticker, name, item_type, exchange in rows:
        code = str(ticker).split(".", 1)[0]
        prefix = str(exchange).lower()
        instruments.append({
            "instrument_id": str(instrument_id),
            "ticker": str(ticker),
            "name": str(name),
            "asset_type": str(item_type),
            "exchange": str(exchange),
            "code": code,
            "provider_symbol": f"{prefix}{code}",
        })
    return instruments


def advance_market_cursor(connection: sqlite3.Connection, asset_type: str, instruments: list[dict[str, Any]], run_id: str) -> None:
    if not instruments:
        return
    dataset = f"low_frequency_{asset_type}_cursor"
    connection.execute(
        """INSERT INTO ingestion_watermarks(dataset, last_success_trade_date, last_success_run_id, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(dataset) DO UPDATE SET
             last_success_trade_date=excluded.last_success_trade_date,
             last_success_run_id=excluded.last_success_run_id,
             updated_at=excluded.updated_at""",
        (dataset, instruments[-1]["ticker"], run_id, utc_now()),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--as-of-date", default=datetime.now(SH_TZ).date().isoformat())
    parser.add_argument("--stock-batch-size", type=int, default=150)
    parser.add_argument("--etf-batch-size", type=int, default=75)
    args = parser.parse_args()

    lock = lock_job()
    if lock is None:
        print(json.dumps({"status": "skipped", "reason": "another_low_frequency_update_is_running"}, ensure_ascii=False))
        return 0
    try:
        apply(args.database)
        as_of = pd.Timestamp(args.as_of_date)
        with open_database(args.database) as conn:
            stocks = load_market_batch(conn, "stock", args.stock_batch_size)
            etfs = load_market_batch(conn, "etf", args.etf_batch_size)
        now = datetime.now(SH_TZ)
        run_id = f"lowfreq_{as_of.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}"
        run_dir = PROJECT_ROOT / "data" / "raw" / run_id
        health_path = PROJECT_ROOT / "data" / "runtime" / "low-frequency-health.json"
        with open_database(args.database) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT OR IGNORE INTO data_sources(source_id, source_tier, notes) VALUES ('baostock_financial', 'convenience', 'Low-frequency compact financial facts; internal research only.')")
            conn.execute(
                "INSERT INTO ingestion_runs(run_id, trigger_kind, started_at, target_trade_date, status, raw_run_path) VALUES (?, 'low_frequency_schedule', ?, ?, 'running', ?)",
                (run_id, utc_now(), args.as_of_date, str(run_dir.relative_to(PROJECT_ROOT))),
            )
            conn.execute("INSERT INTO low_frequency_batches(run_id, as_of_date, status, stock_symbols_attempted, etf_symbols_attempted) VALUES (?, ?, 'running', ?, ?)", (run_id, args.as_of_date, len(stocks), len(etfs)))
            conn.commit()

        observed_at = now.isoformat()
        failures: list[dict[str, str]] = []
        stock_events: list[dict[str, Any]] = []
        etf_events: list[dict[str, Any]] = []
        financials: list[dict[str, Any]] = []
        stock_div_ok = 0
        financial_ok = 0
        etf_ok = 0

        for instrument in stocks:
            code = instrument["code"]
            try:
                frame = source_call("stock_dividend_cninfo", {"symbol": code})
                raw = write_raw(frame, run_dir, f"dividend_{code}", observed_at)
                stock_events.extend(normalize_stock_dividends(frame, instrument, observed_at, sha256_file(raw), as_of))
                stock_div_ok += 1
            except Exception as exc:  # noqa: BLE001
                failures.append({"source": "akshare_cninfo_convenience", "symbol": code, "dataset": "stock_dividend_events", "message": f"{type(exc).__name__}: {exc}"})

        try:
            financials, _financial_symbols, financial_failures = collect_baostock_financials(stocks, year=as_of.year, quarter=1)
            # A company may legitimately have no extractable current-quarter
            # metric.  The quality gate therefore measures actual provider
            # failures, not whether at least one optional field was returned.
            failed_financial_symbols = {failure["symbol"] for failure in financial_failures}
            financial_ok = len(stocks) - len(failed_financial_symbols)
            failures.extend(financial_failures)
            financial_raw = pd.DataFrame(financials)
            if not financial_raw.empty:
                raw = write_raw(financial_raw, run_dir, "baostock_financial", observed_at)
                raw_hash = sha256_file(raw)
                for row in financials:
                    row["source_raw_sha256"] = raw_hash
                    row["observed_at"] = observed_at
        except Exception as exc:  # noqa: BLE001
            failures.append({"source": "baostock_financial", "symbol": "*", "dataset": "financial_observations", "message": f"{type(exc).__name__}: {exc}"})

        for instrument in etfs:
            try:
                frame = source_call("fund_etf_dividend_sina", {"symbol": instrument["provider_symbol"]})
                raw = write_raw(frame, run_dir, f"etf_distribution_{instrument['code']}", observed_at)
                etf_events.extend(normalize_etf_dividends(frame, instrument, observed_at, sha256_file(raw), as_of))
                etf_ok += 1
            except Exception as exc:  # noqa: BLE001
                failures.append({"source": "akshare_sina_convenience", "symbol": instrument["code"], "dataset": "etf_distribution_events", "message": f"{type(exc).__name__}: {exc}"})

        stock_div_coverage = stock_div_ok / len(stocks) if stocks else 0.0
        financial_coverage = financial_ok / len(stocks) if stocks else 0.0
        etf_coverage = etf_ok / len(etfs) if etfs else 0.0
        checks = [
            ("stock_dividend_source_coverage", stock_div_coverage >= 0.80, {"succeeded": stock_div_ok, "attempted": len(stocks), "ratio": stock_div_coverage}),
            ("financial_source_coverage", financial_coverage >= 0.80, {"succeeded": financial_ok, "attempted": len(stocks), "ratio": financial_coverage}),
            ("etf_distribution_source_coverage", etf_coverage >= 0.80, {"succeeded": etf_ok, "attempted": len(etfs), "ratio": etf_coverage}),
            ("low_frequency_event_stage_nonempty", bool(stock_events or etf_events or financials), {"stock_events": len(stock_events), "etf_events": len(etf_events), "financials": len(financials)}),
        ]
        passed = all(ok for _, ok, _ in checks)
        backup_path = backup_database(args.database, run_id) if passed else None
        manifest_path = run_dir / "autonomous_manifest.json"
        manifest = {"run_id": run_id, "run_type": "autonomous_low_frequency", "research_as_of": observed_at, "as_of_date": args.as_of_date, "principal_cny": 200000, "status": "published" if passed else "quarantined", "row_counts": {"stock_dividend_events": len(stock_events), "etf_distribution_events": len(etf_events), "financial_observations": len(financials)}}
        if passed:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with open_database(args.database) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            for failure in failures:
                conn.execute("INSERT INTO ingestion_failures VALUES (?, ?, ?, 'source_failure', ?, ?)", (run_id, failure["source"], failure["dataset"], failure["message"], utc_now()))
            for source_id, dataset, rows in [
                ("akshare_cninfo_convenience", "low_frequency_stock_dividend", len(stock_events)),
                ("baostock_financial", "low_frequency_financial", len(financials)),
                ("akshare_sina_convenience", "low_frequency_etf_distribution", len(etf_events)),
            ]:
                conn.execute("INSERT INTO source_health VALUES (?, ?, ?, 'success', ?, ?, ?)", (run_id, source_id, dataset, rows, utc_now(), None))
            for name, ok, detail in checks:
                conn.execute("INSERT INTO quality_checks VALUES (?, ?, ?, ?)", (run_id, name, int(ok), json.dumps(detail, ensure_ascii=False)))
            if passed:
                # Dividend and financial tables are versioned by data_runs;
                # create the parent row before inserting child facts.
                conn.execute("INSERT INTO data_runs VALUES (?, 'autonomous_low_frequency', ?, 200000, ?, ?, ?, 'published', ?)", (run_id, observed_at, str(manifest_path.relative_to(PROJECT_ROOT)), sha256_file(manifest_path), sha256_file(manifest_path), utc_now()))
                for row in stock_events:
                    conn.execute("INSERT OR IGNORE INTO stock_dividend_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, row["version_id"], row["dividend_event_id"], row.get("supersedes_version_id"), row["instrument_id"], row.get("profit_period_label"), row.get("distribution_type"), int(row.get("installment_no") or 1), row["status"], float(row["cash_per_10_shares_cny"]) if row.get("cash_per_10_shares_cny") else None, float(row["cash_dps_cny"]) if row.get("cash_dps_cny") else None, row.get("published_at_date"), row.get("record_date"), row.get("ex_date"), row.get("payment_date"), row.get("description"), row["source_id"], row.get("source_raw_sha256"), row.get("observed_at")))
                for row in etf_events:
                    conn.execute("INSERT OR IGNORE INTO etf_distribution_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, row["version_id"], row["dividend_event_id"], row["instrument_id"], row["status"], float(row["cash_per_unit_cny"]), float(row["cumulative_distribution_cny"]), row.get("ex_date"), row.get("date_semantics"), row["source_id"], row.get("source_raw_sha256"), row.get("observed_at")))
                for row in financials:
                    conn.execute("INSERT OR IGNORE INTO financial_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, row["instrument_id"], row["statement_type"], row["metric"], row["value"], row["currency"], row["report_date"], row.get("published_at_date"), row.get("updated_at_date"), row["source_id"], row.get("source_raw_sha256"), row.get("observed_at")))
                conn.execute("INSERT INTO ingestion_watermarks(dataset, last_success_trade_date, last_success_run_id, updated_at) VALUES ('low_frequency_core', ?, ?, ?) ON CONFLICT(dataset) DO UPDATE SET last_success_trade_date=excluded.last_success_trade_date, last_success_run_id=excluded.last_success_run_id, updated_at=excluded.updated_at", (args.as_of_date, run_id, utc_now()))
                advance_market_cursor(conn, "stock", stocks, run_id)
                advance_market_cursor(conn, "etf", etfs, run_id)
                release_id = f"release_{run_id}"
                conn.execute("UPDATE low_frequency_batches SET status='published', dividend_events_inserted=?, etf_events_inserted=?, financial_observations_inserted=?, stock_dividend_symbols_succeeded=?, financial_symbols_succeeded=?, etf_distribution_symbols_succeeded=? WHERE run_id=?", (len(stock_events), len(etf_events), len(financials), stock_div_ok, financial_ok, etf_ok, run_id))
                conn.execute("UPDATE ingestion_runs SET status='published', finished_at=?, release_id=? WHERE run_id=?", (utc_now(), release_id, run_id))
                conn.execute("INSERT INTO database_releases VALUES (?, ?, ?, ?, NULL, 'published')", (release_id, run_id, utc_now(), str(args.database.relative_to(PROJECT_ROOT))))
            else:
                conn.execute("UPDATE low_frequency_batches SET status='quarantined', message=? WHERE run_id=?", ("coverage gate failed", run_id))
                conn.execute("UPDATE ingestion_runs SET status='quarantined', finished_at=?, message=? WHERE run_id=?", (utc_now(), "coverage gate failed", run_id))
            conn.commit()
        if passed:
            with open_database(args.database) as conn:
                conn.execute("UPDATE database_releases SET database_sha256=?", (canonical_database_sha256(args.database),))
                conn.commit()
        health = {"run_id": run_id, "status": "published" if passed else "quarantined", "as_of_date": args.as_of_date, "checks": [{"name": name, "passed": ok, "details": detail} for name, ok, detail in checks], "failures": failures, "backup": None if backup_path is None else str(backup_path.relative_to(PROJECT_ROOT))}
        write_health(health_path, health)
        print(json.dumps(health, ensure_ascii=False, indent=2))
        if not passed:
            return 2
        publisher = PROJECT_ROOT / "scripts" / "publish_release.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(publisher),
                "--runtime-root", str(PROJECT_ROOT),
                "--database", str(args.database),
                "--release-id", f"release_{run_id}",
                "--available-cutoff", f"{args.as_of_date}T15:00:00+08:00",
            ],
            text=True,
            check=False,
        )
        return completed.returncode
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
