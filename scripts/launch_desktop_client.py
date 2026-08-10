#!/usr/bin/env python3
"""Install and open the local macOS desktop client after a preflight check."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = PROJECT_ROOT / "client" / "src-tauri" / "target" / "debug" / "bundle" / "macos" / "高股息研究.app"
DEFAULT_INSTALL = Path.home() / "Applications" / "高股息研究.app"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--install", action="store_true", help="Copy the app to ~/Applications before opening it")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    app = args.app.expanduser()
    if not app.is_dir():
        raise SystemExit(f"桌面客户端不存在，请先运行 npm run tauri -- build --debug：{app}")
    preflight = subprocess.run(["/usr/bin/python3", str(PROJECT_ROOT / "scripts" / "desktop_preflight.py")], cwd=PROJECT_ROOT)
    if preflight.returncode != 0:
        return preflight.returncode
    target = DEFAULT_INSTALL if args.install else app
    if args.install:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(app, target)
        print(f"已安装：{target}")
    if not args.no_open:
        subprocess.run(["open", str(target)], check=True)
        print(f"已启动：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
