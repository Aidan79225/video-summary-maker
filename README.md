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
uv sync --inexact   # --inexact：只更新，不移除其他群組裝的套件（例如語音辨識用顯示卡的 cuBLAS）
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

## 臺中市議會

貼「議員個人質詢隨選視訊系統」的片段網址：`https://vod.tccc.gov.tw/index.asp?url=12&cno=<議員>&ano=<片段>`（進到某位議員的頁面、點某一段，網址列就是這個）。

- **沒有逐字稿可抓**：議事錄系統（yishi.tccc.gov.tw）有依發言人拆的正式紀錄，但沒有時間戳，對不上影片。所以這條路走語音辨識——用 ffmpeg 把 HLS 串流的音訊抓成 16 kHz wav，交給 faster-whisper（`large-v3-turbo`）
- **片段長**：市政總質詢一段約 50 分鐘、業務質詢約 15 分鐘（頁面上的「影片長度 00:50」是 HH:MM）。GPU 主機的容器用顯示卡跑語音辨識（`SLIDEBOX_WHISPER_DEVICE=auto`，映像裝了 cuBLAS／cuDNN，因此映像從 4 GB 長到 12 GB），3 分鐘音訊實測 16 秒含載入模型；退回 CPU 時約為片長的四分之一，50 分鐘的片段要 12 分鐘
- 截圖與 IVOD 同一套：ffmpeg 直接切 m3u8，串流網址從播放器頁（rds.ginnet.cloud）解析出來
- 成品會標明「由語音辨識產生，可能有辨識錯誤」；人名偶有同音錯字（「楊啟邦」應為「楊啓邦」），標題裡的姓名以網站為準
- 網站的憑證鏈在 Python 3.13 預設的嚴格驗證下會被拒絕（CA 憑證缺 Subject Key Identifier），程式只關掉 strict 旗標，主機名與信任鏈照常驗證

## 新北市議會

貼「議事影音隨選視訊系統」的影片網址：`https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=<guid>`（從清單點進去時網址列的 `…/VOD/ViewDetailMetaData/<guid>` 也可以）。

- 同臺中，**沒有逐字稿可抓**（會議紀錄兩個多月後才上線，而且沒有時間戳），走語音辨識。音訊直接抓 Wowza 的純音訊串流（串流網址加 `wowzaaudioonly=true`，約 64 kbps AAC），不必把整支影片拉下來——實測 20 秒音訊 1 秒抓完
- 一段是一個黨團或一位議員的時段，市政總質詢一段可達兩小時；GPU 回傳的標題用網站「發言議員」的整份名單（含主席、依筆畫排序）；新聞服務不採用這個標題，文章標題以 Pi 過濾後的講者為準
- 截圖同一套：ffmpeg 切一般的 m3u8，串流網址從播放器頁（`VodStream/VideoPlayer`）解析出來
- 憑證鏈同臺中，只關掉 strict 旗標

## 外語影片

英文、日文、韓文影片一律產出**繁體中文**投影片。字幕依下列順序挑選：

1. **人工字幕**，依偏好：繁中 → 簡中 → 英文。人工翻譯品質最好
2. **影片原始語言的自動字幕**（例如日文影片的 `ja-orig`），交給模型邊摘要邊翻譯
3. YouTube 的**機器翻譯字幕**，最後手段

第 2 層是關鍵：YouTube 提供的「自動字幕中文」其實是對原文語音辨識結果再做機器翻譯，辨識錯誤加翻譯錯誤兩層損失。直接給模型原文，它帶著全文脈絡翻譯，品質明顯較好。實測日文、韓文影片產出的標題裡沒有任何假名或韓文字。

內容來源會寫在成品的標題區與完成訊息裡，例如「使用 YouTube 自動字幕，品質可能較差」。

## 沒有字幕的影片（語音辨識）

影片完全沒有字幕、或字幕軌下載失敗時，會自動改用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 從語音產生字幕，其後流程不變。成品會註明「由語音辨識產生，可能有辨識錯誤」；字幕下載失敗的話，註記會寫出原因。

- **有 NVIDIA 顯示卡就用顯示卡**，但要先裝 cuBLAS（下載約 630 MB；ctranslate2 的 Windows 版自帶 cuDNN，不用另裝）：

  ```powershell
  uv sync --group whisper-gpu
  ```

  載入模型後狀態列會寫「語音辨識模型已載入（cuda）」。預設模型 `large-v3-turbo` 在 RTX 5060 Ti 上實測約 **44 倍即時**：4.7 分鐘的中文語音 6.4 秒辨識完（1 小時推算約 1.5 分鐘）。之後同步套件要用 `uv sync --group whisper-gpu` 或 `uv sync --inexact`——只打 `uv sync` 會把 cuBLAS 移掉，退回 CPU
- **沒裝 cuBLAS、或顯示卡用不了**（驅動太舊之類）就走 CPU，約 **4 倍即時**：10 分鐘影片約 2.5 分鐘，1 小時約 15 分鐘。想強制用 CPU，設環境變數 `SLIDEBOX_WHISPER_DEVICE=cpu`
- **首次使用**會從 Hugging Face 下載約 1.6 GB 的模型，之後離線可用；每次開 app 後第一次辨識要載入 12～40 秒
- 模型載入與「解碼整個音訊檔」這兩段**無法中斷**，按取消會在該步驟完成後才停止
- 純音樂、動畫等沒有人聲的影片會回報「沒有偵測到語音」
- **字幕存在但下載失敗**（例如 YouTube 限流 HTTP 429）時也會改用語音辨識，不必等幾分鐘再重試；代價是比字幕慢，而且人工字幕通常比語音辨識準。語音辨識也失敗時（限流常常連音訊一起擋），錯誤訊息會同時講兩個原因，限流的話提醒過幾分鐘再試
- 立法院 IVOD 還沒有逐字稿的片段**不會**改用語音辨識：那條路用的是 yt-dlp，它不認得 IVOD 網址

實測同一支日文影片，語音辨識版與字幕版的投影片主題、順序一致；日文辨識的詞錯不影響摘要。

## 佇列

等生成的時候看到別支影片，可以直接貼上按 Enter 排進去，跑完自動接著做下一支。

- 一次只跑一項；下面的清單顯示每一項的狀態（⏳ 等待、▶ 進行中、✅ 完成、❌ 失敗、⏹ 已取消）
- **雙擊完成的項目**就開啟它的 HTML
- 單獨一支失敗**不會**停下整排——使用者通常排完就去做別的事了；但**連續兩項失敗**會暫停佇列（那通常代表 Ollama 沒開或模型名稱打錯，繼續跑只會在幾秒內把整排燒掉）
- 失敗或取消的項目選起來按「**重試**」就放回佇列，不必重貼網址
- **重試會接著做，不從頭來**：逐字稿（包括最慢的語音辨識）、摘要、摘要卡每做完一步就存在輸出資料夾的 `.slidebox_cache/`，同一個網址再跑時直接沿用（狀態列會寫「沿用上次…」），只重做沒做完的部分。改了模型、頁數範圍、詳細模式之類的設定，受影響的那一步會重做。整份成功出片後就刪掉那份存檔
- 第一人稱改寫失敗、或重試後還是改不掉時**照樣出片**，來源註記寫「第 N 頁仍有第一人稱的敘述」；頁數不對、空白頁這種真的壞掉的才算失敗
- 按「取消」會停下整個佇列，剩下的維持等待，按「開始」繼續
- 同一支影片用 `youtu.be/X` 和 `watch?v=X` 兩種網址貼，只會排一次
- 生成中關掉視窗會先請它停下來再關（最多等 30 秒），不會留下暫存檔
- 生成中可以繼續排隊，但**設定會鎖住**（設定在每一項開始的當下讀取，中途改會讓正在跑的那一項行為不一致）

## 詳細內容

想知道影片講了什麼但不想看影片時，勾「詳細內容」：

- 每一頁在條列下方多一段 **150～300 字的完整敘述**，外語影片一律翻成繁體中文
- HTML 最後附上**完整逐字稿**（預設收合，帶 mm:ss 時間），保留字幕原文不翻譯——它是唯一無損的那一份，用來回頭查證或找影片位置

- 另外多一張**摘要卡**放在最前面：一句話講清楚講者要什麼、最多 4 個關鍵數字、講者提出的要求與對方的回應。模型只從分段摘要挑數字與寫句子；每個數字必須附上逐字稿裡的原句，句子不在逐字稿裡、或數字不在句子裡，就不上卡片。數字若是在講某部法律某一條，且法律名稱與條號都是講者自己講出來的，會一併記下（新聞服務會去抓條文原文附在旁邊）。卡片產不出來時成品照常出片，只是沒有這一塊

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
| `GET /capabilities` | GPU 能不能做 YouTube 多人說話者區分（`speaker_diarization`，不能時 `reason` 說明原因），以及成品格式的契約版本（`speaker_contract`，目前是 `1`）；和 `/health` 一樣不需要 API key |
| `POST /jobs` | `{url, detailed, min_slides, max_slides, speaker_mode, speaker_count}` → `202 {id}`（議題分類與追問判斷工作見下；多人說話者模式見〈多人說話者模式〉） |
| `GET /jobs/{id}` | 進度與結果（`kind` 是工作種類） |
| `DELETE /jobs/{id}` | 取消 |

環境變數見 `serve_api.py` 的 docstring。設了 `SLIDEBOX_API_KEY` 就會強制 `X-API-Key`。

### 議題分類（`kind: "topic"`）

同一個 `POST /jobs` 也收議題分類工作：把一段文字（摘要卡的一句話＋各段小標）分到呼叫端帶來的政策領域。沒帶 `kind` 就是摘要工作，舊的呼叫方式完全不變。

```json
{"kind": "topic",
 "text": "一句話：……\n各段小標：\n- ……",
 "labels": [{"key": "welfare", "label": "衛生福利", "description": "醫療、健保、長照……"}, …]}
```

- `text` 必填，上限 4000 字；`labels` 2～20 個，`key` 與名稱都不可重複；`url` 不需要（帶了就要是 http／https）
- 領域清單不寫死在 GPU 端，每次由新聞服務帶來；模型看到的是名稱與說明，選完再對回 `key`
- `key` 與名稱不可只有空白
- 成品：`{"primary": "welfare", "secondary": null, "classifier": "qwen3.5:9b#topic-v1#1a2b3c4d"}`。`secondary` 只有第二個領域也占了相當篇幅才有，跟主領域相同就是 `null`
- `classifier` 是「模型#提示詞版本#提示指紋」，新聞服務靠它分辨哪些結果是評估過的版本分的。指紋從模型實際看到的提示（提示詞範本、領域的名稱與說明、schema）和代碼算出來：新聞服務那邊改了任何一個領域的說明，名稱就跟著變，舊的評估不會被當成新清單的評估。指紋看不到的改動——解析規則、溫度、思考開關——**要手動升 `PROMPT_VERSION`**（`src/slidebox/usecases/topics.py`）
- 主領域不在清單裡（或回應解析不出來）時工作失敗，不猜
- 跟摘要排同一個佇列、一次只跑一個；溫度 0、關掉模型的思考，一篇約 3 秒

### 追問判斷（`kind: "followup"`）

同一個 `POST /jobs` 也收追問判斷工作：給一項先前的要求與同一個人後來的一篇發言，判斷後來那篇有沒有再提**同一件具體的事**（同一個要求、同一個案子；同一個領域的別件事不算），有的話從逐字稿片段抄一句當證據。哪些要求算、哪篇是候選、逐字稿取哪一段、追問率怎麼算，都是新聞服務的程式決定。

```json
{"kind": "followup",
 "request": "要求衛福部一個月內提出長照人力補助方案",
 "response": "部長允諾一個月內提出",
 "card": "一句話：……\n要求：\n- ……\n各段小標：\n- ……",
 "excerpt": "後來那篇逐字稿裡跟要求最相關的一段"}
```

- `request`（舊的要求）、`card`（後來那篇的一句話＋要求＋各段小標）與 `excerpt`（逐字稿片段）必填，只有空白也算沒帶（422）；只有 `response`（官員當時的回應）可以空；`url` 不需要（帶了就要是 http／https）。`excerpt` 必填是因為引用只能從片段裡抄：沒有片段的一對注定判成沒有追問，新聞服務記成判過之後就不會再判，所以挑不出片段的那一對不要送
- 上限：`excerpt` 1500 字（新聞服務挑的是一段視窗，不是整份逐字稿）、`request` 與 `response` 各 500 字、`card` 4000 字
- 成品：`{"followed_up": true, "quote": "……", "classifier": "qwen3.5:9b#followup-v1#1a2b3c4d"}`。沒有追問時 `quote` 一律是空字串；有追問卻沒抄出引用就照實回傳——**引用是不是真的在逐字稿裡由新聞服務檢查**（落地檢查），GPU 端不改判
- `classifier` 的做法同議題分類：「模型#提示詞版本#提示指紋」，指紋涵蓋提示詞與 schema；解析規則、溫度、思考開關改了要手動升 `PROMPT_VERSION`（`src/slidebox/usecases/followups.py`）
- `followed_up` 不是布林值（或回應解析不出來）時工作失敗，不猜
- 跟摘要、分類排同一個佇列、一次只跑一個；溫度 0、關掉思考、`num_ctx` 跟摘要同一個。GPU 端保留最近 1000 個判斷工作，跟分類與摘要各自計算，誰也擠不掉誰的成品

**要讓 Pi 連得到這個服務，兩件事都要做**（預設只綁 loopback，所以預設狀態下 Pi 是連不到的）：

```powershell
$env:SLIDEBOX_API_HOST = "0.0.0.0"          # 預設 127.0.0.1
uv run --group api serve_api.py

# 另開一個「以系統管理員身分」的視窗，放行入站連線（只開給私人網路）
New-NetFirewallRule -DisplayName "SlideBox API" -Direction Inbound -LocalPort 8800 `
  -Protocol TCP -Action Allow -Profile Private
```

綁在非 loopback 位址又沒設 `SLIDEBOX_API_KEY` 時，啟動會印一行警告——任何連得到這個埠的人都能佔用你的 GPU。

### 或者用 Docker

`compose.gpu.yaml` 把 Ollama（用顯示卡）和摘要 API 包成兩個容器，不必在 Windows 上裝 uv、設防火牆規則或工作排程。需要 Docker Desktop（WSL2）與 NVIDIA 驅動；`docker run --rm --gpus all ubuntu nvidia-smi` 看得到顯示卡就可以。

```powershell
cp .env.gpu.example .env.gpu        # 填 SLIDEBOX_API_KEY；OLLAMA_MODELS_DIR 指向 Windows 上 Ollama 的資料夾
docker compose -f compose.gpu.yaml --env-file .env.gpu up -d --build
curl http://localhost:8800/health   # ollama_reachable 要是 true
```

- Ollama 容器直接掛 Windows 上已經下載好的模型（`OLLAMA_MODELS_DIR`），不必再抓一次；跑這一套時把原生的 Ollama 關掉，否則兩份模型會一起佔 VRAM
- 容器裡的 ffmpeg 用 Debian 的套件，不用 imageio-ffmpeg 附的靜態檔（後者在容器裡讀立法院的 HLS 串流會 segfault）
- 語音辨識用顯示卡（映像裝了 cuBLAS／cuDNN；載不起來自動退回 CPU），模型快取在 `whisper-cache` volume（YouTube 多人模式的說話者區分模型也放在這裡，只下載一次）；成品在 `summary-output` volume
- 對區網開放是預設（容器一定綁 0.0.0.0），所以 `SLIDEBOX_API_KEY` 是必填
- 桌面 app（`slides.py`）不在容器裡，照常原生執行

### 多人說話者模式（`speaker_mode: "diarize"`）

對談、訪談、座談這類不只一個人說話的 YouTube 影片，要的是「**誰說了什麼**」。字幕沒有說話者，所以這個模式**不用字幕**：

1. 下載音訊，用聲音區分說話者（pyannote.audio，模型 `pyannote/speaker-diarization-community-1`）
2. 語音辨識（faster-whisper，每個字都帶時間）
3. 程式依時間把每個字歸給當時在說話的人，再把同一個人連續說的話合成一段一段的發言。被兩位以上同時蓋住、換人點不在自然的停頓或句尾上、附近沒有說話者的字，一律標成「無法判斷」
4. 語言模型只做一件事：把**已經歸好人的發言**改寫成重點。每個重點先**照抄一小段原文（`quote`）**，再寫一句沒有主詞的重點。這個重點是誰說的，由程式去逐字稿裡找出真正有這段原文的那一輪、取那一輪的說話者，不是模型寫的，也不是模型填的編號；程式再逐點檢查（原文要連續出自同一輪、只能引用主要發言者、內容要有那一輪的依據、數字與否定要一致、不能用代名詞、第一人稱或逐字稿裡的名字…），沒過的重點直接丟掉。細節見下面〈每個重點怎麼核對〉

**誰說了什麼由程式決定，模型不決定。** 模型看不到影片標題、頻道、說明欄，也看不到任何名字或職稱，只看到 `[T12] S1：……` 這樣的匿名代號（請求裡的 `speech_hint` 只給語音辨識當提示，不給語言模型）。成品裡的說話者是程式給的中性標籤：「發言者 A」「發言者 B」，只出現一下的聲音叫「其他聲音 1」，沒把握的叫「無法判斷」。**不猜真人姓名，也不退回單人模式的「講者」寫法**。標籤只在同一支影片裡有意義，另一支影片的「發言者 A」不是同一個人。

寧可失敗也不亂歸人。底下任何一種情況，工作都會失敗、什麼都不發佈（`GET /jobs/{id}` 的 `error` 以例外名稱開頭）：

| 情況 | 例外 |
|---|---|
| 沒有 `HUGGINGFACE_TOKEN`、沒裝 pyannote、被 `SLIDEBOX_DIARIZER=off` 關掉、模型載入失敗（包括沒接受條款）。能力檢查就不過的，在**下載任何東西之前**就失敗 | `DiarizationUnavailable` |
| 音訊下載失敗、語音辨識失敗，或沒有偵測到語音 | `SpeechRecognitionFailed` |
| 聲音區分的結果不可靠：一位主要發言者都沒有；指定 2 人以上卻只找到 1 位；主要發言者超過 8 位；主要發言者加其他聲音合計超過 12 個（成品的說話者清單最多 12 個，新聞服務存檔前也檢查同一個上限，這裡先擋，不等整支影片做完才被退回）。或是模型的重點被程式檢查丟掉超過四分之一、某一段對談一頁都留不下來 | `AttributionUnreliable`（訊息帶數字，看得出是哪一種） |

聲音超過 12 個的失敗訊息會寫出主要發言者、其他聲音與合計各幾個，並建議用 `--speakers N` 指定實際人數再試一次；這個檢查在對齊之後、任何一次語言模型呼叫之前，說話者區分與語音辨識的存檔都留著。換了 `--speakers` 重試時，說話者區分會用新的人數重跑一次（人數是它存檔的一部分），語音辨識的結果沿用。指定人數不能放寬這個上限（有指定時，訊息改成請你確認人數或換較短的片段）。

這些門檻（8 位、12 個、四分之一，以及對齊用的 0.3／1.5／2 秒等）都是**暫定值**，要拿實際的影片校正。不致命但可疑的情況（人數與指定不符、超過四分之一的語音無法判斷、其他聲音有 4 個以上、自動模式只找到 1 位）照常發佈，但成品的 `diarization.warnings` 會標出來。成品的 `source_note` 一律寫明「由語音辨識產生，說話者由聲音自動區分；標籤可能有誤，未經人工確認」。

#### 每個重點怎麼核對

模型每個重點回傳四樣東西：`quote`（照抄的一小段原文）、`turn`（它以為那段原文在第幾輪）、`text`（一句沒有主詞的重點）、`context`（只有「對」「不是」這類很短的回答才要：它回應的上文）。程式核對三件事，任何一件沒過，這個重點整個丟掉：

1. **原文在哪一輪，說話者就是那一輪的人。** 程式在這一窗裡可以被引用的發言（主要發言者的）找 `quote`：`turn` 填的那一輪有就用它；只有另一輪有（模型把問題的編號填在答案上）就改引那一輪；沒有、或好幾輪都有，丟掉。`quote` 照標點與空白切成幾個片段，每個片段都要依序出現在同一輪裡（標點、空白與中間跳過的語助詞不算錯；把兩輪的話接在一起、改了字、倒過來的順序都不行）；合計至少 8 個字（除非它正好是整輪話），其中最長的片段至少 5 個字；單字的回答（「對」）不能當 `quote`
2. **內容要出自那一輪。** 程式在 `quote` 那一輪前後 3 輪內找重點的依據。依據在別輪（最典型的：記者在問句裡複述了發言人的數字，重點卻寫成記者說的）、或兩位說話者一樣符合，丟掉。**絕不因為內容比較像另一個人的話，就把重點改歸給他**
3. **文字要和那一輪的話一致。** 數字比「數值與單位」（六成就是 60%；把「百分之十二」寫成「十二分鐘」、把 55% 寫成「五十五成」都不行）；否定要一致；問句要寫成「詢問…」「追問…」；複述對方的話不算複述者的主張；「」裡的引文要逐字出現在同一位說話者的某一輪。文字裡不可以有代名詞（他、她、對方…）、第一人稱、`[[S1]]` 這類標記、角色字眼（主持人、來賓…），也不可以有逐字稿裡用來稱呼人的名字（陳委員、阿凱…，由程式從這一窗的逐字稿找出來）

**改用原話。** 只有「內容在附近找不到依據」（模型的改寫和原文的用字對不上）這一種情況，而且 `quote` 本身夠格時，這個重點會改成顯示那一段原話：`「…」`。字是逐字稿裡的，不是模型寫的，說話者仍是那一輪的說話者。夠格的意思是：連續的一段、8～100 個字、沒有代名詞、稱謂或名字、不是「你剛才說…」開頭、也不是複述對方等對方確認的那半句（單獨顯示會變成這個人自己的主張）。數字、否定、問句、複述、名字的問題一律不會被這樣救回來。所以成品裡的每個重點，不是通過全部檢查的改寫，就是某位主要發言者的一段原話。

**怎麼讀成品。** 以「」包起來的重點是原話，`points[].evidence` 是那一輪裡最貼近的一句；沒有「」的是模型的改寫，`points[]` 記著它根據哪一輪。每頁最多顯示 4 個重點（多的不算丟掉，也不算模型提出的），一頁留下的重點少於 2 個就整頁不用。一個窗最多問兩次：第一次被丟掉超過四分之一、或一頁都留不下來，就把原因附在第二次的請求裡；兩次都沒過關，選留下來的重點多的那一次，再交給整份的比例檢查。

**記錄裡的一行。** 每份摘要做完，進度紀錄會多一行（數字只是舉例）：

```
對談重點：模型提出 37 個，留下 31 個（其中 4 個直接引用原話）；丟掉 6 個（有問題 4、查不出來 2、同頁規則 0；內容出自別輪 1）
```

「有問題」是檢查發現文字或歸屬不對（數字、否定、問句、稱呼、複述、內容出自別輪…）；「查不出來」是 `quote` 不是任何一輪的原文，或內容找不到依據又不能改用原話；「同頁規則」是同一頁重複、或那一頁留下的點太少；「內容出自別輪」是「有問題」裡的一項，專指上面的第 2 點。三類加起來就是丟掉的個數。`AttributionUnreliable` 的訊息帶同一組分類，看得出是哪一種問題；每個丟掉的點在程式裡有穩定的原因碼（`bad_quote`、`anchor_conflict`、`number_mismatch`…，見 `src/slidebox/usecases/conversation.py`）。

**安全模式（`SLIDEBOX_QUOTE_ONLY`）。** 設成 `1`、`true` 或 `yes`（不分大小寫）：留下的每個重點都顯示核對過的原話，完全不看模型寫的文字，所以改寫才會有的問題（數字寫錯、代名詞、意思寫反）都不會發生；`quote` 不夠格（太短、跳了子句、有代名詞…）的點照樣丟掉，所以留下的點會比較少。預設關閉。懷疑某個模型的改寫常出錯、或想先確保每個字都出自逐字稿時開。開關算在摘要的存檔指紋裡，切換之後會重做摘要，不會沿用舊的結果。

**已知的限制**（程式擋不住，要靠實際的影片校正）：

- 說話者區分把兩個人併成一位時，標籤就是錯的，核對看不出來（所以才有下面的〈先聽過，再相信〉）
- 模型把「每個月」寫成「每日」這類期間的偏差，或主持人複述了數字卻沒有「對嗎」之類的確認語，核對擋不住
- 單獨一個中文數字加量詞（六個月、三份、五座、兩張）不比對數值；阿拉伯數字、帶「十百千萬億」、或後面接年、元、分鐘、人、成這類單位的才比
- 「你們剛才說…」這種開頭的原話不會被認出是在轉述對方（「你剛才說…」會）

#### 請求與成品

```json
{"url": "https://www.youtube.com/watch?v=……", "speaker_mode": "diarize", "speaker_count": null}
```

- `speaker_mode`：`"single"`（預設，成品和以前一模一樣）或 `"diarize"`；其他值 422
- `speaker_count`：`diarize` 時省略（自動判斷人數）或 2～20；`single` 時省略或 1。其他組合 422，例如 `diarize` 加 `1`（一個人請用 `single`）。指定的人數只是告訴說話者區分器「有幾個人」，不會讓檢查放水：指定了卻只找到一位會失敗，不會硬湊；成品的說話者清單（主要發言者加其他聲音）最多 12 個，找到的聲音超過 12 個照樣失敗，不管指定幾人（欄位仍接受到 20，但指定超過 12 人很可能就是這樣失敗）
- 只有摘要工作能指定這兩個欄位；議題分類與追問判斷工作帶了 `speaker_mode`（不是 `single`）或 `speaker_count` 也是 422，不會默默忽略
- 成品比單人多這幾塊：`speaker_mode`、`speakers`（每位說話者的標籤、說話秒數、段數、首末時間與最長的三段樣本）、`diarization`（引擎、版本、模型、指紋，`status`／`warnings`，找到幾位，無法判斷與交疊的比例）、每一頁的 `points`（每個重點和它根據的發言）；`brief` 一律是 `null`（多人模式不產生摘要卡）
- `bullets`、`detail`、`transcript_text` 裡的說話者是 `[[S1]]` 這樣的代號，不是名字：要顯示給人看時，換成 `speakers` 裡同一個代號的 `label`（`[[S0]]` 是「無法判斷」）。桌面 app 輸出的 HTML 已經換好了
- 缺任何一塊出處（說話者清單、區分報告、某一頁的 `points`）的多人成品，GPU 不會交出去，工作直接失敗
- 契約版本寫在 `GET /capabilities` 的 `speaker_contract`；範例放在 `tests/slidebox_api/fixtures/speaker_contract_v1.json`，GPU 這邊的測試對著它驗

#### 一次性的設定

1. 到 Hugging Face 建 token：Settings → Access Tokens → Create new token，類型選 **Read**（唯讀）就夠
2. 用同一個帳號到 <https://huggingface.co/pyannote/speaker-diarization-community-1>，按下接受使用條款。沒接受的話，載入模型時會被拒絕；若仍被拒絕、錯誤訊息點名了別的模型頁，到那一頁也接受
3. 把 token 填進 `.env.gpu` 的 `HUGGINGFACE_TOKEN=`。`.env.gpu` 已在 `.gitignore`，token 只放這裡——不要寫進 `compose.gpu.yaml`、不要提交、不要貼進 issue 或聊天。本程式自己的錯誤訊息與進度不會帶出 token
4. 重新建置並啟動（映像要另外多裝一個放 PyTorch 與 pyannote 的 Python 環境，見下面的〈映像裡的兩個 Python 環境〉與〈資源與時間〉），再看能力檢查：

   ```powershell
   docker compose -f compose.gpu.yaml --env-file .env.gpu up -d --build
   curl http://localhost:8800/capabilities    # speaker_diarization 要是 true
   ```

   **這只是便宜的檢查**（有 token、第二個 Python 環境裡裝了 pyannote.audio、沒被關掉；只問套件裝了沒），不會載入模型也不會連 Hugging Face，所以 `true` 不代表條款已經接受、模型載得起來。真的載入模型是在下面的 `diarize_check --sample`，請先跑它再收任何影片

#### 設定值

寫在 `.env.gpu`（`compose.gpu.yaml` 會傳進容器）；不用 Docker 時設成環境變數，完整清單見 `serve_api.py` 的 docstring。

| 變數 | 預設 | 說明 |
|---|---|---|
| `HUGGINGFACE_TOKEN` | 空 | 沒設就沒有多人模式：`/capabilities` 回 `false` 和原因 |
| `SLIDEBOX_DIARIZER` | `pyannote` | 設成 `off` 關掉多人模式，`/capabilities` 回 `false`；其他值會被當成設定錯誤回報 |
| `SLIDEBOX_DIARIZATION_DEVICE` | `auto` | 說話者區分跑在哪裡。`auto`：有 CUDA 就用顯示卡，載不起來退回 CPU（進度裡會說）；`cuda`：強制，失敗就工作失敗；`cpu`：完全不碰 CUDA（較慢）——驅動和映像的 CUDA 版本對不上、或顯示卡記憶體不夠時的逃生口 |
| `SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS` | 空（`21600`，6 小時） | 說話者區分單次工作的時間上限（秒）。超過就把工作行程殺掉、工作失敗並說明原因——這是防止行程卡死、一直佔著顯示卡的保險，不是 Pi 等單支影片的逾時。`0` 是不設上限；寫成負數或不是數字會被當成設定錯誤回報（`/capabilities` 回 `false`） |
| `SLIDEBOX_DIARIZATION_PYTHON` | 映像已設為 `/opt/diarize-venv/bin/python` | 啟動說話者區分工作行程用的 Python（PyTorch 與 pyannote 裝在那個環境，見下面的〈映像裡的兩個 Python 環境〉）。**Docker 映像已經設好，不要放進 `.env.gpu` 或 `compose.gpu.yaml`**：設成空字串也會蓋掉映像裡的設定，說話者區分就找不到 pyannote。不用 Docker 時不設，就用目前這個環境 |
| `SLIDEBOX_CHUNK_CHARS` | `6000` | 多人對談切成一窗一窗送給模型，每窗最多幾個字（1000～20000）。留空、不是整數或超出範圍就用預設值，通常不用動 |
| `SLIDEBOX_QUOTE_ONLY` | 空（關） | 設成 `1`、`true` 或 `yes`（不分大小寫）開啟安全模式：多人模式留下的每個重點都顯示核對過的原話，不看模型寫的文字（見〈每個重點怎麼核對〉）。留空或其他值都是關；切換後會重做摘要這一步 |
| `SLIDEBOX_WHISPER_DEVICE` | `auto` | 語音辨識的裝置；多人模式、單人模式的語音備援和議會片段都用它 |

`compose.gpu.yaml` 另外把 `PYANNOTE_METRICS_ENABLED=0` 和 `HF_HUB_DISABLE_TELEMETRY=1` 傳進容器：不把使用統計送回 pyannote 或 Hugging Face。

#### 映像裡的兩個 Python 環境

PyTorch 與 pyannote **不在** API 的主環境裡，而是在第二個 Python 環境，由一個獨立的工作行程（`src/slidebox/infrastructure/pyannote_worker.py`）用它來跑說話者區分：

| 環境 | 裡面有什麼 | 誰用 |
|---|---|---|
| `/app/.venv`（主環境） | API、議會與單人工作、語音辨識（faster-whisper／ctranslate2，要 CUDA 12＋cuDNN 9）。和沒有多人模式時完全一樣：沒有 torch、沒有 pyannote；`LD_LIBRARY_PATH` 指向它的 CUDA 12 函式庫 | `serve_api.py`、`diarize_check`、議會與單人工作 |
| `/opt/diarize-venv` | 只有 diarization 群組：pyannote.audio、PyTorch（鎖檔裡的版本要 CUDA 13＋cuDNN 9）；沒有 API 的套件，也沒有 CUDA 12 | 說話者區分的工作行程；`SLIDEBOX_DIARIZATION_PYTHON` 指到它的 Python |

為什麼分開：語音辨識要 CUDA 12 的 cuDNN 9，PyTorch 要 CUDA 13 的 cuDNN 9。兩邊的 cuDNN 套件看起來都把 `libcudnn*.so.9` 放進 `site-packages/nvidia/cudnn/lib`，檔名一樣、內容不同，放在同一個環境會互相蓋掉；而映像的 `LD_LIBRARY_PATH` 又把那個資料夾指給 ctranslate2——最壞的情況是連既有的議會與單人工作的語音辨識都壞掉。所以主環境維持原樣，說話者區分在另一個行程裡跑，那個行程用第二個環境的 Python 啟動，而且**不繼承 `LD_LIBRARY_PATH`**（PyTorch 帶著自己的一套 CUDA 13 函式庫）。這個分析是依套件的檔案配置推論的，沒有在真的映像裡重現過；分開環境就不必賭它是不是真的會發生。

- 建置映像時會跑 `docker/check_environments.py`，檢查兩個環境真的沒混在一起：主環境沒有 torch、沒有 CUDA 13 的套件；第二個環境沒有 CUDA 12 的套件、也沒有主環境的套件；`LD_LIBRARY_PATH` 只指向主環境，而且那裡找得到 cuDNN 9 與 cuBLAS 12。混在一起就讓建置失敗，不會做出一個悄悄壞掉的映像
- 工作行程只拿到音訊檔、模型名稱與裝置；token 只經由環境變數傳給它（不出現在命令列、不出現在錯誤訊息），結果寫成一個 JSON 檔。工作被取消或超過時間上限（`SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS`）就把它整個殺掉，不會留下還佔著顯示卡的行程

**這個映像在開發時沒有真的建置過，也沒有在真的 GPU 上跑過**（開發的機器沒有 Docker、沒有 GPU）：兩個環境的內容是照 `uv.lock` 與 Dockerfile 的文字檢查的，`uv sync` 的旗標（`UV_PROJECT_ENVIRONMENT`、`--only-group`）只對照過文件。第一次 `up -d --build` 就是第一次真的建置，請依序確認（確認過後可以把這一段拿掉）：

1. 建置成功：建置時的環境檢查沒過，建置就會失敗，訊息會列出是哪個環境混進了什麼
2. 再跑一次同樣的檢查（結束碼 `0`、印出「兩個 Python 環境分得很乾淨」才算數）：

   ```powershell
   docker compose -f compose.gpu.yaml --env-file .env.gpu exec api python /opt/check_environments.py /app/.venv/bin/python /opt/diarize-venv/bin/python
   ```
3. 兩個環境各有自己的 cuDNN：列出來的路徑要分屬 `/app/.venv` 與 `/opt/diarize-venv`，而且主環境的那份是 `LD_LIBRARY_PATH` 指的那個資料夾：

   ```powershell
   docker compose -f compose.gpu.yaml --env-file .env.gpu exec api find /app/.venv /opt/diarize-venv -name 'libcudnn.so*'
   ```
4. 語音辨識沒被影響：從 Pi 送一個會跑語音辨識的工作（例如沒有逐字稿的臺中市議會片段），確認它用顯示卡跑完（工作進行中 `nvidia-smi` 看得到使用量，進度裡沒有退回 CPU 的說明）
5. 說話者區分：照下面〈先聽過，再相信〉跑 `diarize_check --sample`，再用強制 `cuda` 的那一行確認顯示卡真的用得上

#### 資源與時間（先看過再決定要不要開）

- **映像會大很多，第一次建置要很久**：多人模式讓映像多裝一整個 Python 環境（上面的第二個環境）：PyTorch、pyannote.audio 和它們的 CUDA 13 函式庫，和主環境原有的 CUDA 12 函式庫並存（兩份 cuDNN、兩份 cuBLAS），大小以 GB 計（沒有實測，依你的網路與磁碟而定）。Docker Desktop 的磁碟要留足空間
- **第一次跑多人工作要下載模型**，存在 `whisper-cache` volume，之後不會再下載。`docker compose down -v` 會把這個 volume 連同已經下載的 Whisper 模型一起刪掉
- **比字幕模式慢很多**：字幕模式只抓文字；多人模式要下載整支影片的音訊，再跑完整支的聲音區分與語音辨識，時間隨影片長度增加。沒有實測數字——先用 `diarize_check` 在你的機器上量（它會印每個階段的耗時），再決定要收多長的影片
- **顯示卡記憶體**：Ollama、faster-whisper 和 pyannote 共用同一張卡，用量沒有實測（`diarize_check` 會印說話者區分工作行程的記憶體峰值和 PyTorch 的顯示卡記憶體峰值；Whisper 與 Ollama 的用量要另外看 `nvidia-smi`）。不夠就把 `SLIDEBOX_DIARIZATION_DEVICE` 設成 `cpu`
- **Pi 那邊要調大 `GPU_JOB_TIMEOUT_SECONDS`**：這是 Pi 等單一支影片的上限（`services/news` 預設 1800 秒，Pi 的 `compose.yaml` 預設 2700 秒），GPU 這一端沒有整支影片的逾時，只有說話者區分的工作行程有一個防卡死的上限（`SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS`，預設 21600 秒，也就是 6 小時，是還沒實測過的保守值）。長的對談影片很容易超過，超過 Pi 就不等了。要在 Pi 的 `api` 與 `scheduler` 兩個容器都調大。調多大看最久的那一階段：說話者區分、語音辨識、整份摘要各自做完才存，哪一階段在途中被切斷，重送時那一階段要整個重做（見下一條），所以每個階段都得在一次等待的時間內做得完
- 被取消或逾時的工作，已經做完的**階段**會存在 `summary-output` volume 的 `.slidebox_cache/` 底下，**再送同一支影片會從那裡接著做**，成功後清掉。存的有三份：說話者區分、語音辨識（字）、整份對談的摘要，各自在那個階段做完時存。**摘要不是一個窗一個窗存的**：要所有的窗都問完、整份的可靠度檢查也通過，才存一次。所以切斷發生在說話者區分或語音辨識的「途中」，那一階段整個重做；發生在逐窗摘要的途中（或整份的可靠度檢查沒過，`AttributionUnreliable`），已經問完的窗都不留下，重送會從第一個窗重問，說話者區分與語音辨識照樣沿用。要調大逾時，不能只靠重試
- **改了程式，存檔就不沿用**：三份存檔的指紋除了設定，還含產生它的程式的原始碼雜湊（換行符號先統一）。說話者區分含區分器與它的工作行程腳本；語音辨識含辨識器的解碼參數與原始碼；摘要含核對每個重點的規則（`slidebox.usecases` 裡驗證用的幾個模組）、摘要器（提示詞、取樣設定、回應解析、串流）的原始碼，以及每一段話的起訖時間。所以更新映像、或改了這些檔案裡的任何一個字之後，**對應的舊存檔不會被沿用，會重做**，沒變的那幾份照樣沿用：寧可多算一次，也不要拿舊規則放行的結果當成新規則的。原始碼讀不到時（例如只剩編譯過的檔案），指紋對不上任何舊存檔，一樣重做，不會出錯。`PROMPT_VERSION`（提示詞或驗證規則的意思改了）與 `ALIGN_VERSION`（原始碼看不出來的改動，例如換了校正用的資料）還在，那是人記得的那一道，不是第二道保險

#### 先聽過，再相信（`diarize_check`）

在相信任何多人文章之前，先用這個工具檢查說話者區分準不準。它只做「下載音訊 → 說話者區分 → 語音辨識 → 對齊」，不呼叫語言模型、不寫成品、不碰 Pi；用的是正式工作同一組對齊與分段的程式，所以這裡看到的標籤，就是文章裡會出現的標籤。

```powershell
# 冒煙測試：下載一段公開的四人中文對話（k2-fsa/sherpa-onnx 的範例音檔），預期找到 4 個聲音
docker compose -f compose.gpu.yaml --env-file .env.gpu exec api python -m slidebox_api.diarize_check --sample

# 檢查自己的影片的一段（秒，或 分:秒、時:分:秒）；--speakers 是你確定的人數，不確定就省略
docker compose -f compose.gpu.yaml --env-file .env.gpu exec api python -m slidebox_api.diarize_check 'https://www.youtube.com/watch?v=VIDEO_ID' --start 0 --end 600 --speakers 2
```

不用 Docker 的話：`$env:PYTHONPATH = "src"; uv run --group api --group diarization python -m slidebox_api.diarize_check --sample`。**在 Windows 主機上，這條路只能驗證 CPU**：鎖檔裡 Windows 的 PyTorch 是只有 CPU 的版本（CUDA 的函式庫只列在 Linux 的相依套件裡），所以不管有沒有顯示卡，說話者區分都在 CPU 上跑。它驗證得了 token、條款有沒有接受、模型載不載得起來和整條流程，驗證不了顯示卡和 GPU 的耗時、記憶體——那些只能用上面容器裡的指令。（這個指令把 PyTorch 與 pyannote 裝在目前的環境裡，沒有設 `SLIDEBOX_DIARIZATION_PYTHON`，工作行程就用同一個環境；分成兩個環境是 Docker 映像的作法。）

它會印出：

- 說話者區分器找到幾個聲音，以及每一段話歸給誰（起訖時間、說話者、文字）
- 每個標籤各占多少秒、幾段
- 說話者清單：每個標籤最長的三段話，附 `&t=` 連結，點開來聽是不是同一個人
- 平均分布的 10 個抽查位置、品質與警告（正式工作會拒絕的結果也會明說）
- 說話者區分用了哪個裝置：`說話者區分：…（模型，cuda）` 那一行括號裡寫的是 `cuda` 還是 `cpu`；是 `cpu` 的話，報告會緊接著多印一行「注意」
- 每個階段的耗時、本行程（語音辨識）與說話者區分工作行程各自的記憶體峰值、PyTorch 的顯示卡記憶體峰值（在 CPU 上跑時沒有這一項）

`--start`／`--end` 只切出那一段來檢查，印出的時間仍是整支影片的時間。音訊放在暫存資料夾，結束（包括失敗與 Ctrl-C）就刪掉。結束碼：`0` 正常；`1` 跑完了，但正式工作會拒絕這個結果（或樣本與預期不符）；`2` 參數不對；`3` 跑不起來（說話者區分不能用、下載或語音辨識失敗）；`130` 被中斷。樣本是第三方 GitHub release 上的檔案，它失效或抓不下來時，改用自己認得出聲音的短片段。

**結束碼 `0` 的意思是「流程跑通、結果可以用」，不是「顯示卡沒問題」**：`SLIDEBOX_DIARIZATION_DEVICE` 預設是 `auto`，沒有顯示卡、或驅動對不上時會退回 CPU，照樣跑完、照樣結束碼 `0`。所以 `--sample` 的 `0` 證明的是：token 有效、條款已接受、模型載得起來、音訊到對齊整條流程是通的、找到的聲音數和樣本一致；用了哪個裝置要看報告裡 `說話者區分：…` 那一行（`cuda` 或 `cpu`）。要**驗證顯示卡**，在 GPU 主機上強制 `cuda` 再跑一次：

```powershell
docker compose -f compose.gpu.yaml --env-file .env.gpu exec -e SLIDEBOX_DIARIZATION_DEVICE=cuda api python -m slidebox_api.diarize_check --sample
```

指定 `cuda` 時沒有可用的顯示卡就直接失敗（結束碼 `3`），不會悄悄退回 CPU；成功的話報告裡會是 `cuda`，並印出 PyTorch 的顯示卡記憶體峰值。這是要先做的事：用 CPU 區分一支一小時的影片慢很多，工作會一路拖到 Pi 的 `GPU_JOB_TIMEOUT_SECONDS`，到那時才發現是裝置的問題。

建議的順序：先 `--sample`，確認 token、條款、模型都正常；再用上面強制 `cuda` 的那一行確認顯示卡真的用得上；再挑一支你認得出每個人聲音的影片，用 `--start`／`--end` 取一段 5～10 分鐘，點開 `&t=` 連結聽過、抽查位置核對過；確認沒問題才開始收真的影片。

#### 疑難排解

- `/capabilities` 回 `false`：看 `reason`——沒設 token、第二個 Python 環境沒裝 `pyannote.audio`，或 `SLIDEBOX_DIARIZATION_PYTHON` 指的 Python 不存在、起不來（Docker 映像會在 `/opt/diarize-venv` 裝好並設定，這種情況多半是映像沒重新建置，或 `.env.gpu`／`compose.gpu.yaml` 把這個變數蓋掉了）、被 `SLIDEBOX_DIARIZER=off` 關掉，或設定值寫錯
- 工作失敗、訊息說模型被拒絕或要先接受條款：回到〈一次性的設定〉第 2、3 步，確認 token 屬於接受了條款的那個帳號，而且有讀取權限
- 顯示卡相關的錯誤，或 `auto` 一直退回 CPU：鎖檔（`uv.lock`）裡的 PyTorch 是 CUDA 13 的版本，驅動要夠新。`docker run --rm --gpus all ubuntu nvidia-smi` 標頭裡的 CUDA 版本要是 13.x（標籤叫 `CUDA Version`，較新的驅動印成 `CUDA UMD Version`）；比它舊就先設 `SLIDEBOX_DIARIZATION_DEVICE=cpu`（完全不碰 CUDA，只是慢）
- 想確認 PyTorch 看不看得到顯示卡：要用第二個環境的 Python，而且要像工作行程一樣不帶 `LD_LIBRARY_PATH`（主環境沒有 torch；帶著它，CUDA 13 的 PyTorch 會撿到主環境 CUDA 12 的函式庫）：`docker compose -f compose.gpu.yaml --env-file .env.gpu exec api env -u LD_LIBRARY_PATH /opt/diarize-venv/bin/python -c "import torch; print(torch.version.cuda, torch.cuda.is_available())"`，預期印出 `13.x True`
- 工作失敗、訊息是「說話者區分的工作行程異常結束」（會寫是被哪個訊號終止；`SIGKILL` 常見的原因是主機記憶體不足，被系統終止）：先看 Docker Desktop／WSL2 分到的記憶體夠不夠；是驅動或 CUDA 的問題的話，設 `SLIDEBOX_DIARIZATION_DEVICE=cpu`。訊息是「超過 N 秒仍未完成，已中止」：那是 `SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS` 的上限，在 CPU 上跑長影片會比較久，調大它

## 2. 後端（Pi）

`services/news/` — Django + django-ninja。每天凌晨抓前一天的質詢片段（立法院 IVOD、臺中市議會與新北市議會）、送去 GPU 主機產生**詳細模式**的摘要、存成文章，並開出 news API。見 `services/news/README.md`。

三個來源的差別只在「怎麼找到當天的片段」：立法院查開放 API，臺中逐一翻 61 位議員的頁面（每天 61 次 HTTP），新北查影音系統的日期查詢（見下）。臺中不想抓的話設 `TCCC_ENABLED=false`、新北設 `NTPC_ENABLED=false`；只想補某一個來源用 `ingest_ivod --source tccc`（或 `ly`、`ntpc`）。臺中的片段長、要跑語音辨識，一篇 10～20 分鐘；`GPU_JOB_TIMEOUT_SECONDS` 預設 1800 對 60 分鐘的片段偏緊，建議設 2700。

### 新北市議會

來源代碼 `ntpc`，查的是「議事影音隨選視訊系統」（`vod.ntp.gov.tw/VodCloudV2`）：每天一次日期查詢，再為每段要收的片段看一次播放器頁，全部循序、間隔一秒。**一個媒體檔登記一篇**，`ivod_id` 是 `ntpc-<檔案 key>`（例：`ntpc-0408R1150916020`）；同一個檔案被上架兩次（兩個 GUID）也只登記一次。沒有逐字稿，一律在 GPU 主機跑語音辨識；市政總質詢一段最長兩個多小時，`GPU_JOB_TIMEOUT_SECONDS` 可能要比臺中再調大。

怎麼挑片段：

- **只收質詢**：議程是「市政總質詢」、含「各機關聯合業務報告及質詢-」（業務質詢），或多黨混合的「市長施政報告」「市長報告…總預算」「…專案報告」（`NTPC_INCLUDE_MIXED=false` 就不收這類）；而且長度至少 3 分鐘。報告事項、三讀、討論議案、預備會議都略過。
- **講者**：影音系統的「發言議員」連主席都列進去，而且依筆畫排序、不是發言順序。議長、副議長從不質詢，一律移除；業務質詢的個人時段（`…-李翁議員月娥`）只掛那一位，黨團時段（`…-國民黨團發言`、`…-民進黨團聯合發言`）拿掉對立大黨團的成員，以及不同黨團的當天主持人（審查會召集人）——不是「只留現在同黨團的人」，因為名冊只有現況，回補舊會期時換過黨團的議員會被誤刪；多黨混合時段另外移除當天開場「報告事項」只列的那一位（當天主席）；市政總質詢不再過濾——一段是一個黨團聯合質詢的一個時間切片。多位講者用「、」串起；過濾後沒有人就先不登記。
- **名冊**：政黨、議長／副議長、黨團都來自官網 `www.ntp.gov.tw`，併在 `sync_members`（`--source ntpc`）裡，一次 68 個請求。個人頁的暫時性錯誤會重試兩次；任何一頁還是抓不到、或找到的議長／副議長不是各一位，整次同步就失敗、不寫入（少了主席，總質詢就會把主席列成講者）。`ingest_ivod` 查新北之前若名冊是空的，會先同步一次；同步失敗就**這次不查新北**、印一次警告，其他來源照跑——講者在登記那一刻就定了，用空名冊登記的片段會一直掛錯人。姓名去空白、間隔號統一成「．」，官網的「宋雨蓁 Nikar‧Falong」才對得上影音系統的「宋雨蓁Nikar．Falong」。
- 文章標的是**政黨**（官網的「無政黨」統一成「無黨籍」）；黨團只拿來判斷時段裡誰不屬於該黨團——有 4 位議員政黨與黨團不同。

回補：第 4 屆第 8 次定期會的質詢在 **2026-09-17** 結束，之後到第 4 屆結束（2026-12-24）是委員會審查與三讀，每日排程近期多半查不到東西。價值在回補過去三個定期會（第 6～8 次，約 290 段）：

```bash
python manage.py ingest_ivod --source ntpc --date 2026-09-17 --days 400 --discover-only
```

大約 400 次查詢加上每段一次播放器頁，一秒一個請求，十幾分鐘。登記好的文章交給每天的處理上限（`INGEST_DAILY_LIMIT`）慢慢消化。

設定：`NTPC_ENABLED`（預設 `true`）、`NTPC_INCLUDE_MIXED`（預設 `true`）、`NTPC_VOD_BASE`（`https://vod.ntp.gov.tw`）、`NTPC_WEB_BASE`（`https://www.ntp.gov.tw`）。

### 人物、任期與政黨

文章會標上講者**當時**的政黨，可依政黨篩選。資料分成 `Person`（真人）與 `Membership`（一段任期：哪個議會、哪個黨、選區、屆次、起訖），同一個人先當議員後當立委是同一個 Person 底下的兩筆任期。

- `sync_members` 每週日 03:30 自動跑（啟動時任何一個啟用中的來源還沒有名單，也會先跑一次）：立法院從 LYAPI `/legislators?屆=LY_TERM` 拿黨籍、選區、到職與離職日；臺中抓官網 `wb_introduction02.asp` 的「黨藉」與選區（官網與影音系統的議員編號不同，用姓名對）；新北抓官網的議員總覽、個人頁與三個黨團頁，另外記下議長／副議長（`Membership.role`，只有新北有）。黨團存在 `Membership.caucus`：立法院用 LYAPI 名冊的「黨團」（院內紀錄的黨團一致率靠它），新北用官網的黨團頁，臺中留空。
- 認人只靠姓名與別名：同名恰一個就連上、沒有就建、同名多個就另建一個並標 `needs_review`，到 admin 的「人物」用「合併」處理。
- 換黨：來源只給目前黨籍，同步偵測到不同就把舊任期結束在當天、新開一筆，兩筆都標 `needs_review`，日期要人到 admin 補。
- 合併後第一次部署要回填既有文章：`python manage.py sync_members --relink`。
- 送去 GPU 的工作會附 `speech_hint`（議會名、會議名、講者姓名），走語音辨識的來源用它當 Whisper 的 `initial_prompt`，人名的同音錯字會少很多。

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

按 Deploy 會直接在 Pi 上建映像（第一次約十幾分鐘）。之後更新程式：stack 頁面 → **Pull and redeploy**，**「Re-pull image」不要勾**——這兩個映像是本機 build 的，不在任何 registry，勾了會去 Docker Hub 拉而失敗（compose 裡已設 `pull_policy: build` 擋這件事，但舊版 Portainer 不一定認得）。或打開 GitOps updates 讓它定時自己拉。

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
