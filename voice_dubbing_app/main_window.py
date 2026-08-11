from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QElapsedTimer, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QFrame,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .pages.profiles_page import ProfilesPage
from .pages.synthesis_page import SynthesisPage
from .runtime_client import RuntimeClient
from .theme import refresh_style
from .view_models import stage_label
from .widgets.log_panel import LogPanel


class MainWindow(QMainWindow):
    def __init__(
        self,
        client: RuntimeClient | None = None,
        *,
        auto_refresh: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client or RuntimeClient(parent=self)
        self.capabilities: dict[str, Any] = {}
        self.profiles: list[dict[str, Any]] = []
        self.elapsed_timer = QElapsedTimer()
        self.elapsed_update = QTimer(self)
        self.elapsed_update.setInterval(250)
        self.elapsed_update.timeout.connect(self._update_elapsed)
        self._build_ui()
        self._connect_client()
        if auto_refresh:
            QTimer.singleShot(0, self.client.refresh_all)

    def _build_ui(self) -> None:
        self.setWindowTitle("Voice Dubbing")
        self.resize(1180, 780)
        self.setMinimumSize(1040, 680)

        title = QLabel("Voice Dubbing")
        title.setObjectName("AppTitle")
        self.runtime_label = QLabel("Runtime: Đang kiểm tra…")
        self.runtime_label.setObjectName("RuntimeState")
        self.runtime_label.setProperty("statusKind", "warning")
        self.engines_label = QLabel("Engines: —")
        self.profiles_label = QLabel("Profiles: —")
        self.engines_label.setObjectName("HeaderMeta")
        self.profiles_label.setObjectName("HeaderMeta")
        self.refresh_button = QPushButton("Làm mới")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_button.setToolTip("Tải lại capability và danh sách profile")
        header_frame = QFrame()
        header_frame.setObjectName("AppHeader")
        header = QHBoxLayout(header_frame)
        header.setContentsMargins(10, 6, 8, 6)
        header.setSpacing(8)
        header.addWidget(title)
        header.addSpacing(8)
        header.addWidget(self.runtime_label)
        separator = QLabel("|")
        separator.setObjectName("MutedLabel")
        header.addWidget(separator)
        header.addWidget(self.engines_label)
        separator = QLabel("|")
        separator.setObjectName("MutedLabel")
        header.addWidget(separator)
        header.addWidget(self.profiles_label)
        header.addStretch(1)
        header.addWidget(self.refresh_button)

        self.profiles_page = ProfilesPage(self.client)
        self.synthesis_page = SynthesisPage(self.client)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.profiles_page, "HỒ SƠ GIỌNG")
        self.tabs.addTab(self.synthesis_page, "TẠO GIỌNG")

        self.stage_label_widget = QLabel("Sẵn sàng")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMinimumWidth(200)
        self.elapsed_label = QLabel("00:00")
        self.cancel_button = QPushButton("Hủy")
        self.cancel_button.setEnabled(False)
        self.log_toggle = QToolButton()
        self.log_toggle.setText("Nhật ký ▾")
        self.log_toggle.setCheckable(True)
        status_frame = QFrame()
        status_frame.setObjectName("StatusBar")
        status_row = QHBoxLayout(status_frame)
        status_row.setContentsMargins(8, 3, 5, 3)
        status_row.setSpacing(7)
        status_row.addWidget(self.stage_label_widget, 1)
        status_row.addWidget(self.progress, 2)
        status_row.addWidget(self.elapsed_label)
        status_row.addWidget(self.cancel_button)
        status_row.addWidget(self.log_toggle)

        self.log_panel = LogPanel()
        self.log_panel.setVisible(False)
        self.log_panel.setMaximumHeight(230)

        central = QWidget()
        central.setObjectName("CentralRoot")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(header_frame)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(status_frame)
        layout.addWidget(self.log_panel)
        self.setCentralWidget(central)

        self.refresh_button.clicked.connect(self.client.refresh_all)
        self.cancel_button.clicked.connect(self.client.cancel_active_job)
        self.log_toggle.toggled.connect(self._toggle_log)
        self.profiles_page.use_profile_requested.connect(self._use_profile)
        self.profiles_page.status_message.connect(self._inline_status)

    def _connect_client(self) -> None:
        self.client.runtime_status.connect(self._runtime_status)
        self.client.capabilities_ready.connect(self._capabilities_ready)
        self.client.profiles_ready.connect(self._profiles_ready)
        self.client.job_started.connect(self._job_started)
        self.client.stage_changed.connect(self._stage_changed)
        self.client.job_result.connect(self._job_result)
        self.client.job_error.connect(self._job_error)
        self.client.busy_changed.connect(self._busy_changed)
        self.client.cancelled.connect(self._cancelled)
        self.client.log_line.connect(self.log_panel.append_message)

    def _toggle_log(self, visible: bool) -> None:
        self.log_panel.setVisible(visible)
        self.log_toggle.setText("Nhật ký ▴" if visible else "Nhật ký ▾")

    def _runtime_status(self, payload: dict[str, Any]) -> None:
        if payload.get("available") is True:
            self.runtime_label.setText("Runtime: Đang tải capability…")
            self._set_runtime_kind("warning")
            self.runtime_label.setToolTip(str(payload.get("python_executable", "")))
        else:
            self.runtime_label.setText("Runtime: Unavailable")
            self._set_runtime_kind("danger")
            self.runtime_label.setToolTip(str(payload.get("reason", "")))

    def _capabilities_ready(self, payload: dict[str, Any]) -> None:
        self.capabilities = dict(payload)
        engines = payload.get("engines") if isinstance(payload.get("engines"), list) else []
        available = sum(1 for item in engines if item.get("available") is True)
        total = len(engines)
        runtime_available = payload.get("runtime", {}).get("available") is True
        if not runtime_available:
            status = "Unavailable"
        elif total > 0 and available == total:
            status = "Ready"
        else:
            status = "Partial"
        self.runtime_label.setText(f"Runtime: {status}")
        self._set_runtime_kind(
            "success" if status == "Ready" else "warning" if status == "Partial" else "danger"
        )
        reasons = [
            f"{item.get('display_name', item.get('id'))}: {item.get('unavailable_reason')}"
            for item in engines
            if item.get("available") is not True
        ]
        self.runtime_label.setToolTip("\n".join(reasons))
        self.engines_label.setText(f"Engines: {available}/{total} available")

    def _set_runtime_kind(self, kind: str) -> None:
        self.runtime_label.setProperty("statusKind", kind)
        refresh_style(self.runtime_label)

    def _profiles_ready(self, profiles: list[dict[str, Any]]) -> None:
        self.profiles = list(profiles)
        self.profiles_label.setText(f"Profiles: {len(profiles)}")

    def _job_started(self, job: dict[str, Any]) -> None:
        self.elapsed_timer.start()
        self.elapsed_update.start()
        self.progress.setValue(0)
        self.stage_label_widget.setText(
            f"Đã bắt đầu {job.get('action', 'job')} ({job.get('job_id', '')})"
        )

    def _stage_changed(self, name: str, progress: float) -> None:
        self.stage_label_widget.setText(stage_label(name))
        self.progress.setValue(round(max(0.0, min(1.0, progress)) * 1000))

    def _job_result(self, result: dict[str, Any]) -> None:
        self.progress.setValue(1000)
        self.stage_label_widget.setText("Hoàn tất")
        result_path = result.get("result_path")
        if isinstance(result_path, str):
            self.log_panel.set_run_path(result_path)

    def _job_error(self, error: dict[str, Any]) -> None:
        code = str(error.get("code") or "RUNTIME_ERROR")
        message = str(error.get("message") or "Runtime error")
        self.stage_label_widget.setText(f"{code}: {message}")
        self.log_panel.append_message(f"{code}: {message}")

    def _busy_changed(self, busy: bool) -> None:
        self.cancel_button.setEnabled(busy)
        self.refresh_button.setEnabled(not busy)
        if not busy:
            self.elapsed_update.stop()
            self._update_elapsed()

    def _cancelled(self) -> None:
        self.stage_label_widget.setText("Tác vụ đã hủy")
        self.progress.setValue(0)

    def _update_elapsed(self) -> None:
        milliseconds = self.elapsed_timer.elapsed() if self.elapsed_timer.isValid() else 0
        seconds = max(0, milliseconds // 1000)
        self.elapsed_label.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")

    def _use_profile(self, profile_id: str) -> None:
        self.synthesis_page.select_profile(profile_id)
        self.tabs.setCurrentWidget(self.synthesis_page)

    def _inline_status(self, message: str) -> None:
        if not self.client.is_busy:
            self.stage_label_widget.setText(message)

    def stop_audio(self) -> None:
        self.profiles_page.stop_audio()
        self.synthesis_page.stop_audio()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.stop_audio()
        self.client.shutdown(timeout_ms=5000)
        event.accept()


__all__ = ["MainWindow"]
