# slidebox — YouTube 影片投影片摘要

日期：2026-09-07

## 目標

給一個 YouTube 網址，產出一份**單一 HTML 檔的投影片摘要**：每一頁有標題、重點條列，以及一張擷取自影片該段落的截圖。概念接近「影片轉漫畫」——用畫面加上濃縮的文字，讓人不看完整支影片也能掌握內容。

這是 `src/` 底下與 `musicbox` 平行的**獨立 app**，共用 repo 與工程標準（Clean Architecture、port/adapter、pytest + 記憶體假物件），不共用程式碼。

## 使用者決定（來自 brainstorming）

- **素材來源**：字幕 + 影片截圖（不是純文字、也不是截圖 OCR）。
- **摘要引擎**：本機 Ollama，首版模型 `qwen3.5:9b`。模型名稱做成設定，UI 下拉可換；`Summarizer` 是 port，雲端實作（Claude 等）之後可加，首版不做。
- **輸出**：單一 HTML 檔，截圖以 base64 內嵌。
- **UI**：完全獨立的 app，自己的進入點與視窗。
- **畫質**：下載預設 1080p（可設定），但**只下載章節對應的片段**，不下載全片。截圖內嵌前縮到 1280px、WebP 有損壓縮。
- **截圖策略**：先讀字幕 → LLM 切章節 → 得知時間點後才下載片段（方案 B「先落地再抽幀」，但只落地需要的片段）。

## 環境事實（實作前已驗證）

| 項目 | 實測結果 |
|------|----------|
| yt-dlp | 2026.07.04，支援 `--download-sections` / `download_ranges` |
| 內建 ffmpeg | imageio-ffmpeg 附帶 7.1，有 `libwebp` 編碼器與 `webp` muxer |
| GPU | RTX 5060 Ti，16 GB VRAM |
| Ollama | 已安裝；現有模型 `gemma4:latest`（8B、Q4_K_M、131K ctx、vision/audio/tools/thinking） |
| 目標模型 | `qwen3.5:9b-q4_K_M` 6.6 GB、256K ctx，留約 9 GB 給 KV cache |

16 GB VRAM 裝不下 `qwen3.5:27b`（17 GB）與 `35b-a3b`（24 GB），這兩個不在選項內。

## 架構

### 專案配置

```
slides.py                       進入點（與現有 main.py 平行）
src/slidebox/
├─ domain/
│  ├─ entities.py               Cue / Transcript / Slide / Deck / Settings
│  ├─ ports.py                  六個 Protocol
│  └─ errors.py                 領域例外
├─ usecases/
│  ├─ chapters.py               純邏輯：字幕挑軌、VTT 解析、壓縮、驗證
│  └─ build_deck.py             BuildDeckUseCase（編排 pipeline）
├─ infrastructure/
│  ├─ ffmpeg.py                 ffmpeg 路徑（自 musicbox 複製 28 行）
│  ├─ ytdlp_subtitles.py        SubtitleGateway
│  ├─ ytdlp_sections.py         VideoSectionGateway
│  ├─ ffmpeg_frames.py          FrameExtractor
│  ├─ ollama_summarizer.py      Summarizer + ModelCatalog
│  ├─ html_renderer.py          DeckRenderer
│  └─ settings_repository.py    JsonSettingsRepository
├─ presentation/
│  ├─ app.py                    QApplication 啟動
│  ├─ main_window.py            單頁視窗
│  ├─ deck_page.py              主要 UI
│  └─ workers.py                QThread worker
└─ composition.py               composition root
```

`ffmpeg.py` 由 `musicbox` 複製而非 import：使用者選擇「完全獨立的 app」，28 行的重複換取零跨 package 耦合。

### 領域實體

```python
@dataclass(frozen=True)
class Cue:
    """字幕的一句。"""
    start: float
    end: float
    text: str

@dataclass(frozen=True)
class Transcript:
    video_id: str
    title: str
    duration: float
    cues: tuple[Cue, ...]
    language: str
    is_automatic: bool      # 自動字幕品質較差，UI 會提示

@dataclass(frozen=True)
class Slide:
    index: int
    title: str
    bullets: tuple[str, ...]
    timestamp: float                 # 代表畫面的秒數
    image_path: str | None = None    # 抽幀後才填

@dataclass(frozen=True)
class Deck:
    source_url: str
    video_title: str
    slides: tuple[Slide, ...]

    @property
    def missing_images(self) -> int:
        return sum(1 for s in self.slides if s.image_path is None)
```

`Slide.image_path` 預設 `None` 是刻意的設計：**LLM 產出的是沒有圖的 Slide，抽幀是後續獨立的一步**。摘要邏輯因此完全不需要知道 ffmpeg 存在，而「重跑摘要」與「重抽圖」可以分開進行。

```python
@dataclass
class Settings:
    """持久化於 slidebox_settings.json。"""
    output_dir: str
    model: str = "qwen3.5:9b"
    ollama_host: str = "http://localhost:11434"
    max_height: int | None = 1080          # 下載畫質上限
    min_slides: int = 8
    max_slides: int = 15
    image_width: int = 1280                # 內嵌前縮放寬度
    num_ctx: int = 32768                   # 送給 Ollama 的 context 長度
    char_budget: int = 20000               # 壓縮後字幕的字元上限
    subtitle_langs: tuple[str, ...] = ("zh-TW", "zh-Hant", "zh-HK", "zh", "en")
```

`char_budget` 與 `num_ctx` 是一組：前者是餵給模型的字幕字元數上限，後者是 Ollama 的 context 長度。預設值刻意讓字幕只佔約 context 的三分之二，其餘留給系統提示與模型輸出。兩者都可調，但調大 `char_budget` 時必須同時調大 `num_ctx`，否則字幕會被 Ollama 靜默截斷。

### Port（`domain/ports.py`）

沿用 musicbox 的回呼型別定義：

```python
ProgressCallback = Callable[[float | None, str], None]
CancelCheck = Callable[[], bool]

class SubtitleGateway(Protocol):
    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        """抓字幕並解析。找不到任何可用字幕時 raise NoSubtitlesAvailable。"""

class Summarizer(Protocol):
    def summarize(self, transcript: Transcript, min_slides: int, max_slides: int,
                  progress: ProgressCallback) -> tuple[Slide, ...]:
        """回傳 image_path 皆為 None 的 Slide 序列。"""

class VideoSectionGateway(Protocol):
    def download_sections(self, url: str, timestamps: Sequence[float],
                          max_height: int | None, dest_dir: str,
                          progress: ProgressCallback,
                          is_cancelled: CancelCheck) -> list[str | None]:
        """每個時間點回傳一個本地片段檔路徑；該點失敗則該位置為 None。"""

class FrameExtractor(Protocol):
    def extract(self, section_path: str, dest_path: str, width: int) -> None:
        """取片段中間那一格，縮到 width，存成 WebP。失敗時 raise。"""

class DeckRenderer(Protocol):
    def render(self, deck: Deck, dest_path: str) -> None: ...

class ModelCatalog(Protocol):
    def list_models(self) -> list[str]:
        """列出本機可用模型，供 UI 下拉。連不上時回傳空陣列。"""
```

`VideoSectionGateway.download_sections` 回傳 `list[str | None]` 而非直接 raise，是為了讓「部分截圖失敗仍然出片」的策略在型別上就成立。

### 純邏輯（`usecases/chapters.py`）

這五個函式不碰網路、不碰磁碟、不碰 LLM，是本專案自動化測試的主要對象。

**`pick_subtitle_track(subtitles, automatic_captions, prefer) -> tuple[str, bool]`**

從 yt-dlp 的兩份字典挑一軌，回傳 `(語言代碼, 是否為自動字幕)`。優先序：手動字幕優先於自動字幕，語言依 `prefer` 順序（預設 `zh-TW` → `zh-Hant` → `zh-HK` → `zh` → `en`）。都沒有則 raise `NoSubtitlesAvailable`。

**`parse_vtt(text) -> tuple[Cue, ...]`**

解析 WebVTT：`HH:MM:SS.mmm --> HH:MM:SS.mmm` 時間軸、多行 cue 文字、移除內嵌標籤（`<c>`、`<00:00:01.234>` 等）、處理 BOM 與 `NOTE` 區塊。

**`compress_cues(cues, char_budget) -> str`**

把數千句字幕壓成帶時間戳的段落文字，格式 `[MM:SS] 文字`。

自動字幕是**滾動式**的——同一句話會在連續數個 cue 裡逐字重複出現，直接丟給 LLM 會浪費一半以上的 context。壓縮流程：先移除與前一句重疊的前綴，再依時間窗合併成段落。

**窗口是自適應的**：先用 15 秒窗合併，若結果超過 `char_budget` 就加大窗口重算，直到符合預算。**不截斷**——截斷會讓影片後半段完全消失在摘要裡，加大窗口只是讓每段更粗，全片仍有涵蓋。

**`validate_slides(slides, duration, min_slides, max_slides) -> list[str]`** 與 **`clamp_timestamps(slides, duration) -> tuple[Slide, ...]`**

前者回傳問題描述清單（數量超出範圍、標題空白、bullets 為空）；後者把超出 `[0, duration)` 的時間戳夾回範圍內。

兩者分工的理由：時間戳越界是 9B 模型的常見小毛病，**夾回去就好，不值得重跑**；數量或標題不對則是模型沒照指示做，需要重試。

### Pipeline（`usecases/build_deck.py`）

`BuildDeckUseCase.execute(url, settings, progress, is_cancelled) -> Deck`

| 步驟 | 動作 | 進度 |
|------|------|------|
| 1 | `SubtitleGateway.fetch()` → `Transcript` | 0 → 5% |
| 2 | `compress_cues()` | — |
| 3 | `Summarizer.summarize()` → `tuple[Slide]` | 5 → 40% |
| 4 | `clamp_timestamps()` + `validate_slides()`；驗證失敗則重試步驟 3 一次 | — |
| 5 | `VideoSectionGateway.download_sections()` | 40 → 75% |
| 6 | `FrameExtractor.extract()` × N，逐一填入 `image_path` | 75 → 90% |
| 7 | `DeckRenderer.render()` | 90 → 100% |
| 8 | 刪除暫存片段 mp4；WebP 保留於 `<output_dir>/<video_id>/` 當快取 | — |

每個步驟之間與步驟 5、6 的迴圈內檢查 `is_cancelled()`，取消時 raise `OperationCancelled` 並清除暫存 mp4。

步驟 4 的重試只做**一次**，且會把驗證錯誤訊息附加到給模型的提示裡。再失敗則 raise `SummarizerOutputInvalid`，訊息建議使用者換模型。

### infrastructure 實作細節

**`ytdlp_subtitles.py`** — 以 `skip_download=True`、`writesubtitles=True`、`writeautomaticsub=True`、`subtitlesformat="vtt"` 取得字幕檔到暫存目錄，同時從 `extract_info` 取 `id` / `title` / `duration`。挑軌與解析交給 `chapters.py` 的純函式。

**`ollama_summarizer.py`** — `POST {host}/api/chat`，用標準函式庫 `urllib.request`（Ollama 只需要 `/api/chat` 與 `/api/tags` 兩個端點，NDJSON 串流逐行讀約十行程式碼，不值得為此加 HTTP 相依）。請求要點：

- `format` 傳 JSON schema（`{slides: [{title, bullets[], timestamp}]}`），這是文法層級約束，格式錯誤幾乎不可能發生。
- **`options.num_ctx` 必須顯式設定**。Ollama 預設 context 很小，不設會讓長字幕被**靜默截斷**——這是本專案最容易踩且最難察覺的坑。
- `options.temperature` 設 0.3：摘要要穩定，不要創意。
- `stream=True` 只為了回報進度。

模組拆成兩半：HTTP 呼叫（無自動化測試）與 `parse_response(payload) -> tuple[Slide]`（純函式，測試涵蓋各種畸形輸出）。

`ModelCatalog` 走 `GET /api/tags`，連不上時回傳空陣列而非 raise——UI 下拉空著即可，不該因此擋住整個視窗。

**`ytdlp_sections.py`** — 每個時間點**一次獨立的 yt-dlp 呼叫**，`download_ranges` 只含 `(t, t+4)` 單一區間，`format` 只取視訊流（`bestvideo[height<=H]`，不要音訊也不要合併，較快且較小），`force_keyframes_at_cuts=False`（強制對齊要重編碼，很慢；切點落在前一個關鍵影格對「取代表畫面」毫無影響）。

選擇一時間點一呼叫而非單次多區間，是為了**失敗隔離**：單點失敗只讓該頁沒圖，符合錯誤處理策略。代價是重複的 info extraction。若實測顯示過慢，改為單次多區間是同一個 port 內的替換。

**`ffmpeg_frames.py`** — `ffmpeg -ss 2 -i <clip> -frames:v 1 -vf scale=<W>:-2 -c:v libwebp -quality 80 -y <out.webp>`。取片段第 2 秒（片段長 4 秒）以避開切點可能的轉場或不完整影格。`scale=W:-2` 維持比例且高度取偶數。

**`html_renderer.py`** — 讀 WebP → base64 → 內嵌 `<img src="data:image/webp;base64,...">`，樣板為模組內的字串常數，自包含 CSS，無外部資源。

**所有來自 LLM 的文字（標題、bullets）與影片標題都必須經過 `html.escape()`。** 這不是形式主義：投影片標題源自 LLM，而 LLM 讀的是 YouTube 字幕，字幕是任何人都能上傳的內容。不跳脫等於把第三方內容當程式碼執行。

### presentation

單頁視窗：網址輸入 + 貼上、模型下拉（來自 `ModelCatalog`）、輸出資料夾、畫質下拉（沿用 musicbox 的 最高／1080p／720p／480p）、頁數範圍、生成按鈕（執行中變成取消）、進度條、狀態列、完成後「開啟 HTML」按鈕。

`workers.py` 的 `BuildDeckWorker` 沿用 musicbox `DownloadWorker` 的形狀：`progress` / `finished_ok` / `failed` / `cancelled` 四個 signal，`threading.Event` 承載取消。

設定沿用 musicbox 的 `_apply_settings` 於套用期間 `blockSignals` 的作法——該作法是既有 repo 修過的實際 bug（commit `bf7ed26`），直接沿用而非重蹈覆轍。

## 錯誤處理

| 失敗 | 例外 | 行為 |
|------|------|------|
| 影片無任何字幕 | `NoSubtitlesAvailable` | 明說「這部影片沒有字幕」，不重試 |
| Ollama 未啟動 | `SummarizerUnavailable` | 「Ollama 未啟動」＋顯示 host |
| 模型未 pull | `SummarizerUnavailable` | 「找不到模型 X，請先 ollama pull」 |
| LLM 輸出驗證失敗 | `SummarizerOutputInvalid` | 重試一次；再失敗則建議換模型 |
| 部分片段下載／抽幀失敗 | — | **不中斷**，該頁 `image_path` 留 `None`，最後告知「N 頁沒有截圖」 |
| 全部截圖失敗 | — | 仍產出純文字 HTML 並警告 |
| 使用者取消 | `OperationCancelled` | 清除暫存 mp4 後結束 |

「部分截圖失敗仍然出片」是刻意的決定：**13/15 頁有圖的成品仍然有用，為兩張圖失敗而什麼都不給是更糟的結果**。LLM 那步是全有全無（沒摘要就沒有東西可放），但截圖那步是可降級的，兩者不該套用同一種嚴格度。

## 測試

pytest + 記憶體假物件，不碰磁碟與網路，沿用 musicbox 的 `tests/fakes.py` 模式。

```
tests/slidebox/
├─ fakes.py                五個 port 的 in-memory 假實作
├─ test_subtitle_pick.py   pick_subtitle_track：手動優先、語言優先序、全無時 raise
├─ test_vtt.py             parse_vtt：時間格式、多行 cue、內嵌標籤、BOM、NOTE 區塊
├─ test_compress.py        compress_cues：滾動字幕去重、時間戳保留、
│                          超出預算時加大窗口而非截斷（含全片涵蓋斷言）
├─ test_validate.py        validate_slides / clamp_timestamps 邊界
├─ test_ollama_parse.py    parse_response：正常、缺欄位、timestamp 為字串、slides 為空
├─ test_build_deck.py      編排：正常流程、無字幕、驗證失敗後重試成功、
│                          重試仍失敗、部分抽幀失敗仍出片、中途取消
├─ test_render.py          HTML：base64 內嵌、html.escape（含 <script> 案例）、無圖降級
└─ test_settings.py        設定 round-trip（照 musicbox 的形狀）
```

未涵蓋於自動化測試者：`ollama_summarizer` 的 HTTP 呼叫、`ytdlp_*` 的實際網路存取、`ffmpeg_frames` 的實際轉檔。這些以一次手動端到端驗證確認（跑一支真實的短影片，確認 HTML 產出正確）。

## 新增依賴

**無。** yt-dlp、imageio-ffmpeg、PySide6 皆為現有相依；Ollama 用標準函式庫 `urllib.request`；HTML 自行組字串。

需要使用者執行一次 `ollama pull qwen3.5:9b`（6.6 GB）。

## 已知範圍界線

- **沒有字幕的影片不支援**，直接報錯。不做語音轉錄。使用者現有的 `gemma4` 具備 audio 能力，這是乾淨的第二版功能，但納入首版會使範圍失控。
- **只做 Ollama 實作**。`Summarizer` port 已就位，雲端實作（Claude 等）之後加，首版不寫。
- **不做投影片內容的手動編輯**。產出即最終品；不滿意就換模型或改頁數重跑。
- **不處理播放清單**，比照 musicbox 只收單一影片。
- **HTML 樣板固定**，不提供主題或版型選擇。
- 快取只做到 WebP 層級（重跑摘要不需重新下載）；字幕與 LLM 回應不快取。

## 檔案異動一覽

| 檔案 | 異動 |
|------|------|
| `slides.py` | 新檔：進入點 |
| `src/slidebox/domain/entities.py` | 新檔：`Cue`／`Transcript`／`Slide`／`Deck`／`Settings` |
| `src/slidebox/domain/ports.py` | 新檔：六個 Protocol |
| `src/slidebox/domain/errors.py` | 新檔：領域例外 |
| `src/slidebox/usecases/chapters.py` | 新檔：五個純函式 |
| `src/slidebox/usecases/build_deck.py` | 新檔：`BuildDeckUseCase` |
| `src/slidebox/infrastructure/ffmpeg.py` | 新檔：自 musicbox 複製 |
| `src/slidebox/infrastructure/ytdlp_subtitles.py` | 新檔：`YtDlpSubtitleGateway` |
| `src/slidebox/infrastructure/ytdlp_sections.py` | 新檔：`YtDlpSectionGateway` |
| `src/slidebox/infrastructure/ffmpeg_frames.py` | 新檔：`FfmpegFrameExtractor` |
| `src/slidebox/infrastructure/ollama_summarizer.py` | 新檔：`OllamaSummarizer` + `OllamaModelCatalog` |
| `src/slidebox/infrastructure/html_renderer.py` | 新檔：`HtmlDeckRenderer` |
| `src/slidebox/infrastructure/settings_repository.py` | 新檔：`JsonSettingsRepository` |
| `src/slidebox/presentation/*.py` | 新檔：`app`／`main_window`／`deck_page`／`workers` |
| `src/slidebox/composition.py` | 新檔：composition root |
| `tests/slidebox/__init__.py` | 新檔：空檔（`tests/` 已是 package，子目錄需比照） |
| `tests/slidebox/*.py` | 新檔：`fakes.py` + 八個測試模組 |
| `pyproject.toml` | `testpaths` 已為 `tests`，涵蓋新子目錄；`pythonpath` 已含 `src`，無需異動 |
| `README.md` | 新增 slidebox 章節 |
