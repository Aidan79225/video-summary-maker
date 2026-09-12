"""主視窗：單頁。"""
from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QWidget


class MainWindow(QMainWindow):
    def __init__(self, page: QWidget):
        super().__init__()
        self.setWindowTitle("SlideBox")
        self.resize(760, 560)
        self.setCentralWidget(page)

    def closeEvent(self, event: QCloseEvent) -> None:
        """關視窗前先把背景工作停妥。

        子 widget 不會收到 closeEvent，所以這件事只能由主視窗轉發。少了這
        一步，Python 會在 QThread 還在跑時銷毀它，Qt 直接中止整個行程。
        """
        page = self.centralWidget()
        shutdown = getattr(page, "shutdown", None)
        if shutdown is not None:
            shutdown()
        super().closeEvent(event)
