#!/usr/bin/env python3
"""Backfill market-wide stock cash-dividend events in bounded, auditable periods.

Eastmoney's market-wide ``stock_fhps_em`` endpoint returns a report-period
table, so this job avoids one request per stock.  It is a collection layer,
not primary-source verification: raw responses and hashes are retained, and
only completed periods pass the batch gate into the runtime source database.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
SOURCE_ID = "akshare_eastmoney_stock_fhps_market"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso_date(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).date().isoformat()


def number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(str(value).replace(",", "").replace("--", ""))
    except (TypeError, ValueError):
        return None


def event_id(instrument_id: str, period: str, cash_ratio: float, announced: str | None, record_date: str | None, ex_date: str | None) -> str:
    raw = "|".join([instrument_id, period, f"{cash_ratio:.10g}", announced or "", record_date or "", ex_date or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def acquire_lock(runtime: Path):
    path = runtime / "dividend-history.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("Another dividend history backfill is running") from exc
    return handle


def period_labels(start_year: int, end_year: int, include_interim: bool) -> list[str]:
    labels: list[str] = []
    for year in range(start_year, end_year + 1):
        labels.append(f"{year}1231")
        if include_interim:
            labels.append(f"{year}0630")
    return labels


def progress_bar(done: int, total: int, width: int = 28) -> str:
    ratio = done / total if total else 1.0
    filled = int(round(width * ratio))
    return f"[{'█' * filled}{'░' * (width - filled)}] {ratio * 100:5.1f}%"


def normalize_period(frame: pd.DataFrame, period: str, instrument_by_code: dict[str, dict[str, str]], cutoff: str, raw_sha256: str, observed_at: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    period_year = period[:4]
    distribution_type = "年度现金分红" if period.endswith("1231") else "中期现金分红"
    for row in frame.to_dict(orient="records"):
        code = str(row.get("代码") or "").strip().zfill(6)
        instrument = instrument_by_code.get(code)
        if instrument is None:
            continue
        cash_ratio = number(row.get("现金分红-现金分红比例"))
        if cash_ratio is None or cash_ratio <= 0:
            continue
        announced = iso_date(row.get("预案公告日"))
        record_date = iso_date(row.get("股权登记日"))
        ex_date = iso_date(row.get("除权除息日"))
        if announced and announced > cutoff:
            continue
        if ex_date and ex_date > cutoff:
            continue
        dps = cash_ratio / 10.0
        status_text = str(row.get("方案进度") or "")
        status = "implemented" if ex_date and ("实施" in status_text or ex_date <= cutoff) else "implementation_announced"
        logical_id = event_id(instrument["instrument_id"], period, cash_ratio, announced, record_date, ex_date)
        records.append({
            "instrument_id": instrument["instrument_id"],
            "dividend_event_id": logical_id,
            "version_id": hashlib.sha256(f"{logical_id}|{raw_sha256}".encode("utf-8")).hexdigest(),
            "profit_period_label": period_year + ("年度" if period.endswith("1231") else "中期"),
            "distribution_type": distribution_type,
            "status": status,
            "cash_per_10_shares_cny": cash_ratio,
            "cash_dps_cny": dps,
            "published_at_date": announced,
            "record_date": record_date,
            "ex_date": ex_date,
            "payment_date": None,
            "description": f"market-period={period}; progress={status_text}",
            "source_raw_sha256": raw_sha256,
            "observed_at": observed_at,
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--include-interim", action="store_true", help="Also collect June 30 report-period tables")
    parser.add_argument("--minimum-success-ratio", type=float, default=0.80)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser()
    database = runtime / "data" / "database" / "high_dividend.db"
    if not database.is_file():
        raise SystemExit(f"Runtime database does not exist: {database}")
    if args.start_year > args.end_year:
        raise SystemExit("--start-year cannot exceed --end-year")
    periods = period_labels(args.start_year, args.end_year, args.include_interim)
    run_id = args.run_id or f"dividend_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    manifest_path = runtime / "data" / "manifests" / f"{run_id}.json"
    raw_dir = runtime / "data" / "raw" / "market_dividends" / run_id
    if manifest_path.exists() or raw_dir.exists():
        raise SystemExit(f"Run already exists and will not be overwritten: {run_id}")
    lock = acquire_lock(runtime)
    observed_at = utc_now()
    try:
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT instrument_id,ticker,name FROM instruments WHERE asset_type='stock' AND active=1").fetchall()
            instrument_by_code = {str(ticker).split(".", 1)[0]: {"instrument_id": str(instrument_id), "ticker": str(ticker), "name": str(name)} for instrument_id, ticker, name in rows}
            existing = {
                (str(instrument_id), str(ex_date), round(float(dps), 10))
                for instrument_id, ex_date, dps in connection.execute("SELECT instrument_id,ex_date,cash_dps_cny FROM stock_dividend_events WHERE ex_date IS NOT NULL AND cash_dps_cny IS NOT NULL")
            }
            cutoff_row = connection.execute("SELECT MAX(trade_date) FROM market_daily_prices WHERE validation_status='approved'").fetchone()
        cutoff = str(cutoff_row[0] or datetime.now().date().isoformat())
        raw_dir.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, Any] = {
            "schema_version": "market-dividend-history-v1",
            "run_id": run_id,
            "source_id": SOURCE_ID,
            "source_role": "convenience_collection_layer",
            "license_scope": "internal_research_only_pending_terms_verification",
            "observed_at": observed_at,
            "cutoff": cutoff,
            "periods_requested": periods,
            "periods_succeeded": [],
            "raw_files": [],
            "failures": [],
            "rows_collected": 0,
            "rows_inserted": 0,
        }
        records: list[dict[str, Any]] = []
        for index, period in enumerate(periods, start=1):
            print(f"{progress_bar(index - 1, len(periods))} 采集报告期 {period}", flush=True)
            try:
                frame = ak.stock_fhps_em(date=period)
                if not isinstance(frame, pd.DataFrame):
                    raise TypeError(f"stock_fhps_em returned {type(frame).__name__}")
                raw_path = raw_dir / f"stock_fhps_{period}.csv"
                frame.to_csv(raw_path, index=False, encoding="utf-8")
                raw_hash = sha256_file(raw_path)
                raw_entry = {"period": period, "path": str(raw_path.relative_to(runtime)), "sha256": raw_hash, "rows": len(frame)}
                manifest["raw_files"].append(raw_entry)
                manifest["periods_succeeded"].append(period)
                normalized = normalize_period(frame, period, instrument_by_code, cutoff, raw_hash, observed_at)
                records.extend(normalized)
                manifest["rows_collected"] += len(normalized)
                print(f"{progress_bar(index, len(periods))} 完成 {period}，事件 {len(normalized)}", flush=True)
            except Exception as exc:  # noqa: BLE001
                manifest["failures"].append({"period": period, "error": f"{type(exc).__name__}: {exc}"})
                print(f"{progress_bar(index, len(periods))} 失败 {period}: {exc}", flush=True)
        success_ratio = len(manifest["periods_succeeded"]) / len(periods) if periods else 1.0
        manifest["quality"] = {"periods_succeeded": len(manifest["periods_succeeded"]), "periods_requested": len(periods), "success_ratio": round(success_ratio, 6), "minimum_success_ratio": args.minimum_success_ratio}
        passed = success_ratio >= args.minimum_success_ratio and bool(records)
        manifest["status"] = "staged" if passed else "quarantined"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not passed:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 2

        backup_path = runtime / "data" / "database" / "releases" / f"{run_id}.before.db"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as source, sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        unique: dict[tuple[str, str, float], dict[str, Any]] = {}
        for row in records:
            key = (row["instrument_id"], row.get("ex_date") or row["dividend_event_id"], round(float(row["cash_dps_cny"]), 10))
            if (row["instrument_id"], row.get("ex_date"), round(float(row["cash_dps_cny"]), 10)) in existing:
                continue
            unique[key] = row
        manifest["rows_inserted"] = len(unique)
        manifest["status"] = "published"
        manifest["backup_path"] = str(backup_path.relative_to(runtime))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_hash = sha256_file(manifest_path)
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT OR IGNORE INTO data_sources(source_id,source_tier,notes) VALUES (?, 'convenience', ?)", (SOURCE_ID, "Market-wide stock dividend history; internal research only pending terms verification."))
            connection.execute("INSERT INTO data_runs VALUES (?, 'market_dividend_history_backfill', ?, 200000, ?, ?, ?, 'published', ?)", (run_id, cutoff, str(manifest_path.relative_to(runtime)), manifest_hash, manifest_hash, utc_now()))
            for row in unique.values():
                connection.execute(
                    """INSERT OR IGNORE INTO stock_dividend_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, row["version_id"], row["dividend_event_id"], None, row["instrument_id"], row["profit_period_label"], row["distribution_type"], 1, row["status"], row["cash_per_10_shares_cny"], row["cash_dps_cny"], row["published_at_date"], row["record_date"], row["ex_date"], row["payment_date"], row["description"], SOURCE_ID, row["source_raw_sha256"], row["observed_at"]),
                )
            connection.commit()
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
