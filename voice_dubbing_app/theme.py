from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


APP_BG = "#111820"
HEADER_BG = "#151D26"
SURFACE_1 = "#18212B"
SURFACE_2 = "#1E2935"
SURFACE_3 = "#263443"
BORDER = "#334353"
BORDER_SOFT = "#293746"
TEXT_PRIMARY = "#E7EDF3"
TEXT_SECONDARY = "#A8B4C2"
TEXT_MUTED = "#7E8B99"
ACCENT = "#0F9D9A"
ACCENT_HOVER = "#13AAA7"
ACCENT_PRESSED = "#0B8582"
SUCCESS = "#5CCB8A"
WARNING = "#D7B15A"
DANGER = "#D96767"
SELECTED_ROW = "#344A5D"
INPUT_BG = "#1E2833"


APP_STYLESHEET = f"""
QMainWindow {{ background-color: {APP_BG}; }}
QWidget {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI";
    font-size: 12px;
}}
QWidget#CentralRoot {{ background-color: {APP_BG}; }}
QFrame#AppHeader, QFrame#StatusBar {{
    background-color: {HEADER_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 7px;
}}
QLabel#AppTitle {{
    color: {TEXT_PRIMARY};
    font-size: 18px;
    font-weight: 700;
}}
QLabel#HeaderMeta, QLabel#MutedLabel, QLabel#PlayerTime {{ color: {TEXT_SECONDARY}; }}
QLabel#RuntimeState[statusKind="success"] {{ color: {SUCCESS}; font-weight: 600; }}
QLabel#RuntimeState[statusKind="warning"] {{ color: {WARNING}; font-weight: 600; }}
QLabel#RuntimeState[statusKind="danger"] {{ color: {DANGER}; font-weight: 600; }}
QLabel[messageKind="info"] {{ color: {TEXT_SECONDARY}; }}
QLabel[messageKind="error"] {{ color: {DANGER}; }}

QGroupBox {{
    background-color: {SURFACE_1};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    margin-top: 9px;
    padding-top: 8px;
    font-weight: 700;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_PRIMARY};
    background-color: {SURFACE_1};
}}
QGroupBox#InnerGroup {{
    background-color: {SURFACE_2};
    border-color: {BORDER};
}}
QGroupBox#InnerGroup::title {{ background-color: {SURFACE_2}; color: {TEXT_SECONDARY}; }}

QPushButton, QToolButton {{
    min-height: 24px;
    padding: 2px 10px;
    color: {TEXT_PRIMARY};
    background-color: {SURFACE_3};
    border: 1px solid {BORDER};
    border-radius: 5px;
}}
QPushButton:hover, QToolButton:hover {{ background-color: #304050; border-color: #476078; }}
QPushButton:pressed, QToolButton:pressed {{ background-color: #202C38; }}
QPushButton:disabled, QToolButton:disabled {{
    color: {TEXT_MUTED};
    background-color: #1A232D;
    border-color: {BORDER_SOFT};
}}
QPushButton[role="primary"] {{
    color: #F4FFFF;
    background-color: {ACCENT};
    border-color: {ACCENT};
    font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[role="primary"]:pressed {{ background-color: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
QPushButton[role="danger"] {{ color: #F3C4C4; background-color: #432A2D; border-color: #654047; }}
QPushButton[role="danger"]:hover {{ color: #FFFFFF; background-color: #583136; border-color: {DANGER}; }}

QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    min-height: 24px;
    color: {TEXT_PRIMARY};
    background-color: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 1px 6px;
    selection-background-color: {SELECTED_ROW};
    selection-color: {TEXT_PRIMARY};
}}
QTextEdit {{ padding: 6px; }}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {TEXT_MUTED};
    background-color: #19212A;
    border-color: {BORDER_SOFT};
}}
QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox QAbstractItemView {{
    color: {TEXT_PRIMARY};
    background-color: {SURFACE_2};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTED_ROW};
    selection-color: {TEXT_PRIMARY};
    outline: 0;
}}
QCheckBox, QRadioButton {{ color: {TEXT_SECONDARY}; spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 13px; height: 13px; }}
QCheckBox::indicator {{ border: 1px solid {BORDER}; border-radius: 3px; background: {INPUT_BG}; }}
QRadioButton::indicator {{ border: 1px solid {BORDER}; border-radius: 7px; background: {INPUT_BG}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background-color: {ACCENT}; border-color: {ACCENT}; }}

QTabWidget::pane {{
    top: -1px;
    background-color: {APP_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 7px;
}}
QTabBar::tab {{
    min-width: 92px;
    min-height: 28px;
    padding: 0 10px;
    color: {TEXT_MUTED};
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    font-size: 11px;
    font-weight: 700;
}}
QTabBar::tab:selected {{ color: {TEXT_PRIMARY}; border-bottom-color: {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT_SECONDARY}; }}

QTableWidget {{
    color: {TEXT_SECONDARY};
    background-color: {SURFACE_2};
    alternate-background-color: #202C38;
    border: 1px solid {BORDER};
    border-radius: 5px;
    gridline-color: {BORDER_SOFT};
    outline: 0;
}}
QTableWidget::item {{ padding: 3px 5px; border: 0; }}
QTableWidget::item:selected {{ background-color: {SELECTED_ROW}; color: {TEXT_PRIMARY}; }}
QHeaderView::section {{
    min-height: 25px;
    padding: 3px 5px;
    color: {TEXT_PRIMARY};
    background-color: {SURFACE_3};
    border: 0;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}

QLabel#StatusBadge {{
    min-height: 17px;
    padding: 0 7px;
    border-radius: 7px;
    font-size: 10px;
    font-weight: 700;
}}
QLabel#StatusBadge[tone="success"] {{ color: #DDF8E8; background-color: #24543C; border: 1px solid #347352; }}
QLabel#StatusBadge[tone="warning"] {{ color: #FFF0C9; background-color: #594B2D; border: 1px solid #79663D; }}
QLabel#StatusBadge[tone="danger"] {{ color: #FFDADA; background-color: #5A3034; border: 1px solid #784047; }}
QLabel#StatusBadge[tone="neutral"] {{ color: {TEXT_SECONDARY}; background-color: {SURFACE_3}; border: 1px solid {BORDER}; }}

QWidget#AudioPlayer {{
    background-color: {SURFACE_3};
    border: 1px solid {BORDER};
    border-radius: 5px;
}}
QWidget#AudioPlayer QLabel {{ background: transparent; border: 0; }}
QWidget#AudioPlayer QPushButton {{ min-height: 20px; max-height: 22px; min-width: 25px; max-width: 25px; padding: 0; }}
QLabel#AudioError {{ color: {DANGER}; }}

QProgressBar {{
    max-height: 5px;
    background-color: {SURFACE_3};
    border: 0;
    border-radius: 2px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 2px; }}
QSplitter::handle {{ background-color: {BORDER_SOFT}; }}
QSplitter::handle:horizontal {{ width: 5px; margin: 3px 1px; }}
QSplitter::handle:vertical {{ height: 5px; margin: 1px 3px; }}

QScrollArea {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ background: {SURFACE_1}; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {SURFACE_3}; min-height: 24px; border-radius: 4px; }}
QScrollBar:horizontal {{ background: {SURFACE_1}; height: 9px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {SURFACE_3}; min-width: 24px; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ color: {TEXT_PRIMARY}; background-color: {SURFACE_3}; border: 1px solid {BORDER}; padding: 4px; }}
"""


def _palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(APP_BG))
    palette.setColor(QPalette.WindowText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Base, QColor(INPUT_BG))
    palette.setColor(QPalette.AlternateBase, QColor(SURFACE_2))
    palette.setColor(QPalette.Text, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Button, QColor(SURFACE_3))
    palette.setColor(QPalette.ButtonText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Highlight, QColor(SELECTED_ROW))
    palette.setColor(QPalette.HighlightedText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_MUTED))
    return palette


def apply_theme(application: QApplication) -> None:
    application.setStyle("Fusion")
    application.setPalette(_palette())
    application.setStyleSheet(APP_STYLESHEET)


def refresh_style(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_message_kind(widget: QWidget, *, error: bool) -> None:
    widget.setProperty("messageKind", "error" if error else "info")
    refresh_style(widget)


__all__ = [
    "ACCENT",
    "APP_BG",
    "APP_STYLESHEET",
    "BORDER",
    "DANGER",
    "HEADER_BG",
    "INPUT_BG",
    "SELECTED_ROW",
    "SUCCESS",
    "SURFACE_1",
    "SURFACE_2",
    "SURFACE_3",
    "TEXT_MUTED",
    "TEXT_PRIMARY",
    "TEXT_SECONDARY",
    "WARNING",
    "apply_theme",
    "refresh_style",
    "set_message_kind",
]
