"""QApplication 啟動。"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from ..composition import build_main_window


def run() -> None:
    app = QApplication(sys.argv)
    window = build_main_window()
    window.show()
    sys.exit(app.exec())
