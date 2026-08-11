from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..runtime_client import RuntimeClient
from ..theme import set_message_kind
from ..view_models import (
    ProfileMode,
    ProfilePageState,
    absolute_profile_asset,
    artifact_path,
    available_languages,
    profile_status,
    source_type_for_path,
)
from ..widgets.audio_player import AudioPlayer
from ..widgets.status_badge import StatusBadge


class ProfilesPage(QWidget):
    use_profile_requested = Signal(str)
    status_message = Signal(str)
    TABLE_COLUMN_RATIOS = (0.22, 0.10, 0.10, 0.11, 0.20, 0.15, 0.12)

    def __init__(self, client: RuntimeClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.mode = ProfileMode.CREATE_NEW
        self.state = ProfilePageState.IDLE
        self.profiles: list[dict[str, Any]] = []
        self.capabilities: list[dict[str, Any]] = []
        self.update_profile: dict[str, Any] | None = None
        self.preparation: dict[str, Any] | None = None
        self.reference_artifacts: dict[str, Any] | None = None
        self.pending_action: str | None = None
        self.last_inline_error = ""

        self._build_ui()
        self._connect_client()
        self.set_create_mode()

    def _build_ui(self) -> None:
        self.new_mode_radio = QRadioButton("Tạo profile mới")
        self.update_mode_radio = QRadioButton("Cập nhật reference")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.new_mode_radio)
        self.mode_group.addButton(self.update_mode_radio)
        self.update_subject = QLabel("Chưa chọn profile để cập nhật")
        self.update_subject.setWordWrap(True)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self.new_mode_radio)
        mode_row.addWidget(self.update_mode_radio)
        mode_row.addStretch(1)

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Chọn file audio hoặc video có giọng")
        self.source_button = QPushButton("Chọn…")
        source_row = QHBoxLayout()
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_button)
        source_widget = QWidget()
        source_widget.setLayout(source_row)

        self.name_edit = QLineEdit()
        self.profile_type_combo = QComboBox()
        self.profile_type_combo.addItem("Cloned", "cloned")
        self.profile_type_combo.addItem("Preset", "preset")
        self.source_language_combo = QComboBox()
        self.default_language_combo = QComboBox()
        self.engine_combo = QComboBox()

        form = QFormLayout()
        form.addRow("Nguồn video/audio", source_widget)
        form.addRow("Tên profile", self.name_edit)
        form.addRow("Loại profile", self.profile_type_combo)
        form.addRow("Ngôn ngữ nguồn", self.source_language_combo)
        form.addRow("Ngôn ngữ mặc định", self.default_language_combo)
        form.addRow("Engine ưu tiên", self.engine_combo)

        self.auto_radio = QRadioButton("Tìm đoạn gợi ý")
        self.manual_radio = QRadioButton("Chọn thủ công")
        self.selection_group = QButtonGroup(self)
        self.selection_group.addButton(self.auto_radio)
        self.selection_group.addButton(self.manual_radio)
        self.auto_radio.setChecked(True)
        selection_mode = QHBoxLayout()
        selection_mode.addWidget(self.auto_radio)
        selection_mode.addWidget(self.manual_radio)
        selection_mode.addStretch(1)

        self.target_window_check = QCheckBox("Giới hạn vùng có người cần clone")
        self.target_start = self._time_spin()
        self.target_end = self._time_spin(30.0)
        self.manual_start = self._time_spin()
        self.manual_end = self._time_spin(10.0)
        selection_grid = QGridLayout()
        selection_grid.addWidget(self.target_window_check, 0, 0, 1, 4)
        selection_grid.addWidget(QLabel("Vùng bắt đầu"), 1, 0)
        selection_grid.addWidget(self.target_start, 1, 1)
        selection_grid.addWidget(QLabel("Vùng kết thúc"), 1, 2)
        selection_grid.addWidget(self.target_end, 1, 3)
        selection_grid.addWidget(QLabel("Manual bắt đầu"), 2, 0)
        selection_grid.addWidget(self.manual_start, 2, 1)
        selection_grid.addWidget(QLabel("Manual kết thúc"), 2, 2)
        selection_grid.addWidget(self.manual_end, 2, 3)

        self.background_check = QCheckBox("Tôi nghe thấy nhạc / âm thanh nền trong nguồn")
        self.consent_check = QCheckBox(
            "Tôi xác nhận có quyền và sự đồng ý cần thiết để sử dụng giọng này."
        )
        self.prepare_button = QPushButton("Chuẩn bị reference")
        self.prepare_button.setProperty("role", "primary")
        self.prepare_button.setDefault(True)
        self.form_message = QLabel("")
        self.form_message.setWordWrap(True)
        self.form_message.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.left_panel = QGroupBox("TẠO / CẬP NHẬT PROFILE")
        self.left_panel.setMinimumWidth(380)
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(10, 12, 10, 10)
        left_layout.setSpacing(7)
        left_layout.addLayout(mode_row)
        left_layout.addWidget(self.update_subject)
        left_layout.addLayout(form)
        selection_box = QGroupBox("Chọn reference 8–15 giây")
        selection_box.setObjectName("InnerGroup")
        selection_layout = QVBoxLayout(selection_box)
        selection_layout.addLayout(selection_mode)
        selection_layout.addLayout(selection_grid)
        left_layout.addWidget(selection_box)
        left_layout.addWidget(self.background_check)
        left_layout.addWidget(self.consent_check)
        left_layout.addWidget(self.form_message)
        left_layout.addWidget(self.prepare_button)
        left_layout.addStretch(1)

        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.left_scroll.setMinimumWidth(380)
        self.left_scroll.setWidget(self.left_panel)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Tìm theo tên hoặc profile ID")
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Tên", "Loại", "Source lang", "Default lang", "Engine", "Trạng thái", "Revision"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(27)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for index in range(7):
            header.setSectionResizeMode(index, QHeaderView.Fixed)

        self.use_button = QPushButton("Dùng để tạo giọng")
        self.update_button = QPushButton("Cập nhật reference")
        self.listen_button = QPushButton("Nghe reference")
        self.open_button = QPushButton("Mở thư mục")
        self.delete_button = QPushButton("Xóa")
        self.use_button.setProperty("role", "primary")
        self.delete_button.setProperty("role", "danger")
        actions = QHBoxLayout()
        for button in (
            self.use_button,
            self.update_button,
            self.listen_button,
            self.open_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.delete_button)

        self.profile_list_card = QGroupBox("DANH SÁCH PROFILE")
        profile_list_layout = QVBoxLayout(self.profile_list_card)
        profile_list_layout.setContentsMargins(10, 12, 10, 9)
        profile_list_layout.setSpacing(6)
        profile_list_layout.addWidget(self.search_edit)
        profile_list_layout.addWidget(self.table, 1)
        profile_list_layout.addLayout(actions)

        self.review_box = QGroupBox("XEM TRƯỚC VÀ KIỂM TRA")
        self.review_status = StatusBadge("UNKNOWN")
        self.review_message = QLabel("Chọn một profile để xem reference hiện tại.")
        self.review_message.setWordWrap(True)
        self.selection_label = QLabel("Selection: —")
        self.selection_label.setObjectName("MutedLabel")
        self.validation_label = QLabel("Technical status: —")
        self.validation_label.setObjectName("MutedLabel")
        self.validation_label.setWordWrap(True)
        self.source_player = AudioPlayer("Source mix")
        self.voice_player = AudioPlayer("Voice only")
        self.listen_confirm = QCheckBox(
            "Tôi đã nghe bản voice-only và chấp nhận reference này."
        )
        self.single_speaker_confirm = QCheckBox(
            "Đoạn này chỉ có đúng một người nói, không có giọng chồng lên."
        )
        self.commit_button = QPushButton("Commit profile")
        self.commit_button.setProperty("role", "primary")
        self.commit_button.setEnabled(False)
        review_layout = QVBoxLayout(self.review_box)
        review_layout.setContentsMargins(10, 12, 10, 9)
        review_layout.setSpacing(5)
        status_row = QHBoxLayout()
        status_row.addWidget(self.review_status)
        status_row.addWidget(self.selection_label)
        status_row.addStretch(1)
        review_layout.addLayout(status_row)
        review_layout.addWidget(self.review_message)
        review_layout.addWidget(self.validation_label)
        review_layout.addWidget(self.source_player)
        review_layout.addWidget(self.voice_player)
        review_layout.addWidget(self.listen_confirm)
        review_layout.addWidget(self.single_speaker_confirm)
        review_layout.addWidget(self.commit_button, 0, Qt.AlignRight)

        self.right_panel = QWidget()
        self.right_panel.setMinimumWidth(620)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(self.profile_list_card, 1)
        right_layout.addWidget(self.review_box, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.left_scroll)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 35)
        self.splitter.setStretchFactor(1, 65)
        self.splitter.setSizes([380, 700])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.splitter, 1)

        self.new_mode_radio.clicked.connect(self.set_create_mode)
        self.update_mode_radio.clicked.connect(self.start_update_selected)
        self.source_button.clicked.connect(self.choose_source)
        self.prepare_button.clicked.connect(self.submit_prepare)
        self.profile_type_combo.currentIndexChanged.connect(self._profile_type_changed)
        self.manual_radio.toggled.connect(self._selection_mode_changed)
        self.target_window_check.toggled.connect(self._selection_mode_changed)
        self.search_edit.textChanged.connect(self._filter_rows)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.use_button.clicked.connect(self.use_selected_profile)
        self.update_button.clicked.connect(self.start_update_selected)
        self.listen_button.clicked.connect(self.listen_selected_profile)
        self.open_button.clicked.connect(self.open_selected_profile)
        self.delete_button.clicked.connect(self.delete_selected_profile)
        self.listen_confirm.toggled.connect(self._update_commit_gate)
        self.single_speaker_confirm.toggled.connect(self._update_commit_gate)
        self.commit_button.clicked.connect(self.submit_commit)
        self._selection_mode_changed()
        self._selection_changed()
        QTimer.singleShot(0, self._resize_table_columns)

    @staticmethod
    def _time_spin(value: float = 0.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 86400.0)
        spin.setDecimals(3)
        spin.setSingleStep(0.25)
        spin.setSuffix(" s")
        spin.setValue(value)
        return spin

    def _connect_client(self) -> None:
        self.client.capabilities_ready.connect(self.set_capabilities)
        self.client.profiles_ready.connect(self.set_profiles)
        self.client.profile_command_result.connect(self._profile_command_result)
        self.client.job_result.connect(self._job_result)
        self.client.job_error.connect(self._job_error)
        self.client.cancelled.connect(self._cancelled)
        self.client.busy_changed.connect(self._busy_changed)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._resize_table_columns()

    def _resize_table_columns(self) -> None:
        width = self.table.viewport().width()
        if width <= 0:
            return
        assigned = 0
        for column, ratio in enumerate(self.TABLE_COLUMN_RATIOS):
            column_width = width - assigned if column == 6 else round(width * ratio)
            self.table.setColumnWidth(column, max(1, column_width))
            assigned += column_width

    def set_capabilities(self, payload: dict[str, Any]) -> None:
        engines = payload.get("engines")
        self.capabilities = engines if isinstance(engines, list) else []
        current_source = self.source_language_combo.currentData()
        current_default = self.default_language_combo.currentData()
        languages = available_languages(self.capabilities)
        for combo in (self.source_language_combo, self.default_language_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Tự động", "auto")
            for language in languages:
                combo.addItem(language, language)
            combo.blockSignals(False)
        self._select_data(self.source_language_combo, current_source or "auto")
        self._select_data(self.default_language_combo, current_default or "auto")

        current_engine = self.engine_combo.currentData()
        self.engine_combo.clear()
        self.engine_combo.addItem("Tự động", "auto")
        for engine in self.capabilities:
            label = str(engine.get("display_name") or engine.get("id"))
            if engine.get("available") is not True:
                label += " — unavailable"
            self.engine_combo.addItem(label, engine.get("id"))
            index = self.engine_combo.count() - 1
            item = self.engine_combo.model().item(index)
            if item is not None and engine.get("available") is not True:
                item.setEnabled(False)
                item.setToolTip(str(engine.get("unavailable_reason") or "Engine unavailable"))
        self._select_data(self.engine_combo, current_engine or "auto")

    @staticmethod
    def _select_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_profiles(self, profiles: list[dict[str, Any]]) -> None:
        selected_id = self.selected_profile_id()
        restored_selection = False
        self.profiles = [item for item in profiles if isinstance(item, dict)]
        self.table.setRowCount(0)
        for profile in self.profiles:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                profile.get("display_name", profile.get("profile_id", "")),
                profile.get("profile_type", ""),
                profile.get("source_language", ""),
                profile.get("default_language", ""),
                profile.get("engine_preference", "auto"),
                profile_status(profile),
                profile.get("profile_revision", "—"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, profile)
                    item.setToolTip(
                        f"{value}\nProfile ID: {profile.get('profile_id', '')}"
                    )
                self.table.setItem(row, column, item)
            if profile.get("valid", True) is not True:
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    item.setToolTip(
                        f"{item.text()}\n{profile.get('profile_error', '')}".strip()
                    )
            if profile.get("profile_id") == selected_id:
                self.table.selectRow(row)
                restored_selection = True
        if not restored_selection and self.table.rowCount() > 0:
            self.table.selectRow(0)
        self._filter_rows(self.search_edit.text())
        self._selection_changed()
        self._resize_table_columns()

    def selected_profile(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return value if isinstance(value, dict) else None

    def selected_profile_id(self) -> str | None:
        profile = self.selected_profile()
        value = profile.get("profile_id") if profile else None
        return str(value) if value else None

    def _filter_rows(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            profile = item.data(Qt.UserRole) if item is not None else {}
            haystack = f"{profile.get('display_name', '')} {profile.get('profile_id', '')}".casefold()
            self.table.setRowHidden(row, bool(needle and needle not in haystack))

    def _selection_changed(self) -> None:
        selected = self.selected_profile() is not None
        for button in (
            self.use_button,
            self.update_button,
            self.listen_button,
            self.open_button,
            self.delete_button,
        ):
            button.setEnabled(selected and not self.client.is_busy)
        if self.preparation is None and self.state != ProfilePageState.PREPARING_REFERENCE:
            self._show_selected_profile_preview()

    def set_create_mode(self) -> None:
        self.mode = ProfileMode.CREATE_NEW
        self.update_profile = None
        self.new_mode_radio.setChecked(True)
        self.update_mode_radio.setChecked(False)
        self.update_subject.setText("Đang tạo profile mới; chọn row không làm đổi chế độ này.")
        self.name_edit.clear()
        self.profile_type_combo.setEnabled(True)
        self.profile_type_combo.setCurrentIndex(0)
        self.consent_check.setEnabled(True)
        self.consent_check.setChecked(False)
        self.reset_review()
        self._show_message("")

    def start_update_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            self._show_message("Hãy chọn một profile trong danh sách trước.", error=True)
            self.new_mode_radio.setChecked(True)
            return
        if profile.get("valid", True) is not True:
            self._show_message("Profile đang lỗi nên không thể cập nhật reference.", error=True)
            return
        self.mode = ProfileMode.UPDATE_EXISTING_REFERENCE
        self.update_profile = dict(profile)
        self.update_mode_radio.setChecked(True)
        self.new_mode_radio.setChecked(False)
        self.update_subject.setText(
            f"Đang cập nhật: {profile.get('display_name')} ({profile.get('profile_id')}, "
            f"revision {profile.get('profile_revision', '—')})"
        )
        self.name_edit.setText(str(profile.get("display_name", "")))
        self.profile_type_combo.setEnabled(False)
        self._select_data(self.profile_type_combo, profile.get("profile_type", "cloned"))
        self._select_data(self.source_language_combo, profile.get("source_language", "auto"))
        self._select_data(self.default_language_combo, profile.get("default_language", "auto"))
        self._select_data(self.engine_combo, profile.get("engine_preference", "auto"))
        self.consent_check.setChecked(False)
        self.consent_check.setEnabled(False)
        self.reset_review()
        self._show_message("")
        self.source_edit.setFocus()

    def choose_source(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Chọn nguồn giọng",
            self.source_edit.text() or str(Path.home()),
            (
                "Audio / Video (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.mp4 *.mov *.mkv "
                "*.avi *.webm *.m4v *.ts *.mts *.m2ts);;Audio (*.wav *.mp3 *.m4a *.aac "
                "*.flac *.ogg);;Video (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts *.mts *.m2ts)"
            ),
        )
        if path:
            self.source_edit.setText(path)

    def _selection_mode_changed(self) -> None:
        manual = self.manual_radio.isChecked()
        self.manual_start.setEnabled(manual)
        self.manual_end.setEnabled(manual)
        window = self.target_window_check.isChecked()
        self.target_start.setEnabled(window)
        self.target_end.setEnabled(window)

    def _profile_type_changed(self) -> None:
        profile_type = self.profile_type_combo.currentData()
        self.prepare_button.setText(
            "Tạo / cập nhật preset" if profile_type == "preset" else "Chuẩn bị reference"
        )

    def _show_message(self, message: str, *, error: bool = False) -> None:
        self.last_inline_error = message if error else ""
        self.form_message.setText(message)
        set_message_kind(self.form_message, error=error)
        if message:
            self.status_message.emit(message)

    def _validate_form(self) -> tuple[dict[str, Any] | None, str | None]:
        source = Path(self.source_edit.text().strip()).expanduser()
        source_type = source_type_for_path(source)
        if source_type is None:
            return None, "Định dạng nguồn không được runtime hỗ trợ."
        if not source.is_file():
            return None, "File nguồn không tồn tại."
        profile_type = str(self.profile_type_combo.currentData() or "cloned")
        if profile_type == "preset" and source_type != "audio":
            return None, "Runtime hiện tại chỉ cho preset dùng nguồn audio."
        if self.mode == ProfileMode.CREATE_NEW and not self.name_edit.text().strip():
            return None, "Tên profile không được để trống."
        if self.mode == ProfileMode.CREATE_NEW and not self.consent_check.isChecked():
            return None, "Bạn phải xác nhận quyền và sự đồng ý cho profile mới."
        selection: dict[str, Any]
        if self.manual_radio.isChecked():
            start = self.manual_start.value()
            end = self.manual_end.value()
            duration_ms = round((end - start) * 1000)
            if end <= start or not 8000 <= duration_ms <= 15000:
                return None, "Reference thủ công phải dài từ 8 đến 15 giây."
            selection = {"mode": "manual", "start_seconds": start, "end_seconds": end}
        else:
            selection = {"mode": "auto"}
        target: dict[str, float] | None = None
        if self.target_window_check.isChecked():
            start = self.target_start.value()
            end = self.target_end.value()
            if end <= start:
                return None, "Vùng target speaker phải có kết thúc lớn hơn bắt đầu."
            if selection["mode"] == "manual" and (
                selection["start_seconds"] < start or selection["end_seconds"] > end
            ):
                return None, "Reference thủ công phải nằm hoàn toàn trong vùng target speaker."
            target = {"start_seconds": start, "end_seconds": end}
        return {
            "source": str(source.resolve()),
            "source_type": source_type,
            "profile_type": profile_type,
            "selection": selection,
            "target_speaker_window": target,
        }, None

    def submit_prepare(self) -> None:
        validated, error = self._validate_form()
        if error or validated is None:
            self._show_message(error or "Form không hợp lệ.", error=True)
            return
        if self.client.is_busy:
            self._show_message("Đang có một tác vụ chạy.", error=True)
            return
        profile_type = validated["profile_type"]
        job: dict[str, Any] = {
            "schema_version": 1,
            "job_id": str(uuid.uuid4()),
            "action": "create_profile" if profile_type == "preset" else "prepare_profile_reference",
            "input_path": validated["source"],
            "source_type": validated["source_type"],
            "profile_type": profile_type,
            "reported_background_audio": self.background_check.isChecked(),
            "selection": validated["selection"],
        }
        if validated["target_speaker_window"] is not None:
            job["target_speaker_window"] = validated["target_speaker_window"]
        if self.mode == ProfileMode.UPDATE_EXISTING_REFERENCE:
            profile = self.update_profile
            if profile is None:
                self._show_message("State cập nhật không có profile đích.", error=True)
                return
            job.update(
                {
                    "profile_id": profile.get("profile_id"),
                    "update_existing": True,
                    "expected_profile_revision": profile.get("profile_revision"),
                    "expected_profile_identity": {
                        key: profile.get(key) for key in ("profile_id", "display_name", "created_at")
                    },
                }
            )
        else:
            job.update(
                {
                    "display_name": self.name_edit.text().strip(),
                    "source_language": self.source_language_combo.currentData() or "auto",
                    "default_language": self.default_language_combo.currentData() or "auto",
                    "engine_preference": self.engine_combo.currentData() or "auto",
                    "consent": {"confirmed": True, "source": "standalone_gui_checkbox"},
                }
            )
        self.pending_action = str(job["action"])
        self.state = ProfilePageState.PREPARING_REFERENCE
        self.reset_review()
        self._show_message("Đã gửi tác vụ tới runtime.")
        if not self.client.submit_job(job):
            self.pending_action = None
            self.state = ProfilePageState.IDLE

    def _job_result(self, result: dict[str, Any]) -> None:
        action = result.get("_submitted_action") or result.get("action")
        if self.pending_action is None or action != self.pending_action:
            return
        self.pending_action = None
        if action == "create_profile":
            self.state = ProfilePageState.READY
            profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
            self._show_message(f"Preset {profile.get('display_name', '')} đã được lưu.")
            self.client.refresh_profiles()
            return
        if action == "commit_profile_reference":
            self.state = ProfilePageState.READY
            profile_id = str(result.get("profile_id") or "")
            self._show_message(f"Profile {profile_id} đã READY.")
            self.reset_review()
            self.client.refresh_profiles()
            return
        self._apply_preparation(result)

    def _apply_preparation(self, result: dict[str, Any]) -> None:
        self.preparation = dict(result)
        artifacts = result.get("reference_artifacts")
        self.reference_artifacts = artifacts if isinstance(artifacts, dict) else None
        status = str(result.get("profile_status") or result.get("candidate_status") or "UNKNOWN")
        candidate_status = str(result.get("candidate_status") or status)
        selection = result.get("selection") if isinstance(result.get("selection"), dict) else {}
        start = selection.get("start_seconds")
        end = selection.get("end_seconds")
        self.review_box.setVisible(True)
        self.review_status.set_status(candidate_status)
        self.selection_label.setText(f"Selection: {start!s}s → {end!s}s")
        validation = result.get("reference_validation")
        validation_status = validation.get("status") if isinstance(validation, dict) else "—"
        self.validation_label.setText(f"Technical status: {validation_status}")
        self.source_player.set_source(artifact_path(result.get("ref_source_mix")))
        self.voice_player.set_source(artifact_path(result.get("ref_voice_only")))
        self.listen_confirm.setChecked(False)
        self.single_speaker_confirm.setChecked(False)

        if status == "NEEDS_MANUAL_REFERENCE":
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                self.manual_start.setValue(float(start))
                self.manual_end.setValue(float(end))
            self.manual_radio.setChecked(True)
            self.state = ProfilePageState.AUTO_SUGGESTION_NEEDS_MANUAL
            message = (
                result.get("message")
                or f"Đã tìm được đoạn gợi ý {start}s–{end}s. Hãy nghe/chỉnh và chuẩn bị lại ở chế độ thủ công."
            )
        elif result.get("ready_for_commit") is True and status == "TECHNICAL_PASS_PENDING_LISTENING":
            self.state = ProfilePageState.REFERENCE_REVIEW
            message = "Reference đã qua gate kỹ thuật. Hãy nghe A/B và xác nhận cả hai điều kiện."
        else:
            self.state = ProfilePageState.IDLE
            message = result.get("message") or (
                "Candidate chưa thể commit. Hãy nghe preview và chọn một đoạn khác."
            )
        self.review_message.setText(str(message))
        self._show_message(str(message), error=False)
        self._update_commit_gate()

    def _update_commit_gate(self) -> None:
        enabled = bool(
            self.state == ProfilePageState.REFERENCE_REVIEW
            and self.preparation
            and self.preparation.get("ready_for_commit") is True
            and self.preparation.get("profile_status") == "TECHNICAL_PASS_PENDING_LISTENING"
            and self.listen_confirm.isChecked()
            and self.single_speaker_confirm.isChecked()
            and not self.client.is_busy
        )
        self.commit_button.setEnabled(enabled)

    def submit_commit(self) -> None:
        if not self.commit_button.isEnabled() or self.preparation is None:
            return
        if self.reference_artifacts is None:
            self._show_message("Preparation không có reference_artifacts hợp lệ.", error=True)
            return
        job = {
            "schema_version": 1,
            "job_id": str(uuid.uuid4()),
            "action": "commit_profile_reference",
            "profile_id": self.preparation.get("profile_id"),
            "preparation_id": self.preparation.get("preparation_id") or self.preparation.get("job_id"),
            "user_listening_confirmed": True,
            "single_speaker_confirmed": True,
            "use_voice_only": True,
            "reference_artifacts": self.reference_artifacts,
        }
        self.pending_action = "commit_profile_reference"
        self.state = ProfilePageState.COMMITTING
        if not self.client.submit_job(job):
            self.pending_action = None
            self.state = ProfilePageState.REFERENCE_REVIEW

    def _job_error(self, error: dict[str, Any]) -> None:
        operation = error.get("operation")
        if operation == "profile_delete":
            self._show_message(str(error.get("message", "Xóa profile thất bại.")), error=True)
            return
        if self.pending_action is None:
            return
        action = error.get("action")
        if action and action != self.pending_action:
            return
        self.pending_action = None
        self.state = ProfilePageState.IDLE
        code = str(error.get("code") or "RUNTIME_JOB_FAILED")
        message = str(error.get("message") or "Runtime job thất bại.")
        self._show_message(f"{code}: {message}", error=True)
        if code == "PROFILE_REVISION_MISMATCH":
            self.client.refresh_profiles()

    def _busy_changed(self, busy: bool) -> None:
        self.prepare_button.setEnabled(not busy)
        self.source_button.setEnabled(not busy)
        self._selection_changed()
        self._update_commit_gate()

    def _cancelled(self) -> None:
        if self.pending_action is None:
            return
        self.pending_action = None
        self.state = ProfilePageState.IDLE
        self._show_message("Tác vụ đã được hủy; form được giữ để chỉnh lại.")

    def _profile_command_result(self, operation: str, payload: dict[str, Any]) -> None:
        if operation == "profile_delete":
            self._show_message(f"Đã chuyển profile {payload.get('profile_id')} vào vùng recoverable.")
            self.client.refresh_profiles()

    def use_selected_profile(self) -> None:
        profile = self.selected_profile()
        if profile is not None:
            self.use_profile_requested.emit(str(profile.get("profile_id", "")))

    def listen_selected_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        self.reset_review()
        self._show_selected_profile_preview()

    def _show_selected_profile_preview(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            self.review_status.set_status("UNKNOWN")
            self.review_message.setText("Chọn một profile để xem reference hiện tại.")
            self.selection_label.setText("Selection: —")
            self.validation_label.setText("Technical status: —")
            self.source_player.clear()
            self.voice_player.clear()
            self.listen_confirm.setVisible(False)
            self.single_speaker_confirm.setVisible(False)
            self.commit_button.setVisible(False)
            return
        source_mix = absolute_profile_asset(profile, "source_mix")
        voice_only = absolute_profile_asset(profile, "voice_only")
        primary = absolute_profile_asset(profile, "primary")
        self.review_status.set_status(profile_status(profile))
        self.review_message.setText(f"Reference hiện tại của {profile.get('display_name')}.")
        self.selection_label.setText("Selection: profile hiện tại")
        self.validation_label.setText("Technical status: xem profile status")
        self.source_player.set_source(source_mix)
        self.voice_player.set_source(voice_only or primary)
        self.listen_confirm.setVisible(False)
        self.single_speaker_confirm.setVisible(False)
        self.commit_button.setVisible(False)

    def open_selected_profile(self) -> None:
        profile = self.selected_profile()
        path = Path(str(profile.get("profile_path", ""))) if profile else None
        if path is not None and path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def delete_selected_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Xóa profile",
            f"Chuyển profile “{profile.get('display_name')}” vào vùng recoverable?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.client.delete_profile(str(profile.get("profile_id", "")))

    def reset_review(self) -> None:
        self.preparation = None
        self.reference_artifacts = None
        self.review_status.set_status("UNKNOWN")
        self.review_message.setText("Chọn một profile để xem reference hiện tại.")
        self.selection_label.setText("Selection: —")
        self.validation_label.setText("Technical status: —")
        self.source_player.clear()
        self.voice_player.clear()
        self.listen_confirm.setChecked(False)
        self.single_speaker_confirm.setChecked(False)
        self.listen_confirm.setVisible(True)
        self.single_speaker_confirm.setVisible(True)
        self.commit_button.setVisible(True)
        self.commit_button.setEnabled(False)
        if self.state != ProfilePageState.PREPARING_REFERENCE:
            self._show_selected_profile_preview()

    def stop_audio(self) -> None:
        self.source_player.clear()
        self.voice_player.clear()


__all__ = ["ProfilesPage"]
