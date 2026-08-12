from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from voice_dubbing_app.theme import apply_theme


def application() -> QApplication:
    instance = QApplication.instance()
    application = instance or QApplication([])
    apply_theme(application)
    return application


class FakeRuntimeClient(QObject):
    runtime_status = Signal(dict)
    capabilities_ready = Signal(dict)
    profiles_ready = Signal(list)
    profile_command_result = Signal(str, dict)
    job_started = Signal(dict)
    stage_changed = Signal(str, float)
    job_result = Signal(dict)
    job_error = Signal(dict)
    busy_changed = Signal(bool)
    log_line = Signal(str)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[dict[str, Any]] = []
        self.refresh_profiles_count = 0
        self.refresh_all_count = 0
        self.deleted: list[str] = []
        self.cancel_count = 0
        self.shutdown_count = 0
        self.shutdown_timeout_ms: int | None = None
        self.is_busy = False

    def submit_job(self, job: dict[str, Any]) -> bool:
        if self.is_busy:
            return False
        self.jobs.append(job)
        return True

    def refresh_profiles(self) -> None:
        self.refresh_profiles_count += 1

    def refresh_all(self) -> None:
        self.refresh_all_count += 1

    def delete_profile(self, profile_id: str) -> None:
        self.deleted.append(profile_id)

    def cancel_active_job(self) -> None:
        self.cancel_count += 1

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self.shutdown_count += 1
        self.shutdown_timeout_ms = timeout_ms


def capability_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime": {"available": True, "version": "test"},
        "engines": [
            {
                "id": "vixtts_vi",
                "display_name": "viXTTS",
                "available": True,
                "languages": ["vi"],
                "devices": ["cpu"],
                "profile_types": ["cloned", "preset"],
                "supports_keep_model_loaded": False,
                "unavailable_reason": None,
            },
            {
                "id": "xtts_v2_multilingual",
                "display_name": "XTTS-v2",
                "available": True,
                "languages": ["en", "ko", "zh-cn"],
                "devices": ["cpu"],
                "profile_types": ["cloned", "preset"],
                "supports_keep_model_loaded": True,
                "unavailable_reason": None,
            },
            {
                "id": "offline_engine",
                "display_name": "Offline engine",
                "available": False,
                "languages": ["fr"],
                "devices": ["cpu"],
                "profile_types": ["cloned"],
                "supports_keep_model_loaded": True,
                "unavailable_reason": "MODEL_MISSING",
            },
        ],
    }


def profile_row(
    profile_id: str = "ready_voice",
    *,
    status: str = "READY",
    profile_type: str = "cloned",
    profile_path: str = "C:/test-data/profiles/sample_voice",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "display_name": profile_id.replace("_", " ").title(),
        "profile_type": profile_type,
        "source_type": "audio",
        "source_language": "vi",
        "default_language": "vi",
        "engine_preference": "auto",
        "reference_files": [{"path": "references/ref_primary.wav"}],
        "created_at": "2026-08-10T00:00:00Z",
        "updated_at": "2026-08-10T00:00:00Z",
        "enabled": True,
        "valid": True,
        "profile_status": status,
        "profile_revision": 3,
        "profile_path": profile_path,
    }
