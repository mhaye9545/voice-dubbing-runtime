"""Copy-and-verify migration from the legacy application data namespace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import (
    MIGRATION_MARKER,
    legacy_user_data_root,
    migration_is_complete,
    standalone_user_data_root,
    user_data_root,
)


MIGRATABLE_SUBTREES = ("profiles", "runs", "licenses", "config", "state")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _manifest(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for subtree in MIGRATABLE_SUBTREES:
        subtree_root = root / subtree
        if not subtree_root.is_dir():
            continue
        for path in sorted(item for item in subtree_root.rglob("*") if item.is_file()):
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return records


def _write_marker(target: Path, payload: dict[str, Any]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    marker = target / MIGRATION_MARKER
    temporary = target / f".{MIGRATION_MARKER}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def _marker_payload(
    source: Path,
    target: Path,
    *,
    status: str,
    file_count: int,
    files_copied: int,
    verified_count: int,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source_root": source.name,
        "target_root": target.name,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "file_count": file_count,
        "files_copied": files_copied,
        "verified_count": verified_count,
        "hash_verification": {
            "algorithm": "SHA-256",
            "verified_count": verified_count,
            "status": "PASS" if status == "COMPLETE" else "INCOMPLETE",
        },
        "status": status,
    }
    if error:
        payload["error"] = error
    return payload


def _copy_missing_file(source: Path, target: Path) -> bool:
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.migration.tmp"
    try:
        shutil.copy2(source, temporary)
        if _sha256(temporary) != _sha256(source):
            raise OSError(f"Hash mismatch while copying {source.name}")
        os.rename(temporary, target)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def migrate_storage(
    source_root: Path | None = None,
    target_root: Path | None = None,
) -> dict[str, Any]:
    """Copy legacy user data, verify every hash, then atomically mark COMPLETE."""

    source = (source_root or legacy_user_data_root()).resolve()
    target = (target_root or standalone_user_data_root()).resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Storage migration roots must be distinct and non-nested.")

    if not source.exists():
        return {
            "schema_version": 1,
            "status": "NO_SOURCE",
            "source_root": str(source),
            "target_root": str(target),
            "file_count": 0,
            "files_copied": 0,
            "verified_count": 0,
            "source_preserved": True,
        }

    source_manifest = _manifest(source)
    source_by_path = {record["path"]: record for record in source_manifest}
    copied = 0
    verified = 0
    try:
        for record in source_manifest:
            source_file = source / record["path"]
            target_file = target / record["path"]
            if target_file.exists():
                if not target_file.is_file() or _sha256(target_file) != record["sha256"]:
                    raise OSError(f"Target hash conflict: {record['path']}")
            elif _copy_missing_file(source_file, target_file):
                copied += 1

        target_manifest = _manifest(target)
        target_by_path = {record["path"]: record for record in target_manifest}
        for relative, source_record in source_by_path.items():
            target_record = target_by_path.get(relative)
            if target_record is None or target_record["sha256"] != source_record["sha256"]:
                raise OSError(f"Post-copy hash mismatch: {relative}")
            verified += 1

        payload = _marker_payload(
            source,
            target,
            status="COMPLETE",
            file_count=len(source_manifest),
            files_copied=copied,
            verified_count=verified,
        )
        _write_marker(target, payload)
    except OSError as exc:
        payload = _marker_payload(
            source,
            target,
            status="FAILED",
            file_count=len(source_manifest),
            files_copied=copied,
            verified_count=verified,
            error=str(exc),
        )
        _write_marker(target, payload)
        payload.update(
            {
                "source_root": str(source),
                "target_root": str(target),
                "source_preserved": source.exists(),
                "effective_root": str(source),
            }
        )
        return payload

    payload.update(
        {
            "source_root": str(source),
            "target_root": str(target),
            "source_preserved": source.exists(),
            "effective_root": str(target),
        }
    )
    return payload


def storage_status() -> dict[str, Any]:
    legacy = legacy_user_data_root().resolve()
    standalone = standalone_user_data_root().resolve()
    effective = user_data_root().resolve()
    return {
        "schema_version": 1,
        "legacy_root": str(legacy),
        "legacy_exists": legacy.exists(),
        "standalone_root": str(standalone),
        "standalone_exists": standalone.exists(),
        "migration_complete": migration_is_complete(standalone),
        "effective_root": str(effective),
        "fallback_legacy": effective == legacy,
    }
