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

同一個 repo 裡的第二個 app：給一個 YouTube 或**立法院 IVOD** 網址，用字幕加影片截圖產出**單一 HTML 檔的投影片摘要**。

```powershell
uv run slides.py
```

貼上網址 → 按 Enter（或「加入佇列」）→ 立刻開始。流程是：抓字幕 → 本機 LLM 切章節寫摘要 → 只下載那幾個時間點的影片片段 → 抽幀縮成 WebP → 全部 base64 內嵌成一個 HTML 檔。

## 前置需求

需要本機跑 [Ollama](https://ollama.com/)，並先下載模型：

```powershell
ollama pull qwen3.5:9b
```

模型可以在介面上換（下拉會列出 `ollama list` 的結果）。16 GB VRAM 建議用 `qwen3.5:9b`（6.6 GB）；27B 以上裝不下，會溢到 CPU 而慢到不能用。

## 立法院 IVOD

直接貼 `https://ivod.ly.gov.tw/Play/Clip/1M/<id>`（完整會議的 `/Play/Full/...` 也可以）。

- **逐字稿不用自己跑**：立法院已經用 WhisperX 產好帶時間戳的逐字稿，透過開放 API [`ly.govapi.tw`](https://ly.govapi.tw/v2/ivods) 取得，所以省掉最慢的語音辨識那一步
- **截圖**用 ffmpeg 直接從 HLS 串流切片段（實測每頁約 1 秒）
- 資料夾名稱是 `日期 委員－會議 [IVOD id]`，例如 `2026-08-27 洪毓祥－第11屆第5會期第23次會議 [171180]`
- 成品會標明「逐字稿由立法院 AI 自動產生，可能有辨識錯誤」——實測看得到錯字（「朝野黨壇協商」應為「黨團」），台語發言更差
- **畫質設定對 IVOD 無效**：API 只提供一個串流網址
- **完整會議**（8 小時以上）的影片主機實測連不上，會產出**沒有截圖**的摘要；而且一場 8 小時的會議壓成十幾頁本來就不是好的呈現單位——IVOD 的 Clip（一位委員的一段發言）才是自然的單位
- 還沒有 AI 逐字稿的片段會直接說明原因，不會退到語音辨識（那條路用的是 yt-dlp，它不認得 IVOD 網址）

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

## 佇列

等生成的時候看到別支影片，可以直接貼上按 Enter 排進去，跑完自動接著做下一支。

- 一次只跑一項；下面的清單顯示每一項的狀態（⏳ 等待、▶ 進行中、✅ 完成、❌ 失敗、⏹ 已取消）
- **雙擊完成的項目**就開啟它的 HTML
- 單獨一支失敗**不會**停下整排——使用者通常排完就去做別的事了；但**連續兩項失敗**會暫停佇列（那通常代表 Ollama 沒開或模型名稱打錯，繼續跑只會在幾秒內把整排燒掉）
- 失敗或取消的項目選起來按「**重試**」就放回佇列，不必重貼網址
- 按「取消」會停下整個佇列，剩下的維持等待，按「開始」繼續
- 同一支影片用 `youtu.be/X` 和 `watch?v=X` 兩種網址貼，只會排一次
- 生成中關掉視窗會先請它停下來再關（最多等 30 秒），不會留下暫存檔
- 生成中可以繼續排隊，但**設定會鎖住**（設定在每一項開始的當下讀取，中途改會讓正在跑的那一項行為不一致）

## 詳細內容

想知道影片講了什麼但不想看影片時，勾「詳細內容」：

- 每一頁在條列下方多一段 **150～300 字的完整敘述**，外語影片一律翻成繁體中文
- HTML 最後附上**完整逐字稿**（預設收合，帶 mm:ss 時間），保留字幕原文不翻譯——它是唯一無損的那一份，用來回頭查證或找影片位置

代價是生成時間較長（實測 9 分鐘的韓文影片約多一倍），HTML 檔也較大。

## 輸出

每支影片一個資料夾，名稱是 `影片標題 [影片id]`，例如 `발표잘하는법 따라해보세요! [NZkVcvVd2OQ]`。標題在前面才看得出是哪支影片；id 留在後面，同名影片不會互相覆蓋，同一支影片重跑則會寫回同一個資料夾。

## 設定

- **模型**：任何本機有的 Ollama 模型
- **畫質**：最高／1080p／720p／480p——只影響截圖來源畫質，因為只下載片段，1080p 的下載量也只有數十 MB
- **頁數**：下限與上限，實際頁數由模型依內容決定
- **詳細內容**：見上一節

設定記在專案根目錄的 `slidebox_settings.json`。語音辨識模型只能在設定檔改（`whisper_model`，例如改成 `small` 約快兩成但日文明顯較差），改完需重開 app。

## 已知限制

- 背景音樂很重、多人同時說話的影片，語音辨識品質會明顯下降；**歌詞會被當成內容**摘要進投影片
- 佇列**不會保存**：關掉 app 之後要重新貼
- 只處理單一影片，忽略播放清單
- 部分截圖失敗時仍會出片，缺圖的那幾頁會標示出來

## 架構

與 `musicbox` 平行的獨立 app，同樣的 Clean Architecture 分層：

```
slides.py                    進入點
src/slidebox/
├─ domain/                   實體與 port
├─ usecases/
│  ├─ chapters.py            純邏輯：挑軌、VTT 解析、壓縮、逐字稿、驗證
│  ├─ naming.py              純邏輯：輸出資料夾命名
│  ├─ queue.py               純邏輯：生成佇列
│  └─ build_deck.py          pipeline 編排
├─ infrastructure/           yt-dlp／ffmpeg／Ollama／faster-whisper／HTML 的實作
├─ presentation/             PySide6 UI
└─ composition.py            composition root
```

設計文件：
- `docs/superpowers/specs/2026-09-07-slidebox-youtube-slide-summary-design.md`（整體）
- `docs/superpowers/specs/2026-09-12-slidebox-speech-transcription-design.md`（語音辨識，含所有裁定與實測數據）
- `docs/superpowers/specs/2026-09-12-slidebox-usability-design.md`（命名、佇列、詳細內容）
- `docs/superpowers/specs/2026-09-12-slidebox-ivod-design.md`（立法院 IVOD）

---

# 立法院質詢每日新聞

在 slidebox 之上的三段式服務：每天把立法院的質詢片段變成可讀的新聞頁。

```
GPU 主機（這台）                         Raspberry Pi
┌────────────────────────┐              ┌──────────────────────────────┐
│ serve_api.py           │  ◄── HTTP ── │ services/news  Django+ninja  │
│  FastAPI，包住 slidebox │              │  每日排程 → 呼叫 GPU → 存文章 │
│  Ollama / ffmpeg 在這  │              │  開 news API                 │
└────────────────────────┘              │ web/news       Astro 前端    │
                                        └──────────────────────────────┘
```

Pi 上不跑任何模型、也不裝 slidebox——兩邊只透過 HTTP 說話。

## 1. 摘要 API（GPU 主機）

```powershell
uv run --group api serve_api.py
```

吃一個 YouTube 或 IVOD 網址，非同步產出結構化的摘要材料（含 base64 截圖）。一支影片要幾分鐘，所以是工作佇列而不是同步呼叫：

| 端點 | 說明 |
|---|---|
| `GET /health` | Ollama 是否連得上、目前忙不忙 |
| `POST /jobs` | `{url, detailed, min_slides, max_slides}` → `202 {id}` |
| `GET /jobs/{id}` | 進度與結果 |
| `DELETE /jobs/{id}` | 取消 |

環境變數見 `serve_api.py` 的 docstring。設了 `SLIDEBOX_API_KEY` 就會強制 `X-API-Key`。

**要讓 Pi 連得到這個服務，兩件事都要做**（預設只綁 loopback，所以預設狀態下 Pi 是連不到的）：

```powershell
$env:SLIDEBOX_API_HOST = "0.0.0.0"          # 預設 127.0.0.1
uv run --group api serve_api.py

# 另開一個「以系統管理員身分」的視窗，放行入站連線（只開給私人網路）
New-NetFirewallRule -DisplayName "SlideBox API" -Direction Inbound -LocalPort 8800 `
  -Protocol TCP -Action Allow -Profile Private
```

綁在非 loopback 位址又沒設 `SLIDEBOX_API_KEY` 時，啟動會印一行警告——任何連得到這個埠的人都能佔用你的 GPU。

## 2. 後端（Pi）

`services/news/` — Django + django-ninja。每天凌晨抓前一天的質詢片段、送去 GPU 主機產生**詳細模式**的摘要、存成文章，並開出 news API。見 `services/news/README.md`。

## 3. 前端（Pi）

`web/news/` — Astro。讀 news API，呈現新聞頁。見 `web/news/README.md`。

## 用 Docker 部署 Pi 這一側

後端、每日排程、前端與 Cloudflare Tunnel 包成一份 `compose.yaml`。GPU 主機上的摘要 API 不在裡面——它要直接用 Windows 上的 Ollama 與顯示卡，維持原生執行。

### Portainer

Stacks → Add stack → **Repository**：

| 欄位 | 值 |
|---|---|
| Repository URL | `https://github.com/Aidan79225/video-summary-maker` |
| Repository reference | `refs/heads/master` |
| Compose path | `compose.yaml` |
| Environment variables | 照 `.env.docker.example` 填（Advanced mode 可以整份貼上） |

按 Deploy 會直接在 Pi 上建映像（第一次約十幾分鐘）。之後更新程式：stack 頁面 → **Pull and redeploy**；或打開 GitOps updates 讓它定時自己拉。

管理指令用容器的 Console（Containers → `api` → Console → `/bin/sh`）：

```bash
python manage.py ingest_ivod --date 2026-08-27 --limit 3   # 手動匯入
python manage.py createsuperuser                           # 後台帳號
```

### 命令列

```bash
cp .env.docker.example .env
docker compose up -d --build
docker compose exec api python manage.py ingest_ivod --date 2026-08-27 --limit 3
docker compose logs -f scheduler
```

- 資料庫與截圖放在 `DATA_DIR`（Pi 的磁碟陣列，`appdata/ly-news`），第一次要先 `sudo mkdir -p` 並 `chown -R 1000:1000`；容器以 uid 1000 執行，刪 stack 也不會動到它
- 建置走 `network: host`：Pi5 上容器走 bridge 連國外套件庫很慢（和 nextcloud 那個 stack 一樣的問題）
- 映像的基底都是多架構的，同一份檔案在 amd64 與 Raspberry Pi（arm64）都能建
- Tunnel 用 Cloudflare 後台建立的 token 模式，轉送規則在後台設：`media/*` → `http://api:8000`，其餘 → `http://web:4321`。`/api` 與 `/admin` 不對外。只在區網、不對外的話把 `cloudflared` 那段從 compose 刪掉

設計文件：`docs/superpowers/specs/2026-09-12-news-service-design.md`
