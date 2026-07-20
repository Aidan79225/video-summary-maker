# MusicBox

PySide6 桌面 app，整合兩個功能：**YouTube 下載**、**歌曲重新編號**。以 Clean Architecture 分層，核心邏輯不依賴 UI。

用 [uv](https://docs.astral.sh/uv/) 管理。`yt-dlp` 負責下載、`imageio-ffmpeg` 內建 ffmpeg（不用另裝系統版）、`pyside6` 做介面。

## 執行

```powershell
cd C:\Users\Aidan\Desktop\yt-downloader
uv run main.py
```

左側選單切換兩個功能：

### ⬇ 下載
貼上 YouTube 網址 → 選格式（影片 MP4／音樂 MP3）→ 選畫質 → 選儲存資料夾 → 開始下載。
- **畫質**：最高／1080p／720p／480p（僅 MP4，選 MP3 時停用）
- **取消**：下載中「開始下載」會變成「取消」，可隨時中止
- 下載跑背景執行緒，進度條即時更新，不會卡住視窗。只下單一影片（忽略播放清單）。

### ＃ 重新編號
選資料夾 → 自動預覽「舊檔名 → 新檔名」表格。可調設定即時更新預覽：
- **分隔符**：減號 `-` ／空格 ／點 `.`
- **模式**：連續重編（填補空缺）／保留原號（統一格式）
- **補零位數**
- **手動排序**：選一列用「▲ 上移／▼ 下移」調整順序，流水號依新順序重算
- **試聽**：選一列按「▶ 試聽選取」播放，再按一次停止
- **復原**：套用後可用「復原上次改名」把檔名改回去

按「套用改名」才實際改名（兩階段安全改名，不會覆蓋既有檔案）。

設定（資料夾、格式、畫質、分隔符…）會自動記憶在專案根目錄的 `settings.json`，下次開啟沿用。

## 預設資料夾

下載與重新編號預設指向 `C:\Users\Aidan\Desktop\音樂 - test`。
要改預設值，編輯 `src/musicbox/composition.py` 的 `DEFAULT_MUSIC_DIR`。

## 架構

```
main.py                      進入點
src/musicbox/
├─ domain/                   實體與介面（無外部相依）
│  ├─ entities.py
│  └─ ports.py
├─ usecases/                 應用邏輯
│  ├─ download_video.py
│  └─ rename_songs.py        （純邏輯，移植自舊 rename_songs.py）
├─ infrastructure/           介面實作（IO／框架）
│  ├─ ffmpeg.py
│  ├─ ytdlp_downloader.py
│  └─ filesystem_renamer.py
├─ presentation/             PySide6 UI
│  ├─ main_window.py         側邊選單 + QStackedWidget
│  ├─ workers.py             QThread 背景執行緒
│  └─ pages/{download,rename}_page.py
└─ composition.py            組裝所有相依（composition root）
```

依賴方向由外往內：`presentation → usecases → domain`；`infrastructure` 實作 `domain` 定義的 port。核心邏輯可不開視窗獨立測試（設 `QT_QPA_PLATFORM=offscreen` 也能建構 UI）。

## 測試

核心邏輯（重新編號、下載委派、設定）用 pytest，以記憶體假物件測試，不碰真實磁碟／網路：

```powershell
uv run pytest
```

## 更新 yt-dlp

YouTube 偶爾改版，下載失敗時先更新：

```powershell
uv lock --upgrade-package yt-dlp
uv sync
```
