"""Small dependency-free state and runtime-payload helpers for the GUI."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class ProfileMode(str, Enum):
    CREATE_NEW = "CREATE_NEW"
    UPDATE_EXISTING_REFERENCE = "UPDATE_EXISTING_REFERENCE"


class ProfilePageState(str, Enum):
    IDLE = "IDLE"
    PREPARING_REFERENCE = "PREPARING_REFERENCE"
    AUTO_SUGGESTION_NEEDS_MANUAL = "AUTO_SUGGESTION_NEEDS_MANUAL"
    REFERENCE_REVIEW = "REFERENCE_REVIEW"
    COMMITTING = "COMMITTING"
    READY = "READY"


class SynthesisPageState(str, Enum):
    IDLE = "IDLE"
    SYNTHESIZING = "SYNTHESIZING"
    RESULT_READY = "RESULT_READY"


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
    ".ts",
    ".mts",
    ".m2ts",
}


def source_type_for_path(path: str | Path) -> str | None:
    suffix = Path(path).suffix.casefold()
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def profile_status(profile: dict[str, Any]) -> str:
    return str(profile.get("profile_status") or profile.get("status") or "UNKNOWN").upper()


def is_ready_profile(profile: dict[str, Any]) -> bool:
    return bool(profile.get("valid", True)) and profile.get("enabled", True) is True and profile_status(profile) == "READY"


def absolute_profile_asset(profile: dict[str, Any], role: str = "primary") -> Path | None:
    profile_path = profile.get("profile_path")
    if not isinstance(profile_path, str) or not profile_path:
        return None
    record: Any = None
    assets = profile.get("reference_assets")
    if isinstance(assets, dict):
        record = assets.get(role)
    if record is None and role == "primary":
        references = profile.get("reference_files")
        if isinstance(references, list) and references:
            record = references[0]
    relative = record.get("path") if isinstance(record, dict) else record
    if not isinstance(relative, str) or not relative:
        return None
    candidate = Path(relative)
    return candidate if candidate.is_absolute() else Path(profile_path) / candidate


def artifact_path(record: Any) -> Path | None:
    raw = record.get("path") if isinstance(record, dict) else record
    return Path(raw) if isinstance(raw, str) and raw else None


def available_languages(
    engines: Iterable[dict[str, Any]], engine_id: str = "auto"
) -> list[str]:
    values: list[str] = []
    for engine in engines:
        if engine_id != "auto" and engine.get("id") != engine_id:
            continue
        if engine.get("available") is not True:
            continue
        for value in engine.get("languages", []):
            code = str(value).strip().lower().replace("_", "-")
            if code and code not in values:
                values.append(code)
    return values


STAGE_LABELS = {
    "probe_source": "Đang kiểm tra nguồn",
    "normalize_audio": "Đang chuẩn hóa audio",
    "select_reference": "Đang chọn reference",
    "cut_candidate": "Đang cắt đoạn giọng",
    "separate_background": "Đang tách giọng khỏi nền",
    "source_separation_load_model": "Đang tải bộ tách nền",
    "source_separation_complete": "Đã tách giọng khỏi nền",
    "validate_voice_only": "Đang kiểm tra reference",
    "await_manual_reference": "Cần chọn đoạn thủ công",
    "await_listening": "Chờ bạn nghe xác nhận",
    "revalidate_reference": "Đang kiểm tra lại reference",
    "commit_profile_reference": "Đang lưu profile",
    "reference_ready": "Profile đã sẵn sàng",
    "load_profile": "Đang tải profile",
    "load_model": "Đang tải model",
    "model_ready": "Model đã sẵn sàng",
    "synthesize": "Đang tạo giọng",
    "validate": "Đang kiểm tra output",
    "failed": "Tác vụ thất bại",
}


def stage_label(name: str) -> str:
    return STAGE_LABELS.get(name, name.replace("_", " "))
