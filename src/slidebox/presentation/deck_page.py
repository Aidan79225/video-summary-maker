"""投影片摘要頁：網址、模型、輸出設定、進度、結果。"""
from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..domain.entities import Settings
from ..domain.ports import ModelCatalog
from ..usecases.build_deck import BuildDeckUseCase
from .workers import BuildDeckWorker

# 畫質下拉：顯示文字 → max_height（沿用 musicbox 的形狀）
_QUALITIES = [("最高", None), ("1080p", 1080), ("720p", 720), ("480p", 480)]


class DeckPage(QWidget):
    def __init__(
        self,
        usecase: BuildDeckUseCase,
        catalog: ModelCatalog,
        settings: Settings,
        save: Callable[[], None],
    ):
        super().__init__()
        self._usecase = usecase
        self._catalog = catalog
        self._settings = settings
        self._save = save
        self._worker: BuildDeckWorker | None = None
        self._html_path: str | None = None

        self._build_ui()
        self._load_models()
        self._apply_settings()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        title = QLabel("影片投影片摘要")
        title.setStyleSheet("font-size:20px; font-weight:600;")
        layout.addWidget(title)

        # 網址
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("貼上 YouTube 網址")
        paste_btn = QPushButton("貼上")
        paste_btn.clicked.connect(self._paste)
        url_row.addWidget(QLabel("網址"))
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(paste_btn)
        layout.addLayout(url_row)

        # 輸出資料夾
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.editingFinished.connect(self._persist)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(40)
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(QLabel("輸出資料夾"))
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # 設定
        opt_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)   # Ollama 連不上時仍可手打
        self.model_combo.currentTextChanged.connect(self._persist)

        self.quality_combo = QComboBox()
        for label, _ in _QUALITIES:
            self.quality_combo.addItem(label)
        self.quality_combo.currentIndexChanged.connect(self._persist)

        self.min_spin = QSpinBox()
        self.min_spin.setRange(1, 50)
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 50)
        self.min_spin.valueChanged.connect(self._on_range_changed)
        self.max_spin.valueChanged.connect(self._on_range_changed)

        opt_row.addWidget(QLabel("模型"))
        opt_row.addWidget(self.model_combo, 1)
        opt_row.addSpacing(12)
        opt_row.addWidget(QLabel("畫質"))
        opt_row.addWidget(self.quality_combo)
        opt_row.addSpacing(12)
        opt_row.addWidget(QLabel("頁數"))
        opt_row.addWidget(self.min_spin)
        opt_row.addWidget(QLabel("–"))
        opt_row.addWidget(self.max_spin)
        layout.addLayout(opt_row)

        layout.addStretch(1)

        # 動作
        action_row = QHBoxLayout()
        self.open_btn = QPushButton("開啟 HTML")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_result)
        self.action_btn = QPushButton("生成摘要")
        self.action_btn.setMinimumHeight(36)
        self.action_btn.clicked.connect(self._on_action)
        action_row.addStretch(1)
        action_row.addWidget(self.open_btn)
        action_row.addWidget(self.action_btn)
        layout.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    # --- 設定 ---
    def _load_models(self) -> None:
        models = self._catalog.list_models()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.blockSignals(False)
        if not models:
            self.status.setText(
                f"提示：連不上 Ollama（{self._settings.ollama_host}），"
                "模型清單無法載入。可直接輸入模型名稱，生成時會再試一次。"
            )

    def _apply_settings(self) -> None:
        # 套用期間全部擋信號，避免以尚未套用的預設值覆蓋已存設定。
        # 這是 musicbox commit bf7ed26 修過的實際 bug，直接沿用作法。
        s = self._settings
        widgets = (self.url_edit, self.dir_edit, self.model_combo,
                   self.quality_combo, self.min_spin, self.max_spin)
        for w in widgets:
            w.blockSignals(True)
        self.dir_edit.setText(s.output_dir)
        self.model_combo.setCurrentText(s.model)
        idx = next((i for i, (_, h) in enumerate(_QUALITIES) if h == s.max_height), 0)
        self.quality_combo.setCurrentIndex(idx)
        self.min_spin.setValue(s.min_slides)
        self.max_spin.setValue(s.max_slides)
        for w in widgets:
            w.blockSignals(False)

    def _persist(self) -> None:
        self._settings.output_dir = self.dir_edit.text().strip()
        self._settings.model = self.model_combo.currentText().strip()
        self._settings.max_height = _QUALITIES[self.quality_combo.currentIndex()][1]
        self._settings.min_slides = self.min_spin.value()
        self._settings.max_slides = self.max_spin.value()
        self._save()

    def _on_range_changed(self) -> None:
        """下限不得超過上限。"""
        if self.min_spin.value() > self.max_spin.value():
            self.max_spin.setValue(self.min_spin.value())
        self._persist()

    # --- 動作 ---
    def _paste(self) -> None:
        from PySide6.QtWidgets import QApplication
        self.url_edit.setText(QApplication.clipboard().text().strip())

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "選擇輸出資料夾", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)
            self._persist()

    def _on_action(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.status.setText("取消中…")
            return
        self._start()

    def _start(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.status.setText("請先貼上 YouTube 網址。")
            return
        if not self.model_combo.currentText().strip():
            self.status.setText("請先選擇或輸入模型名稱。")
            return
        self._persist()
        self._html_path = None
        self.open_btn.setEnabled(False)
        self.progress.setValue(0)
        self.status.setText("開始…")
        self._set_busy(True)

        self._worker = BuildDeckWorker(self._usecase, url, self._settings)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _on_progress(self, frac, status: str) -> None:
        if frac is None:
            self.progress.setRange(0, 0)          # 不確定 → 忙碌動畫
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(frac * 100))
        self.status.setText(status)

    def _on_done(self, result) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._html_path = result.html_path
        self.open_btn.setEnabled(True)
        missing = result.deck.missing_images
        note = f"，其中 {missing} 頁沒有截圖" if missing else ""
        self.status.setText(f"✅ 完成，共 {len(result.deck.slides)} 頁{note}。\n{result.html_path}")
        self._set_busy(False)

    def _on_fail(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.status.setText(f"❌ 失敗：{message}")
        self._set_busy(False)

    def _on_cancelled(self) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("已取消。")
        self._set_busy(False)

    def _open_result(self) -> None:
        if self._html_path and os.path.exists(self._html_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._html_path))

    def _set_busy(self, busy: bool) -> None:
        self.action_btn.setText("取消" if busy else "生成摘要")
        for w in (self.url_edit, self.dir_edit, self.model_combo,
                  self.quality_combo, self.min_spin, self.max_spin):
            w.setEnabled(not busy)
