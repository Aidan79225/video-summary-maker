"""主視窗：左側選單 + 右側 QStackedWidget。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QMainWindow,
    QStackedWidget,
    QWidget,
)

_NAV_STYLE = """
QListWidget {
    background: #1f2430;
    color: #cdd3df;
    border: none;
    outline: none;
    padding-top: 8px;
    font-size: 14px;
}
QListWidget::item {
    padding: 12px 18px;
}
QListWidget::item:selected {
    background: #2f3646;
    color: #ffffff;
    border-left: 3px solid #5b9dff;
}
QListWidget::item:hover {
    background: #272d3a;
}
"""


class MainWindow(QMainWindow):
    def __init__(self, download_page: QWidget, rename_page: QWidget):
        super().__init__()
        self.setWindowTitle("MusicBox")
        self.resize(820, 600)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setFixedWidth(160)
        self.nav.addItem("⬇   下載")
        self.nav.addItem("＃   重新編號")
        self.nav.setCurrentRow(0)
        self.nav.setStyleSheet(_NAV_STYLE)

        self.stack = QStackedWidget()
        self.stack.addWidget(download_page)
        self.stack.addWidget(rename_page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
