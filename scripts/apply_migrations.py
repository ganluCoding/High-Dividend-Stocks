#!/usr/bin/env python3
"""Apply idempotent SQLite schema migrations for the local runtime."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT_ROOT / "database" / "migrations"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(database: Path) -> list[str]:
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, file_sha256 TEXT NOT NULL)")
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        completed: list[str] = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (path.name, datetime.now(timezone.utc).replace(microsecond=0).isoformat(), sha256(path)),
            )
            completed.append(path.name)
        conn.commit()
    return completed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data/database/high_dividend.db")
    args = parser.parse_args()
    print("applied:", ", ".join(apply(args.database)) or "none")
