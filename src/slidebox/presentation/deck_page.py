"""投影片摘要頁：網址、佇列、模型、輸出設定、進度、結果。"""
from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..domain.entities import Settings
from ..domain.ports import ModelCatalog
from ..usecases.build_deck import BuildDeckUseCase
from ..usecases.queue import (
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    JobQueue,
    QueueItem,
)
from .workers import BuildDeckWorker

# 畫質下拉：顯示文字 → max_height（沿用 musicbox 的形狀）
_QUALITIES = [("最高", None), ("1080p", 1080), ("720p", 720), ("480p", 480)]

# 連續失敗幾次就停下整個佇列。「一支失敗不停佇列」的理由在「這支影片有
# 問題」時成立；連續失敗代表的是環境壞掉（Ollama 沒開、模型名稱打錯），
# 此時繼續跑只會在幾秒內把整排燒成紅色，使用者得一支一支重貼。
MAX_CONSECUTIVE_FAILURES = 2

# 清單列上的錯誤訊息截斷長度：Ollama 的原始錯誤可以很長，整串塞進
# QListWidget 會撐出橫向捲軸。
_MAX_MESSAGE = 70

# 關視窗時最多等 worker 幾毫秒。下載片段與抽幀無法中斷，等得太短就等於
# 沒等；等得太久使用者會以為當掉。
_SHUTDOWN_WAIT_MS = 30_000

# 佇列每一項的狀態符號。純文字符號，不依賴任何圖示資源。
_ICONS = {
    PENDING: "⏳",
    RUNNING: "▶",
    DONE: "✅",
    FAILED: "❌",
    CANCELLED: "⏹",
}


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
        # 已經送出結果、但 QThread 還沒真正收尾的 worker 留在這裡。少了這一
        # 步，_on_done 一鬆手，Python 就會在 thread 還在跑時銷毀 QThread，
        # Qt 直接中止整個行程——跟關視窗時那個崩潰是同一個原因。
        self._retiring: set[BuildDeckWorker] = set()
        self._queue = JobQueue()
        self._consecutive_failures = 0
        # 使用者選的是「哪一項」，不是「第幾列」：移除或清除已完成會讓列號
        # 位移，記列號會讓選取自己跳到別支影片，接著按移除就刪錯人。
        self._selected_item: QueueItem | None = None

        self._build_ui()
        self._load_models()
        self._apply_settings()
        self._refresh()

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
        self.url_edit.setPlaceholderText("貼上 YouTube 網址，按 Enter 加入佇列")
        self.url_edit.returnPressed.connect(self._add)
        self.url_edit.textChanged.connect(lambda _: self._refresh_buttons())
        paste_btn = QPushButton("貼上")
        paste_btn.clicked.connect(self._paste)
        self.add_btn = QPushButton("加入佇列")
        self.add_btn.clicked.connect(self._add)
        url_row.addWidget(QLabel("網址"))
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(paste_btn)
        url_row.addWidget(self.add_btn)
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

        self.detail_check = QCheckBox("詳細內容（每頁加一段完整敘述，並附上完整逐字稿）")
        self.detail_check.setToolTip(
            "適合不想看影片但要知道全部內容的情況。\n生成時間較長，HTML 檔也較大。"
        )
        # PySide6 6.7 起 stateChanged 改名 checkStateChanged，舊名仍可用但已棄用
        signal = getattr(self.detail_check, "checkStateChanged", None)
        (signal or self.detail_check.stateChanged).connect(self._persist)
        layout.addWidget(self.detail_check)

        # 佇列
        self.queue_list = QListWidget()
        self.queue_list.itemDoubleClicked.connect(self._open_item)
        self.queue_list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self.queue_list, 1)

        # 動作
        action_row = QHBoxLayout()
        self.remove_btn = QPushButton("移除")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.retry_btn = QPushButton("重試")
        self.retry_btn.clicked.connect(self._retry_selected)
        self.clear_btn = QPushButton("清除已完成")
        self.clear_btn.clicked.connect(self._clear_finished)
        self.open_btn = QPushButton("開啟 HTML")
        self.open_btn.clicked.connect(self._open_selected)
        self.action_btn = QPushButton("開始")
        self.action_btn.setMinimumHeight(36)
        self.action_btn.clicked.connect(self._on_action)
        action_row.addWidget(self.retry_btn)
        action_row.addWidget(self.remove_btn)
        action_row.addWidget(self.clear_btn)
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
                   self.quality_combo, self.min_spin, self.max_spin,
                   self.detail_check)
        for w in widgets:
            w.blockSignals(True)
        self.dir_edit.setText(s.output_dir)
        self.model_combo.setCurrentText(s.model)
        idx = next((i for i, (_, h) in enumerate(_QUALITIES) if h == s.max_height), 0)
        self.quality_combo.setCurrentIndex(idx)
        self.min_spin.setValue(s.min_slides)
        self.max_spin.setValue(s.max_slides)
        self.detail_check.setChecked(s.detailed)
        for w in widgets:
            w.blockSignals(False)

    def _persist(self) -> None:
        self._settings.output_dir = self.dir_edit.text().strip()
        self._settings.model = self.model_combo.currentText().strip()
        self._settings.max_height = _QUALITIES[self.quality_combo.currentIndex()][1]
        self._settings.min_slides = self.min_spin.value()
        self._settings.max_slides = self.max_spin.value()
        self._settings.detailed = self.detail_check.isChecked()
        self._save()

    def _on_range_changed(self) -> None:
        """下限不得超過上限。"""
        if self.min_spin.value() > self.max_spin.value():
            self.max_spin.setValue(self.min_spin.value())
        self._persist()

    # --- 佇列 ---
    def _paste(self) -> None:
        from PySide6.QtWidgets import QApplication
        self.url_edit.setText(QApplication.clipboard().text().strip())

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "選擇輸出資料夾", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)
            self._persist()

    def _add(self) -> None:
        """把網址列的內容加進佇列；沒有東西在跑就立刻開始。"""
        url = self.url_edit.text().strip()
        if not url:
            self.status.setText("請先貼上 YouTube 網址。")
            return
        if self._queue.add(url) is None:
            self.status.setText("這個網址已經在佇列裡了。")
            return
        self.url_edit.clear()
        self._refresh()
        self._start_next()

    def _on_row_changed(self, row: int) -> None:
        self._selected_item = (
            self._queue.items[row] if 0 <= row < len(self._queue.items) else None)
        self._refresh_buttons()

    def _selected(self) -> QueueItem | None:
        """目前選的項目；已經被移除的話就當成沒有選。"""
        item = self._selected_item
        return item if item is not None and item in self._queue.items else None

    def _remove_selected(self) -> None:
        item = self._selected()
        if item is None:
            return
        if not self._queue.remove(item):
            self.status.setText("進行中的項目不能移除，請先按取消。")
            return
        self._refresh()

    def _retry_selected(self) -> None:
        item = self._selected()
        if item is None or not self._queue.retry(item):
            return
        # 使用者親手按下重試，等於說「環境我修好了」——連續失敗計數歸零
        self._consecutive_failures = 0
        self._refresh()
        self._start_next()

    def _clear_finished(self) -> None:
        self._queue.clear_finished()
        self._refresh()

    def _open_selected(self) -> None:
        item = self._selected()
        # 沒選任何一項時開最後一個完成的——「做完就想看」是最常見的動作
        if item is None or item.status != DONE:
            fallback = next(
                (i for i in reversed(self._queue.items) if i.status == DONE), None)
            # 但要說清楚開的是哪一支：靜默改開別支影片會讓人以為成品錯了
            if fallback is not None and item is not None:
                self.status.setText(
                    f"這一項還沒有成品，改開啟最近完成的：{fallback.display}")
            item = fallback
        self._open_item_data(item)

    def _open_item(self, list_item: QListWidgetItem) -> None:
        # 直接由被點的那一列反查，不依賴「Qt 會先更新 currentRow」這個隱性順序
        row = self.queue_list.row(list_item)
        if 0 <= row < len(self._queue.items):
            self._open_item_data(self._queue.items[row])

    def _open_item_data(self, item: QueueItem | None) -> None:
        if item is not None and item.html_path and os.path.exists(item.html_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.html_path))

    # --- 執行 ---
    def _on_action(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.status.setText("取消中…（部分步驟無法中斷，會在該步驟完成後停止）")
            return
        # 網址列還有東西就順手加入：使用者貼完直接按「開始」是最自然的動作
        if self.url_edit.text().strip():
            self._add()
            return
        self._start_next()

    def _start_next(self) -> None:
        # 守衛看佇列狀態，不看 QThread.isRunning()：finished signal 是跨執行緒
        # 排進事件迴圈的，送達時 worker thread 往往還沒真的結束，isRunning()
        # 仍是 True，於是下一項永遠停在等待且毫無訊息。佇列狀態才是單一真相。
        if self._queue.running is not None:
            return
        if not self.model_combo.currentText().strip():
            self.status.setText("請先選擇或輸入模型名稱。")
            return
        item = self._queue.start_next()
        if item is None:
            self._refresh()
            return
        self._persist()
        self.progress.setValue(0)
        self.status.setText(f"開始：{item.display}")
        self._refresh()

        self._worker = BuildDeckWorker(self._usecase, item.url, self._settings)
        self._worker.progress.connect(self._on_progress)
        # 用預設引數綁住當下這一項：等 signal 送達時 self._queue.running
        # 可能已經換人了，用它去記結果會寫錯項目。
        self._worker.finished_ok.connect(lambda r, it=item: self._on_done(r, it))
        self._worker.failed.connect(lambda m, it=item: self._on_fail(m, it))
        self._worker.cancelled.connect(lambda it=item: self._on_cancelled(it))
        self._worker.finished.connect(lambda w=self._worker: self._retire(w))
        self._worker.start()

    def _on_progress(self, frac, status: str) -> None:
        if frac is None:
            self.progress.setRange(0, 0)          # 不確定 → 忙碌動畫
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(frac * 100))
        self.status.setText(status)

    def _on_done(self, result, item: QueueItem) -> None:
        self._release_worker()
        self._consecutive_failures = 0
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._queue.finish(item, result.html_path, result.deck.video_title)
        missing = result.deck.missing_images
        note = f"，其中 {missing} 頁沒有截圖" if missing else ""
        # 內容來源（例如「由語音辨識產生」）留在完成訊息裡，否則做完就看不出來
        source = f"\n{result.deck.source_note}" if result.deck.source_note else ""
        item.message = f"{len(result.deck.slides)} 頁{note}"
        self.status.setText(
            f"✅ 完成，共 {len(result.deck.slides)} 頁{note}。{source}\n{result.html_path}"
        )
        self._refresh()
        self._start_next()

    def _on_fail(self, message: str, item: QueueItem) -> None:
        self._release_worker()
        self._consecutive_failures += 1
        self.progress.setRange(0, 100)
        self._queue.fail(item, message)
        self.status.setText(f"❌ 失敗：{message}")
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            # 連續失敗代表環境壞掉，不是這支影片的問題。停下來保住剩下的
            # 項目，使用者修好之後選起來按「重試」即可。
            self.status.setText(
                f"❌ 連續 {self._consecutive_failures} 項失敗，已暫停佇列。"
                f"請確認 Ollama 與模型設定，修好後選起來按「重試」。\n最後的錯誤：{message}"
            )
            self._refresh()
            return
        self._refresh()
        # 單獨一支失敗不該讓整排停下——使用者可能排完就去做別的事了
        self._start_next()

    def _on_cancelled(self, item: QueueItem) -> None:
        self._release_worker()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._queue.cancel(item)
        pending = self._queue.pending_count
        self.status.setText(
            "已取消。" + (f"還有 {pending} 項在等待，按「開始」繼續。" if pending else "")
        )
        self._refresh()   # 取消就是停下來，不自動接續下一項

    def _release_worker(self) -> None:
        """結果已經收下，但 QThread 要等它自己的 finished 訊號才算真的結束。"""
        if self._worker is not None:
            self._retiring.add(self._worker)
        self._worker = None

    def _retire(self, worker: BuildDeckWorker) -> None:
        """QThread 的 finished 訊號送達（主執行緒）後才放手。"""
        worker.wait()
        self._retiring.discard(worker)

    def shutdown(self) -> bool:
        """關視窗時呼叫：要求取消並等它停下來。回傳是否已經停妥。

        沒有這一步的話，Python 會在還有 QThread 在跑時銷毀它，Qt 直接中止
        整個行程（0xC0000409「應用程式已停止運作」），而且 use case 裡
        finally 的暫存清理全部不會跑，_clips 與音訊暫存檔就留在硬碟上。
        """
        worker = self._worker
        if worker is None or not worker.isRunning():
            self._wait_for_retiring()
            return True
        worker.cancel()
        self.status.setText("正在停止…（部分步驟無法中斷，請稍候）")
        # 邊等邊讓畫面還能重繪，否則使用者看到的是沒有反應的當掉視窗
        from PySide6.QtWidgets import QApplication
        deadline = _SHUTDOWN_WAIT_MS
        while deadline > 0 and worker.isRunning():
            QApplication.processEvents()
            worker.wait(50)
            deadline -= 50
        stopped = not worker.isRunning()
        self._wait_for_retiring()
        return stopped

    def _wait_for_retiring(self) -> None:
        """等那些已經交出結果、正在收尾的 thread 真的結束。"""
        for worker in list(self._retiring):
            worker.wait(_SHUTDOWN_WAIT_MS)
        self._retiring.clear()

    # --- 畫面狀態 ---
    def _refresh(self) -> None:
        # 用物件身分而不是索引還原選取：移除或清除已完成會讓索引位移，
        # 使用者會發現選取自己跳到別支影片，接著按「移除」就刪錯人。
        previous = self._selected()
        self._selected_item = previous
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for item in self._queue.items:
            text = f"{_ICONS.get(item.status, '')} {item.display}"
            if item.message:
                text += f" — {item.message[:_MAX_MESSAGE]}"
            self.queue_list.addItem(text)
        if previous is not None:
            index = next(
                (n for n, i in enumerate(self._queue.items) if i is previous), -1)
            self.queue_list.setCurrentRow(index)
        self.queue_list.blockSignals(False)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        running = self._queue.running is not None
        self.action_btn.setText("取消" if running else "開始")
        self.action_btn.setEnabled(
            running or self._queue.pending_count > 0 or bool(self.url_edit.text().strip())
        )
        selected = self._selected()
        self.remove_btn.setEnabled(selected is not None and selected.status != RUNNING)
        self.retry_btn.setEnabled(
            selected is not None and selected.status in (FAILED, CANCELLED))
        self.open_btn.setEnabled(
            any(i.status == DONE and i.html_path for i in self._queue.items)
        )
        # 設定在每一項開始的當下讀取，生成途中改動會讓正在跑的那一項行為
        # 不一致，所以照舊鎖住；網址列不鎖——邊等邊排隊正是佇列的用途。
        for w in (self.dir_edit, self.model_combo, self.quality_combo,
                  self.min_spin, self.max_spin, self.detail_check):
            w.setEnabled(not running)
