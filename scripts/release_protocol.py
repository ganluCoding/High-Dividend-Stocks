"""Immutable SQLite release helpers shared by publisher and screening commands."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sqlite_backup(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination_path) as destination:
        source.backup(destination)


def integrity_check(database: Path) -> None:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite integrity_check failed for {database}: {result}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_release(release_dir: Path) -> dict[str, Any]:
    facts = release_dir / "facts.sqlite"
    manifest_path = release_dir / "manifest.json"
    manifest_hash_path = release_dir / "manifest.sha256"
    facts_hash_path = release_dir / "facts.sqlite.sha256"
    ready = release_dir / "READY"
    for required in (facts, manifest_path, manifest_hash_path, facts_hash_path, ready):
        if not required.is_file():
            raise RuntimeError(f"Release is incomplete: missing {required.name}")
    manifest = read_json(manifest_path)
    actual_facts_hash = sha256_file(facts)
    expected_facts_hash = facts_hash_path.read_text(encoding="utf-8").strip()
    if actual_facts_hash != expected_facts_hash or actual_facts_hash != manifest.get("facts_sha256"):
        raise RuntimeError("Release facts.sqlite hash does not match its manifest")
    if facts.stat().st_size != manifest.get("facts_size_bytes"):
        raise RuntimeError("Release facts.sqlite size does not match its manifest")
    actual_manifest_hash = sha256_file(manifest_path)
    if actual_manifest_hash != manifest_hash_path.read_text(encoding="utf-8").strip():
        raise RuntimeError("Release manifest hash does not match")
    integrity_check(facts)
    return manifest


def resolve_release(runtime_root: Path, release_id: str | None = None) -> tuple[Path, dict[str, Any]]:
    if release_id is None:
        pointer = read_json(runtime_root / "current-release.json")
        release_id = pointer["release_id"]
    release_dir = runtime_root / "releases" / release_id
    manifest = validate_release(release_dir)
    if manifest.get("release_id") != release_id:
        raise RuntimeError("Release id and manifest do not match")
    return release_dir, manifest
