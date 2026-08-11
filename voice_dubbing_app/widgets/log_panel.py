from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTextEdit, QVBoxLayout, QWidget


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._run_path: Path | None = None
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.document().setMaximumBlockCount(1000)
        self.text.setPlaceholderText("Nhật ký runtime sẽ xuất hiện ở đây.")
        self.copy_button = QPushButton("Sao chép log")
        self.open_button = QPushButton("Mở run folder")
        self.open_button.setEnabled(False)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.open_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.text, 1)
        layout.addLayout(actions)

        self.copy_button.clicked.connect(self.copy_log)
        self.open_button.clicked.connect(self.open_run_folder)

    def append_message(self, message: str) -> None:
        line = str(message).strip()
        if not line:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.text.append(f"[{stamp}] {line}")

    def set_run_path(self, path: str | Path | None) -> None:
        candidate = Path(path) if path else None
        if candidate is not None and candidate.is_file():
            candidate = candidate.parent
        self._run_path = candidate if candidate is not None and candidate.is_dir() else None
        self.open_button.setEnabled(self._run_path is not None)

    def copy_log(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())

    def open_run_folder(self) -> None:
        if self._run_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._run_path)))


__all__ = ["LogPanel"]
