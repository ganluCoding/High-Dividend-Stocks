#!/usr/bin/env python3
"""Print a compact, machine-readable progress view for data backfill."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"


def bar(ratio: float, width: int = 32) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(width * ratio))
    return f"[{'█' * filled}{'░' * (width - filled)}] {ratio * 100:5.1f}%"


def coverage(runtime: Path) -> dict[str, Any]:
    pointer = json.loads((runtime / "current-release.json").read_text(encoding="utf-8"))
    release_id = pointer["release_id"]
    facts = runtime / "releases" / release_id / "facts.sqlite"
    with sqlite3.connect(facts) as connection:
        matrix = {
            str(dataset): {"instruments": int(instruments), "collected": int(collected), "ratio": (float(collected) / float(instruments) if instruments else 0.0)}
            for dataset, instruments, collected in connection.execute(
                "SELECT dataset, COUNT(*), SUM(collection_status='collected') FROM coverage_matrix GROUP BY dataset"
            )
        }
        history = {
            str(asset_type): {"instruments": int(total), "five_year": int(five_year), "with_any_history": int(with_any_history), "ratio": (float(five_year) / float(total) if total else 0.0)}
            for asset_type, total, five_year, with_any_history in connection.execute(
                """SELECT i.asset_type, COUNT(*), SUM(CASE WHEN COALESCE(x.days, 0) >= 1250 THEN 1 ELSE 0 END), SUM(CASE WHEN COALESCE(x.days, 0) > 0 THEN 1 ELSE 0 END)
                   FROM instruments i LEFT JOIN (SELECT instrument_id, COUNT(DISTINCT trade_date) days FROM price_daily WHERE adjustment='unadjusted' GROUP BY instrument_id) x USING (instrument_id)
                   WHERE i.active=1
                   GROUP BY i.asset_type"""
            )
        }
    return {"release_id": release_id, "datasets": matrix, "price_history_5y": history}


def print_progress(snapshot: dict[str, Any]) -> None:
    datasets = snapshot["datasets"]
    history = snapshot["price_history_5y"]
    print(f"发布版本：{snapshot['release_id']}")
    rows = [
        ("全市场最新价格", datasets.get("market_prices", {})),
        ("股票历史价格（5年）", history.get("stock", {})),
        ("ETF历史价格（5年）", history.get("etf", {})),
        ("股票分红事件", datasets.get("stock_dividends", {})),
        ("股票财务事实", datasets.get("financials", {})),
        ("ETF分配事件", datasets.get("etf_distributions", {})),
        ("ETF产品事实", datasets.get("etf_product_facts", {})),
    ]
    for label, item in rows:
        ratio = float(item.get("ratio", 0.0))
        print(f"{label:<20} {bar(ratio)} {item.get('collected', item.get('five_year', 0))}/{item.get('instruments', 0)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser()
    while True:
        snapshot = coverage(runtime)
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        else:
            print_progress(snapshot)
        if not args.watch:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
