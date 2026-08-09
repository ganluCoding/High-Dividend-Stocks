#!/usr/bin/env python3
"""Build the local SQLite query layer from one immutable curated snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ID = "run_20260809_composite_v2a"
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "database" / "high_dividend.db"
DEFAULT_UNIVERSE = PROJECT_ROOT / "config" / "candidate_universe_core.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_or_none(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def number_or_none(value: str | None) -> float | None:
    value = text_or_none(value)
    return float(value) if value is not None else None


def integer_or_none(value: str | None) -> int | None:
    value = text_or_none(value)
    return int(value) if value is not None else None


def rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def ensure_source(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        """
        INSERT INTO data_sources(source_id, source_tier, notes)
        VALUES (?, 'convenience', 'Source identifier observed in frozen snapshot; detailed licence verification pending.')
        ON CONFLICT(source_id) DO NOTHING
        """,
        (source_id,),
    )


def insert_many(conn: sqlite3.Connection, sql: str, values: Iterable[tuple]) -> None:
    conn.executemany(sql, values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-run", default=DEFAULT_RUN_ID)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument(
        "--additional-run",
        action="append",
        default=[],
        help="Normalized collection run to import in addition to the curated base snapshot. May be repeated.",
    )
    parser.add_argument("--replace", action="store_true", help="Replace an existing generated database.")
    args = parser.parse_args()

    snapshot_dir = PROJECT_ROOT / "data" / "curated" / args.snapshot_run
    manifest_path = PROJECT_ROOT / "data" / "manifests" / f"{args.snapshot_run}.json"
    schema_path = PROJECT_ROOT / "database" / "schema.sql"
    universe_path = args.universe
    sources_path = PROJECT_ROOT / "config" / "research_primary_sources.json"
    if not snapshot_dir.is_dir() or not manifest_path.is_file():
        raise SystemExit(f"Snapshot run is missing: {args.snapshot_run}")
    if args.database.exists() and not args.replace:
        raise SystemExit(f"Database already exists: {args.database}. Use --replace only when replacement is intended.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    primary_sources = json.loads(sources_path.read_text(encoding="utf-8"))
    args.database.parent.mkdir(parents=True, exist_ok=True)
    temp_path = args.database.with_suffix(".tmp")
    if temp_path.exists():
        temp_path.unlink()

    conn = sqlite3.connect(temp_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO app_metadata(key, value) VALUES (?, ?)", ("schema_version", "1"))
        conn.execute("INSERT INTO app_metadata(key, value) VALUES (?, ?)", ("database_purpose", "High-dividend research query layer; not a recommendation engine."))
        def register_run(run_id: str, run_manifest_path: Path, run_manifest: dict[str, object], run_type: str) -> None:
            conn.execute(
                """INSERT INTO data_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    run_type,
                    run_manifest.get("research_as_of"),
                    float(run_manifest.get("principal_cny", 0)),
                    str(run_manifest_path.relative_to(PROJECT_ROOT)),
                    sha256_file(run_manifest_path),
                    run_manifest.get("deterministic_content_hash") or run_manifest.get("input_manifest_hash"),
                    run_manifest.get("status", "unknown"),
                    datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                ),
            )

        register_run(args.snapshot_run, manifest_path, manifest, "curated_snapshot")
        input_sets: list[tuple[str, Path]] = [(args.snapshot_run, snapshot_dir)]
        for run_id in args.additional_run:
            extra_dir = PROJECT_ROOT / "data" / "normalized" / run_id
            extra_manifest_path = PROJECT_ROOT / "data" / "manifests" / f"{run_id}.json"
            if not extra_dir.is_dir() or not extra_manifest_path.is_file():
                raise SystemExit(f"Additional normalized run is missing: {run_id}")
            extra_manifest = json.loads(extra_manifest_path.read_text(encoding="utf-8"))
            register_run(run_id, extra_manifest_path, extra_manifest, "normalized_collection")
            input_sets.append((run_id, extra_dir))

        for source in primary_sources["sources"]:
            conn.execute(
                """INSERT INTO data_sources(source_id, publisher, source_tier, url, use_scope, notes)
                   VALUES (?, ?, ?, ?, 'primary_disclosure', ?)
                   ON CONFLICT(source_id) DO UPDATE SET publisher=excluded.publisher, source_tier=excluded.source_tier, url=excluded.url, notes=excluded.notes""",
                (source["source_id"], source.get("publisher"), source.get("source_tier", "primary"), source.get("url"), source.get("limitation")),
            )

        for item in universe["instruments"]:
            ticker = item["ticker"]
            exchange = ticker.rsplit(".", 1)[-1] if "." in ticker else None
            conn.execute(
                "INSERT INTO instruments(instrument_id, ticker, name, asset_type, exchange, benchmark_id) VALUES (?, ?, ?, ?, ?, ?)",
                (item["instrument_id"], ticker, item["name"], item["asset_type"], exchange, item.get("benchmark_id")),
            )
            conn.execute(
                """INSERT INTO candidate_universe_memberships
                   (universe_id, instrument_id, category, dividend_style, risk_level, data_status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    universe.get("candidate_universe_id", "legacy_universe"),
                    item["instrument_id"],
                    item.get("category", "未分类"),
                    item.get("dividend_style", "未分类"),
                    item.get("risk_level", "未评估"),
                    item.get("data_status", "待采集"),
                ),
            )
        conn.execute(
            "INSERT INTO instruments(instrument_id, ticker, name, asset_type, exchange) VALUES (?, ?, ?, 'index', 'CSI')",
            ("INDEX.CSI.H30269", "H30269", "中证红利低波动指数"),
        )

        def rows_from_sets(filename: str) -> list[tuple[str, dict[str, str]]]:
            output: list[tuple[str, dict[str, str]]] = []
            for run_id, directory in input_sets:
                output.extend((run_id, row) for row in rows(directory / filename))
            return output

        prices = rows_from_sets("daily_prices.csv")
        for row in prices:
            ensure_source(conn, row[1]["source_id"])
        insert_many(
            conn,
            """INSERT INTO price_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((run_id, row["instrument_id"], row["trade_date"], row["adjustment"], row["source_id"], text_or_none(row["source_raw_sha256"]), text_or_none(row["observed_at"]), number_or_none(row["open"]), number_or_none(row["high"]), number_or_none(row["low"]), number_or_none(row["close"]), number_or_none(row["volume"]), number_or_none(row["turnover_cny"]), number_or_none(row["amplitude_pct"]), number_or_none(row["change_pct"]), number_or_none(row["change_cny"]), number_or_none(row["turnover_rate_pct"])) for run_id, row in prices),
        )

        stock_events = rows_from_sets("stock_dividend_events.csv")
        for row in stock_events:
            ensure_source(conn, row[1]["source_id"])
        insert_many(
            conn,
            """INSERT INTO stock_dividend_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((run_id, row["version_id"], row["dividend_event_id"], text_or_none(row["supersedes_version_id"]), row["instrument_id"], text_or_none(row["profit_period_label"]), text_or_none(row["distribution_type"]), integer_or_none(row["installment_no"]), row["status"], number_or_none(row["cash_per_10_shares_cny"]), number_or_none(row["cash_dps_cny"]), text_or_none(row["published_at_date"]), text_or_none(row["record_date"]), text_or_none(row["ex_date"]), text_or_none(row["payment_date"]), text_or_none(row["description"]), row["source_id"], text_or_none(row["source_raw_sha256"]), text_or_none(row["observed_at"])) for run_id, row in stock_events),
        )

        etf_events = rows_from_sets("etf_distribution_events.csv")
        for row in etf_events:
            ensure_source(conn, row[1]["source_id"])
        insert_many(
            conn,
            """INSERT INTO etf_distribution_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((run_id, row["version_id"], row["dividend_event_id"], row["instrument_id"], row["status"], number_or_none(row["cash_per_unit_cny"]), number_or_none(row["cumulative_distribution_cny"]), text_or_none(row["ex_date"]), text_or_none(row["date_semantics"]), row["source_id"], text_or_none(row["source_raw_sha256"]), text_or_none(row["observed_at"])) for run_id, row in etf_events),
        )

        financials = rows_from_sets("financial_observations.csv")
        for row in financials:
            ensure_source(conn, row[1]["source_id"])
        insert_many(
            conn,
            """INSERT INTO financial_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((run_id, row["instrument_id"], row["statement_type"], row["metric"], number_or_none(row["value"]), text_or_none(row["currency"]), row["report_date"], text_or_none(row["published_at_date"]), text_or_none(row["updated_at_date"]), row["source_id"], text_or_none(row["source_raw_sha256"]), text_or_none(row["observed_at"])) for run_id, row in financials),
        )
        conn.execute(
            """
            UPDATE candidate_universe_memberships AS m
            SET data_status = CASE
                WHEN EXISTS (SELECT 1 FROM price_daily AS p WHERE p.instrument_id = m.instrument_id)
                 AND (
                    EXISTS (SELECT 1 FROM stock_dividend_events AS d WHERE d.instrument_id = m.instrument_id)
                    OR EXISTS (SELECT 1 FROM etf_distribution_events AS e WHERE e.instrument_id = m.instrument_id)
                 ) THEN '价格与分红已采集（待公告核验）'
                WHEN EXISTS (SELECT 1 FROM price_daily AS p WHERE p.instrument_id = m.instrument_id)
                    THEN '价格已采集；分红/分配待核验'
                ELSE '待采集'
            END
            """
        )
        conn.commit()

        summary = {row["entity"]: row["row_count"] for row in conn.execute("SELECT * FROM v_database_summary")}
        expected_counts = {
            "instruments": len(universe["instruments"]) + 1,
            "price_daily": len(prices),
            "stock_dividend_events": len(stock_events),
            "etf_distribution_events": len(etf_events),
            "financial_observations": len(financials),
        }
        if summary != expected_counts:
            raise RuntimeError(f"Unexpected imported row counts: {summary}")
        conn.execute("PRAGMA foreign_key_check")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Foreign-key validation failed")
    finally:
        conn.close()

    temp_path.replace(args.database)
    print(json.dumps({"database": str(args.database.relative_to(PROJECT_ROOT)), "snapshot_run": args.snapshot_run, "row_counts": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
