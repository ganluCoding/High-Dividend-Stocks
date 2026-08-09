#!/usr/bin/env python3
"""Install/remove the local-only immutable release publisher LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.highdividend.publish-release"
TEMPLATE = PROJECT_ROOT / "deploy" / f"{LABEL}.plist.template"
RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--install", action="store_true")
    actions.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if args.uninstall:
        launchctl("bootout", domain, str(target))
        if target.exists():
            target.unlink()
        print(f"removed {target}")
        return 0
    script = RUNTIME_ROOT / "scripts" / "publish_release.py"
    source = RUNTIME_ROOT / "data" / "database" / "high_dividend.db"
    if not script.is_file() or not source.is_file():
        raise SystemExit("Runtime publisher or source database is missing; run deploy_runtime.py first.")
    payload = plistlib.loads(TEMPLATE.read_bytes())
    payload["ProgramArguments"] = ["/usr/bin/python3", str(script), "--runtime-root", str(RUNTIME_ROOT), "--database", str(source)]
    logs = RUNTIME_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload["StandardOutPath"] = str(logs / "publish-release.out.log")
    payload["StandardErrorPath"] = str(logs / "publish-release.err.log")
    target.parent.mkdir(parents=True, exist_ok=True)
    launchctl("bootout", domain, str(target))
    with target.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    result = launchctl("bootstrap", domain, str(target))
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "launchctl bootstrap failed")
    print(f"installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
