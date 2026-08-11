from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .theme import apply_theme


def create_application(argv: list[str] | None = None) -> QApplication:
    application = QApplication.instance()
    if application is None:
        application = QApplication(argv if argv is not None else sys.argv)
    application.setApplicationName("Voice Dubbing")
    application.setOrganizationName("Voice Dubbing Runtime")
    apply_theme(application)
    return application


def main(argv: list[str] | None = None) -> int:
    application = create_application(argv)
    window = MainWindow()
    window.show()
    return application.exec()


__all__ = ["create_application", "main"]
