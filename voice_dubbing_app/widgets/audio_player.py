from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStyle, QWidget


def _clock(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class AudioPlayer(QWidget):
    """Reusable in-process player for runtime WAV artifacts."""

    source_changed = Signal(str)

    def __init__(self, title: str = "Audio", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AudioPlayer")
        self._source: Path | None = None
        self._title = title
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.85)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)

        self.name_label = QLabel(f"{title}: Không có")
        self.name_label.setMinimumWidth(110)
        self.play_button = QPushButton()
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.setIconSize(QSize(12, 12))
        self.play_button.setToolTip("Phát / Tạm dừng")
        self.play_button.setFixedWidth(38)
        self.stop_button = QPushButton()
        self.stop_button.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_button.setIconSize(QSize(12, 12))
        self.stop_button.setToolTip("Dừng")
        self.stop_button.setFixedWidth(38)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("PlayerTime")
        self.error_label = QLabel("")
        self.error_label.setObjectName("AudioError")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(5)
        layout.addWidget(self.name_label, 1)
        layout.addWidget(self.play_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.time_label)
        layout.addWidget(self.error_label, 1)

        self.play_button.clicked.connect(self.play_pause)
        self.stop_button.clicked.connect(self.stop)
        self.player.positionChanged.connect(self._update_time)
        self.player.durationChanged.connect(self._update_time)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.errorOccurred.connect(self._media_error)
        self._set_enabled(False)

    @property
    def source_path(self) -> Path | None:
        return self._source

    def _set_enabled(self, enabled: bool) -> None:
        self.play_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)
        self.play_button.setVisible(enabled)
        self.stop_button.setVisible(enabled)
        self.time_label.setVisible(enabled)

    def set_source(self, path: str | Path | None) -> None:
        self.stop()
        self.error_label.clear()
        candidate = Path(path).expanduser() if path else None
        if candidate is None or not candidate.is_file():
            self._source = None
            self.player.setSource(QUrl())
            self.name_label.setText(f"{self._title}: Không có")
            self.time_label.setText("00:00 / 00:00")
            self._set_enabled(False)
            if candidate is not None:
                self.error_label.setText("File không tồn tại")
            self.source_changed.emit("")
            return
        self._source = candidate.resolve()
        self.player.setSource(QUrl.fromLocalFile(str(self._source)))
        self.name_label.setText(f"{self._title}: {self._source.name}")
        self.time_label.setText("00:00 / 00:00")
        self._set_enabled(True)
        self.source_changed.emit(str(self._source))

    def play_pause(self) -> None:
        if self._source is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        self.player.stop()

    def clear(self) -> None:
        self.set_source(None)
        # Windows Media Foundation can retain an open WAV handle until its
        # source-change event is processed. Flush that event so profile/run
        # folders can be moved or cleaned immediately after closing the app.
        QCoreApplication.processEvents()

    def _update_time(self, _value: int = 0) -> None:
        self.time_label.setText(
            f"{_clock(self.player.position())} / {_clock(self.player.duration())}"
        )

    def _playback_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        icon = QStyle.SP_MediaPause if state == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))

    def _media_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if message:
            self.error_label.setText(message[:100])


__all__ = ["AudioPlayer"]
