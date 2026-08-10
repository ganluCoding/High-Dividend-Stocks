#!/usr/bin/env python3
"""Run bounded historical-price batches and publish them atomically.

This agent is intentionally low frequency.  It advances through instruments
that do not yet have a long ``price_daily`` history, so a failed provider call
does not block the existing release or delete prior rows.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"


def latest_market_date(runtime: Path) -> str:
    database = runtime / "data" / "database" / "high_dividend.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT MAX(trade_date) FROM market_daily_prices WHERE validation_status='approved'").fetchone()
    if row and row[0]:
        return str(row[0])
    pointer = json.loads((runtime / "current-release.json").read_text(encoding="utf-8"))
    manifest = json.loads((runtime / "releases" / pointer["release_id"] / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["available_cutoff"][:10])


def run_batch(runtime: Path, end_date: str, asset_type: str, limit: int) -> bool:
    script = runtime / "scripts" / "collect_price_history.py"
    command = [
        sys.executable,
        str(script),
        "--runtime-root", str(runtime),
        "--asset-type", asset_type,
        "--start-date", "2019-01-01",
        "--end-date", end_date,
        "--limit", str(limit),
        "--minimum-existing-days", "1000",
        "--minimum-success-ratio", "0.80",
        "--sleep-seconds", "0.20",
    ]
    if asset_type == "stock":
        command.append("--dividend-only")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return completed.returncode == 0 and '"status": "published"' in completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--stock-batch-size", type=int, default=500)
    parser.add_argument("--etf-batch-size", type=int, default=100)
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser()
    end_date = latest_market_date(runtime)
    changed = []
    for asset_type, limit in (("stock", args.stock_batch_size), ("etf", args.etf_batch_size)):
        if limit <= 0:
            continue
        if run_batch(runtime, end_date, asset_type, limit):
            changed.append(asset_type)
    if changed:
        publisher = runtime / "scripts" / "publish_release.py"
        database = runtime / "data" / "database" / "high_dividend.db"
        release_id = f"release_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        command = [
            sys.executable,
            str(publisher),
            "--runtime-root", str(runtime),
            "--database", str(database),
            "--release-id", release_id,
            "--available-cutoff", f"{end_date}T15:00:00+08:00",
        ]
        completed = subprocess.run(command, text=True, check=False)
        return completed.returncode
    print(json.dumps({"status": "no_new_history_batch_published", "end_date": end_date}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
