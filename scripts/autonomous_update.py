#!/usr/bin/env python3
"""Low-frequency, local-only market data updater with cross-source gating.

This program never publishes partial data: source failures or disagreement
leave the last approved database state in place and create a quarantine run.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from apply_migrations import apply


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "autonomous_update.json"
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "database" / "high_dividend.db"
SH_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_database_sha256(database: Path) -> str:
    """Hash a release while ignoring self-referential audit columns."""
    temporary = Path(tempfile.mktemp(prefix="highdividend-canonical-", suffix=".db"))
    try:
        with sqlite3.connect(database) as source, sqlite3.connect(temporary) as destination:
            source.backup(destination)
            destination.execute("UPDATE database_releases SET database_sha256=NULL")
            destination.commit()
        return sha256_file(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def call_adapter_worker(adapter: str, queue: Any, params: dict[str, Any] | None = None) -> None:
    """Run one third-party call in a killable child process."""
    params = params or {}
    try:
        import akshare as ak

        functions = {
            "stock_info_a_code_name": ak.stock_info_a_code_name,
            "stock_zh_a_spot": ak.stock_zh_a_spot,
            "stock_zh_a_spot_em": ak.stock_zh_a_spot_em,
            "fund_etf_spot_em": ak.fund_etf_spot_em,
            "fund_etf_spot_ths": ak.fund_etf_spot_ths,
            "tool_trade_date_hist_sina": ak.tool_trade_date_hist_sina,
        }
        if adapter == "baostock_all_stock":
            import baostock as bs

            login = bs.login()
            if login.error_code != "0":
                raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
            try:
                query = bs.query_all_stock(day=params["day"])
                if query.error_code != "0":
                    raise RuntimeError(f"BaoStock query_all_stock failed: {query.error_msg}")
                rows = []
                while query.next():
                    rows.append(query.get_row_data())
                frame = pd.DataFrame(rows, columns=["code", "status", "name"])
            finally:
                bs.logout()
        elif adapter == "tencent_batch_quote":
            frame = fetch_tencent_quotes(params["codes"], int(params.get("batch_size", 100)))
        elif adapter == "stock_dividend_cninfo":
            frame = ak.stock_dividend_cninfo(symbol=params["symbol"])
        elif adapter == "fund_etf_dividend_sina":
            frame = ak.fund_etf_dividend_sina(symbol=params["symbol"])
        elif adapter == "stock_financial_abstract":
            frame = ak.stock_financial_abstract(symbol=params["symbol"])
        else:
            frame = functions[adapter]()
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(f"{adapter} returned {type(frame).__name__}, expected DataFrame")
        queue.put({"ok": True, "csv": frame.to_csv(index=False), "columns": list(frame.columns)})
    except Exception as exc:  # noqa: BLE001
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def run_adapter(adapter: str, timeout_seconds: int, params: dict[str, Any] | None = None) -> pd.DataFrame:
    context = mp.get_context("spawn")
    queue: Any = context.Queue()
    process = context.Process(target=call_adapter_worker, args=(adapter, queue, params or {}))
    process.start()
    try:
        result = queue.get(timeout=timeout_seconds)
    except Empty:
        process.terminate()
        process.join(timeout=5)
        raise TimeoutError(f"{adapter} exceeded {timeout_seconds}s")
    finally:
        if process.is_alive():
            process.join(timeout=5)
        if process.is_alive():
            process.terminate()
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return pd.read_csv(io.StringIO(result["csv"]), dtype=str)


def fetch_tencent_quotes(codes: list[str], batch_size: int = 100) -> pd.DataFrame:
    """Fetch one end-of-day snapshot from Tencent's public quote endpoint.

    The endpoint is used at low frequency and only for local research.  A
    caller-provided list keeps requests bounded and makes the raw input
    reproducible.
    """
    rows: list[dict[str, Any]] = []
    for start in range(0, len(codes), max(1, batch_size)):
        batch = codes[start : start + max(1, batch_size)]
        url = "http://qt.gtimg.cn/q=" + ",".join(batch)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("gbk", "ignore")
        for line in payload.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values = value.strip().strip('"').split("~")
            if len(values) < 35 or not values[2]:
                continue
            rows.append({
                "market_key": key.removeprefix("v_"),
                "name": values[1],
                "code": values[2],
                "最新价": values[3],
                "昨收": values[4],
                "今开": values[5],
                "成交量": values[6],
                "最高": values[33],
                "最低": values[34],
                "成交额": values[37] if len(values) > 37 else "",
                "quote_timestamp": values[30],
            })
    return pd.DataFrame(rows)


def clean_code(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    code = str(value).strip().upper().replace(".0", "")
    if "." in code:
        code = code.split(".", 1)[0]
    return code.zfill(6) if code.isdigit() and len(code) <= 6 else None


def first_value(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, "") and not pd.isna(value):
            return value
    return None


def numeric(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).replace(",", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


def exchange_for(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def instrument_id(code: str, exchange: str) -> str:
    market = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}[exchange]
    return f"CN.{market}.{code}"


def normalize_master(frame: pd.DataFrame, asset_type: str, source_id: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for row in frame.to_dict(orient="records"):
        code = clean_code(first_value(row, ("代码", "code", "symbol")))
        name = first_value(row, ("名称", "name", "简称"))
        if code is None or name is None:
            continue
        exchange = exchange_for(code)
        records.append({
            "instrument_id": instrument_id(code, exchange),
            "ticker": f"{code}.{exchange}",
            "name": str(name).strip(),
            "asset_type": asset_type,
            "exchange": exchange,
            "source_id": source_id,
        })
    return records


def normalize_baostock_master(frame: pd.DataFrame, source_id: str) -> list[dict[str, str]]:
    """Keep listed A shares and ETFs from BaoStock's all-stock snapshot.

    BaoStock also returns index symbols.  They are intentionally excluded
    from the security master because the database's market fact layer is for
    tradable stocks and ETFs; index methodology remains a separate dataset.
    """
    stock_prefixes = (
        "000", "001", "002", "003", "300", "301", "600", "601", "603",
        "605", "688", "689",
    )
    records: list[dict[str, str]] = []
    for row in frame.to_dict(orient="records"):
        raw_code = str(first_value(row, ("code", "代码")) or "").strip().lower()
        if "." not in raw_code:
            continue
        exchange_prefix, raw_symbol = raw_code.split(".", 1)
        code = clean_code(raw_symbol)
        name = first_value(row, ("name", "名称", "简称"))
        status = str(first_value(row, ("status", "状态")) or "").strip()
        if code is None or not name or status not in {"1", "上市", "active"}:
            continue
        if code.startswith(stock_prefixes):
            asset_type = "stock"
        elif code.startswith("159") or "ETF" in str(name).upper():
            asset_type = "etf"
        else:
            continue
        exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(exchange_prefix)
        if exchange is None:
            continue
        records.append({
            "instrument_id": instrument_id(code, exchange),
            "ticker": f"{code}.{exchange}",
            "name": str(name).strip(),
            "asset_type": asset_type,
            "exchange": exchange,
            "source_id": source_id,
        })
    unique = {row["instrument_id"]: row for row in records}
    return list(unique.values())


def normalize_prices(frame: pd.DataFrame, asset_type: str, source_id: str) -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        code = clean_code(first_value(row, ("代码", "code", "symbol")))
        close = numeric(first_value(row, ("最新价", "trade", "最新", "现价", "close")))
        if code is None or close is None:
            continue
        exchange = exchange_for(code)
        prices[instrument_id(code, exchange)] = {
            "instrument_id": instrument_id(code, exchange),
            "ticker": f"{code}.{exchange}",
            "name": str(first_value(row, ("名称", "name", "简称")) or code).strip(),
            "asset_type": asset_type,
            "exchange": exchange,
            "close": close,
            "open": numeric(first_value(row, ("今开", "open"))),
            "high": numeric(first_value(row, ("最高", "high"))),
            "low": numeric(first_value(row, ("最低", "low"))),
            "volume": numeric(first_value(row, ("成交量", "volume"))),
            "turnover_cny": numeric(first_value(row, ("成交额", "amount", "turnover"))),
            "source_id": source_id,
        }
    return prices


def normalize_tencent_prices(
    frame: pd.DataFrame,
    master_by_ticker: dict[str, dict[str, str]],
    source_id: str,
) -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        code = clean_code(row.get("code"))
        market_key = str(row.get("market_key") or "").lower()
        exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(market_key[:2])
        if code is None or exchange is None:
            continue
        ticker = f"{code}.{exchange}"
        master = master_by_ticker.get(ticker)
        close = numeric(row.get("最新价"))
        if master is None or close is None:
            continue
        quote_timestamp = str(row.get("quote_timestamp") or "").strip()
        prices[master["instrument_id"]] = {
            **master,
            "close": close,
            "open": numeric(row.get("今开")),
            "high": numeric(row.get("最高")),
            "low": numeric(row.get("最低")),
            "volume": numeric(row.get("成交量")),
            # Tencent reports field 37 in ten-thousand CNY units.
            "turnover_cny": (numeric(row.get("成交额")) or 0.0) * 10000 or None,
            "quote_timestamp": quote_timestamp,
            "quote_trade_date": quote_timestamp[:8] if len(quote_timestamp) >= 8 else None,
            "source_id": source_id,
        }
    return prices


def write_raw(frame: pd.DataFrame, run_dir: Path, source_id: str, observed_at: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    data_path = run_dir / f"{source_id}.csv"
    frame.to_csv(data_path, index=False, encoding="utf-8")
    meta_path = data_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps({
        "source_id": source_id,
        "observed_at": observed_at,
        "rows": len(frame),
        "columns": list(frame.columns),
        "sha256": sha256_file(data_path),
        "collection_scope": "local_low_frequency_research_only",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data_path


def compare_prices(primary: dict[str, dict[str, Any]], validator: dict[str, dict[str, Any]], config: dict[str, Any], label: str) -> list[tuple[str, bool, str]]:
    overlap = set(primary) & set(validator)
    base = min(len(primary), len(validator))
    overlap_ratio = len(overlap) / base if base else 0.0
    threshold = float(config["maximum_price_difference_ratio"])
    mismatches = [
        iid for iid in overlap
        if abs(primary[iid]["close"] - validator[iid]["close"]) / max(primary[iid]["close"], validator[iid]["close"]) > threshold
    ]
    mismatch_ratio = len(mismatches) / len(overlap) if overlap else 1.0
    return [
        (
            f"{label}_cross_source_overlap",
            overlap_ratio >= float(config["minimum_cross_source_overlap"]),
            json.dumps({"overlap": len(overlap), "ratio": overlap_ratio, "primary": len(primary), "validator": len(validator)}),
        ),
        (
            f"{label}_cross_source_price_match",
            mismatch_ratio <= float(config["maximum_price_mismatch_ratio"]),
            json.dumps({"mismatch": len(mismatches), "ratio": mismatch_ratio, "threshold": threshold}),
        ),
    ]


def collect_baostock_sample(primary: dict[str, dict[str, Any]], trade_date: str) -> dict[str, dict[str, Any]]:
    """Validate a small deterministic sample without making one call per A share."""
    import baostock as bs

    sample_size = min(40, len(primary))
    identifiers = sorted(primary)
    step = max(1, len(identifiers) // sample_size)
    sample = identifiers[::step][:sample_size]
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    output: dict[str, dict[str, Any]] = {}
    try:
        for iid in sample:
            values = primary[iid]
            code = values["ticker"].split(".", 1)[0]
            prefix = "sh" if values["exchange"] == "SH" else "sz" if values["exchange"] == "SZ" else "bj"
            query = bs.query_history_k_data_plus(f"{prefix}.{code}", "date,code,close", start_date=trade_date, end_date=trade_date, frequency="d", adjustflag="3")
            if query.error_code != "0" or not query.next():
                continue
            row = query.get_row_data()
            close = numeric(row[2])
            if close is not None:
                output[iid] = {**values, "close": close, "source_id": "baostock_a_share_sample"}
    finally:
        bs.logout()
    minimum = max(10, int(sample_size * 0.5))
    if len(output) < minimum:
        raise RuntimeError(f"BaoStock sample coverage too low: {len(output)}/{sample_size}")
    return output


def optional_source(config: dict[str, Any], role: str) -> dict[str, Any] | None:
    return next((item for item in config["sources"] if item["role"] == role and item["enabled"]), None)


def source_by_role(config: dict[str, Any], role: str) -> dict[str, Any]:
    return next(item for item in config["sources"] if item["role"] == role and item["enabled"])


def write_health(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def acquire_lock() -> Any:
    lock_path = PROJECT_ROOT / "data" / "runtime" / "daily-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"pid={os.getpid()} started_at={utc_now()}\n")
    handle.flush()
    return handle


def backup_database(database: Path, run_id: str) -> Path:
    backup_dir = PROJECT_ROOT / "data" / "database" / "releases"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{run_id}.before.db"
    with sqlite3.connect(database) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    return backup_path


def should_run(now: datetime, config: dict[str, Any], force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced"
    if now.weekday() >= 5:
        return False, "weekend"
    close_time = now.replace(hour=int(config["market_close_hour"]), minute=int(config["market_close_minute"]), second=0, microsecond=0)
    if now < close_time:
        return False, "before_close"
    return True, "weekday_after_close"


def latest_trade_date(frame: pd.DataFrame, now: datetime) -> str:
    for column in ("trade_date", "日期", "date"):
        if column not in frame.columns:
            continue
        values = pd.to_datetime(frame[column], errors="coerce")
        values = values[values.dt.date <= now.date()]
        if not values.empty:
            return values.max().date().isoformat()
    raise ValueError("trade calendar has no usable date column")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Allow a manual run outside the normal trading-day window.")
    parser.add_argument("--target-trade-date", help="YYYY-MM-DD; required together with --force when manually publishing a historical date.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    now = datetime.now(SH_TZ)
    allowed, reason = should_run(now, config, args.force)
    plan = {"now": now.isoformat(), "allowed": allowed, "reason": reason, "sources": [{"id": item["source_id"], "role": item["role"], "enabled": item["enabled"]} for item in config["sources"] if item["enabled"]]}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not allowed:
        print(json.dumps({**plan, "status": "skipped"}, ensure_ascii=False))
        return 0

    lock = acquire_lock()
    if lock is None:
        print(json.dumps({**plan, "status": "skipped", "reason": "another_update_is_running"}, ensure_ascii=False))
        return 0
    try:
        calendar_source = source_by_role(config, "trade_calendar")
        calendar_frame = run_adapter(calendar_source["adapter"], int(config["source_timeout_seconds"]))
        target_trade_date = args.target_trade_date or latest_trade_date(calendar_frame, now)
        if args.force and args.target_trade_date is None:
            raise ValueError("--force requires --target-trade-date so a stale quote cannot be labelled as today")
        if not args.force and target_trade_date != now.date().isoformat():
            print(json.dumps({**plan, "status": "skipped", "reason": "market_holiday", "latest_trade_date": target_trade_date}, ensure_ascii=False))
            return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({**plan, "status": "skipped", "reason": "trade_calendar_unavailable", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    apply(args.database)
    with sqlite3.connect(args.database) as conn:
        already_published = conn.execute(
            "SELECT 1 FROM ingestion_runs WHERE target_trade_date=? AND trigger_kind='local_schedule' AND status='published' LIMIT 1",
            (target_trade_date,),
        ).fetchone() is not None
        attempts = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE target_trade_date=? AND trigger_kind='local_schedule' AND status IN ('published', 'quarantined', 'failed')",
            (target_trade_date,),
        ).fetchone()[0]
    if already_published:
        print(json.dumps({**plan, "status": "skipped", "reason": "trade_date_already_published", "target_trade_date": target_trade_date}, ensure_ascii=False))
        return 0
    if attempts >= int(config["maximum_attempts_per_trade_date"]):
        print(json.dumps({**plan, "status": "skipped", "reason": "maximum_attempts_reached", "target_trade_date": target_trade_date, "attempts": attempts}, ensure_ascii=False))
        return 0
    run_id = f"auto_{target_trade_date.replace('-', '')}_{now.strftime('%H%M%S')}"
    run_dir = PROJECT_ROOT / "data" / "raw" / run_id
    health_path = PROJECT_ROOT / "data" / "runtime" / "health.json"
    timeout = int(config["source_timeout_seconds"])
    try:
        with sqlite3.connect(args.database) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO ingestion_runs(run_id, trigger_kind, started_at, target_trade_date, status, raw_run_path) VALUES (?, 'local_schedule', ?, ?, 'running', ?)", (run_id, utc_now(), target_trade_date, str(run_dir.relative_to(PROJECT_ROOT))))
            conn.commit()

        frames: dict[str, pd.DataFrame] = {"trade_calendar": calendar_frame}
        failures: list[tuple[str, str]] = []
        checks: list[tuple[str, bool, str]] = []
        master_rows: list[dict[str, str]] = []
        prices: dict[str, dict[str, Any]] = {}

        calendar_source = source_by_role(config, "trade_calendar")
        master_source = source_by_role(config, "security_master_primary")
        price_source = source_by_role(config, "market_price_primary")
        validator_source = optional_source(config, "market_price_sample_validator")
        write_raw(calendar_frame, run_dir, calendar_source["source_id"], now.isoformat())
        try:
            master_frame = run_adapter(master_source["adapter"], timeout, {"day": target_trade_date})
            frames[master_source["role"]] = master_frame
            write_raw(master_frame, run_dir, master_source["source_id"], now.isoformat())
            master_rows = normalize_baostock_master(master_frame, master_source["source_id"])
        except Exception as exc:  # noqa: BLE001
            failures.append((master_source["source_id"], f"{type(exc).__name__}: {exc}"))

        try:
            if not master_rows:
                raise RuntimeError("security master is empty; price request was not attempted")
            master_by_ticker = {row["ticker"]: row for row in master_rows}
            quote_codes = [
                ("sh" if row["exchange"] == "SH" else "sz" if row["exchange"] == "SZ" else "bj") + row["ticker"].split(".", 1)[0]
                for row in master_rows
            ]
            price_frame = run_adapter(price_source["adapter"], timeout, {"codes": quote_codes, "batch_size": 100})
            frames[price_source["role"]] = price_frame
            write_raw(price_frame, run_dir, price_source["source_id"], now.isoformat())
            prices = normalize_tencent_prices(price_frame, master_by_ticker, price_source["source_id"])
        except Exception as exc:  # noqa: BLE001
            failures.append((price_source["source_id"], f"{type(exc).__name__}: {exc}"))

        stock_master = {row["instrument_id"]: row for row in master_rows if row["asset_type"] == "stock"}
        etf_master = {row["instrument_id"]: row for row in master_rows if row["asset_type"] == "etf"}
        stock_prices = {iid: row for iid, row in prices.items() if row["asset_type"] == "stock"}
        etf_prices = {iid: row for iid, row in prices.items() if row["asset_type"] == "etf"}
        required_roles = ["trade_calendar", "security_master_primary", "market_price_primary"]
        checks.append(("all_required_sources_returned", all(role in frames for role in required_roles), json.dumps({"missing": [role for role in required_roles if role not in frames]})))
        checks.append(("stock_master_nonempty", len(stock_master) >= int(config["minimum_stock_master"]), json.dumps({"rows": len(stock_master)})))
        checks.append(("etf_master_nonempty", len(etf_master) >= int(config["minimum_etf_master"]), json.dumps({"rows": len(etf_master)})))
        checks.append(("stock_price_coverage", len(stock_prices) / len(stock_master) >= float(config["minimum_stock_price_coverage"]) if stock_master else False, json.dumps({"prices": len(stock_prices), "master": len(stock_master)})))
        checks.append(("etf_price_coverage", len(etf_prices) / len(etf_master) >= float(config["minimum_etf_price_coverage"]) if etf_master else False, json.dumps({"prices": len(etf_prices), "master": len(etf_master)})))
        quote_dates = sorted({row.get("quote_trade_date") for row in prices.values() if row.get("quote_trade_date")})
        checks.append(("quote_trade_date_matches_target", quote_dates == [target_trade_date.replace("-", "")], json.dumps({"quote_dates": quote_dates, "target": target_trade_date.replace("-", "")})))
        if validator_source is not None and prices:
            try:
                sample = collect_baostock_sample(prices, target_trade_date)
                frames[validator_source["role"]] = pd.DataFrame.from_records(list(sample.values()))
                checks.extend(compare_prices(prices, sample, config, "market_sample"))
            except Exception as exc:  # noqa: BLE001
                failures.append((validator_source["source_id"], f"{type(exc).__name__}: {exc}"))

        passed = not failures and all(item[1] for item in checks)
        backup_path: Path | None = backup_database(args.database, run_id) if passed else None
        with sqlite3.connect(args.database) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            for source_id, message in failures:
                conn.execute("INSERT INTO ingestion_failures VALUES (?, ?, 'market_snapshot', 'source_or_validation_failure', ?, ?)", (run_id, source_id, message, utc_now()))
            for item in config["sources"]:
                if not item["enabled"]:
                    continue
                frame = frames.get(item["role"])
                status = "success" if frame is not None else "failed"
                conn.execute("INSERT INTO source_health VALUES (?, ?, 'market_snapshot', ?, ?, ?, ?)", (run_id, item["source_id"], status, None if frame is None else len(frame), utc_now(), None))
            for name, ok, detail in checks:
                conn.execute("INSERT INTO quality_checks VALUES (?, ?, ?, ?)", (run_id, name, int(ok), detail))
            if passed:
                for row in master_rows:
                    conn.execute(
                        """INSERT INTO security_master(instrument_id, ticker, name, asset_type, exchange, source_id, first_seen_at, last_seen_at, last_seen_run_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(instrument_id) DO UPDATE SET ticker=excluded.ticker, name=excluded.name, listing_status='active', source_id=excluded.source_id, last_seen_at=excluded.last_seen_at, last_seen_run_id=excluded.last_seen_run_id""",
                        (row["instrument_id"], row["ticker"], row["name"], row["asset_type"], row["exchange"], row["source_id"], utc_now(), utc_now(), run_id),
                    )
                    conn.execute(
                        """INSERT INTO instruments(instrument_id, ticker, name, asset_type, exchange, active)
                           VALUES (?, ?, ?, ?, ?, 1)
                           ON CONFLICT(instrument_id) DO UPDATE SET ticker=excluded.ticker, name=excluded.name, asset_type=excluded.asset_type, exchange=excluded.exchange, active=1""",
                        (row["instrument_id"], row["ticker"], row["name"], row["asset_type"], row["exchange"]),
                    )
                for values in prices.values():
                    conn.execute(
                        "INSERT INTO market_daily_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved')",
                        (run_id, values["instrument_id"], target_trade_date, values["source_id"], values["close"], values["open"], values["high"], values["low"], values["volume"], values["turnover_cny"]),
                    )
                manifest_path = run_dir / "autonomous_manifest.json"
                manifest = {
                    "run_id": run_id,
                    "run_type": "autonomous_market_snapshot",
                    "research_as_of": now.isoformat(),
                    "target_trade_date": target_trade_date,
                    "principal_cny": 200000,
                    "status": "published",
                    "row_counts": {"security_master": len(master_rows), "market_daily_prices": len(prices)},
                    "sources": [item["source_id"] for item in config["sources"] if item["enabled"]],
                }
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                conn.execute(
                    """INSERT INTO data_runs(run_id, run_type, research_as_of, principal_cny, manifest_path, manifest_sha256, deterministic_content_hash, status, imported_at)
                       VALUES (?, 'autonomous_market_snapshot', ?, 200000, ?, ?, ?, 'published', ?)""",
                    (run_id, now.isoformat(), str(manifest_path.relative_to(PROJECT_ROOT)), sha256_file(manifest_path), sha256_file(manifest_path), utc_now()),
                )
                conn.execute("INSERT INTO ingestion_watermarks(dataset, last_success_trade_date, last_success_run_id, updated_at) VALUES ('market_eod', ?, ?, ?) ON CONFLICT(dataset) DO UPDATE SET last_success_trade_date=excluded.last_success_trade_date, last_success_run_id=excluded.last_success_run_id, updated_at=excluded.updated_at", (target_trade_date, run_id, utc_now()))
                release_id = f"release_{run_id}"
                conn.execute("UPDATE ingestion_runs SET status='published', finished_at=?, release_id=? WHERE run_id=?", (utc_now(), release_id, run_id))
                conn.execute("INSERT INTO database_releases VALUES (?, ?, ?, ?, NULL, 'published')", (release_id, run_id, utc_now(), str(args.database.relative_to(PROJECT_ROOT))))
            else:
                conn.execute("UPDATE ingestion_runs SET status='quarantined', finished_at=?, message=? WHERE run_id=?", (utc_now(), "; ".join(message for _, message in failures)[:2000], run_id))
            conn.commit()
        if passed:
            with sqlite3.connect(args.database) as conn:
                conn.execute("UPDATE database_releases SET database_sha256=?", (canonical_database_sha256(args.database),))
                conn.commit()
        health = {"run_id": run_id, "status": "published" if passed else "quarantined", "target_trade_date": target_trade_date, "backup": None if backup_path is None else str(backup_path.relative_to(PROJECT_ROOT)), "failures": [{"source": source, "message": message} for source, message in failures], "checks": [{"name": name, "passed": ok, "details": json.loads(detail)} for name, ok, detail in checks]}
        write_health(health_path, health)
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return 0 if passed else 2
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
