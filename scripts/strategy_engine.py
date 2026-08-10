#!/usr/bin/env python3
"""Versioned strategy-library runner for local, non-advisory research workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_protocol import resolve_release, sha256_file, utc_now
from run_screen import initialize_workbench, record_artifact


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
DEFAULT_WORKBENCH = DEFAULT_RUNTIME_ROOT / "workbench.db"


def load_strategies() -> list[tuple[Path, dict[str, Any]]]:
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in sorted((PROJECT_ROOT / "strategies").glob("*.json"))]


def list_strategies() -> list[dict[str, Any]]:
    return [document for _, document in load_strategies()]


def run_strategy(strategy_id: str, runtime_root: Path, workbench: Path, release_id: str | None = None) -> dict[str, Any]:
    matching = [(path, document) for path, document in load_strategies() if document["strategy_id"] == strategy_id]
    if len(matching) != 1:
        raise ValueError(f"Unknown strategy_id: {strategy_id}")
    strategy_path, strategy = matching[0]
    release_dir, manifest = resolve_release(runtime_root, release_id)
    rule_path = PROJECT_ROOT / "rules" / strategy["rule_file"]
    command = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "run_screen.py"), "--runtime-root", str(runtime_root),
        "--workbench", str(workbench), "--release-id", manifest["release_id"], "--rule", str(rule_path),
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=True, text=True, capture_output=True)
    screen_summary = json.loads(completed.stdout)
    strategy_sha = sha256_file(strategy_path)
    run_material = f"{manifest['release_id']}:{manifest['facts_sha256']}:{strategy_sha}:{screen_summary['screen_run_id']}"
    strategy_run_id = hashlib.sha256(run_material.encode()).hexdigest()[:24]
    initialize_workbench(workbench)
    with sqlite3.connect(workbench) as connection:
        record_artifact(connection, "strategy", f"{strategy['strategy_id']}@{strategy['strategy_version']}", strategy_path)
        connection.execute("INSERT OR IGNORE INTO strategy_versions_v1 VALUES (?, ?, ?, ?, ?, ?)", (
            strategy["strategy_id"], strategy["strategy_version"], strategy_sha, strategy["name"], strategy["lane"], utc_now(),
        ))
        connection.execute("INSERT OR REPLACE INTO strategy_runs_v1 VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')", (
            strategy_run_id, manifest["release_id"], manifest["facts_sha256"], strategy["strategy_id"], strategy["strategy_version"], screen_summary["screen_run_id"], utc_now(),
        ))
        connection.commit()
    return {"status": "completed", "strategy_run_id": strategy_run_id, "strategy": strategy, "release_id": manifest["release_id"], "screen": screen_summary}


def main() -> int:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--list", action="store_true")
    actions.add_argument("--run", metavar="STRATEGY_ID")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--workbench", type=Path, default=DEFAULT_WORKBENCH)
    parser.add_argument("--release-id")
    args = parser.parse_args()
    if args.list:
        print(json.dumps({"strategies": list_strategies()}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(run_strategy(args.run, args.runtime_root.expanduser(), args.workbench.expanduser(), args.release_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
