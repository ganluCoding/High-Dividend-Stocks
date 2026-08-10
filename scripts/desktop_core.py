#!/usr/bin/env python3
"""Narrow JSON-stdio core for the desktop client; no HTTP server and no SQL exposure."""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from release_protocol import resolve_release
from run_screen import initialize_workbench
from strategy_engine import list_strategies, run_strategy


DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "HighDividend"
DEFAULT_WORKBENCH = DEFAULT_RUNTIME_ROOT / "workbench.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dashboard(runtime_root: Path, workbench: Path) -> dict[str, Any]:
    _, manifest = resolve_release(runtime_root)
    initialize_workbench(workbench)
    coverage = manifest["coverage"]
    latest_runs: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(workbench) as connection:
        for row in connection.execute(
            """SELECT s.strategy_id, s.strategy_version, s.screen_run_id, s.created_at
               FROM strategy_runs_v1 s JOIN (
                 SELECT strategy_id, MAX(created_at) created_at FROM strategy_runs_v1 WHERE release_id=? GROUP BY strategy_id
               ) latest ON s.strategy_id=latest.strategy_id AND s.created_at=latest.created_at""", (manifest["release_id"],)
        ):
            latest_runs[row[0]] = {"strategy_version": row[1], "screen_run_id": row[2], "created_at": row[3]}
    return {"release": {"release_id": manifest["release_id"], "available_cutoff": manifest["available_cutoff"], "facts_sha256": manifest["facts_sha256"], "coverage": coverage}, "strategies": list_strategies(), "latest_runs": latest_runs}


def candidates(workbench: Path, strategy_run_id: str) -> dict[str, Any]:
    initialize_workbench(workbench)
    with sqlite3.connect(workbench) as connection:
        row = connection.execute("SELECT release_id, screen_run_id FROM strategy_runs_v1 WHERE strategy_run_id=?", (strategy_run_id,)).fetchone()
        if row is None:
            raise ValueError("Strategy run does not exist")
        records = connection.execute(
            """SELECT instrument_id, research_state, reason_codes_json, research_priority, payload_json
               FROM screen_results_v1 WHERE screen_run_id=?
               ORDER BY CASE research_state WHEN '资料足以研究' THEN 0 WHEN '继续观察' THEN 1 WHEN '不纳入本模板' THEN 2 ELSE 3 END,
                        research_priority IS NULL, research_priority, instrument_id""", (row[1],)
        ).fetchall()
    values = [{"instrument_id": item[0], "research_state": item[1], "reason_codes": json.loads(item[2]), "research_priority": item[3], "payload": json.loads(item[4])} for item in records]
    return {"release_id": row[0], "strategy_run_id": strategy_run_id, "candidates": values}


def research_card(workbench: Path, strategy_run_id: str, instrument_id: str) -> dict[str, Any]:
    response = candidates(workbench, strategy_run_id)
    item = next((candidate for candidate in response["candidates"] if candidate["instrument_id"] == instrument_id), None)
    if item is None:
        raise ValueError("Instrument is not in this strategy run")
    return {"release_id": response["release_id"], "strategy_run_id": strategy_run_id, "candidate": item,
            "unanswered_questions": ["尚未进行估值判断", "历史现金分配不代表未来承诺", "请按下一次公告或年报复核"],
            "disclaimer": "符合研究条件，不等于买入建议。"}


def save_watch(workbench: Path, release_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    initialize_workbench(workbench)
    watch_id = str(uuid.uuid4())
    now = utc_now()
    with sqlite3.connect(workbench) as connection:
        connection.execute("INSERT INTO watch_items_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            watch_id, payload["instrument_id"], release_id, str(payload.get("note", "")), payload.get("review_condition"), payload.get("review_due_date"), now, now,
        ))
        connection.commit()
    return {"watch_item_id": watch_id, "release_id": release_id}


def dispatch(command: str, payload: dict[str, Any], runtime_root: Path, workbench: Path) -> dict[str, Any]:
    if command == "dashboard":
        return dashboard(runtime_root, workbench)
    if command == "run_strategy":
        requested_release = payload.get("release_id")
        if not requested_release:
            raise ValueError("release_id is required for strategy runs")
        _, manifest = resolve_release(runtime_root, str(requested_release))
        return run_strategy(payload["strategy_id"], runtime_root, workbench, manifest["release_id"])
    if command == "candidates":
        return candidates(workbench, payload["strategy_run_id"])
    if command == "research_card":
        return research_card(workbench, payload["strategy_run_id"], payload["instrument_id"])
    if command == "save_watch":
        _, manifest = resolve_release(runtime_root, payload["release_id"])
        return save_watch(workbench, manifest["release_id"], payload)
    raise ValueError(f"Unsupported command: {command}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--workbench", type=Path, default=DEFAULT_WORKBENCH)
    args = parser.parse_args()
    try:
        result = dispatch(args.command, json.loads(args.payload), args.runtime_root.expanduser(), args.workbench.expanduser())
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
