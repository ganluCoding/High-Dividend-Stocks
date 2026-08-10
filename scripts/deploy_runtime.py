#!/usr/bin/env python3
"""Deploy an ASCII-path, launchd-readable local runtime outside Documents."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = Path.home() / "Library" / "Application Support" / "HighDividend"


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--refresh-database", action="store_true", help="Replace the runtime database with the current workspace seed database.")
    args = parser.parse_args()
    runtime = args.runtime_root
    for relative in (
        "scripts/autonomous_update.py",
        "scripts/autonomous_low_frequency.py",
        "scripts/apply_migrations.py",
        "scripts/release_protocol.py",
        "scripts/publish_release.py",
        "scripts/run_screen.py",
        "scripts/strategy_engine.py",
        "scripts/desktop_core.py",
        "scripts/collect_price_history.py",
        "scripts/autonomous_history_backfill.py",
        "scripts/run_mvp_backtest.py",
        "scripts/install_release_publisher_agent.py",
        "scripts/collect_spike_data.py",
        "config/autonomous_update.json",
        "config/candidate_universe_core.json",
        "config/backtest_mvp.json",
    ):
        copy_file(PROJECT_ROOT / relative, runtime / relative)
    shutil.copytree(PROJECT_ROOT / "database" / "migrations", runtime / "database" / "migrations", dirs_exist_ok=True)
    copy_file(PROJECT_ROOT / "database" / "workbench_schema.sql", runtime / "database" / "workbench_schema.sql")
    shutil.copytree(PROJECT_ROOT / "rules", runtime / "rules", dirs_exist_ok=True)
    shutil.copytree(PROJECT_ROOT / "strategies", runtime / "strategies", dirs_exist_ok=True)
    runtime_db = runtime / "data" / "database" / "high_dividend.db"
    if args.refresh_database or not runtime_db.exists():
        copy_file(PROJECT_ROOT / "data" / "database" / "high_dividend.db", runtime_db)
    for relative in ("data/raw", "data/runtime", "data/database/releases", "releases", "logs"):
        (runtime / relative).mkdir(parents=True, exist_ok=True)
    print(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
