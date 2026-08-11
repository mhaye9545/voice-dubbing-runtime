from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy

from ..theme import refresh_style


class StatusBadge(QLabel):
    """Compact text-first status label; colour is only a secondary cue."""

    _SUCCESS = {"READY", "SUCCESS", "PASS"}
    _WARNING = {"TECHNICAL_PASS_PENDING_LISTENING", "NEEDS_MANUAL_REFERENCE"}
    _DANGER = {
        "BACKGROUND_AUDIO_DETECTED",
        "BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING",
        "SOURCE_SEPARATION_NO_EFFECT",
        "PROFILE_ERROR",
        "UNAVAILABLE",
    }

    def __init__(self, status: str = "UNKNOWN", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusBadge")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFixedHeight(22)
        self.set_status(status)

    def set_status(self, status: str) -> None:
        value = str(status or "UNKNOWN").upper()
        self.setText(value)
        tone = (
            "success"
            if value in self._SUCCESS
            else "warning"
            if value in self._WARNING
            else "danger"
            if value in self._DANGER
            else "neutral"
        )
        self.setProperty("tone", tone)
        refresh_style(self)


__all__ = ["StatusBadge"]
