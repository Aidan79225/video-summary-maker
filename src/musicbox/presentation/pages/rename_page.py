"""重新編號頁：選資料夾、即時預覽、可調設定、手動排序、復原、試聽。"""
from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...domain.entities import RenameMode, RenameOptions, RenamePlan, Settings
from ...usecases.rename_songs import (
    ApplyRenamePlanUseCase,
    BuildRenamePlanUseCase,
    build_undo_plan,
    derive_title,
    duplicate_new_names,
    has_illegal_chars,
)
from ...usecases import rename_songs as _rename_songs
from ..workers import RenameApplyWorker

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _HAS_MEDIA = True
except Exception:  # 缺多媒體後端時停用試聽
    _HAS_MEDIA = False

# 分隔符下拉：顯示文字 → 實際字元
_SEPARATORS = [("減號  -", "-"), ("空格", " "), ("點  .", ".")]
_MODES = [("連續重編（填補空缺）", RenameMode.RENUMBER), ("保留原號（統一格式）", RenameMode.KEEP)]


class RenamePage(QWidget):
    def __init__(
        self,
        build_usecase: BuildRenamePlanUseCase,
        apply_usecase: ApplyRenamePlanUseCase,
        settings: Settings,
        save: Callable[[], None],
    ):
        super().__init__()
        self._build_usecase = build_usecase
        self._apply_usecase = apply_usecase
        self._settings = settings
        self._save = save

        self._order: list[str] = []
        self._titles: dict[str, str] = {}   # old_name → 目前歌名
        self._manual: set[str] = set()      # 被手動編輯過的 old_name
        self._loading = False               # 程式化更新表格時避免 itemChanged 遞迴
        self._opencc_hint_shown = False   # 只在缺 OpenCC 時提示一次
        self._plan: RenamePlan | None = None
        self._applied: RenamePlan | None = None
        self._undo_plan: RenamePlan | None = None
        self._worker: RenameApplyWorker | None = None
        self._undoing = False
        self._applied_write_tags = False    # 記住上次套用時是否寫標籤，供復原沿用
        self._invalid_count = 0
        self._changed_count = 0

        self._player = None
        self._audio = None
        self._playing_path: str | None = None

        self._build_ui()
        self._apply_settings()
        self._reload_from_folder()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        title = QLabel("重新編號")
        title.setStyleSheet("font-size:20px; font-weight:600;")
        layout.addWidget(title)

        # 資料夾
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.textChanged.connect(self._reload_from_folder)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(40)
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(QLabel("資料夾"))
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # 設定
        opt_row = QHBoxLayout()
        self.sep_combo = QComboBox()
        for label, _ in _SEPARATORS:
            self.sep_combo.addItem(label)
        self.sep_combo.currentIndexChanged.connect(self._on_option_changed)

        self.mode_combo = QComboBox()
        for label, _ in _MODES:
            self.mode_combo.addItem(label)
        self.mode_combo.currentIndexChanged.connect(self._on_option_changed)

        self.pad_spin = QSpinBox()
        self.pad_spin.setRange(1, 5)
        self.pad_spin.valueChanged.connect(self._on_option_changed)

        self.normalize_check = QCheckBox("正規化歌名")
        self.normalize_check.toggled.connect(self._on_normalize_toggled)

        self.write_tags_check = QCheckBox("同時寫入 ID3 標題")
        self.write_tags_check.toggled.connect(self._on_write_tags_toggled)

        opt_row.addWidget(QLabel("分隔符"))
        opt_row.addWidget(self.sep_combo)
        opt_row.addSpacing(12)
        opt_row.addWidget(QLabel("模式"))
        opt_row.addWidget(self.mode_combo)
        opt_row.addSpacing(12)
        opt_row.addWidget(QLabel("補零位數"))
        opt_row.addWidget(self.pad_spin)
        opt_row.addSpacing(12)
        opt_row.addWidget(self.normalize_check)
        opt_row.addSpacing(12)
        opt_row.addWidget(self.write_tags_check)
        opt_row.addStretch(1)
        layout.addLayout(opt_row)

        # 表格 + 右側按鈕列
        table_row = QHBoxLayout()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["舊檔名", "歌名", "新檔名"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._on_item_changed)
        table_row.addWidget(self.table, 1)

        side = QVBoxLayout()
        self.up_btn = QPushButton("▲ 上移")
        self.down_btn = QPushButton("▼ 下移")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn.clicked.connect(lambda: self._move(1))
        side.addWidget(self.up_btn)
        side.addWidget(self.down_btn)
        if _HAS_MEDIA:
            self.play_btn = QPushButton("▶ 試聽選取")
            self.play_btn.clicked.connect(self._toggle_play)
            side.addSpacing(12)
            side.addWidget(self.play_btn)
        side.addStretch(1)
        table_row.addLayout(side)
        layout.addLayout(table_row, 1)

        # 套用列
        apply_row = QHBoxLayout()
        self.summary = QLabel("")
        self.undo_btn = QPushButton("復原上次改名")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._undo)
        self.apply_btn = QPushButton("套用改名")
        self.apply_btn.setMinimumHeight(36)
        self.apply_btn.clicked.connect(self._apply)
        apply_row.addWidget(self.summary, 1)
        apply_row.addWidget(self.undo_btn)
        apply_row.addWidget(self.apply_btn)
        layout.addLayout(apply_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        if _HAS_MEDIA:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.playbackStateChanged.connect(self._on_playback_state)

    # --- 設定 ---
    def _apply_settings(self) -> None:
        s = self._settings
        # 套用期間全部擋信號，避免觸發 reload/persist 而以尚未套用的預設值覆蓋已存設定；
        # __init__ 末端會顯式呼叫 _reload_from_folder() 做單次正確載入。
        widgets = (self.dir_edit, self.sep_combo, self.mode_combo, self.pad_spin,
                   self.normalize_check, self.write_tags_check)
        for w in widgets:
            w.blockSignals(True)
        self.dir_edit.setText(s.rename_folder)
        sep_idx = next((i for i, (_, c) in enumerate(_SEPARATORS) if c == s.separator), 0)
        self.sep_combo.setCurrentIndex(sep_idx)
        mode_idx = next((i for i, (_, m) in enumerate(_MODES) if m == s.mode), 0)
        self.mode_combo.setCurrentIndex(mode_idx)
        self.pad_spin.setValue(s.padding)
        self.normalize_check.setChecked(s.normalize)
        self.write_tags_check.setChecked(s.write_tags)
        for w in widgets:
            w.blockSignals(False)

    def _persist(self) -> None:
        self._settings.rename_folder = self.dir_edit.text().strip()
        self._settings.separator = _SEPARATORS[self.sep_combo.currentIndex()][1]
        self._settings.mode = _MODES[self.mode_combo.currentIndex()][1]
        self._settings.padding = self.pad_spin.value()
        self._settings.normalize = self.normalize_check.isChecked()
        self._settings.write_tags = self.write_tags_check.isChecked()
        self._save()

    def _current_options(self) -> RenameOptions:
        separator = _SEPARATORS[self.sep_combo.currentIndex()][1]
        mode = _MODES[self.mode_combo.currentIndex()][1]
        return RenameOptions(separator=separator, mode=mode, padding=self.pad_spin.value())

    # --- 預覽 ---
    def _reload_from_folder(self) -> None:
        """資料夾改變：重讀、依數字排序、重設歌名與手改狀態。"""
        folder = self.dir_edit.text().strip()
        try:
            plan = self._build_usecase.execute(folder, self._current_options())
        except Exception as e:  # noqa: BLE001
            self._order = []
            self._titles = {}
            self._manual = set()
            self._plan = None
            self._loading = True
            self.table.setRowCount(0)
            self._loading = False
            self.summary.setText(f"無法讀取資料夾：{e}")
            self.apply_btn.setEnabled(False)
            self._persist()
            return
        self._order = [it.old_name for it in plan.items]
        self._manual = set()
        on = self.normalize_check.isChecked()
        self._titles = {old: derive_title(old, on) for old in self._order}
        self._recompute()
        self._persist()

    def _on_option_changed(self) -> None:
        """分隔符／模式／補零改變：保留順序與歌名，只重算號碼。"""
        self._recompute()
        self._persist()

    def _on_normalize_toggled(self) -> None:
        """切換正規化：只重算非手改檔案的歌名，手改保留。"""
        on = self.normalize_check.isChecked()
        for old in self._order:
            if old not in self._manual:
                self._titles[old] = derive_title(old, on)
        self._recompute()
        self._persist()

    def _recompute(self) -> None:
        folder = self.dir_edit.text().strip()
        ordered = [(old, self._titles.get(old, "")) for old in self._order]
        self._plan = self._build_usecase.plan_from_titles(folder, ordered, self._current_options())
        self._render()
        self._maybe_opencc_hint()

    def _maybe_opencc_hint(self) -> None:
        """正規化開啟但 OpenCC 不可用時，提示一次（其餘規則照常）。"""
        if (
            self.normalize_check.isChecked()
            and not _rename_songs.OPENCC_AVAILABLE
            and not self._opencc_hint_shown
        ):
            self._opencc_hint_shown = True
            self.status.setText("提示：未啟用簡繁轉換（缺 OpenCC），其餘正規化規則照常運作。")

    def _render(self) -> None:
        self._loading = True
        items = self._plan.items if self._plan else ()
        dups = duplicate_new_names(self._plan) if self._plan else set()
        gray = QBrush(QColor(150, 150, 150))
        red = QBrush(QColor(220, 80, 80))
        self.table.setRowCount(len(items))
        invalid = 0
        for row, it in enumerate(items):
            old = self._order[row]
            title = self._titles.get(old, "")
            old_cell = QTableWidgetItem(it.old_name)
            old_cell.setFlags(old_cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            title_cell = QTableWidgetItem(title)  # 預設含 ItemIsEditable → 可編輯
            new_cell = QTableWidgetItem(it.new_name)
            new_cell.setFlags(new_cell.flags() & ~Qt.ItemFlag.ItemIsEditable)

            bad = not title.strip() or has_illegal_chars(title) or it.new_name in dups
            if bad:
                invalid += 1
                for c in (old_cell, title_cell, new_cell):
                    c.setForeground(red)
            elif not it.changed:
                old_cell.setForeground(gray)
                new_cell.setForeground(gray)

            self.table.setItem(row, 0, old_cell)
            self.table.setItem(row, 1, title_cell)
            self.table.setItem(row, 2, new_cell)
        self._loading = False

        self._changed_count = self._plan.changed_count if self._plan else 0
        self._invalid_count = invalid
        if invalid:
            self.summary.setText(f"共 {len(items)} 個檔案；有 {invalid} 列名稱空白、重複或含非法字元，請修正後再套用。")
        else:
            self.summary.setText(f"共 {len(items)} 個檔案，其中 {self._changed_count} 個需要改名。")
        self._refresh_apply_enabled()

    def _refresh_apply_enabled(self) -> None:
        items = self._plan.items if self._plan else ()
        if self._invalid_count > 0:
            self.apply_btn.setEnabled(False)
            return
        write_tags = self.write_tags_check.isChecked()
        self.apply_btn.setEnabled(self._changed_count > 0 or (write_tags and len(items) > 0))

    def _on_write_tags_toggled(self) -> None:
        self._refresh_apply_enabled()
        self._persist()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """使用者在「歌名」欄打字：更新歌名、標記手改（清空則還原自動）。"""
        if self._loading or item.column() != 1:
            return
        row = item.row()
        if row < 0 or row >= len(self._order):
            return
        old = self._order[row]
        text = item.text().strip()
        if text:
            self._titles[old] = text
            self._manual.add(old)
        else:
            self._manual.discard(old)
            self._titles[old] = derive_title(old, self.normalize_check.isChecked())
        self._recompute()

    # --- 手動排序 ---
    def _move(self, delta: int) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        target = row + delta
        if not (0 <= target < len(self._order)):
            return
        self._order[row], self._order[target] = self._order[target], self._order[row]
        self._recompute()
        self.table.selectRow(target)

    # --- 試聽 ---
    def _toggle_play(self) -> None:
        if not _HAS_MEDIA or self._plan is None:
            return
        row = self.table.currentRow()
        if row < 0 or row >= len(self._plan.items):
            self.status.setText("請先選一列。")
            return
        path = os.path.join(self._plan.folder, self._plan.items[row].old_name)
        if not os.path.isfile(path):
            self.status.setText("找不到檔案，無法試聽。")
            return
        state_playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if state_playing and self._playing_path == path:
            self._player.stop()
            return
        self._playing_path = path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _on_playback_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("⏸ 停止" if playing else "▶ 試聽選取")

    # --- 套用 / 復原 ---
    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "選擇資料夾", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    def _apply(self) -> None:
        if not self._plan:
            return
        write_tags = self.write_tags_check.isChecked()
        if self._plan.changed_count == 0 and not write_tags:
            return
        self._applied = self._plan
        self._applied_write_tags = write_tags
        self._undoing = False
        self._run_worker(self._plan, write_tags, "改名中…")

    def _undo(self) -> None:
        if not self._undo_plan or not self._undo_plan.items:
            return
        self._undoing = True
        self._run_worker(self._undo_plan, self._applied_write_tags, "復原中…")

    def _run_worker(self, plan: RenamePlan, write_tags: bool, busy_text: str) -> None:
        self._set_busy(True)
        self.progress.setValue(0)
        self.status.setText(busy_text)
        self._worker = RenameApplyWorker(self._apply_usecase, plan, write_tags)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.start()

    def _on_progress(self, frac, status: str) -> None:
        if frac is not None:
            self.progress.setValue(int(frac * 100))
        self.status.setText(status)

    def _on_done(self, count: int) -> None:
        self.progress.setValue(100)
        if self._undoing:
            self.status.setText(f"✅ 已復原 {count} 個檔案。")
            self._undo_plan = None
            self.undo_btn.setEnabled(False)
        else:
            self.status.setText(f"✅ 完成，已改名 {count} 個檔案。")
            self._undo_plan = build_undo_plan(self._applied) if self._applied else None
            self.undo_btn.setEnabled(bool(self._undo_plan and self._undo_plan.items))
        self._set_busy(False)
        self._reload_from_folder()

    def _on_fail(self, message: str) -> None:
        self.status.setText(f"❌ 失敗：{message}")
        self._set_busy(False)
        self._reload_from_folder()

    def _set_busy(self, busy: bool) -> None:
        self.apply_btn.setEnabled(not busy)
        self.undo_btn.setEnabled(not busy and bool(self._undo_plan and self._undo_plan.items))
        self.apply_btn.setText("處理中…" if busy else "套用改名")
