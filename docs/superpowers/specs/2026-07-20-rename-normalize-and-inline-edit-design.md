# 重新編號頁：歌名正規化 + 列表內編輯

日期：2026-07-20

## 目標

替「重新編號」頁的改名功能加兩項能力：

1. **正規化歌曲名稱** — 一鍵清掉 YouTube 下載常見的雜訊（雜訊括號、全形字、簡體字、非法檔名字元、多餘空白）。
2. **列表內直接編輯歌名** — 在表格裡直接打字，把某首歌的名稱改成想要的字串；流水號仍由程式自動管理。

兩者可並存：正規化提供好的起點，手動編輯做最後微調。

## 使用者決定（來自 brainstorming）

- 正規化為**可開關的選項**（總開關一個勾選框），非永遠自動。
- 括號移除**只清已知雜訊**關鍵字；`(Live)`、`(Remix)`、`feat.` 等有意義內容保留。
- 列表**只編輯歌名部分**，流水號段由程式自動管理。
- 手改內容在調設定（分隔符／補零）或上下移排序時**保留**，不被重算蓋掉。

## 設計

### 表格改為三欄

`舊檔名 | 歌名(可編輯) | 新檔名(唯讀預覽)`

- 「歌名」欄可編輯（`QLineEdit` 內嵌，或該欄 cell 開啟編輯）。
- 「新檔名」欄唯讀，即時顯示 `{號碼}{分隔符}{歌名}{副檔名}`，例如 `01-告白氣球.mp3`。
- 使用者永遠碰不到號碼段，因此排序／補零重算不會打壞手改。

### `RenamePage` 狀態與資料流

新增狀態：

- `self._titles: dict[str, str]` — key = `old_name`，value = 該檔目前的「歌名」字串。
- `self._manual: set[str]` — 被使用者手動編輯過的 `old_name` 集合。
- `self._loading: bool` — 程式化更新表格時設 True，避免 `itemChanged` 遞迴。

歌名來源規則：

- 自動歌名 = `derive_title(old_name, normalize_on)`。
- 檔案在 `_manual` 內 → 用 `_titles[old_name]`（手改值），重算時不覆蓋。
- 檔案不在 `_manual` → 用自動歌名。

各操作對狀態的影響：

| 操作 | 號碼 | 歌名 |
|------|------|------|
| 選資料夾（reload） | 重讀重排 | 全部重設為自動歌名，清空 `_manual` |
| 改分隔符／補零 | 重算 | 不動 |
| 上下移排序 | 依新順序重算 | 不動 |
| 切換正規化開關 | 不變 | 只重算「非手改」檔案；手改保留 |
| 在歌名欄打字 | 不變 | 更新 `_titles`，加入 `_manual`，重組該列預覽 |
| 把某格歌名清空 | 不變 | 還原為自動歌名，移出 `_manual` |

### 核心邏輯（`usecases/rename_songs.py`，純函式）

- 保留 `clean_title(stem)`：去掉開頭舊號碼與分隔符。
- 新增 `normalize_title(title: str) -> str`，依序套用：
  1. **全形轉半形**：U+FF01–FF5E → U+21–7E，全形空格 U+3000 → 半形空格。
  2. **移除已知雜訊括號**：`()`、`[]`、`【】`、`{}` 群組，內容（去空白、小寫後）比對關鍵字清單才整段移除。
  3. **簡轉繁**：OpenCC `s2twp`；OpenCC 無法載入時跳過本步，其餘照做。
  4. **移除非法檔名字元**：`\ / : * ? " < > |` 與控制字元 (U+00–U+1F)。
  5. **整理空白**：底線 `_` 轉空格、連續空白併為一個、頭尾去空白。
- 新增 `derive_title(old_name: str, normalize: bool) -> str`：
  取 `clean_title(splitext(old_name)[0])`，若 `normalize` 為真再套 `normalize_title`。
- `BuildRenamePlanUseCase` 新增 `plan_from_titles(folder, ordered: Sequence[tuple[str, str]], options) -> RenamePlan`：
  以 `(old_name, title)` 直接組新檔名，號碼沿用現有 `_number_for` 邏輯，副檔名取自 `old_name`。不再於內部呼叫 `clean_title`。
  - 既有 `execute()` / `plan_from_order()` 保留供初次讀取排序使用；presentation 之後改走 `plan_from_titles`。

### 已知雜訊關鍵字清單

集中在模組頂端一個常數，便於調整。初版（大小寫不敏感）：

```
official, official video, official music video, official audio,
mv, m/v, music video, lyric, lyrics, lyric video, audio,
hd, hq, 4k, 高畫質, 中文字幕, 歌詞, 動態歌詞
```

**保留**（不在清單，故不移除）：`live`、`remix`、`feat.`、`cover`、`acoustic` 等。

### 安全防護（手動編輯易撞名）

套用前驗證，任一不通過即停用「套用改名」並於狀態列說明、對問題列標紅：

- **撞名**：兩列以上產生相同新檔名。
- **非法字元**：手改歌名含 `\ / : * ? " < > |` 或控制字元。

（正規化開關關閉、或手改後未觸發正規化時，非法字元可能殘留，故此驗證獨立於正規化存在。）

### 設定持久化

- `RenameOptions` 增 `normalize: bool = False`。
- `Settings` 增 `normalize: bool = False`。
- `settings_repository.py` 的 `load` / `save` 增讀寫 `normalize` 欄位。
- `RenamePage` 的 `_apply_settings` / `_persist` / `_current_options` 一併處理。

### 相依套件

- 新增 `opencc-python-reimplemented`（純 Python，Windows 免編譯），加入 `pyproject.toml`，`uv lock` / `uv sync`。
- 匯入採防禦式：載入或初始化失敗時，`normalize_title` 的簡轉繁步驟靜默略過，其餘規則照常；狀態列提示一次「簡繁轉換未啟用」。

## 不變的部分

側邊選單、下載頁、兩階段安全改名、復原（`build_undo_plan`）、試聽、進度條、背景執行緒（`RenameApplyWorker`）皆維持原樣。

## 測試（pytest，沿用記憶體假物件風格）

- `normalize_title`：每條規則各一組案例；含 OpenCC 缺席時的降級（簡體字原樣通過、其餘規則仍生效）。
- `derive_title`：normalize 開/關兩種結果。
- `plan_from_titles`：手改歌名組出正確號碼、分隔符、副檔名；RENUMBER 與 KEEP 兩模式。
- 撞名偵測與非法字元偵測的判定函式。

## 檔案異動一覽

| 檔案 | 異動 |
|------|------|
| `src/musicbox/domain/entities.py` | `RenameOptions`、`Settings` 加 `normalize` |
| `src/musicbox/usecases/rename_songs.py` | 加 `normalize_title`、`derive_title`、`plan_from_titles`、關鍵字常數 |
| `src/musicbox/presentation/pages/rename_page.py` | 三欄表格、歌名編輯、狀態機、正規化勾選框、撞名/非法字元驗證 |
| `src/musicbox/infrastructure/settings_repository.py` | 讀寫 `normalize` |
| `pyproject.toml` / `uv.lock` | 加 `opencc-python-reimplemented` |
| `tests/` | 新增上述測試 |
| `README.md` | 補充正規化與列表編輯說明 |
