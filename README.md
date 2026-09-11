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
- **正規化歌名**：勾選後自動清掉歌名雜訊——移除 `(Official MV)`／`【MV】`／`[Official Audio]` 等已知雜訊括號、全形轉半形、簡體轉繁體（台灣慣用詞）、移除非法檔名字元、整理多餘空白。`(Live)`、`(Remix)`、`feat.` 等有意義內容保留。
- **列表內編輯**：直接在「歌名」欄打字改成想要的名稱，流水號仍由程式自動管理；手改內容在調設定或排序時保留。若歌名重複或含非法字元，該列標紅並暫停「套用改名」。
- **同時寫入 ID3 標題**：勾選後按「套用改名」時，把每個檔案的新檔名（去副檔名，例如 `01-告白氣球`）寫進該 MP3 的 ID3 標題標籤，讓播放器顯示正確名稱。即使沒有檔名要改也能單獨補標籤；「復原上次改名」會把標題一併改回舊檔名。
- **手動排序**：選一列用「▲ 上移／▼ 下移」調整順序，流水號依新順序重算
- **試聽**：選一列按「▶ 試聽選取」播放，再按一次停止
- **復原**：套用後可用「復原上次改名」把檔名改回去

按「套用改名」才實際改名（兩階段安全改名，不會覆蓋既有檔案）。

設定（資料夾、格式、畫質、分隔符…）會自動記憶在專案根目錄的 `settings.json`，下次開啟沿用。

## 簡繁轉換

「正規化歌名」的簡體轉繁體用 [OpenCC](https://github.com/BYVoid/OpenCC)（`opencc-python-reimplemented`，純 Python）。若該套件未安裝，正規化的其他規則照常運作，僅略過簡繁轉換。

## ID3 標題

「同時寫入 ID3 標題」用 [mutagen](https://mutagen.readthedocs.io/) 直接改寫標籤、不重編碼音訊。只寫「標題」一個欄位（不動演出者／專輯）。

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

---

# SlideBox

同一個 repo 裡的第二個 app：給一個 YouTube 網址，用字幕加影片截圖產出**單一 HTML 檔的投影片摘要**。

```powershell
uv run slides.py
```

貼上網址 → 選模型 → 按「生成摘要」。流程是：抓字幕 → 本機 LLM 切章節寫摘要 → 只下載那幾個時間點的影片片段 → 抽幀縮成 WebP → 全部 base64 內嵌成一個 HTML 檔。

## 前置需求

需要本機跑 [Ollama](https://ollama.com/)，並先下載模型：

```powershell
ollama pull qwen3.5:9b
```

模型可以在介面上換（下拉會列出 `ollama list` 的結果）。16 GB VRAM 建議用 `qwen3.5:9b`（6.6 GB）；27B 以上裝不下，會溢到 CPU 而慢到不能用。

## 外語影片

英文、日文、韓文影片一律產出**繁體中文**投影片。字幕依下列順序挑選：

1. **人工字幕**，依偏好：繁中 → 簡中 → 英文。人工翻譯品質最好
2. **影片原始語言的自動字幕**（例如日文影片的 `ja-orig`），交給模型邊摘要邊翻譯
3. YouTube 的**機器翻譯字幕**，最後手段

第 2 層是關鍵：YouTube 提供的「自動字幕中文」其實是對原文語音辨識結果再做機器翻譯，辨識錯誤加翻譯錯誤兩層損失。直接給模型原文，它帶著全文脈絡翻譯，品質明顯較好。實測日文、韓文影片產出的標題裡沒有任何假名或韓文字。

內容來源會寫在成品的標題區與完成訊息裡，例如「使用 YouTube 自動字幕，品質可能較差」。

## 沒有字幕的影片（語音辨識）

影片完全沒有字幕時，會自動改用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 從語音產生字幕，其後流程不變。成品會註明「由語音辨識產生，可能有辨識錯誤」。

- **只用 CPU**（本機沒有 CUDA，且 RTX 5060 Ti 的 Blackwell 架構支援不確定）。預設模型 `large-v3-turbo`，實測約 **4 倍即時**：10 分鐘影片約 2.5 分鐘，1 小時約 15 分鐘
- **首次使用**會從 Hugging Face 下載約 1.6 GB 的模型，之後離線可用；每次開 app 後第一次辨識要載入 12～40 秒
- 模型載入與「解碼整個音訊檔」這兩段**無法中斷**，按取消會在該步驟完成後才停止
- 純音樂、動畫等沒有人聲的影片會回報「沒有偵測到語音」
- **字幕存在但暫時下載失敗**（YouTube 限流 HTTP 429）時**不會**改用語音——會請你過幾分鐘再試，因為人工字幕永遠比語音辨識好

實測同一支日文影片，語音辨識版與字幕版的投影片主題、順序一致；日文辨識的詞錯不影響摘要。

## 設定

- **模型**：任何本機有的 Ollama 模型
- **畫質**：最高／1080p／720p／480p——只影響截圖來源畫質，因為只下載片段，1080p 的下載量也只有數十 MB
- **頁數**：下限與上限，實際頁數由模型依內容決定

設定記在專案根目錄的 `slidebox_settings.json`。語音辨識模型只能在設定檔改（`whisper_model`，例如改成 `small` 約快兩成但日文明顯較差），改完需重開 app。

## 已知限制

- 背景音樂很重、多人同時說話的影片，語音辨識品質會明顯下降；**歌詞會被當成內容**摘要進投影片
- 只處理單一影片，忽略播放清單
- 部分截圖失敗時仍會出片，缺圖的那幾頁會標示出來

## 架構

與 `musicbox` 平行的獨立 app，同樣的 Clean Architecture 分層：

```
slides.py                    進入點
src/slidebox/
├─ domain/                   實體與 port
├─ usecases/
│  ├─ chapters.py            純邏輯：挑軌、VTT 解析、壓縮、驗證
│  └─ build_deck.py          pipeline 編排
├─ infrastructure/           yt-dlp／ffmpeg／Ollama／faster-whisper／HTML 的實作
├─ presentation/             PySide6 UI
└─ composition.py            composition root
```

設計文件：
- `docs/superpowers/specs/2026-09-07-slidebox-youtube-slide-summary-design.md`（整體）
- `docs/superpowers/specs/2026-09-12-slidebox-speech-transcription-design.md`（語音辨識，含所有裁定與實測數據）
