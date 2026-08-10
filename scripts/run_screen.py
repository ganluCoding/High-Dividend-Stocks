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

from release_protocol import resolve_release


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
DEFAULT_WORKBENCH = DEFAULT_RUNTIME_ROOT / "workbench.db"
ENGINE_VERSION = "screen-engine-v1"


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


def record_artifact(connection: sqlite3.Connection, artifact_type: str, artifact_id: str, path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO immutable_artifacts_v1
           (artifact_type, artifact_id, content_sha256, content_text, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (artifact_type, artifact_id, content_hash, content, utc_now()),
    )
    return content_hash


def coverage_is_complete(connection: sqlite3.Connection, instrument_id: str, datasets: list[str], available_cutoff: str) -> bool:
    rows = connection.execute(
        """SELECT dataset, collection_status, verification_status, completeness_ratio,
                  latest_available_date
           FROM coverage_matrix WHERE instrument_id=?""",
        (instrument_id,),
    ).fetchall()
    by_dataset = {row[0]: row[1:] for row in rows}
    return all(
        dataset in by_dataset
        and by_dataset[dataset][0] == "collected"
        and by_dataset[dataset][1] == "approved"
        and by_dataset[dataset][2] >= 1.0
        for dataset in datasets
    )


def latest_price(connection: sqlite3.Connection, instrument_id: str, cutoff_date: str) -> tuple[str, float] | None:
    row = connection.execute(
        """SELECT trade_date, close
           FROM market_daily_prices
           WHERE instrument_id=? AND validation_status='approved' AND trade_date<=?
           ORDER BY trade_date DESC, run_id DESC LIMIT 1""",
        (instrument_id, cutoff_date),
    ).fetchone()
    return None if row is None else (str(row[0]), float(row[1]))


def load_coverage_matrix(connection: sqlite3.Connection) -> dict[str, dict[str, tuple[str, str, float]]]:
    """Load coverage facts once for a full-market screen."""
    rows = connection.execute(
        """SELECT instrument_id, dataset, collection_status, verification_status, completeness_ratio
           FROM coverage_matrix"""
    ).fetchall()
    coverage: dict[str, dict[str, tuple[str, str, float]]] = {}
    for instrument_id, dataset, collection_status, verification_status, completeness_ratio in rows:
        coverage.setdefault(str(instrument_id), {})[str(dataset)] = (
            str(collection_status), str(verification_status), float(completeness_ratio),
        )
    return coverage


def coverage_is_complete_from_map(
    coverage: dict[str, dict[str, tuple[str, str, float]]], instrument_id: str, datasets: list[str]
) -> bool:
    by_dataset = coverage.get(instrument_id, {})
    return all(
        dataset in by_dataset
        and by_dataset[dataset][0] == "collected"
        and by_dataset[dataset][1] == "approved"
        and by_dataset[dataset][2] >= 1.0
        for dataset in datasets
    )


def load_latest_stock_metrics(
    connection: sqlite3.Connection, cutoff_date: str, available_cutoff: str
) -> dict[str, tuple[str, float, int, float | None, float | None]]:
    """Batch latest prices and dividend metrics for all stocks in one read."""
    canonical_events = """
        ranked_events AS (
            SELECT d.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.dividend_event_id
                       ORDER BY CASE WHEN d.status='implemented' THEN 1 ELSE 0 END DESC,
                                COALESCE(d.observed_at, '') DESC,
                                d.run_id DESC,
                                d.version_id DESC
                   ) AS rn
            FROM stock_dividend_events AS d
        ),
        eligible_events AS (
            SELECT instrument_id, distribution_type, cash_dps_cny, ex_date
            FROM ranked_events
            WHERE rn=1 AND status='implemented' AND ex_date IS NOT NULL
              AND COALESCE(published_at_date, substr(observed_at, 1, 10), '9999-12-31') <= ?
        )
    """
    rows = connection.execute(
        """WITH ranked_prices AS (
                   SELECT instrument_id, trade_date, close,
                          ROW_NUMBER() OVER (
                              PARTITION BY instrument_id
                              ORDER BY trade_date DESC, run_id DESC
                          ) AS rn
                   FROM market_daily_prices
                   WHERE validation_status='approved' AND trade_date<=?
               ),
               latest_prices AS (
                   SELECT instrument_id, trade_date, close
                   FROM ranked_prices
                   WHERE rn=1
               ),
               """ + canonical_events + """
               ,metrics AS (
                   SELECT p.instrument_id, p.trade_date, p.close,
                          COUNT(DISTINCT CASE
                              WHEN e.ex_date <= p.trade_date
                              THEN substr(e.ex_date, 1, 4)
                          END) AS history_years,
                          SUM(CASE
                              WHEN e.ex_date > date(p.trade_date, '-12 months')
                               AND e.ex_date <= p.trade_date
                               AND e.distribution_type NOT IN ('特别分红', '股改分红')
                              THEN COALESCE(e.cash_dps_cny, 0)
                              ELSE 0
                          END) AS ordinary_cash,
                          SUM(CASE
                              WHEN e.ex_date > date(p.trade_date, '-12 months')
                               AND e.ex_date <= p.trade_date
                              THEN COALESCE(e.cash_dps_cny, 0)
                              ELSE 0
                          END) AS all_cash
                   FROM latest_prices AS p
                   LEFT JOIN eligible_events AS e ON e.instrument_id=p.instrument_id
                   GROUP BY p.instrument_id, p.trade_date, p.close
               )
               SELECT instrument_id, trade_date, close, history_years, ordinary_cash, all_cash
               FROM metrics""",
        (cutoff_date, available_cutoff[:10]),
    ).fetchall()
    values: dict[str, tuple[str, float, int, float | None, float | None]] = {}
    for instrument_id, trade_date, close, history_years, ordinary_cash, all_cash in rows:
        close_value = float(close)
        values[str(instrument_id)] = (
            str(trade_date),
            close_value,
            int(history_years or 0),
            None if not close_value or not ordinary_cash else float(ordinary_cash) / close_value,
            None if not close_value or not all_cash else float(all_cash) / close_value,
        )
    return values


def stock_metrics(connection: sqlite3.Connection, instrument_id: str, price_date: str, available_cutoff: str) -> tuple[int, float | None, float | None]:
    cutoff_date = available_cutoff[:10]
    canonical_events = """
        WITH ranked_events AS (
            SELECT d.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.dividend_event_id
                       ORDER BY CASE WHEN d.status='implemented' THEN 1 ELSE 0 END DESC,
                                COALESCE(d.observed_at, '') DESC,
                                d.run_id DESC,
                                d.version_id DESC
                   ) AS rn
            FROM stock_dividend_events AS d
        )
    """
    years, ordinary_cash, all_cash = connection.execute(
        canonical_events + """SELECT COUNT(DISTINCT substr(ex_date, 1, 4)),
                  SUM(CASE WHEN distribution_type NOT IN ('特别分红', '股改分红') THEN COALESCE(cash_dps_cny, 0) ELSE 0 END),
                  SUM(COALESCE(cash_dps_cny, 0))
           FROM ranked_events
           WHERE rn=1 AND instrument_id=? AND status='implemented' AND ex_date IS NOT NULL
             AND ex_date > date(?, '-12 months') AND ex_date <= ?
             AND COALESCE(published_at_date, substr(observed_at, 1, 10), '9999-12-31') <= ?""",
        (instrument_id, price_date, price_date, cutoff_date),
    ).fetchone()
    history_years = connection.execute(
        canonical_events + """SELECT COUNT(DISTINCT substr(ex_date, 1, 4))
           FROM ranked_events
           WHERE rn=1 AND instrument_id=? AND status='implemented' AND ex_date IS NOT NULL
             AND ex_date <= ?
             AND COALESCE(published_at_date, substr(observed_at, 1, 10), '9999-12-31') <= ?""",
        (instrument_id, price_date, cutoff_date),
    ).fetchone()[0]
    price = latest_price(connection, instrument_id, cutoff_date)
    close = None if price is None else price[1]
    ordinary_yield = None if not close or not ordinary_cash else float(ordinary_cash) / close
    all_yield = None if not close or not all_cash else float(all_cash) / close
    return int(history_years), ordinary_yield, all_yield


def run_stock_rule(connection: sqlite3.Connection, rule: dict[str, Any], taxonomy: dict[str, dict[str, Any]], available_cutoff: str) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    results: list[dict[str, Any]] = []
    coverage = load_coverage_matrix(connection)
    stock_metrics_by_instrument = load_latest_stock_metrics(connection, available_cutoff[:10], available_cutoff)
    instruments = connection.execute("SELECT instrument_id, ticker, name FROM security_master WHERE asset_type='stock' ORDER BY instrument_id").fetchall()
    for instrument_id, ticker, name in instruments:
        reasons: list[str] = []
        state = "资料不足"
        profile = taxonomy.get(instrument_id)
        if profile is None:
            reasons.append("MISSING_TAXONOMY")
        elif profile.get("dividend_style") in params["excluded_dividend_styles"]:
            state, reasons = "不纳入本模板", ["CYCLE_EXCLUDED"]
        elif not coverage_is_complete_from_map(coverage, instrument_id, params["required_datasets"]):
            reasons.append("MISSING_COVERAGE")
        else:
            metric_row = stock_metrics_by_instrument.get(instrument_id)
            if metric_row is None:
                reasons.append("MISSING_COVERAGE")
            else:
                _, _, history_years, ordinary_yield, _ = metric_row
                if history_years < params["minimum_ordinary_dividend_years"]:
                    reasons.append("DIVIDEND_HISTORY_SHORT")
                elif ordinary_yield is None or ordinary_yield < params["minimum_ordinary_ttm_cash_yield"]:
                    reasons.append("ORDINARY_YIELD_BELOW_ENTRY")
                else:
                    state, reasons = "资料足以研究", ["PASS"]
        metric_row = stock_metrics_by_instrument.get(instrument_id)
        if metric_row is None:
            price_row = None
            history_years, ordinary_yield, all_yield = 0, None, None
        else:
            price_date, close, history_years, ordinary_yield, all_yield = metric_row
            price_row = (price_date, close)
        priority = 0 if state == "资料足以研究" else None
        results.append({"instrument_id": instrument_id, "state": state, "reasons": reasons, "priority": priority, "payload": {
            "ticker": ticker, "name": name, "price_date": None if price_row is None else price_row[0],
            "reference_close_cny": None if price_row is None else price_row[1], "ordinary_dividend_history_years": history_years,
            "ordinary_ttm_cash_yield_pre_tax": ordinary_yield, "all_ttm_cash_yield_pre_tax": all_yield,
            "taxonomy": profile, "boundary": "历史税前现金分红参考，不代表未来分红、估值或买入建议。",
        }})
    return results


def run_etf_rule(connection: sqlite3.Connection, rule: dict[str, Any], available_cutoff: str) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    results: list[dict[str, Any]] = []
    instruments = connection.execute("SELECT instrument_id, ticker, name FROM security_master WHERE asset_type='etf' ORDER BY instrument_id").fetchall()
    for instrument_id, ticker, name in instruments:
        state, reasons = "资料不足", []
        if not coverage_is_complete(connection, instrument_id, params["required_datasets"], available_cutoff):
            reasons.append("MISSING_PRODUCT_FACTS" if params["require_product_facts"] else "MISSING_COVERAGE")
        results.append({"instrument_id": instrument_id, "state": state, "reasons": reasons, "priority": None, "payload": {
            "ticker": ticker, "name": name,
            "boundary": "过去12个月现金分配率不是股息率、未来分配或基金总回报。",
        }})
    return results


def run_cyclical_rule(connection: sqlite3.Connection, rule: dict[str, Any], taxonomy: dict[str, dict[str, Any]], available_cutoff: str) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    results: list[dict[str, Any]] = []
    instruments = connection.execute("SELECT instrument_id, ticker, name FROM security_master WHERE asset_type='stock' ORDER BY instrument_id").fetchall()
    for instrument_id, ticker, name in instruments:
        profile = taxonomy.get(instrument_id)
        if profile is None:
            state, reasons = "资料不足", ["MISSING_TAXONOMY"]
        elif profile.get("dividend_style") not in params["included_dividend_styles"]:
            state, reasons = "不纳入本模板", ["NOT_CYCLICAL"]
        elif not coverage_is_complete(connection, instrument_id, params["required_datasets"], available_cutoff):
            state, reasons = "资料不足", ["MISSING_COVERAGE"]
        else:
            state, reasons = "继续观察", ["WATCH"]
        price_row = latest_price(connection, instrument_id, available_cutoff[:10])
        results.append({"instrument_id": instrument_id, "state": state, "reasons": reasons, "priority": None, "payload": {
            "ticker": ticker, "name": name, "price_date": None if price_row is None else price_row[0],
            "reference_close_cny": None if price_row is None else price_row[1], "taxonomy": profile,
            "boundary": "周期性利润和静态收益率不能外推为未来分红；仅供观察与复核。",
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
    rule_hash = hashlib.sha256(args.rule.read_bytes()).hexdigest()
    taxonomy_hash = hashlib.sha256(args.taxonomy.read_bytes()).hexdigest()
    with sqlite3.connect(f"file:{facts}?mode=ro", uri=True) as facts_connection:
        if rule["rule_id"] == "stable_dividend_stock":
            results = run_stock_rule(facts_connection, rule, taxonomy, manifest["available_cutoff"])
        elif rule["rule_id"] == "dividend_etf":
            results = run_etf_rule(facts_connection, rule, manifest["available_cutoff"])
        elif rule["rule_id"] == "cyclical_dividend_watch":
            results = run_cyclical_rule(facts_connection, rule, taxonomy, manifest["available_cutoff"])
        else:
            raise SystemExit(f"Unsupported rule id: {rule['rule_id']}")
    screen_run_id = hashlib.sha256(f"{manifest['release_id']}:{manifest['facts_sha256']}:{rule_hash}:{taxonomy_hash}:{ENGINE_VERSION}".encode()).hexdigest()[:24]
    with sqlite3.connect(args.workbench.expanduser()) as workbench:
        workbench.execute("INSERT OR IGNORE INTO rule_versions VALUES (?, ?, ?, ?)", (rule["rule_id"], rule["rule_version"], rule_hash, utc_now()))
        # Keep the exact rule/taxonomy text alongside the run contract.  This
        # intentionally uses INSERT OR IGNORE so a later edit cannot overwrite
        # the artifact used by an earlier run.
        rule_content = args.rule.read_text(encoding="utf-8")
        taxonomy_content = args.taxonomy.read_text(encoding="utf-8")
        workbench.executemany(
            """INSERT OR IGNORE INTO immutable_artifacts_v1
               (artifact_type, artifact_id, content_sha256, content_text, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                ("rule", f"{rule['rule_id']}@{rule['rule_version']}", rule_hash, rule_content, utc_now()),
                ("taxonomy", args.taxonomy.name, taxonomy_hash, taxonomy_content, utc_now()),
            ],
        )
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
