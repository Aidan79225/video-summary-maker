"""下載頁：網址、格式、畫質、儲存資料夾、進度條、取消。"""
from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...domain.entities import DownloadFormat, DownloadRequest, Settings
from ...usecases.download_video import DownloadVideoUseCase
from ..workers import DownloadWorker

# 畫質下拉：顯示文字 → 高度上限（None = 最高）
_QUALITIES: list[tuple[str, int | None]] = [
    ("最高", None), ("1080p", 1080), ("720p", 720), ("480p", 480),
]


class DownloadPage(QWidget):
    def __init__(
        self,
        usecase: DownloadVideoUseCase,
        settings: Settings,
        save: Callable[[], None],
    ):
        super().__init__()
        self._usecase = usecase
        self._settings = settings
        self._save = save
        self._worker: DownloadWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        title = QLabel("YouTube 下載")
        title.setStyleSheet("font-size:20px; font-weight:600;")
        layout.addWidget(title)

        # 網址
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("貼上 YouTube 網址…")
        paste_btn = QPushButton("貼上")
        paste_btn.clicked.connect(self._paste)
        url_row.addWidget(QLabel("網址"))
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(paste_btn)
        layout.addLayout(url_row)

        # 格式 + 畫質
        fmt_row = QHBoxLayout()
        self.mp4_radio = QRadioButton("影片 MP4")
        self.mp3_radio = QRadioButton("音樂 MP3")
        group = QButtonGroup(self)
        group.addButton(self.mp4_radio)
        group.addButton(self.mp3_radio)
        self.mp4_radio.toggled.connect(self._on_format_changed)

        self.quality_combo = QComboBox()
        for label, _ in _QUALITIES:
            self.quality_combo.addItem(label)

        fmt_row.addWidget(QLabel("格式"))
        fmt_row.addWidget(self.mp4_radio)
        fmt_row.addWidget(self.mp3_radio)
        fmt_row.addSpacing(16)
        fmt_row.addWidget(QLabel("畫質"))
        fmt_row.addWidget(self.quality_combo)
        fmt_row.addStretch(1)
        layout.addLayout(fmt_row)

        # 儲存資料夾
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(40)
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(QLabel("儲存"))
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # 開始／取消（同一顆按鈕依狀態切換）
        self.action_btn = QPushButton("開始下載")
        self.action_btn.setMinimumHeight(38)
        self.action_btn.clicked.connect(self._on_action)
        layout.addWidget(self.action_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)

        self._apply_settings()

    # --- 設定 ---
    def _apply_settings(self) -> None:
        s = self._settings
        self.dir_edit.setText(s.output_dir)
        (self.mp4_radio if s.fmt == DownloadFormat.MP4 else self.mp3_radio).setChecked(True)
        idx = next((i for i, (_, h) in enumerate(_QUALITIES) if h == s.max_height), 0)
        self.quality_combo.setCurrentIndex(idx)
        self._on_format_changed()

    def _persist(self) -> None:
        self._settings.output_dir = self.dir_edit.text().strip()
        self._settings.fmt = (
            DownloadFormat.MP4 if self.mp4_radio.isChecked() else DownloadFormat.MP3
        )
        self._settings.max_height = _QUALITIES[self.quality_combo.currentIndex()][1]
        self._save()

    # --- 事件 ---
    def _on_format_changed(self) -> None:
        self.quality_combo.setEnabled(self.mp4_radio.isChecked())

    def _paste(self) -> None:
        self.url_edit.setText(QGuiApplication.clipboard().text().strip())

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "選擇儲存資料夾", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    def _on_action(self) -> None:
        if self._worker and self._worker.isRunning():
            self._cancel()
        else:
            self._start()

    def _start(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.status.setText("請先貼上網址。")
            return
        output_dir = self.dir_edit.text().strip()
        if not output_dir:
            self.status.setText("請先選擇儲存資料夾。")
            return

        self._persist()
        fmt = DownloadFormat.MP4 if self.mp4_radio.isChecked() else DownloadFormat.MP3
        max_height = _QUALITIES[self.quality_combo.currentIndex()][1] if fmt == DownloadFormat.MP4 else None
        request = DownloadRequest(url=url, fmt=fmt, output_dir=output_dir, max_height=max_height)

        self._set_busy(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("開始…")

        self._worker = DownloadWorker(self._usecase, request)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
            self.status.setText("取消中…")
            self.action_btn.setEnabled(False)

    def _on_progress(self, frac, status: str) -> None:
        if frac is None:
            self.progress.setRange(0, 0)  # 忙碌動畫
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(frac * 100))
        self.status.setText(status)

    def _on_done(self, path: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        name = os.path.basename(path) if path else ""
        self.status.setText(f"✅ 完成：{name}")
        self._set_busy(False)

    def _on_fail(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(f"❌ 失敗：{message}")
        self._set_busy(False)

    def _on_cancelled(self) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("已取消。")
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.action_btn.setEnabled(True)
        self.action_btn.setText("取消" if busy else "開始下載")
