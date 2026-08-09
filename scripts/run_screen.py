#!/usr/bin/env python3
"""Run a versioned, non-advisory research screen against one immutable release."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from release_protocol import resolve_release, sha256_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
DEFAULT_WORKBENCH = DEFAULT_RUNTIME_ROOT / "workbench.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_taxonomy(path: Path) -> dict[str, dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return {item["instrument_id"]: item for item in document["instruments"]}


def initialize_workbench(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = (PROJECT_ROOT / "database" / "workbench_schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.execute("INSERT OR REPLACE INTO workbench_metadata VALUES ('schema_version', 'workbench-v1')")
        connection.commit()


def coverage_is_complete(connection: sqlite3.Connection, instrument_id: str, datasets: list[str]) -> bool:
    rows = connection.execute(
        "SELECT dataset, collection_status, verification_status, completeness_ratio FROM coverage_matrix WHERE instrument_id=?",
        (instrument_id,),
    ).fetchall()
    by_dataset = {row[0]: row[1:] for row in rows}
    return all(dataset in by_dataset and by_dataset[dataset][0] == "collected" and by_dataset[dataset][1] == "approved" and by_dataset[dataset][2] >= 1.0 for dataset in datasets)


def stock_metrics(connection: sqlite3.Connection, instrument_id: str, price_date: str) -> tuple[int, float | None, float | None]:
    years, ordinary_cash, all_cash = connection.execute(
        """SELECT COUNT(DISTINCT substr(ex_date, 1, 4)),
                  SUM(CASE WHEN distribution_type NOT IN ('特别分红', '股改分红') THEN COALESCE(cash_dps_cny, 0) ELSE 0 END),
                  SUM(COALESCE(cash_dps_cny, 0))
           FROM stock_dividend_events
           WHERE instrument_id=? AND status='implemented' AND ex_date IS NOT NULL
             AND ex_date > date(?, '-12 months') AND ex_date <= ?""",
        (instrument_id, price_date, price_date),
    ).fetchone()
    history_years = connection.execute(
        "SELECT COUNT(DISTINCT substr(ex_date, 1, 4)) FROM stock_dividend_events WHERE instrument_id=? AND status='implemented' AND ex_date IS NOT NULL",
        (instrument_id,),
    ).fetchone()[0]
    price = connection.execute(
        "SELECT close FROM v_latest_market_eod_price WHERE instrument_id=?", (instrument_id,)
    ).fetchone()
    close = None if price is None else float(price[0])
    ordinary_yield = None if not close or not ordinary_cash else float(ordinary_cash) / close
    all_yield = None if not close or not all_cash else float(all_cash) / close
    return int(history_years), ordinary_yield, all_yield


def run_stock_rule(connection: sqlite3.Connection, rule: dict[str, Any], taxonomy: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    results: list[dict[str, Any]] = []
    instruments = connection.execute("SELECT instrument_id, ticker, name FROM security_master WHERE asset_type='stock' ORDER BY instrument_id").fetchall()
    for instrument_id, ticker, name in instruments:
        reasons: list[str] = []
        state = "资料不足"
        profile = taxonomy.get(instrument_id)
        if profile is None:
            reasons.append("MISSING_TAXONOMY")
        elif profile.get("dividend_style") in params["excluded_dividend_styles"]:
            state, reasons = "不纳入本模板", ["CYCLE_EXCLUDED"]
        elif not coverage_is_complete(connection, instrument_id, params["required_datasets"]):
            reasons.append("MISSING_COVERAGE")
        else:
            price_row = connection.execute("SELECT trade_date FROM v_latest_market_eod_price WHERE instrument_id=?", (instrument_id,)).fetchone()
            if price_row is None:
                reasons.append("MISSING_COVERAGE")
            else:
                history_years, ordinary_yield, all_yield = stock_metrics(connection, instrument_id, price_row[0])
                if history_years < params["minimum_ordinary_dividend_years"]:
                    reasons.append("DIVIDEND_HISTORY_SHORT")
                elif ordinary_yield is None or ordinary_yield < params["minimum_ordinary_ttm_cash_yield"]:
                    reasons.append("ORDINARY_YIELD_BELOW_ENTRY")
                else:
                    state, reasons = "资料足以研究", ["PASS"]
        price_row = connection.execute("SELECT trade_date, close FROM v_latest_market_eod_price WHERE instrument_id=?", (instrument_id,)).fetchone()
        history_years, ordinary_yield, all_yield = stock_metrics(connection, instrument_id, price_row[0]) if price_row else (0, None, None)
        priority = 0 if state == "资料足以研究" else None
        results.append({"instrument_id": instrument_id, "state": state, "reasons": reasons, "priority": priority, "payload": {
            "ticker": ticker, "name": name, "price_date": None if price_row is None else price_row[0],
            "reference_close_cny": None if price_row is None else price_row[1], "ordinary_dividend_history_years": history_years,
            "ordinary_ttm_cash_yield_pre_tax": ordinary_yield, "all_ttm_cash_yield_pre_tax": all_yield,
            "taxonomy": profile, "boundary": "历史税前现金分红参考，不代表未来分红、估值或买入建议。",
        }})
    return results


def run_etf_rule(connection: sqlite3.Connection, rule: dict[str, Any]) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    results: list[dict[str, Any]] = []
    instruments = connection.execute("SELECT instrument_id, ticker, name FROM security_master WHERE asset_type='etf' ORDER BY instrument_id").fetchall()
    for instrument_id, ticker, name in instruments:
        state, reasons = "资料不足", []
        if not coverage_is_complete(connection, instrument_id, params["required_datasets"]):
            reasons.append("MISSING_PRODUCT_FACTS" if params["require_product_facts"] else "MISSING_COVERAGE")
        results.append({"instrument_id": instrument_id, "state": state, "reasons": reasons, "priority": None, "payload": {
            "ticker": ticker, "name": name,
            "boundary": "过去12个月现金分配率不是股息率、未来分配或基金总回报。",
        }})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--workbench", type=Path, default=DEFAULT_WORKBENCH)
    parser.add_argument("--release-id")
    parser.add_argument("--taxonomy", type=Path, default=PROJECT_ROOT / "config" / "candidate_universe_core.json")
    args = parser.parse_args()
    rule = json.loads(args.rule.read_text(encoding="utf-8"))
    required = {"rule_id", "rule_version", "scope", "required_parameters", "missing_data_policy", "reason_codes"}
    missing = required - rule.keys()
    if missing:
        raise SystemExit(f"Rule is incomplete: {', '.join(sorted(missing))}")
    release_dir, manifest = resolve_release(args.runtime_root.expanduser(), args.release_id)
    facts = release_dir / "facts.sqlite"
    initialize_workbench(args.workbench.expanduser())
    taxonomy = load_taxonomy(args.taxonomy)
    with sqlite3.connect(f"file:{facts}?mode=ro", uri=True) as facts_connection:
        if rule["rule_id"] == "stable_dividend_stock":
            results = run_stock_rule(facts_connection, rule, taxonomy)
        elif rule["rule_id"] == "dividend_etf":
            results = run_etf_rule(facts_connection, rule)
        else:
            raise SystemExit(f"Unsupported rule id: {rule['rule_id']}")
    rule_hash = sha256_file(args.rule)
    screen_run_id = hashlib.sha256(f"{manifest['release_id']}:{rule_hash}".encode()).hexdigest()[:24]
    with sqlite3.connect(args.workbench.expanduser()) as workbench:
        workbench.execute("INSERT OR REPLACE INTO rule_versions VALUES (?, ?, ?, ?)", (rule["rule_id"], rule["rule_version"], rule_hash, utc_now()))
        workbench.execute("INSERT OR REPLACE INTO screen_runs_v1 VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')", (
            screen_run_id, manifest["release_id"], manifest["facts_sha256"], rule["rule_id"], rule["rule_version"], manifest["available_cutoff"], utc_now(),
        ))
        workbench.execute("DELETE FROM screen_results_v1 WHERE screen_run_id=?", (screen_run_id,))
        workbench.executemany("INSERT INTO screen_results_v1 VALUES (?, ?, ?, ?, ?, ?)", [
            (screen_run_id, item["instrument_id"], item["state"], json.dumps(item["reasons"], ensure_ascii=False), item["priority"], json.dumps(item["payload"], ensure_ascii=False, sort_keys=True)) for item in results
        ])
        workbench.commit()
    by_state: dict[str, int] = {}
    for item in results:
        by_state[item["state"]] = by_state.get(item["state"], 0) + 1
    print(json.dumps({"status": "completed", "screen_run_id": screen_run_id, "release_id": manifest["release_id"], "rule": f"{rule['rule_id']}@{rule['rule_version']}", "states": by_state}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
