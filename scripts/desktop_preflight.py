#!/usr/bin/env python3
"""Verify the local desktop runtime before opening the Tauri client."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser()
    required = [
        runtime / "scripts" / "desktop_core.py",
        runtime / "scripts" / "release_protocol.py",
        runtime / "current-release.json",
        runtime / "workbench.db",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print(json.dumps({"status": "blocked", "reason": "missing_runtime", "missing": missing}, ensure_ascii=False, indent=2))
        return 1
    command = [
        sys.executable,
        str(runtime / "scripts" / "desktop_core.py"),
        "--command",
        "dashboard",
        "--runtime-root",
        str(runtime),
        "--workbench",
        str(runtime / "workbench.db"),
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stdout or completed.stderr, end="")
        return completed.returncode
    payload = json.loads(completed.stdout)
    result = payload.get("result", {})
    print(json.dumps({
        "status": "ready",
        "runtime_root": str(runtime),
        "release_id": result.get("release", {}).get("release_id"),
        "strategy_count": len(result.get("strategies", [])),
        "latest_run_count": len(result.get("latest_runs", {})),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
