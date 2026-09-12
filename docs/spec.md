# MusicBox 桌面應用 — 設計規格

PySide6 桌面 app，Clean Architecture，整合兩個功能：YouTube 下載、歌曲重新編號。

## 分層

依賴方向由外往內；`infrastructure` 實作 `domain` 定義的介面（port）。核心邏輯不依賴 PySide，可獨立測試。

- **domain** — 實體與介面，無外部相依
  - `entities.py`：`DownloadFormat`、`DownloadRequest`、`RenameMode`、`RenameOptions`、`RenameItem`、`RenamePlan`
  - `ports.py`：`VideoDownloader`、`FileSystemGateway`、`ProgressCallback`
- **usecases** — 應用邏輯
  - `download_video.py`：`DownloadVideoUseCase`
  - `rename_songs.py`：`BuildRenamePlanUseCase`、`ApplyRenamePlanUseCase`（純邏輯，移植自舊 `rename_songs.py`）
- **infrastructure** — 介面的實作（IO / 框架）
  - `ffmpeg.py`：imageio-ffmpeg 路徑處理
  - `ytdlp_downloader.py`：`VideoDownloader` 的 yt-dlp 實作
  - `filesystem_renamer.py`：`FileSystemGateway` 的檔案系統實作
- **presentation** — PySide6 UI
  - `main_window.py`：側邊選單 + `QStackedWidget`
  - `pages/download_page.py`、`pages/rename_page.py`
  - `workers.py`：`QThread` worker，把 use case 跑在背景執行緒，用 signal 回報進度
- **composition.py** — 組裝所有相依（composition root）

## 功能

### 下載
網址輸入＋貼上、格式 radio（MP4／MP3）、儲存資料夾（預設 `音樂 - test`）、進度條、狀態。下載跑背景執行緒，yt-dlp `progress_hooks` → Qt signal。

### 重新編號
選資料夾 → 自動預覽表格（舊 → 新）。UI 可調：分隔符（`-`／空格／`.`）、模式（連續重編／保留原號統一格式）、補零位數。改設定即時更新預覽；按「套用」才實際兩階段改名。

## 執行
`uv run main.py` 開視窗。相依：`pyside6`、`yt-dlp`、`imageio-ffmpeg`。
