"""Runtime and per-user storage path resolution."""

from __future__ import annotations

import os
import json
from pathlib import Path


STANDALONE_DIRECTORY = "VoiceDubbingRuntime"
LEGACY_PARENT_DIRECTORY = "FrameExtractStudio"
LEGACY_DIRECTORY = "VoiceDubbing"
MIGRATION_MARKER = "migration.json"


def runtime_root() -> Path:
    # Deployed package lives directly under voice-dubbing-runtime.
    return Path(__file__).resolve().parents[1]


def _local_data_base(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data)
    return Path.home() / ".local" / "share"


def standalone_user_data_root(environ: dict[str, str] | None = None) -> Path:
    return _local_data_base(environ) / STANDALONE_DIRECTORY


def legacy_user_data_root(environ: dict[str, str] | None = None) -> Path:
    return _local_data_base(environ) / LEGACY_PARENT_DIRECTORY / LEGACY_DIRECTORY


def migration_is_complete(root: Path) -> bool:
    marker = root / MIGRATION_MARKER
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("schema_version") == 1 and payload.get("status") == "COMPLETE"


def user_data_root(environ: dict[str, str] | None = None) -> Path:
    """Return the effective store without mutating or migrating either root."""

    standalone = standalone_user_data_root(environ)
    legacy = legacy_user_data_root(environ)
    if migration_is_complete(standalone) or not legacy.exists():
        return standalone
    return legacy


def default_profiles_root(environ: dict[str, str] | None = None) -> Path:
    return user_data_root(environ) / "profiles"


def default_runs_root(environ: dict[str, str] | None = None) -> Path:
    return user_data_root(environ) / "runs"


def default_xtts_license_path(environ: dict[str, str] | None = None) -> Path:
    return user_data_root(environ) / "licenses" / "coqui_xtts_v2_cpml.json"
