"""主視窗：單頁。"""
from __future__ import annotations

from PySide6.QtWidgets import QMainWindow, QWidget


class MainWindow(QMainWindow):
    def __init__(self, page: QWidget):
        super().__init__()
        self.setWindowTitle("SlideBox")
        self.resize(760, 520)
        self.setCentralWidget(page)
