#!/usr/bin/env python3
"""Publish a verified immutable facts snapshot from the local collector database."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apply_migrations import apply
from release_protocol import integrity_check, read_json, sha256_file, sqlite_backup, utc_now, validate_release, write_json_atomic


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
DEFAULT_DATABASE = DEFAULT_RUNTIME_ROOT / "data" / "database" / "high_dividend.db"
SH_TZ = ZoneInfo("Asia/Shanghai")


def latest_value(connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> str | None:
    row = connection.execute(sql, parameters).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def build_coverage(connection: sqlite3.Connection, release_id: str, available_cutoff: str) -> list[dict[str, object]]:
    """Materialize coverage truth; absence is explicit and never converted to zero yield."""
    as_of = latest_value(connection, "SELECT MAX(trade_date) FROM market_daily_prices WHERE validation_status='approved'") or available_cutoff[:10]
    generated_at = utc_now()
    connection.execute("DELETE FROM coverage_matrix")
    rows: list[dict[str, object]] = []

    def add(dataset: str, window: str, sql: str, asset_type: str, expected: int) -> None:
        for row in connection.execute(sql, (asset_type,)).fetchall():
            instrument_id, latest_effective, latest_available, count_value, run_ids = row
            ratio = min(float(count_value or 0) / expected, 1.0)
            collected = int(count_value or 0) > 0
            payload = {
                "instrument_id": instrument_id,
                "dataset": dataset,
                "required_history_window": window,
                "as_of_date": as_of,
                "available_cutoff": available_cutoff,
                "latest_effective_date": latest_effective,
                "latest_available_date": latest_available,
                "collection_status": "collected" if collected else "missing",
                "verification_status": "approved" if collected else "unverified",
                "completeness_ratio": ratio,
                "freshness_status": "fresh" if latest_effective == as_of else "stale" if collected else "unknown",
                "source_tier": "convenience" if collected else "unknown",
                "release_id": release_id,
                "source_run_ids_json": run_ids or "[]",
                "generated_at": generated_at,
            }
            rows.append(payload)

    add(
        "market_prices", "1d",
        """SELECT s.instrument_id, MAX(p.trade_date), MAX(p.run_id), COUNT(p.instrument_id), json_group_array(DISTINCT p.run_id)
           FROM security_master s LEFT JOIN market_daily_prices p ON p.instrument_id=s.instrument_id AND p.validation_status='approved'
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "stock", 1,
    )
    add(
        "market_prices", "1d",
        """SELECT s.instrument_id, MAX(p.trade_date), MAX(p.run_id), COUNT(p.instrument_id), json_group_array(DISTINCT p.run_id)
           FROM security_master s LEFT JOIN market_daily_prices p ON p.instrument_id=s.instrument_id AND p.validation_status='approved'
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "etf", 1,
    )
    add(
        "price_history", "5y",
        """SELECT s.instrument_id, MAX(p.trade_date), MAX(p.observed_at), COUNT(DISTINCT p.trade_date), json_group_array(DISTINCT p.run_id)
           FROM security_master s LEFT JOIN price_daily p ON p.instrument_id=s.instrument_id AND p.adjustment='unadjusted'
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "stock", 1250,
    )
    add(
        "price_history", "5y",
        """SELECT s.instrument_id, MAX(p.trade_date), MAX(p.observed_at), COUNT(DISTINCT p.trade_date), json_group_array(DISTINCT p.run_id)
           FROM security_master s LEFT JOIN price_daily p ON p.instrument_id=s.instrument_id AND p.adjustment='unadjusted'
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "etf", 1250,
    )
    add(
        "stock_dividends", "3y",
        """SELECT s.instrument_id, MAX(d.ex_date), MAX(COALESCE(d.published_at_date, d.observed_at)),
                  COUNT(DISTINCT substr(d.ex_date, 1, 4)), json_group_array(DISTINCT d.run_id)
           FROM security_master s LEFT JOIN stock_dividend_events d ON d.instrument_id=s.instrument_id
             AND d.status='implemented' AND d.ex_date IS NOT NULL
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "stock", 3,
    )
    add(
        "financials", "latest_report",
        """SELECT s.instrument_id, MAX(f.report_date), MAX(COALESCE(f.published_at_date, f.observed_at)),
                  COUNT(DISTINCT f.report_date), json_group_array(DISTINCT f.run_id)
           FROM security_master s LEFT JOIN financial_observations f ON f.instrument_id=s.instrument_id
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "stock", 1,
    )
    add(
        "etf_distributions", "1y",
        """SELECT s.instrument_id, MAX(d.ex_date), MAX(d.observed_at),
                  COUNT(DISTINCT substr(d.ex_date, 1, 4)), json_group_array(DISTINCT d.run_id)
           FROM security_master s LEFT JOIN etf_distribution_events d ON d.instrument_id=s.instrument_id
             AND d.status='implemented' AND d.ex_date IS NOT NULL
           WHERE s.asset_type=? GROUP BY s.instrument_id""",
        "etf", 1,
    )
    # Product facts are deliberately absent until an auditable source is connected.
    for (instrument_id,) in connection.execute("SELECT instrument_id FROM security_master WHERE asset_type='etf'"):
        rows.append({
            "instrument_id": instrument_id, "dataset": "etf_product_facts", "required_history_window": "current",
            "as_of_date": as_of, "available_cutoff": available_cutoff, "latest_effective_date": None,
            "latest_available_date": None, "collection_status": "missing", "verification_status": "unverified",
            "completeness_ratio": 0.0, "freshness_status": "unknown", "source_tier": "unknown",
            "release_id": release_id, "source_run_ids_json": "[]", "generated_at": generated_at,
        })
    connection.executemany(
        """INSERT INTO coverage_matrix VALUES (:instrument_id,:dataset,:required_history_window,:as_of_date,:available_cutoff,
           :latest_effective_date,:latest_available_date,:collection_status,:verification_status,:completeness_ratio,
           :freshness_status,:source_tier,:release_id,:source_run_ids_json,:generated_at)""", rows,
    )
    connection.execute("INSERT OR REPLACE INTO release_fact_metadata VALUES ('release_id', ?)", (release_id,))
    connection.execute("INSERT OR REPLACE INTO release_fact_metadata VALUES ('available_cutoff', ?)", (available_cutoff,))
    connection.commit()
    summaries = connection.execute(
        "SELECT dataset, COUNT(*), SUM(collection_status='collected'), AVG(completeness_ratio) FROM coverage_matrix GROUP BY dataset ORDER BY dataset"
    ).fetchall()
    return [{"dataset": row[0], "instruments": row[1], "collected": row[2], "average_completeness": round(row[3] or 0, 6)} for row in summaries]


def deduplicate_event_versions(connection: sqlite3.Connection) -> None:
    """Keep one physical row for each logical event version in a release.

    The mutable collector database intentionally retains the run id for lineage,
    so the same immutable ``version_id`` can be observed by several runs.  A
    release is the query contract consumed by screening and must not count those
    observations as separate cash events.  Distinct versions of the same event
    are retained for later supersession handling; only exact version repeats are
    collapsed, preferring the newest observation deterministically.
    """
    for table in ("stock_dividend_events", "etf_distribution_events"):
        connection.execute(
            f"""
            DELETE FROM {table}
            WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY dividend_event_id, version_id
                               ORDER BY COALESCE(observed_at, '') DESC, run_id DESC
                           ) AS rn
                    FROM {table}
                )
                WHERE rn > 1
            )
            """
        )
    connection.commit()


def acquire_lock(runtime_root: Path):
    path = runtime_root / "publisher.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("Another publisher is already running") from exc
    return handle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--release-id")
    parser.add_argument("--available-cutoff", help="ISO timestamp; defaults to publish time")
    args = parser.parse_args()
    runtime_root = args.runtime_root.expanduser()
    source = args.database.expanduser()
    if not source.is_file():
        raise SystemExit(f"Source database does not exist: {source}")
    release_id = args.release_id or f"release_{datetime.now(SH_TZ).strftime('%Y%m%d_%H%M%S')}"
    available_cutoff = args.available_cutoff or utc_now()
    source_sha256 = sha256_file(source)
    releases = runtime_root / "releases"
    final_dir = releases / release_id
    if final_dir.exists():
        raise SystemExit(f"Release already exists: {final_dir}")
    lock = acquire_lock(runtime_root)
    temporary: Path | None = None
    try:
        releases.mkdir(parents=True, exist_ok=True)
        pointer = runtime_root / "current-release.json"
        if not args.release_id and pointer.is_file():
            try:
                current_id = read_json(pointer)["release_id"]
                current_manifest = validate_release(releases / current_id)
                raw_inputs_path = releases / current_id / "raw-inputs.json"
                if raw_inputs_path.is_file() and read_json(raw_inputs_path).get("source_database_sha256") == source_sha256:
                    print(json.dumps({"status": "skipped", "reason": "source_database_unchanged", "release_id": current_id}, ensure_ascii=False))
                    return 0
            except (KeyError, RuntimeError, json.JSONDecodeError):
                pass
        temporary = Path(tempfile.mkdtemp(prefix=f".{release_id}.", dir=releases))
        facts = temporary / "facts.sqlite"
        sqlite_backup(source, facts)
        apply(facts)
        with sqlite3.connect(facts) as connection:
            deduplicate_event_versions(connection)
        integrity_check(facts)
        with sqlite3.connect(facts) as connection:
            coverage = build_coverage(connection, release_id, available_cutoff)
        integrity_check(facts)
        facts_sha = sha256_file(facts)
        facts_size = facts.stat().st_size
        raw_inputs = {
            "source_database_path": str(source), "source_database_sha256": source_sha256,
            "published_at": utc_now(), "source_kind": "mutable_local_collector_snapshot",
        }
        (temporary / "raw-inputs.json").write_text(json.dumps(raw_inputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": "release-manifest-v1", "release_id": release_id, "published_at": utc_now(),
            "available_cutoff": available_cutoff, "facts_filename": "facts.sqlite", "facts_sha256": facts_sha,
            "facts_size_bytes": facts_size, "coverage": coverage,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temporary / "facts.sqlite.sha256").write_text(facts_sha + "\n", encoding="utf-8")
        (temporary / "manifest.sha256").write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")
        (temporary / "READY").write_text(release_id + "\n", encoding="utf-8")
        validate_release(temporary)
        temporary.replace(final_dir)
        write_json_atomic(runtime_root / "current-release.json", {"release_id": release_id, "updated_at": utc_now()})
        print(json.dumps({"status": "published", "release_id": release_id, "path": str(final_dir), "coverage": coverage}, ensure_ascii=False, indent=2))
        return 0
    finally:
        if temporary and temporary.exists():
            shutil.rmtree(temporary)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
