#!/usr/bin/env python3
"""Resumable, auditable historical daily-price backfill for the local runtime.

The job deliberately works in bounded batches.  It stages provider responses
under the runtime raw directory, applies a batch coverage gate, and only then
inserts approved rows into ``price_daily``.  It never edits the immutable
release directly; run ``publish_release.py`` after a successful batch.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
SOURCE_IDS = {
    "stock": "baostock_history_backfill",
    "etf": "akshare_sina_etf_history_backfill",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed


def provider_code(ticker: str) -> str:
    code, exchange = ticker.split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper())
    if prefix is None:
        raise ValueError(f"Unsupported exchange for BaoStock: {ticker}")
    return f"{prefix}.{code}"


def sina_code(ticker: str) -> str:
    return provider_code(ticker).replace(".", "", 1)


def collect_baostock_daily(ticker: str, start_date: str, end_date: str, bs_module: Any | None = None) -> list[dict[str, Any]]:
    """Fetch one ticker using an already-authenticated BaoStock session when supplied.

    A batch should keep one login for all tickers. Logging in and out for every
    security is both slow and more likely to trigger the provider's session
    throttling; callers that pass ``bs_module`` own that session lifecycle.
    """
    import baostock as bs

    session_owned = bs_module is None
    client = bs_module or bs
    if session_owned:
        login = client.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        query = client.query_history_k_data_plus(
            provider_code(ticker),
            "date,code,open,high,low,close,volume,amount,adjustflag",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3",
        )
        if query.error_code != "0":
            raise RuntimeError(f"BaoStock query failed: {query.error_msg}")
        rows: list[dict[str, Any]] = []
        while query.next():
            values = dict(zip(query.fields, query.get_row_data()))
            close = number(values.get("close"))
            trade_date = str(values.get("date") or "")[:10]
            if not trade_date or close is None or close <= 0:
                continue
            rows.append({
                "trade_date": trade_date,
                "open": number(values.get("open")),
                "high": number(values.get("high")),
                "low": number(values.get("low")),
                "close": close,
                "volume": number(values.get("volume")),
                "turnover_cny": number(values.get("amount")),
                "adjustment": "unadjusted",
            })
        return rows
    finally:
        if session_owned:
            client.logout()


def collect_sina_etf_daily(ticker: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    import akshare as ak

    frame = ak.fund_etf_hist_sina(symbol=sina_code(ticker))
    if frame is None or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for values in frame.to_dict(orient="records"):
        trade_date = str(values.get("date") or "")[:10]
        if not trade_date or trade_date < start_date or trade_date > end_date:
            continue
        close = number(values.get("close"))
        if close is None or close <= 0:
            continue
        rows.append({
            "trade_date": trade_date,
            "open": number(values.get("open")),
            "high": number(values.get("high")),
            "low": number(values.get("low")),
            "close": close,
            "volume": number(values.get("volume")),
            "turnover_cny": number(values.get("amount")),
            "adjustment": "unadjusted",
        })
    rows.sort(key=lambda row: row["trade_date"])
    return rows


def write_raw(path: Path, instrument: dict[str, str], rows: list[dict[str, Any]], observed_at: str, source_id: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["trade_date", "open", "high", "low", "close", "volume", "turnover_cny", "adjustment"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "schema_version": "historical-price-raw-v1",
        "instrument_id": instrument["instrument_id"],
        "ticker": instrument["ticker"],
        "source_id": source_id,
        "source_role": "convenience_collection_layer",
        "license_scope": "internal_research_only_pending_terms_verification",
        "observed_at": observed_at,
        "start_date": rows[0]["trade_date"] if rows else None,
        "end_date": rows[-1]["trade_date"] if rows else None,
        "row_count": len(rows),
        "content_sha256": sha256_file(path),
    }
    path.with_suffix(".meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata["content_sha256"]


def select_instruments(connection: sqlite3.Connection, asset_type: str, minimum_existing_days: int, start_date: str, offset: int, limit: int, dividend_only: bool = False) -> list[dict[str, str]]:
    existing = {
        row[0]: (int(row[1]), str(row[2]) if row[2] else None)
        for row in connection.execute(
            "SELECT instrument_id, COUNT(DISTINCT trade_date), MIN(trade_date) FROM price_daily WHERE adjustment='unadjusted' GROUP BY instrument_id"
        )
    }
    query = "SELECT instrument_id, ticker, name, asset_type FROM instruments WHERE asset_type=? AND active=1"
    params: list[Any] = [asset_type]
    if dividend_only and asset_type == "stock":
        query += " AND EXISTS (SELECT 1 FROM stock_dividend_events d WHERE d.instrument_id=instruments.instrument_id AND d.status='implemented')"
    query += " ORDER BY instrument_id"
    rows = connection.execute(query, params).fetchall()
    candidates = [
        {"instrument_id": str(row[0]), "ticker": str(row[1]), "name": str(row[2]), "asset_type": str(row[3])}
        for row in rows
        if (
            row[0] not in existing
            or existing[row[0]][0] == 0
            or (
                existing[row[0]][0] < minimum_existing_days
                and existing[row[0]][1] is not None
                and existing[row[0]][1] <= start_date
            )
        )
    ]
    return candidates[offset : offset + limit]


def acquire_lock(runtime_root: Path):
    path = runtime_root / "history-price.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("Another historical price backfill is running") from exc
    return handle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--asset-type", choices=("stock", "etf"), default="stock")
    parser.add_argument("--start-date", default="2019-01-01")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD; must not be a future trading date")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--minimum-existing-days", type=int, default=1000)
    parser.add_argument("--dividend-only", action="store_true", help="For stocks, prioritize instruments with an implemented dividend event.")
    parser.add_argument("--minimum-success-ratio", type=float, default=0.80)
    parser.add_argument("--sleep-seconds", type=float, default=0.20)
    parser.add_argument("--workers", type=int, default=6, help="ETF并发请求数；股票仍保持单会话串行")
    parser.add_argument("--run-id")
    args = parser.parse_args()

    runtime = args.runtime_root.expanduser()
    database = runtime / "data" / "database" / "high_dividend.db"
    if not database.is_file():
        raise SystemExit(f"Runtime database does not exist: {database}")
    if args.limit <= 0 or args.offset < 0 or args.workers <= 0:
        raise SystemExit("--limit must be positive and --offset cannot be negative")
    run_id = args.run_id or f"history_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.asset_type}_{args.offset:05d}_{args.offset + args.limit:05d}"
    source_id = SOURCE_IDS[args.asset_type]
    raw_dir = runtime / "data" / "raw" / "historical_prices" / run_id
    manifest_path = runtime / "data" / "manifests" / f"{run_id}.json"
    if raw_dir.exists() or manifest_path.exists():
        raise SystemExit(f"Run already exists and will not be overwritten: {run_id}")

    lock = acquire_lock(runtime)
    observed_at = utc_now()
    raw_dir.mkdir(parents=True, exist_ok=False)
    try:
        with sqlite3.connect(database) as connection:
            selected = select_instruments(connection, args.asset_type, args.minimum_existing_days, args.start_date, args.offset, args.limit, args.dividend_only)
        if not selected:
            payload = {"run_id": run_id, "status": "nothing_to_do", "asset_type": args.asset_type, "selected": 0}
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        success: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        raw_files: list[dict[str, Any]] = []
        bs_module = None
        if args.asset_type == "stock":
            import baostock as bs_module
            login = bs_module.login()
            if login.error_code != "0":
                raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        def fetch_one(instrument: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
            try:
                if args.asset_type == "stock":
                    rows = collect_baostock_daily(instrument["ticker"], args.start_date, args.end_date, bs_module)
                else:
                    rows = collect_sina_etf_daily(instrument["ticker"], args.start_date, args.end_date)
                return rows, None
            except Exception as exc:  # noqa: BLE001
                return [], f"{type(exc).__name__}: {exc}"

        def record_result(index: int, instrument: dict[str, str], rows: list[dict[str, Any]], error: str | None) -> None:
            print(f"[{index}/{len(selected)}] {instrument['ticker']} {instrument['name']}", flush=True)
            if error is not None:
                failures.append({"instrument_id": instrument["instrument_id"], "ticker": instrument["ticker"], "error": error})
                return
            try:
                unique_dates = {row["trade_date"] for row in rows}
                if len(unique_dates) != len(rows):
                    raise RuntimeError("duplicate trade dates returned")
                if not rows:
                    raise RuntimeError("provider returned no valid rows")
                raw_path = raw_dir / f"{instrument['instrument_id']}.csv"
                content_hash = write_raw(raw_path, instrument, rows, observed_at, source_id)
                raw_files.append({"instrument_id": instrument["instrument_id"], "path": str(raw_path.relative_to(runtime)), "sha256": content_hash, "rows": len(rows)})
                success.append({"instrument": instrument, "rows": rows, "raw_sha256": content_hash})
            except Exception as exc:  # noqa: BLE001
                failures.append({"instrument_id": instrument["instrument_id"], "ticker": instrument["ticker"], "error": f"{type(exc).__name__}: {exc}"})

        try:
            if args.asset_type == "etf" and args.workers > 1:
                # ETF requests are independent I/O calls. Keep concurrency bounded
                # to avoid overwhelming the free endpoint or triggering bans.
                with ThreadPoolExecutor(max_workers=args.workers) as executor:
                    for index, (instrument, result) in enumerate(zip(selected, executor.map(fetch_one, selected)), start=1):
                        record_result(index, instrument, *result)
                        if args.sleep_seconds > 0:
                            time.sleep(args.sleep_seconds)
            else:
                for index, instrument in enumerate(selected, start=1):
                    record_result(index, instrument, *fetch_one(instrument))
                    if args.sleep_seconds > 0:
                        time.sleep(args.sleep_seconds)
        finally:
            if bs_module is not None:
                bs_module.logout()

        success_ratio = len(success) / len(selected)
        quality = {
            "selected": len(selected),
            "succeeded": len(success),
            "failed": len(failures),
            "success_ratio": round(success_ratio, 6),
            "minimum_success_ratio": args.minimum_success_ratio,
            "rows_collected": sum(len(item["rows"]) for item in success),
            "date_bounds": {"requested_start": args.start_date, "requested_end": args.end_date},
        }
        passed = success_ratio >= args.minimum_success_ratio
        manifest: dict[str, Any] = {
            "schema_version": "historical-price-backfill-v1",
            "run_id": run_id,
            "asset_type": args.asset_type,
            "source_id": source_id,
            "source_role": "convenience_collection_layer",
            "license_scope": "internal_research_only_pending_terms_verification",
            "observed_at": observed_at,
            "requested": {"start_date": args.start_date, "end_date": args.end_date, "offset": args.offset, "limit": args.limit, "minimum_existing_days": args.minimum_existing_days},
            "quality": quality,
            "raw_files": raw_files,
            "failures": failures,
            "status": "staged" if passed else "quarantined",
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not passed:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 2

        backup_path = runtime / "data" / "database" / "releases" / f"{run_id}.before.db"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as source, sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        instrument_ids = [item["instrument"]["instrument_id"] for item in success]
        placeholders = ",".join("?" for _ in instrument_ids)
        with sqlite3.connect(database) as connection:
            existing_keys = set(
                connection.execute(
                    f"SELECT instrument_id, trade_date, adjustment FROM price_daily WHERE instrument_id IN ({placeholders})",
                    instrument_ids,
                ).fetchall()
            )
        inserted = sum(
            1
            for item in success
            for row in item["rows"]
            if (item["instrument"]["instrument_id"], row["trade_date"], row["adjustment"]) not in existing_keys
        )
        manifest["status"] = "published"
        manifest["quality"]["rows_inserted"] = inserted
        manifest["backup_path"] = str(backup_path.relative_to(runtime))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_sha = sha256_file(manifest_path)
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT OR IGNORE INTO data_sources(source_id, source_tier, notes) VALUES (?, 'convenience', ?)", (source_id, "Historical daily prices; internal research only pending source terms verification."))
            connection.execute(
                "INSERT INTO data_runs VALUES (?, 'historical_price_backfill', ?, 200000, ?, ?, ?, 'published', ?)",
                (run_id, args.end_date, str(manifest_path.relative_to(runtime)), manifest_sha, manifest_sha, utc_now()),
            )
            for item in success:
                instrument_id = item["instrument"]["instrument_id"]
                for row in item["rows"]:
                    if (instrument_id, row["trade_date"], row["adjustment"]) in existing_keys:
                        continue
                    connection.execute(
                        """INSERT INTO price_daily(run_id,instrument_id,trade_date,adjustment,source_id,source_raw_sha256,observed_at,open,high,low,close,volume,turnover_cny)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, instrument_id, row["trade_date"], row["adjustment"], source_id, item["raw_sha256"], observed_at, row["open"], row["high"], row["low"], row["close"], row["volume"], row["turnover_cny"]),
                    )
            connection.commit()
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
