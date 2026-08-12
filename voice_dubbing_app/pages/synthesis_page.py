from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..runtime_client import RuntimeClient
from ..theme import set_message_kind
from ..view_models import (
    SynthesisPageState,
    available_languages,
    is_ready_profile,
    profile_status,
)
from ..widgets.audio_player import AudioPlayer
from ..widgets.status_badge import StatusBadge


class SynthesisPage(QWidget):
    def __init__(self, client: RuntimeClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.state = SynthesisPageState.IDLE
        self.profiles: list[dict[str, Any]] = []
        self.capabilities: list[dict[str, Any]] = []
        self.pending_action: str | None = None
        self.output_path: Path | None = None
        self.last_inline_error = ""
        self._requested_profile_id: str | None = None
        self._build_ui()
        self._connect_client()

    def _build_ui(self) -> None:
        self.profile_combo = QComboBox()
        self.profile_status_badge = StatusBadge("UNKNOWN")
        self.language_combo = QComboBox()
        self.engine_combo = QComboBox()
        self.device_label = QLabel("CPU")
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.50, 2.00)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setValue(1.0)
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_147_483_647)
        self.seed_spin.setValue(42)
        self.keep_loaded_check = QCheckBox("Giữ model trong RAM")
        self.keep_loaded_check.setChecked(False)
        self.keep_loaded_check.setEnabled(False)

        form = QFormLayout()
        form.addRow("Voice profile", self.profile_combo)
        form.addRow("Trạng thái", self.profile_status_badge)
        form.addRow("Ngôn ngữ đầu ra", self.language_combo)
        form.addRow("Engine", self.engine_combo)
        form.addRow("Thiết bị", self.device_label)
        form.addRow("Tốc độ", self.speed_spin)
        form.addRow("Seed", self.seed_spin)
        form.addRow("", self.keep_loaded_check)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Nhập nội dung cần tạo giọng…")
        self.text_edit.setMinimumHeight(160)
        self.char_count = QLabel("0 ký tự")
        self.generate_button = QPushButton("Tạo audio")
        self.generate_button.setProperty("role", "primary")
        self.generate_button.setDefault(True)
        self.cancel_button = QPushButton("Hủy")
        self.cancel_button.setEnabled(False)
        action_row = QHBoxLayout()
        action_row.addWidget(self.generate_button)
        action_row.addWidget(self.cancel_button)
        action_row.addStretch(1)

        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.settings_box = QGroupBox("CÀI ĐẶT TẠO GIỌNG")
        self.settings_box.setMinimumWidth(350)
        settings_layout = QVBoxLayout(self.settings_box)
        settings_layout.setContentsMargins(10, 12, 10, 9)
        settings_layout.setSpacing(7)
        settings_layout.addLayout(form)
        settings_layout.addStretch(1)
        settings_layout.addWidget(self.message_label)
        settings_layout.addLayout(action_row)

        self.text_box = QGroupBox("VĂN BẢN ĐẦU VÀO")
        text_layout = QVBoxLayout(self.text_box)
        text_layout.setContentsMargins(10, 12, 10, 9)
        text_layout.setSpacing(5)
        text_layout.addWidget(self.text_edit, 1)
        text_layout.addWidget(self.char_count, 0, Qt.AlignRight)

        self.output_label = QLabel("Output: —")
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.metrics_label = QLabel("Duration: —    Elapsed: —    Peak RAM: —")
        self.result_player = AudioPlayer("Generated WAV")
        self.save_button = QPushButton("Lưu WAV thành…")
        self.open_button = QPushButton("Mở thư mục")
        self.save_button.setEnabled(False)
        self.open_button.setEnabled(False)
        result_actions = QHBoxLayout()
        result_actions.addWidget(self.save_button)
        result_actions.addWidget(self.open_button)
        result_actions.addStretch(1)

        self.result_box = QGroupBox("KẾT QUẢ")
        result_layout = QVBoxLayout(self.result_box)
        result_layout.setContentsMargins(10, 12, 10, 9)
        result_layout.setSpacing(5)
        result_layout.addWidget(self.output_label)
        result_layout.addWidget(self.metrics_label)
        result_layout.addWidget(self.result_player)
        result_layout.addLayout(result_actions)

        self.right_panel = QWidget()
        self.right_panel.setMinimumWidth(620)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(self.text_box, 1)
        right_layout.addWidget(self.result_box, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.settings_box)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 34)
        self.splitter.setStretchFactor(1, 66)
        self.splitter.setSizes([360, 700])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.splitter, 1)

        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        self.text_edit.textChanged.connect(self._text_changed)
        self.generate_button.clicked.connect(self.submit_synthesis)
        self.cancel_button.clicked.connect(self.client.cancel_active_job)
        self.save_button.clicked.connect(self.save_as)
        self.open_button.clicked.connect(self.open_output_folder)
        self._text_changed()

    def _connect_client(self) -> None:
        self.client.capabilities_ready.connect(self.set_capabilities)
        self.client.profiles_ready.connect(self.set_profiles)
        self.client.job_result.connect(self._job_result)
        self.client.job_error.connect(self._job_error)
        self.client.busy_changed.connect(self._busy_changed)
        self.client.cancelled.connect(self._cancelled)

    @staticmethod
    def _select_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_capabilities(self, payload: dict[str, Any]) -> None:
        values = payload.get("engines")
        self.capabilities = values if isinstance(values, list) else []
        current = self.engine_combo.currentData()
        self.engine_combo.blockSignals(True)
        self.engine_combo.clear()
        self.engine_combo.addItem("Tự động", "auto")
        for engine in self.capabilities:
            label = str(engine.get("display_name") or engine.get("id"))
            available = engine.get("available") is True
            if not available:
                label += " — unavailable"
            self.engine_combo.addItem(label, engine.get("id"))
            index = self.engine_combo.count() - 1
            item = self.engine_combo.model().item(index)
            if item is not None and not available:
                item.setEnabled(False)
                item.setToolTip(str(engine.get("unavailable_reason") or "Engine unavailable"))
        self.engine_combo.blockSignals(False)
        self._select_data(self.engine_combo, current or "auto")
        self._engine_changed()

    def set_profiles(self, profiles: list[dict[str, Any]]) -> None:
        selected = self._requested_profile_id or self.profile_combo.currentData()
        self.profiles = [item for item in profiles if isinstance(item, dict)]
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("Chọn voice profile…", None)
        for profile in self.profiles:
            label = str(profile.get("display_name") or profile.get("profile_id"))
            status = profile_status(profile)
            if status != "READY":
                label += f" — {status}"
            self.profile_combo.addItem(label, profile.get("profile_id"))
            self.profile_combo.setItemData(
                self.profile_combo.count() - 1, profile, Qt.UserRole + 1
            )
        self.profile_combo.blockSignals(False)
        self._select_data(self.profile_combo, selected)
        self._requested_profile_id = None
        self._profile_changed()

    def select_profile(self, profile_id: str) -> None:
        index = self.profile_combo.findData(profile_id)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)
        else:
            self._requested_profile_id = profile_id

    def selected_profile(self) -> dict[str, Any] | None:
        value = self.profile_combo.currentData(Qt.UserRole + 1)
        return value if isinstance(value, dict) else None

    def _profile_changed(self) -> None:
        profile = self.selected_profile()
        status = profile_status(profile) if profile else "UNKNOWN"
        self.profile_status_badge.set_status(status)
        if profile is not None:
            preference = profile.get("engine_preference", "auto")
            if preference and self.engine_combo.findData(preference) >= 0:
                self._select_data(self.engine_combo, preference)
            language = profile.get("default_language", "auto")
            if language and self.language_combo.findData(language) >= 0:
                self._select_data(self.language_combo, language)
        self._update_generate_gate()

    def _engine_changed(self) -> None:
        engine_id = str(self.engine_combo.currentData() or "auto")
        current_language = self.language_combo.currentData()
        languages = available_languages(self.capabilities, engine_id)
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for language in languages:
            self.language_combo.addItem(language, language)
        self.language_combo.blockSignals(False)
        if languages:
            self._select_data(self.language_combo, current_language or languages[0])

        capability = next(
            (item for item in self.capabilities if item.get("id") == engine_id), None
        )
        supports = bool(
            engine_id != "auto"
            and capability
            and capability.get("available") is True
            and capability.get(
                "supports_keep_model_loaded", capability.get("supports_keep_model_warm", False)
            )
        )
        self.keep_loaded_check.setEnabled(supports and not self.client.is_busy)
        if not supports:
            self.keep_loaded_check.setChecked(False)
        self._update_generate_gate()

    def _text_changed(self) -> None:
        count = len(self.text_edit.toPlainText())
        self.char_count.setText(f"{count} ký tự")
        self._update_generate_gate()

    def _update_generate_gate(self) -> None:
        profile = self.selected_profile()
        enabled = bool(
            not self.client.is_busy
            and profile is not None
            and is_ready_profile(profile)
            and bool(self.text_edit.toPlainText().strip())
            and self.language_combo.currentData()
            and self.engine_combo.currentData() is not None
        )
        self.generate_button.setEnabled(enabled)

    def _show_message(self, message: str, *, error: bool = False) -> None:
        self.last_inline_error = message if error else ""
        self.message_label.setText(message)
        set_message_kind(self.message_label, error=error)

    def submit_synthesis(self) -> None:
        profile = self.selected_profile()
        if profile is None or not is_ready_profile(profile):
            self._show_message("Chỉ profile READY mới có thể tạo giọng.", error=True)
            return
        text = self.text_edit.toPlainText().strip()
        if not text:
            self._show_message("Text không được để trống.", error=True)
            return
        speed = self.speed_spin.value()
        if not 0.5 <= speed <= 2.0:
            self._show_message("Tốc độ phải nằm trong khoảng 0.50–2.00.", error=True)
            return
        language = self.language_combo.currentData()
        if not language:
            self._show_message("Engine hiện tại không có ngôn ngữ khả dụng.", error=True)
            return
        job = {
            "schema_version": 1,
            "job_id": str(uuid.uuid4()),
            "action": "synthesize",
            "profile_id": profile.get("profile_id"),
            "text": text,
            "language": language,
            "engine": self.engine_combo.currentData() or "auto",
            "device": "cpu",
            "speed": speed,
            "seed": self.seed_spin.value(),
            "keep_model_loaded": self.keep_loaded_check.isChecked(),
        }
        self.pending_action = "synthesize"
        self.state = SynthesisPageState.SYNTHESIZING
        self._show_message("Đã gửi synthesis job tới runtime.")
        if not self.client.submit_job(job):
            self.pending_action = None
            self.state = SynthesisPageState.IDLE

    def _job_result(self, result: dict[str, Any]) -> None:
        action = result.get("_submitted_action")
        if self.pending_action != "synthesize" or action != "synthesize":
            return
        self.pending_action = None
        self.state = SynthesisPageState.RESULT_READY
        output = result.get("output_audio")
        self.output_path = Path(output) if isinstance(output, str) and output else None
        self.output_label.setText(f"Output: {self.output_path or '—'}")
        self.metrics_label.setText(
            "Duration: {duration} s    Elapsed: {elapsed} s    Peak RAM: {ram} GiB".format(
                duration=self._metric(result.get("duration_seconds")),
                elapsed=self._metric(result.get("elapsed_seconds")),
                ram=self._metric(result.get("peak_ram_gib")),
            )
        )
        self.result_player.set_source(self.output_path)
        exists = bool(self.output_path and self.output_path.is_file())
        self.save_button.setEnabled(exists)
        self.open_button.setEnabled(exists)
        result_path = result.get("result_path")
        self._show_message("Audio đã tạo và kiểm tra thành công.")
        if isinstance(result_path, str):
            self.output_label.setToolTip(result_path)

    @staticmethod
    def _metric(value: Any) -> str:
        try:
            return f"{float(value):.3f}"
        except (TypeError, ValueError):
            return "—"

    def _job_error(self, error: dict[str, Any]) -> None:
        if self.pending_action != "synthesize":
            return
        action = error.get("action")
        if action and action != "synthesize":
            return
        self.pending_action = None
        self.state = SynthesisPageState.IDLE
        self._show_message(
            f"{error.get('code', 'RUNTIME_JOB_FAILED')}: {error.get('message', 'Synthesis thất bại.')}",
            error=True,
        )

    def _busy_changed(self, busy: bool) -> None:
        self.cancel_button.setEnabled(busy and self.pending_action == "synthesize")
        self.profile_combo.setEnabled(not busy)
        self.engine_combo.setEnabled(not busy)
        self.language_combo.setEnabled(not busy)
        self.speed_spin.setEnabled(not busy)
        self.seed_spin.setEnabled(not busy)
        self.text_edit.setReadOnly(busy)
        self._engine_changed()
        self._update_generate_gate()

    def _cancelled(self) -> None:
        if self.pending_action != "synthesize":
            return
        self.pending_action = None
        self.state = SynthesisPageState.IDLE
        self._show_message("Synthesis đã được hủy.")

    def save_as(self) -> None:
        if self.output_path is None or not self.output_path.is_file():
            return
        target, _filter = QFileDialog.getSaveFileName(
            self,
            "Lưu WAV thành",
            str(self.output_path.with_name("generated-copy.wav")),
            "WAV audio (*.wav)",
        )
        if not target:
            return
        destination = Path(target)
        if destination.suffix.casefold() != ".wav":
            destination = destination.with_suffix(".wav")
        if destination.exists():
            answer = QMessageBox.question(
                self,
                "Ghi đè file",
                f"File đã tồn tại:\n{destination}\n\nBạn có muốn ghi đè?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        try:
            shutil.copy2(self.output_path, destination)
        except OSError as exc:
            self._show_message(f"Không lưu được WAV: {exc}", error=True)
            return
        self._show_message(f"Đã lưu WAV: {destination}")

    def open_output_folder(self) -> None:
        if self.output_path is not None and self.output_path.parent.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_path.parent)))

    def stop_audio(self) -> None:
        self.result_player.clear()


__all__ = ["SynthesisPage"]
