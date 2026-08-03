"""Runtime and per-user storage path resolution."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_root() -> Path:
    # Deployed package lives directly under voice-dubbing-runtime.
    return Path(__file__).resolve().parents[1]


def user_data_root(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "FrameExtractStudio" / "VoiceDubbing"
    # This fallback keeps tests and non-Windows development deterministic.
    return Path.home() / ".local" / "share" / "FrameExtractStudio" / "VoiceDubbing"


def default_profiles_root(environ: dict[str, str] | None = None) -> Path:
    return user_data_root(environ) / "profiles"


def default_runs_root(environ: dict[str, str] | None = None) -> Path:
    return user_data_root(environ) / "runs"
