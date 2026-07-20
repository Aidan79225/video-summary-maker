# 改名時寫入 ID3 標題

日期：2026-07-21

## 目標

在「重新編號」頁加一個「同時寫入 ID3 標題」勾選框。勾選後按「套用改名」時，把每個檔案的新檔名（去副檔名）寫進該 MP3 的 ID3 標題標籤（TIT2），讓播放器（手機／車機／Windows）顯示正確名稱。

## 使用者決定（來自 brainstorming）

- 方向：**檔名 → 寫進 ID3 標題**（不是從標籤讀出來）。
- 寫入內容：**完整新檔名去副檔名**，例如 `01-告白氣球.mp3` → 標題寫 `01-告白氣球`。
- 觸發：**勾選框**，套用改名時一併寫；設定會記憶。
- 啟用條件：勾了「寫入 ID3 標題」時，**即使沒有檔名要改也能按「套用改名」**，單純補標籤。
- 復原：**復原時也把標題改回對應舊檔名**。

## 設計

### 標籤內容

每個檔案的 ID3 標題 = `os.path.splitext(new_name)[0]`。對「未改名」的檔案，`new_name == old_name`，標題即其現有檔名去副檔名。

復原是一份「新檔名改回舊檔名」的改名計畫，套用同一套「標題＝該計畫 new_name 去副檔名」邏輯時，寫出來的正好是**舊檔名**，因此改名與復原共用同一段程式。

### 架構（延續 Clean Architecture）

**domain（`ports.py`）** — 新增 port：

```python
class AudioTagGateway(Protocol):
    def write_title(self, path: str, title: str) -> None:
        """把 title 寫入該音檔的標題標籤；無標籤時建立。失敗時 raise。"""
        ...
```

**infrastructure（新檔 `tags.py`）** — `MutagenTagGateway` 用 `mutagen` 實作 `write_title`：

```python
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

（`mutagen` 直接改標籤、不重編碼音訊；檔案無 ID3 header 時 `add_tags()` 會建立。）

**usecases（`ApplyRenamePlanUseCase`）** — 注入選用的 tag gateway，`execute` 加 `write_tags` 旗標：

```python
class ApplyRenamePlanUseCase:
    def __init__(self, gateway: FileSystemGateway, tag_gateway: AudioTagGateway | None = None):
        ...

    def execute(self, plan, progress=None, write_tags=False) -> int:
        # 1. 兩階段改名（只處理 changed 的檔）——維持現狀
        # 2. 若 write_tags 且有 tag_gateway：對 plan.items 每個檔寫入
        #    title = splitext(it.new_name)[0]，路徑 join(folder, it.new_name)
        # 回傳實際改名數（renamed count）
```

- `todo`（changed）為空但 `write_tags` 為真時：跳過改名、只寫標籤、回傳 0。
- `todo` 為空且 `write_tags` 為假時：維持原本「沒有需要改名的檔案」行為。
- 進度計算納入寫標籤階段（`2*len(todo) + len(plan.items)` 步）。

**composition** — `ApplyRenamePlanUseCase(gateway, MutagenTagGateway())`。

**presentation（`workers.py`）** — `RenameApplyWorker.__init__(usecase, plan, write_tags=False)`，`run()` 把 `write_tags` 傳給 `execute`。

**presentation（`rename_page.py`）**
- 在「正規化歌名」旁加勾選框 `write_tags_check`「同時寫入 ID3 標題」。
- `_apply_settings` 的 blockSignals 群組加入此勾選框；`_persist` 寫入 `settings.write_tags`。
- 勾選框 toggled → `_refresh_apply_enabled()` + `_persist()`（只更新啟用狀態，不重建表格）。
- 啟用條件抽成 `_refresh_apply_enabled()`：無無效列，且 `changed_count > 0 or (write_tags and 有檔案)`；`_render()` 算完 `_invalid_count`／`_changed_count` 後呼叫它。
- `_apply`：`write_tags = write_tags_check.isChecked()`；`changed_count == 0 and not write_tags` 才 return；記 `self._applied_write_tags = write_tags`；`_run_worker(plan, write_tags, "改名中…")`。
- `_undo`：`_run_worker(undo_plan, self._applied_write_tags, "復原中…")` — 用當初套用時的旗標。
- `_run_worker(plan, write_tags, busy_text)` 建立帶 `write_tags` 的 worker。

**domain（`entities.py`）／`settings_repository.py`** — `Settings` 加 `write_tags: bool = False`，讀寫加該欄位。

### 新增依賴

`mutagen`（純 Python，Windows 免編譯）。加入 `pyproject.toml`、`uv lock` / `uv sync`。

## 測試

- **use case（假物件）**：`FakeTagGateway` 記錄 `write_title` 呼叫。
  - `write_tags=True`：清單每個檔（含未改名）都以 `title = 新檔名去副檔名` 被寫入一次；回傳改名數正確。
  - `write_tags=True` 且無檔名變動：不改名、標籤照寫、回傳 0。
  - `write_tags=False`：gateway 完全未被呼叫。
  - 套用→復原（帶旗標）：復原時各檔以**舊檔名去副檔名**被寫入。
- **`MutagenTagGateway`（真實整合）**：用內建 ffmpeg（`imageio-ffmpeg`）合成一個極短靜音 mp3，`write_title` 後用 mutagen 讀回，斷言標題正確；對「原本無標籤」與「已有標籤」兩種檔各驗一次。放獨立測試檔，ffmpeg 不可用時 skip。

## 不變的部分

正規化、列表內編輯、三欄表格、手動排序、試聽、兩階段安全改名的既有行為皆不變。改名與復原的檔名邏輯不動，只在其後多一個寫標籤步驟。

## 已知範圍界線

- 只寫**標題**一個標籤；不動 artist／album／track 等其他欄位。
- 只處理 `.mp3`（沿用現有副檔名過濾）。
- 若某檔寫標籤失敗（檔案損毀等），該次套用會 raise 並在狀態列顯示錯誤（與現有失敗處理一致）；已完成的改名不會回滾。

## 檔案異動一覽

| 檔案 | 異動 |
|------|------|
| `src/musicbox/domain/ports.py` | 新增 `AudioTagGateway` port |
| `src/musicbox/domain/entities.py` | `Settings` 加 `write_tags` |
| `src/musicbox/infrastructure/tags.py` | 新檔：`MutagenTagGateway` |
| `src/musicbox/infrastructure/settings_repository.py` | 讀寫 `write_tags` |
| `src/musicbox/usecases/rename_songs.py` | `ApplyRenamePlanUseCase` 注入 tag gateway、`execute` 加 `write_tags` |
| `src/musicbox/presentation/workers.py` | `RenameApplyWorker` 加 `write_tags` |
| `src/musicbox/presentation/pages/rename_page.py` | 勾選框、啟用條件、apply/undo 傳旗標 |
| `src/musicbox/composition.py` | 注入 `MutagenTagGateway` |
| `pyproject.toml` / `uv.lock` | 加 `mutagen` |
| `tests/fakes.py`、`tests/test_rename.py`、`tests/test_tags.py` | 測試 |
| `README.md` | 補充說明 |
