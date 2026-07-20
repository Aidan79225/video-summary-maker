# 歌名正規化 + 列表內編輯 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓「重新編號」頁能一鍵正規化歌名（清雜訊括號、全形轉半形、簡轉繁、去非法字元、整理空白），並可在列表內直接編輯歌名，流水號仍由程式自動管理。

**Architecture:** 純邏輯（正規化、組檔名、驗證）放在 `usecases/rename_songs.py`，以 pytest 用記憶體假物件測試。`presentation/pages/rename_page.py` 改成三欄表格（舊檔名 / 可編輯歌名 / 唯讀新檔名預覽），維護 `_titles` 與 `_manual` 兩個狀態，重算號碼時不動歌名。簡轉繁用 OpenCC，採防禦式延遲載入，缺套件時靜默降級。

**Tech Stack:** Python 3.13、PySide6、pytest、uv、opencc-python-reimplemented。

## Global Constraints

- 依賴管理一律用 uv：加套件用 `uv add`，跑測試用 `uv run pytest`。
- 分層依賴方向：`presentation → usecases → domain`；純邏輯不可 import PySide6。
- 既有行為（兩階段安全改名、復原、試聽、背景執行緒、下載頁）不得破壞。
- 正規化為總開關（一個勾選框），只清「已知雜訊」；`(Live)`、`(Remix)`、`feat.` 等須保留。
- 列表只編輯歌名段，流水號由程式管理；手改內容在改設定／排序時保留。
- 簡轉繁失敗時其餘規則照常運作，不可讓整個正規化拋例外。
- 檔案編碼一律 UTF-8；註解與 UI 文案用繁體中文，貼合既有風格。

**設計說明（相對 spec 的一處精修）：** spec 提到把 `normalize` 加進 `RenameOptions`，但 use case 端不消費它（歌名在 presentation 端就已用 `derive_title` 算好再傳入 `plan_from_titles`）。為避免死欄位，`normalize` 只加在 `Settings`，由 UI 勾選框持有當前狀態。功能與 spec 完全一致。

---

### Task 1: `Settings` 增加 `normalize` 欄位與持久化

**Files:**
- Modify: `src/musicbox/domain/entities.py`（`Settings` dataclass）
- Modify: `src/musicbox/infrastructure/settings_repository.py`（`load` / `save`）
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `Settings.normalize: bool`（預設 `False`）；`JsonSettingsRepository` 讀寫 `normalize` 鍵。

- [ ] **Step 1: 寫失敗測試** — 在 `tests/test_settings.py` 的 `test_round_trip` 加入 `normalize=True`，並新增一條預設值測試。

在檔案末端新增：

```python
def test_normalize_defaults_false_and_round_trips(tmp_path):
    path = str(tmp_path / "settings.json")
    repo = JsonSettingsRepository(path)
    assert _default().normalize is False           # 預設關閉
    saved = Settings(output_dir="/dl", rename_folder="/songs", normalize=True)
    repo.save(saved)
    assert repo.load(_default()).normalize is True  # 存得回、讀得到
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_settings.py::test_normalize_defaults_false_and_round_trips -v`
Expected: FAIL（`TypeError: ... unexpected keyword argument 'normalize'`）

- [ ] **Step 3: 在 `Settings` 加欄位**

`src/musicbox/domain/entities.py` 的 `Settings` dataclass 末端（`padding` 之後）加一行：

```python
    padding: int = 2
    normalize: bool = False
```

- [ ] **Step 4: 讓 repository 讀寫 `normalize`**

`src/musicbox/infrastructure/settings_repository.py`：

`load()` 的 `return Settings(...)` 內，`padding=` 之後加：

```python
            padding=data.get("padding", default.padding),
            normalize=data.get("normalize", default.normalize),
```

`save()` 的 `data = {...}` 內，`"padding": ...` 之後加：

```python
            "padding": settings.padding,
            "normalize": settings.normalize,
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_settings.py -v`
Expected: PASS（全部）

- [ ] **Step 6: Commit**

```bash
git add src/musicbox/domain/entities.py src/musicbox/infrastructure/settings_repository.py tests/test_settings.py
git commit -m "feat: Settings 增加 normalize 欄位與持久化"
```

---

### Task 2: 正規化核心純函式（含 OpenCC 簡轉繁）

**Files:**
- Modify: `pyproject.toml` / `uv.lock`（加 `opencc-python-reimplemented`）
- Modify: `src/musicbox/usecases/rename_songs.py`（新增常數與函式）
- Test: `tests/test_normalize.py`（新建）

**Interfaces:**
- Consumes: 既有 `_clean_title(stem)`。
- Produces:
  - `normalize_title(title: str) -> str`
  - `derive_title(old_name: str, normalize: bool) -> str`
  - 模組級旗標 `OPENCC_AVAILABLE: bool`（延遲載入後才有正確值）

- [ ] **Step 1: 加入 OpenCC 依賴**

Run: `uv add opencc-python-reimplemented`
Expected: `pyproject.toml` 的 `dependencies` 多一行、`uv.lock` 更新、安裝成功。（import 名稱為 `opencc`）

- [ ] **Step 2: 寫失敗測試** — 新建 `tests/test_normalize.py`：

```python
"""歌名正規化純函式的單元測試。"""
from __future__ import annotations

from musicbox.usecases import rename_songs as rs
from musicbox.usecases.rename_songs import derive_title, normalize_title


def test_removes_known_noise_brackets():
    assert normalize_title("告白氣球 (Official Music Video)") == "告白氣球"
    assert normalize_title("Shape of You [Official Audio]") == "Shape of You"
    assert normalize_title("演唱會 【MV】") == "演唱會"
    assert normalize_title("某歌 (Lyric Video)") == "某歌"


def test_keeps_meaningful_brackets():
    assert normalize_title("Hotel California (Live)") == "Hotel California (Live)"
    assert normalize_title("某歌 (Remix)") == "某歌 (Remix)"
    assert normalize_title("某歌 (feat. ABC)") == "某歌 (feat. ABC)"


def test_fullwidth_to_halfwidth():
    assert normalize_title("ＡＢＣ１２３") == "ABC123"


def test_removes_illegal_filename_chars():
    assert normalize_title('a: b / c') == "a b c"


def test_collapses_whitespace_and_underscores():
    assert normalize_title("a__b   c") == "a b c"


def test_simplified_to_traditional():
    normalize_title("觸發初始化")          # 讓 OpenCC 延遲載入
    assert rs.OPENCC_AVAILABLE is True     # 本專案已把 opencc 列為硬依賴
    assert normalize_title("简体字") == "簡體字"


def test_traditional_fallback_when_opencc_absent(monkeypatch):
    # 模擬「已嘗試載入但失敗」：簡轉繁略過，其餘規則仍生效
    monkeypatch.setattr(rs, "_opencc_tried", True)
    monkeypatch.setattr(rs, "_opencc_converter", None)
    assert normalize_title("简体 (Official Video)") == "简体"


def test_derive_title_off_vs_on():
    name = "01-告白氣球 (Official MV).mp3"
    assert derive_title(name, normalize=False) == "告白氣球 (Official MV)"
    assert derive_title(name, normalize=True) == "告白氣球"
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: FAIL（`ImportError: cannot import name 'normalize_title'`）

- [ ] **Step 4: 實作正規化函式**

在 `src/musicbox/usecases/rename_songs.py` 檔頭 `import re` 之後加 `import unicodedata` 不需要（不使用）；在 `_clean_title` 函式之後、`build_undo_plan` 之前，插入：

```python
# --- 歌名正規化 ---

# 已知雜訊關鍵字。拉丁詞：整個括號內容的每個詞都是雜訊詞才移除。
_LATIN_NOISE = {
    "official", "music", "video", "audio", "lyric", "lyrics",
    "mv", "m/v", "hd", "hq", "4k",
}
# CJK 雜訊詞：括號內容包含即移除。
_CJK_NOISE = ("高畫質", "中文字幕", "歌詞", "動態歌詞")

# 括號群組：() [] 【】 {}（全形括號會先被轉成半形）
_BRACKET_RE = re.compile(r"[(\[【{]([^()\[\]【】{}]*)[)\]】}]")
# Windows 非法檔名字元與控制字元
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# OpenCC 簡轉繁：延遲載入，缺套件時降級。
_opencc_converter = None
_opencc_tried = False
OPENCC_AVAILABLE = False


def _fullwidth_to_halfwidth(s: str) -> str:
    """全形英數與標點轉半形，全形空格轉半形空格。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _is_noise_bracket(inner: str) -> bool:
    t = inner.strip().lower()
    if not t:
        return False
    for kw in _CJK_NOISE:
        if kw in t:
            return True
    tokens = re.findall(r"[a-z0-9/]+", t)
    return bool(tokens) and all(tok in _LATIN_NOISE for tok in tokens)


def _strip_noise_brackets(s: str) -> str:
    return _BRACKET_RE.sub(lambda m: "" if _is_noise_bracket(m.group(1)) else m.group(0), s)


def _to_traditional(s: str) -> str:
    """簡體轉繁體（台灣慣用詞）；OpenCC 不可用時原樣回傳。"""
    global _opencc_converter, _opencc_tried, OPENCC_AVAILABLE
    if not _opencc_tried:
        _opencc_tried = True
        try:
            from opencc import OpenCC
            _opencc_converter = OpenCC("s2twp")
            OPENCC_AVAILABLE = True
        except Exception:  # noqa: BLE001 缺套件或初始化失敗都降級
            _opencc_converter = None
            OPENCC_AVAILABLE = False
    if _opencc_converter is None:
        return s
    try:
        return _opencc_converter.convert(s)
    except Exception:  # noqa: BLE001
        return s


def normalize_title(title: str) -> str:
    """依序：全形轉半形 → 移除已知雜訊括號 → 簡轉繁 → 去非法字元 → 整理空白。"""
    s = _fullwidth_to_halfwidth(title)
    s = _strip_noise_brackets(s)
    s = _to_traditional(s)
    s = _ILLEGAL_RE.sub("", s)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def derive_title(old_name: str, normalize: bool) -> str:
    """由舊檔名推導歌名：去掉開頭舊號碼，視開關套用正規化。"""
    stem = os.path.splitext(old_name)[0]
    title = _clean_title(stem)
    if normalize:
        title = normalize_title(title)
    return title
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: PASS（含簡轉繁與降級案例）

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/musicbox/usecases/rename_songs.py tests/test_normalize.py
git commit -m "feat: 歌名正規化核心函式與 OpenCC 簡轉繁"
```

---

### Task 3: `plan_from_titles` 與驗證函式

**Files:**
- Modify: `src/musicbox/usecases/rename_songs.py`（`BuildRenamePlanUseCase` 新增方法、模組級驗證函式）
- Test: `tests/test_rename.py`（新增案例）

**Interfaces:**
- Consumes: 既有 `_number_for(stem, index, width, mode)`、`RenameItem`、`RenamePlan`。
- Produces:
  - `BuildRenamePlanUseCase.plan_from_titles(folder: str, ordered: Sequence[tuple[str, str]], options: RenameOptions) -> RenamePlan`
  - `duplicate_new_names(plan: RenamePlan) -> set[str]`
  - `has_illegal_chars(title: str) -> bool`

- [ ] **Step 1: 寫失敗測試** — 在 `tests/test_rename.py` 末端新增：

```python
def test_plan_from_titles_numbers_and_assembles():
    gw = FakeGateway({FOLDER: []})
    uc = BuildRenamePlanUseCase(gw)
    ordered = [("x.mp3", "告白氣球"), ("y.mp3", "晴天")]
    plan = uc.plan_from_titles(FOLDER, ordered, RenameOptions())
    assert _mapping(plan) == {"x.mp3": "01-告白氣球.mp3", "y.mp3": "02-晴天.mp3"}


def test_plan_from_titles_keep_mode_uses_original_number():
    gw = FakeGateway({FOLDER: []})
    uc = BuildRenamePlanUseCase(gw)
    ordered = [("05-a.mp3", "Alpha")]
    plan = uc.plan_from_titles(FOLDER, ordered, RenameOptions(mode=RenameMode.KEEP))
    assert _mapping(plan) == {"05-a.mp3": "05-Alpha.mp3"}


def test_duplicate_new_names_detects_collisions():
    from musicbox.domain.entities import RenameItem, RenamePlan
    from musicbox.usecases.rename_songs import duplicate_new_names
    plan = RenamePlan(folder=FOLDER, items=(
        RenameItem("a.mp3", "01-x.mp3"),
        RenameItem("b.mp3", "01-x.mp3"),
        RenameItem("c.mp3", "02-y.mp3"),
    ))
    assert duplicate_new_names(plan) == {"01-x.mp3"}


def test_has_illegal_chars():
    from musicbox.usecases.rename_songs import has_illegal_chars
    assert has_illegal_chars("a:b") is True
    assert has_illegal_chars("正常名稱") is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_rename.py::test_plan_from_titles_numbers_and_assembles -v`
Expected: FAIL（`AttributeError: 'BuildRenamePlanUseCase' object has no attribute 'plan_from_titles'`）

- [ ] **Step 3: 實作方法與驗證函式**

`src/musicbox/usecases/rename_songs.py`：在 `BuildRenamePlanUseCase` 內，`plan_from_order` 方法之後、`_number_for` 之前，加入：

```python
    def plan_from_titles(
        self,
        folder: str,
        ordered: Sequence[tuple[str, str]],
        options: RenameOptions,
    ) -> RenamePlan:
        """依給定的 (舊檔名, 歌名) 序列組出計畫；歌名已由呼叫端決定，不再 clean。"""
        width = max(options.padding, len(str(len(ordered))))
        items: list[RenameItem] = []
        for i, (old, title) in enumerate(ordered, start=1):
            stem, ext = os.path.splitext(old)
            num = self._number_for(stem, i, width, options.mode)
            new = f"{num}{options.separator}{title}{ext}"
            items.append(RenameItem(old_name=old, new_name=new))
        return RenamePlan(folder=folder, items=tuple(items))
```

在檔案末端（`ApplyRenamePlanUseCase` 之後）加入模組級驗證函式：

```python
def duplicate_new_names(plan: RenamePlan) -> set[str]:
    """找出計畫中重複的新檔名（手動編輯後可能撞名）。"""
    seen: set[str] = set()
    dups: set[str] = set()
    for it in plan.items:
        if it.new_name in seen:
            dups.add(it.new_name)
        seen.add(it.new_name)
    return dups


def has_illegal_chars(title: str) -> bool:
    """歌名是否含 Windows 非法檔名字元或控制字元。"""
    return bool(_ILLEGAL_RE.search(title))
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_rename.py -v`
Expected: PASS（含新舊全部）

- [ ] **Step 5: Commit**

```bash
git add src/musicbox/usecases/rename_songs.py tests/test_rename.py
git commit -m "feat: plan_from_titles 與撞名/非法字元驗證函式"
```

---

### Task 4: 重新編號頁改為三欄可編輯 + 正規化開關

**Files:**
- Modify: `src/musicbox/presentation/pages/rename_page.py`
- 驗證：手動執行 `uv run main.py`（本專案無 UI 單元測試慣例）

**Interfaces:**
- Consumes: `derive_title`、`duplicate_new_names`、`has_illegal_chars`、`plan_from_titles`（Task 2、3）；`Settings.normalize`（Task 1）。
- Produces: 三欄表格與正規化勾選框的完整互動；不對外導出新符號。

- [ ] **Step 1: 更新 import**

`src/musicbox/presentation/pages/rename_page.py`：

`from PySide6.QtCore import QUrl` → 改成：

```python
from PySide6.QtCore import Qt, QUrl
```

`QtWidgets` import 區塊加入 `QCheckBox`（依字母序放在 `QComboBox` 之前）：

```python
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
```

`from ...usecases.rename_songs import (...)` 改成同時匯入新符號：

```python
from ...usecases.rename_songs import (
    ApplyRenamePlanUseCase,
    BuildRenamePlanUseCase,
    build_undo_plan,
    derive_title,
    duplicate_new_names,
    has_illegal_chars,
)
```

- [ ] **Step 2: 新增狀態欄位**

`__init__` 內，`self._order: list[str] = []` 之後加入：

```python
        self._order: list[str] = []
        self._titles: dict[str, str] = {}   # old_name → 目前歌名
        self._manual: set[str] = set()      # 被手動編輯過的 old_name
        self._loading = False               # 程式化更新表格時避免 itemChanged 遞迴
```

- [ ] **Step 3: 表格改三欄、加正規化勾選框、開啟編輯**

`_build_ui` 的設定列（`opt_row`）在 `self.pad_spin` 建立與連接之後、`opt_row.addWidget(QLabel("分隔符"))` 之前，加入勾選框：

```python
        self.normalize_check = QCheckBox("正規化歌名")
        self.normalize_check.toggled.connect(self._on_normalize_toggled)
```

並在 `opt_row` 佈局中把它加在補零之後：

找到這段：

```python
        opt_row.addWidget(QLabel("補零位數"))
        opt_row.addWidget(self.pad_spin)
        opt_row.addStretch(1)
```

改成：

```python
        opt_row.addWidget(QLabel("補零位數"))
        opt_row.addWidget(self.pad_spin)
        opt_row.addSpacing(12)
        opt_row.addWidget(self.normalize_check)
        opt_row.addStretch(1)
```

表格建立區塊，把兩欄改三欄並開啟編輯：

```python
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
```

- [ ] **Step 4: 設定讀寫納入 normalize**

`_apply_settings` 末端加入（用 blockSignals 避免載入時提前觸發重算）：

```python
        self.normalize_check.blockSignals(True)
        self.normalize_check.setChecked(s.normalize)
        self.normalize_check.blockSignals(False)
```

`_persist` 內，`self._settings.padding = self.pad_spin.value()` 之後加：

```python
        self._settings.normalize = self.normalize_check.isChecked()
```

- [ ] **Step 5: 改寫預覽/重算/渲染邏輯**

把 `_reload_from_folder`、`_on_option_changed`、`_recompute`、`_render` 四個方法替換為：

```python
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

            bad = has_illegal_chars(title) or it.new_name in dups
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

        changed = self._plan.changed_count if self._plan else 0
        if invalid:
            self.summary.setText(f"共 {len(items)} 個檔案；有 {invalid} 列名稱重複或含非法字元，請修正後再套用。")
            self.apply_btn.setEnabled(False)
        else:
            self.summary.setText(f"共 {len(items)} 個檔案，其中 {changed} 個需要改名。")
            self.apply_btn.setEnabled(changed > 0)

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
```

- [ ] **Step 6: 試聽的列索引仍正確**

確認 `_toggle_play` 用 `self._plan.items[row]` 取檔案——因表格列順序與 `self._plan.items` 一致（皆依 `self._order`），無需改動。快速掃視該方法確保沒有硬編欄位索引造成問題（它用 `currentRow()`，OK）。

- [ ] **Step 7: 手動驗證**

Run: `uv run main.py`

依序確認：
1. 表格出現三欄「舊檔名 / 歌名 / 新檔名」。
2. 勾「正規化歌名」→ 帶括號雜訊的歌名（如 `... (Official MV)`）的「歌名」欄變乾淨、「新檔名」即時更新；取消勾選則還原。
3. 雙擊某列「歌名」欄打字 → 「新檔名」即時反映；改分隔符或按「▲上移／▼下移」後，手改的歌名仍保留。
4. 在歌名欄打入 `a:b`（含非法字元）→ 該列標紅、「套用改名」停用、狀態列提示。
5. 讓兩列產生相同新檔名 → 標紅、停用套用。
6. 清空某列手改歌名 → 還原成自動歌名。
7. 「套用改名」「復原上次改名」「▶ 試聽選取」功能如常。

- [ ] **Step 8: 跑全部測試確認未回歸**

Run: `uv run pytest -v`
Expected: PASS（全部）

- [ ] **Step 9: Commit**

```bash
git add src/musicbox/presentation/pages/rename_page.py
git commit -m "feat: 重新編號頁三欄可編輯歌名與正規化開關"
```

---

### Task 5: 更新 README 說明

**Files:**
- Modify: `README.md`（「＃ 重新編號」段落）

- [ ] **Step 1: 補充說明**

`README.md` 的「### ＃ 重新編號」清單中，於「**補零位數**」項之後加入兩項：

```markdown
- **正規化歌名**：勾選後自動清掉歌名雜訊——移除 `(Official MV)`／`【MV】`／`[Official Audio]` 等已知雜訊括號、全形轉半形、簡體轉繁體（台灣慣用詞）、移除非法檔名字元、整理多餘空白。`(Live)`、`(Remix)`、`feat.` 等有意義內容保留。
- **列表內編輯**：直接在「歌名」欄打字改成想要的名稱，流水號仍由程式自動管理；手改內容在調設定或排序時保留。若歌名重複或含非法字元，該列標紅並暫停「套用改名」。
```

並在檔案結尾「## 更新 yt-dlp」之前，補一句簡繁相依說明（新增小節）：

```markdown
## 簡繁轉換

「正規化歌名」的簡體轉繁體用 [OpenCC](https://github.com/BYVoid/OpenCC)（`opencc-python-reimplemented`，純 Python）。若該套件未安裝，正規化的其他規則照常運作，僅略過簡繁轉換。
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README 補充正規化歌名與列表編輯說明"
```

---

## Self-Review

**Spec coverage：**
- 正規化五規則（全形轉半形／雜訊括號／簡轉繁／非法字元／整理空白）→ Task 2 `normalize_title`。✔
- 可開關、設定記憶 → Task 1（Settings）＋ Task 4（勾選框、`_persist`）。✔
- 只清已知雜訊、保留 `(Live)/(Remix)/feat.` → Task 2 `_is_noise_bracket` 邏輯＋測試 `test_keeps_meaningful_brackets`。✔
- 只編輯歌名、流水號自動管理 → Task 4 三欄＋唯讀新檔名欄。✔
- 手改在改設定／排序時保留 → Task 4 `_titles`/`_manual`／`_recompute` 不動歌名，手動驗證步驟 3。✔
- 撞名／非法字元擋下套用 → Task 3 驗證函式＋ Task 4 `_render` 標紅停用。✔
- OpenCC 依賴與降級 → Task 2 `_to_traditional`＋`test_traditional_fallback_when_opencc_absent`。✔
- 測試涵蓋 normalize/derive/plan_from_titles/驗證 → Task 2、3。✔

**Placeholder scan：** 無 TBD/TODO；每個改碼步驟均含完整程式碼與確切指令。✔

**Type consistency：** `normalize_title`/`derive_title`/`plan_from_titles`/`duplicate_new_names`/`has_illegal_chars` 在 Task 2、3 定義，Task 4 匯入使用，名稱一致；`Settings.normalize` 於 Task 1 定義、Task 4 讀寫；`_titles`/`_manual`/`_loading` 於 Task 4 Step 2 宣告後於各方法使用。✔
