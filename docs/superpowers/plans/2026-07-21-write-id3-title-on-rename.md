# 改名時寫入 ID3 標題 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在「重新編號」頁加「同時寫入 ID3 標題」勾選框；套用改名（與復原）時，把每個檔案的新檔名（去副檔名）寫進該 MP3 的 ID3 標題標籤。

**Architecture:** 純標籤寫入抽象成 domain port `AudioTagGateway`，用 `mutagen` 在 infrastructure 實作；`ApplyRenamePlanUseCase` 注入選用的 tag gateway，改名兩階段完成後多一個寫標題階段（`title = 新檔名去副檔名`，對清單每個檔）。復原是「改回舊檔名」的改名計畫，套同一段邏輯即寫回舊檔名。UI 加勾選框並把旗標經 worker 傳入 use case。

**Tech Stack:** Python 3.13、PySide6、mutagen、pytest、uv、imageio-ffmpeg（測試用）。

## Global Constraints

- 依賴管理用 uv（`uv add`）；跑測試用 `uv run pytest`。
- 分層依賴方向：`presentation → usecases → domain`；`infrastructure` 實作 domain port；usecases 與 domain 不可 import PySide6 或 mutagen（mutagen 只在 infrastructure）。
- 既有行為（正規化、列表編輯、三欄表格、兩階段安全改名、復原、試聽、背景執行緒、下載頁）不得破壞。
- ID3 標題內容 = `os.path.splitext(new_name)[0]`（完整新檔名去副檔名，含編號）。
- 寫標籤對「清單每個檔」執行（含未改名者），不只改名的檔。
- 套用啟用條件：無無效列，且 `changed_count > 0` 或（`write_tags` 為真且清單非空）。
- 復原用當初套用時記下的 `write_tags` 旗標。
- 只寫「標題」一個標籤，不動 artist／album／track；只處理既有的 `.mp3` 過濾。
- 檔案編碼 UTF-8；註解／UI 文案用繁體中文，貼合既有風格。

---

### Task 1: `Settings` 增加 `write_tags` 欄位與持久化

**Files:**
- Modify: `src/musicbox/domain/entities.py`（`Settings`）
- Modify: `src/musicbox/infrastructure/settings_repository.py`（`load` / `save`）
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `Settings.write_tags: bool`（預設 `False`）；repository 讀寫 `write_tags` 鍵。

- [ ] **Step 1: 寫失敗測試** — 在 `tests/test_settings.py` 末端新增：

```python
def test_write_tags_defaults_false_and_round_trips(tmp_path):
    path = str(tmp_path / "settings.json")
    repo = JsonSettingsRepository(path)
    assert _default().write_tags is False
    saved = Settings(output_dir="/dl", rename_folder="/songs", write_tags=True)
    repo.save(saved)
    assert repo.load(_default()).write_tags is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_settings.py::test_write_tags_defaults_false_and_round_trips -v`
Expected: FAIL（`TypeError: ... unexpected keyword argument 'write_tags'`）

- [ ] **Step 3: 在 `Settings` 加欄位**

`src/musicbox/domain/entities.py` 的 `Settings` dataclass，`normalize: bool = False` 之後加一行：

```python
    normalize: bool = False
    write_tags: bool = False
```

- [ ] **Step 4: repository 讀寫**

`src/musicbox/infrastructure/settings_repository.py`：

`load()` 的 `Settings(...)` 內，`normalize=` 之後加：

```python
            normalize=data.get("normalize", default.normalize),
            write_tags=data.get("write_tags", default.write_tags),
```

`save()` 的 `data = {...}` 內，`"normalize": ...` 之後加：

```python
            "normalize": settings.normalize,
            "write_tags": settings.write_tags,
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_settings.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/musicbox/domain/entities.py src/musicbox/infrastructure/settings_repository.py tests/test_settings.py
git commit -m "feat: Settings 增加 write_tags 欄位與持久化"
```

---

### Task 2: `AudioTagGateway` port 與 `MutagenTagGateway` 實作

**Files:**
- Modify: `src/musicbox/domain/ports.py`（新增 port）
- Modify: `pyproject.toml` / `uv.lock`（加 `mutagen`）
- Create: `src/musicbox/infrastructure/tags.py`
- Test: `tests/test_tags.py`（新建）

**Interfaces:**
- Produces:
  - `AudioTagGateway` Protocol，方法 `write_title(self, path: str, title: str) -> None`
  - `MutagenTagGateway`（實作 `write_title`）

- [ ] **Step 1: 加入 mutagen 依賴**

Run: `uv add mutagen`
Expected: `pyproject.toml` 的 `dependencies` 多一行、`uv.lock` 更新、安裝成功。

- [ ] **Step 2: 新增 port**

`src/musicbox/domain/ports.py`，在 `SettingsRepository` 之後加：

```python
class AudioTagGateway(Protocol):
    def write_title(self, path: str, title: str) -> None:
        """把 title 寫入該音檔的標題標籤；無標籤時建立。失敗時 raise。"""
        ...
```

- [ ] **Step 3: 寫失敗測試** — 新建 `tests/test_tags.py`：

```python
"""MutagenTagGateway 的真實整合測試（用內建 ffmpeg 合成極短 mp3）。"""
from __future__ import annotations

import subprocess

import pytest

try:
    import imageio_ffmpeg
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # noqa: BLE001
    _FFMPEG = None

from musicbox.infrastructure.tags import MutagenTagGateway

pytestmark = pytest.mark.skipif(_FFMPEG is None, reason="需要 ffmpeg 合成測試用 mp3")


def _make_mp3(path: str) -> None:
    subprocess.run(
        [_FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "0.1", "-q:a", "9", "-y", path],
        check=True, capture_output=True,
    )


def _read_title(path: str):
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3
    audio = MP3(path, ID3=EasyID3)
    if audio.tags and "title" in audio:
        return audio["title"][0]
    return None


def test_write_title_creates_tag_when_absent(tmp_path):
    p = str(tmp_path / "song.mp3")
    _make_mp3(p)
    assert _read_title(p) is None            # 合成檔一開始沒有標題
    MutagenTagGateway().write_title(p, "01-告白氣球")
    assert _read_title(p) == "01-告白氣球"


def test_write_title_overwrites_existing(tmp_path):
    p = str(tmp_path / "song.mp3")
    _make_mp3(p)
    gw = MutagenTagGateway()
    gw.write_title(p, "舊標題")
    gw.write_title(p, "新標題")
    assert _read_title(p) == "新標題"
```

- [ ] **Step 4: 跑測試確認失敗**

Run: `uv run pytest tests/test_tags.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'musicbox.infrastructure.tags'`）

- [ ] **Step 5: 實作 `MutagenTagGateway`**

新建 `src/musicbox/infrastructure/tags.py`：

```python
"""AudioTagGateway 的 mutagen 實作：寫 MP3 的 ID3 標題（不重編碼音訊）。"""
from __future__ import annotations

from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3


class MutagenTagGateway:
    def write_title(self, path: str, title: str) -> None:
        audio = MP3(path, ID3=EasyID3)
        if audio.tags is None:
            audio.add_tags()
        audio["title"] = title
        audio.save()
```

- [ ] **Step 6: 跑測試確認通過**

Run: `uv run pytest tests/test_tags.py -v`
Expected: PASS（兩個測試；若環境無 ffmpeg 則 skip，但本專案有 imageio-ffmpeg 應會實跑）

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/musicbox/domain/ports.py src/musicbox/infrastructure/tags.py tests/test_tags.py
git commit -m "feat: AudioTagGateway port 與 mutagen ID3 標題寫入實作"
```

---

### Task 3: `ApplyRenamePlanUseCase` 支援寫標籤

**Files:**
- Modify: `src/musicbox/usecases/rename_songs.py`（`ApplyRenamePlanUseCase`）
- Modify: `tests/fakes.py`（新增 `FakeTagGateway`）
- Test: `tests/test_rename.py`（新增案例）

**Interfaces:**
- Consumes: `AudioTagGateway`（Task 2）、既有 `FileSystemGateway`、`build_undo_plan`。
- Produces:
  - `ApplyRenamePlanUseCase.__init__(gateway, tag_gateway=None)`
  - `ApplyRenamePlanUseCase.execute(plan, progress=None, write_tags=False) -> int`（回傳改名數）
  - `FakeTagGateway`（`.writes: list[tuple[str, str]]`）

- [ ] **Step 1: 新增 FakeTagGateway** — `tests/fakes.py` 末端加：

```python
class FakeTagGateway:
    """記錄 write_title 呼叫（path, title），不碰真實檔案。"""

    def __init__(self):
        self.writes: list[tuple[str, str]] = []

    def write_title(self, path: str, title: str) -> None:
        self.writes.append((path, title))
```

- [ ] **Step 2: 寫失敗測試** — 在 `tests/test_rename.py` 頂端 import 區加 `import os`（若尚無）與 `from .fakes import FakeGateway, FakeTagGateway`（把 FakeTagGateway 併入現有 import），然後在末端新增：

```python
def test_apply_writes_tags_for_all_items_when_enabled():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())      # 兩檔都會改名
    count = apply.execute(plan, write_tags=True)
    assert count == 2
    assert set(tags.writes) == {
        (os.path.join(FOLDER, "01-a.mp3"), "01-a"),
        (os.path.join(FOLDER, "02-b.mp3"), "02-b"),
    }


def test_apply_writes_tags_without_renaming():
    gw = FakeGateway({FOLDER: ["01-a.mp3", "02-b.mp3"]})   # 已是正確編號
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())
    count = apply.execute(plan, write_tags=True)
    assert count == 0
    assert gw.names(FOLDER) == {"01-a.mp3", "02-b.mp3"}    # 沒改名
    assert set(tags.writes) == {
        (os.path.join(FOLDER, "01-a.mp3"), "01-a"),
        (os.path.join(FOLDER, "02-b.mp3"), "02-b"),
    }


def test_apply_does_not_write_tags_when_disabled():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())
    apply.execute(plan, write_tags=False)
    assert tags.writes == []


def test_undo_writes_tags_with_old_names():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())
    apply.execute(plan, write_tags=True)
    tags.writes.clear()
    apply.execute(build_undo_plan(plan), write_tags=True)  # 復原
    assert gw.names(FOLDER) == {"06-b.mp3", "02-a.mp3"}
    assert set(tags.writes) == {
        (os.path.join(FOLDER, "06-b.mp3"), "06-b"),
        (os.path.join(FOLDER, "02-a.mp3"), "02-a"),
    }
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_rename.py::test_apply_writes_tags_for_all_items_when_enabled -v`
Expected: FAIL（`TypeError: __init__() takes 2 positional arguments but 3 were given`）

- [ ] **Step 4: 實作**

`src/musicbox/usecases/rename_songs.py`，把 `ApplyRenamePlanUseCase` 整個類別替換為：

```python
class ApplyRenamePlanUseCase:
    """實際套用改名，採兩階段避免與現有檔名衝突而覆蓋；可選一併寫入 ID3 標題。"""

    def __init__(self, gateway: FileSystemGateway, tag_gateway: AudioTagGateway | None = None):
        self._gateway = gateway
        self._tag_gateway = tag_gateway

    def execute(
        self,
        plan: RenamePlan,
        progress: ProgressCallback | None = None,
        write_tags: bool = False,
    ) -> int:
        cb: ProgressCallback = progress or (lambda frac, status: None)
        todo = [it for it in plan.items if it.changed]
        do_tags = write_tags and self._tag_gateway is not None

        if not todo and not do_tags:
            cb(1.0, "沒有需要改名的檔案")
            return 0

        total = 2 * len(todo) + (len(plan.items) if do_tags else 0)
        step = 0

        # 第一階段：全部改成獨一無二的暫存名
        temps: list[tuple[str, str]] = []
        for it in todo:
            tmp = f".__rename_tmp_{len(temps)}__{it.new_name}"
            self._gateway.rename(plan.folder, it.old_name, tmp)
            temps.append((tmp, it.new_name))
            step += 1
            cb(step / total, f"準備 {it.old_name}")

        # 第二階段：暫存名改成正式新名
        for tmp, new in temps:
            self._gateway.rename(plan.folder, tmp, new)
            step += 1
            cb(step / total, f"改名 {new}")

        # 第三階段：寫入 ID3 標題（清單每個檔，title = 新檔名去副檔名）
        if do_tags:
            for it in plan.items:
                title = os.path.splitext(it.new_name)[0]
                self._tag_gateway.write_title(os.path.join(plan.folder, it.new_name), title)
                step += 1
                cb(step / total, f"寫入標題 {it.new_name}")

        if do_tags:
            cb(1.0, f"完成，已改名 {len(todo)} 個檔案，已寫入 {len(plan.items)} 個標題")
        else:
            cb(1.0, f"完成，已改名 {len(todo)} 個檔案")
        return len(todo)
```

同時在檔案頂端的 import 區，把 `AudioTagGateway` 加入 domain ports 的匯入。找到：

```python
from ..domain.ports import FileSystemGateway, ProgressCallback
```
改成：

```python
from ..domain.ports import AudioTagGateway, FileSystemGateway, ProgressCallback
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_rename.py -v`
Expected: PASS（含既有 `test_apply_*` 與新四則；既有以單一參數建構 `ApplyRenamePlanUseCase(gw)` 的測試仍過，因 `tag_gateway` 預設 None）

- [ ] **Step 6: Commit**

```bash
git add src/musicbox/usecases/rename_songs.py tests/fakes.py tests/test_rename.py
git commit -m "feat: ApplyRenamePlanUseCase 支援套用/復原時寫入 ID3 標題"
```

---

### Task 4: 接線——composition 注入 gateway、worker 傳旗標

**Files:**
- Modify: `src/musicbox/composition.py`
- Modify: `src/musicbox/presentation/workers.py`
- 驗證：offscreen 建構 + 全套件

**Interfaces:**
- Consumes: `MutagenTagGateway`（Task 2）、`ApplyRenamePlanUseCase(gateway, tag_gateway)`（Task 3）。
- Produces: `RenameApplyWorker.__init__(usecase, plan, write_tags=False)`，`run()` 把 `write_tags` 傳入 `execute`。

- [ ] **Step 1: composition 注入 tag gateway**

`src/musicbox/composition.py`：import 區加：

```python
from .infrastructure.tags import MutagenTagGateway
```

找到：

```python
    apply_uc = ApplyRenamePlanUseCase(gateway)
```
改成：

```python
    apply_uc = ApplyRenamePlanUseCase(gateway, MutagenTagGateway())
```

- [ ] **Step 2: worker 傳遞 write_tags**

`src/musicbox/presentation/workers.py`，把 `RenameApplyWorker` 替換為：

```python
class RenameApplyWorker(QThread):
    progress = Signal(object, str)
    finished_ok = Signal(int)        # 實際改名數
    failed = Signal(str)

    def __init__(self, usecase: ApplyRenamePlanUseCase, plan: RenamePlan, write_tags: bool = False):
        super().__init__()
        self._usecase = usecase
        self._plan = plan
        self._write_tags = write_tags

    def run(self) -> None:
        try:
            count = self._usecase.execute(
                self._plan,
                lambda frac, status: self.progress.emit(frac, status),
                self._write_tags,
            )
            self.finished_ok.emit(count)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
```

- [ ] **Step 3: offscreen 建構煙霧測試**

Run:
```bash
QT_QPA_PLATFORM=offscreen uv run python -c "import sys; sys.path.insert(0,'src'); from PySide6.QtWidgets import QApplication; app=QApplication([]); from musicbox.composition import build_main_window; build_main_window(); print('constructed OK')"
```
Expected: 印出 `constructed OK`，無例外。

- [ ] **Step 4: 全套件**

Run: `uv run pytest`
Expected: PASS（無回歸）

- [ ] **Step 5: Commit**

```bash
git add src/musicbox/composition.py src/musicbox/presentation/workers.py
git commit -m "feat: 注入 MutagenTagGateway 並讓 RenameApplyWorker 傳遞 write_tags"
```

---

### Task 5: 重新編號頁加「同時寫入 ID3 標題」勾選框

**Files:**
- Modify: `src/musicbox/presentation/pages/rename_page.py`
- 驗證：offscreen 建構 + 啟用邏輯 headless 檢查 + 全套件

**Interfaces:**
- Consumes: `Settings.write_tags`（Task 1）、`RenameApplyWorker(usecase, plan, write_tags)`（Task 4）。
- Produces: 勾選框互動、抽出的 `_refresh_apply_enabled()`、apply/undo 傳旗標。

- [ ] **Step 1: 新增狀態欄位**

`__init__` 內，`self._undoing = False` 之後加：

```python
        self._applied_write_tags = False    # 記住上次套用時是否寫標籤，供復原沿用
        self._invalid_count = 0
        self._changed_count = 0
```

- [ ] **Step 2: 加勾選框**

`_build_ui` 內，在 `self.normalize_check` 建立與連接之後加：

```python
        self.write_tags_check = QCheckBox("同時寫入 ID3 標題")
        self.write_tags_check.toggled.connect(self._on_write_tags_toggled)
```

並在 `opt_row` 佈局中，`opt_row.addWidget(self.normalize_check)` 之後加：

```python
        opt_row.addWidget(self.normalize_check)
        opt_row.addSpacing(12)
        opt_row.addWidget(self.write_tags_check)
```

- [ ] **Step 3: 設定讀寫納入 write_tags**

`_apply_settings` 內，把 blockSignals 的 `widgets` 元組加入新勾選框，並設值。找到：

```python
        widgets = (self.dir_edit, self.sep_combo, self.mode_combo, self.pad_spin, self.normalize_check)
```
改成：

```python
        widgets = (self.dir_edit, self.sep_combo, self.mode_combo, self.pad_spin,
                   self.normalize_check, self.write_tags_check)
```

在 `self.normalize_check.setChecked(s.normalize)` 之後加：

```python
        self.write_tags_check.setChecked(s.write_tags)
```

`_persist` 內，`self._settings.normalize = ...` 之後加：

```python
        self._settings.write_tags = self.write_tags_check.isChecked()
```

- [ ] **Step 4: 抽出啟用邏輯，改 `_render` 尾段**

在 `_render` 方法尾段，把現有的「摘要與啟用」區塊：

```python
        changed = self._plan.changed_count if self._plan else 0
        if invalid:
            self.summary.setText(f"共 {len(items)} 個檔案；有 {invalid} 列名稱空白、重複或含非法字元，請修正後再套用。")
            self.apply_btn.setEnabled(False)
        else:
            self.summary.setText(f"共 {len(items)} 個檔案，其中 {changed} 個需要改名。")
            self.apply_btn.setEnabled(changed > 0)
```

替換為：

```python
        self._changed_count = self._plan.changed_count if self._plan else 0
        self._invalid_count = invalid
        if invalid:
            self.summary.setText(f"共 {len(items)} 個檔案；有 {invalid} 列名稱空白、重複或含非法字元，請修正後再套用。")
        else:
            self.summary.setText(f"共 {len(items)} 個檔案，其中 {self._changed_count} 個需要改名。")
        self._refresh_apply_enabled()
```

- [ ] **Step 5: 新增 `_refresh_apply_enabled` 與勾選框處理**

在 `_render` 之後加兩個方法：

```python
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
```

- [ ] **Step 6: `_apply` / `_undo` / `_run_worker` 傳旗標**

把這三個方法替換為：

```python
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
```

- [ ] **Step 7: offscreen 建構 + 啟用邏輯檢查**

Run:
```bash
cd "C:/Users/Aidan/Desktop/yt-downloader" && PYTHONUTF8=1 PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen uv run python - <<'PY'
import os, sys, tempfile
sys.path.insert(0, r"C:\Users\Aidan\Desktop\yt-downloader\src")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from musicbox.domain.entities import Settings
from musicbox.infrastructure.filesystem_renamer import FilesystemGateway
from musicbox.usecases.rename_songs import ApplyRenamePlanUseCase, BuildRenamePlanUseCase
from musicbox.presentation.pages.rename_page import RenamePage
folder = tempfile.mkdtemp()
for n in ["01-a.mp3", "02-b.mp3"]:  # 已是正確編號 → 無檔名要改
    open(os.path.join(folder, n), "w").close()
gw = FilesystemGateway()
page = RenamePage(BuildRenamePlanUseCase(gw), ApplyRenamePlanUseCase(gw), Settings(output_dir=folder, rename_folder=folder), lambda: None)
print("apply enabled (no changes, write_tags off):", page.apply_btn.isEnabled())
assert page.apply_btn.isEnabled() is False
page.write_tags_check.setChecked(True)   # 勾了寫標籤
print("apply enabled (no changes, write_tags on):", page.apply_btn.isEnabled())
assert page.apply_btn.isEnabled() is True
page.write_tags_check.setChecked(False)
assert page.apply_btn.isEnabled() is False
print("OK: write_tags 勾選使無變動時也能套用")
PY
```
Expected: 印出 `OK: write_tags 勾選使無變動時也能套用`，無 AssertionError。

- [ ] **Step 8: 全套件**

Run: `uv run pytest`
Expected: PASS（無回歸）

- [ ] **Step 9: Commit**

```bash
git add src/musicbox/presentation/pages/rename_page.py
git commit -m "feat: 重新編號頁加同時寫入 ID3 標題勾選框"
```

---

### Task 6: 更新 README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 補充說明**

`README.md` 的「### ＃ 重新編號」清單，於「**列表內編輯**」項之後加入：

```markdown
- **同時寫入 ID3 標題**：勾選後按「套用改名」時，把每個檔案的新檔名（去副檔名，例如 `01-告白氣球`）寫進該 MP3 的 ID3 標題標籤，讓播放器顯示正確名稱。即使沒有檔名要改也能單獨補標籤；「復原上次改名」會把標題一併改回舊檔名。
```

並在既有「## 簡繁轉換」小節之後，新增一節：

```markdown
## ID3 標題

「同時寫入 ID3 標題」用 [mutagen](https://mutagen.readthedocs.io/) 直接改寫標籤、不重編碼音訊。只寫「標題」一個欄位（不動演出者／專輯）。
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README 補充同時寫入 ID3 標題說明"
```

---

## Self-Review

**Spec coverage：**
- 檔名→ID3 標題、內容為新檔名去副檔名 → Task 3 `execute` 第三階段。✔
- 勾選框、設定記憶 → Task 1（Settings）＋ Task 5（勾選框、`_persist`）。✔
- 清單每個檔（含未改名）都寫 → Task 3 `for it in plan.items` + `test_apply_writes_tags_without_renaming`。✔
- 啟用條件（無變動也能按）→ Task 5 `_refresh_apply_enabled` + Step 7 headless 檢查。✔
- 復原也寫回舊檔名 → Task 3 同段邏輯 + `test_undo_writes_tags_with_old_names`；Task 5 `_applied_write_tags` 供復原沿用。✔
- Clean Architecture：port + mutagen 只在 infra → Task 2；usecase 依賴 port → Task 3。✔
- 新依賴 mutagen → Task 2。✔
- 真實標籤寫入驗證 → Task 2 `tests/test_tags.py`（ffmpeg 合成 mp3）。✔
- README → Task 6。✔

**Placeholder scan：** 無 TBD/TODO；每個改碼步驟均含完整程式碼與指令。✔

**Type consistency：** `AudioTagGateway.write_title(path, title)`（Task 2）由 `MutagenTagGateway`（Task 2）與 `FakeTagGateway`（Task 3）實作、`ApplyRenamePlanUseCase`（Task 3）消費、composition（Task 4）注入；`execute(plan, progress, write_tags)` 簽章在 Task 3 定義、Task 4 worker 呼叫、Task 5 UI 透過 worker 觸發；`Settings.write_tags`（Task 1）於 Task 5 讀寫；`_refresh_apply_enabled`/`_applied_write_tags`/`_invalid_count`/`_changed_count`（Task 5）宣告後於同任務各方法使用。✔
