# slidebox — 無字幕影片的語音轉字幕

日期：2026-09-12

## 目標

沒有任何字幕的影片，改用語音辨識產生帶時間戳的字幕，之後流程完全不變（壓縮 → 摘要 → 截圖 → HTML）。日文、英文、韓文影片一律輸出繁體中文投影片。

## 這份設計是怎麼來的

使用者就寢前明確要求此功能（「如果可以的話，我希望沒有字幕的影片可以用語音翻譯成字幕」），並表示早上驗證。過程中無法提問，所以**所有決定都以裁定（Ruling）形式記錄在本文件**，早上的驗證就是審核關卡。實作放在分支上，**不合併進 master**。

## 事前實測（本文件的依據）

### 1. gemma4 經 Ollama 吃音訊：不可行

原始 spec 曾寫「使用者現有的 gemma4 具備 audio 能力，是乾淨的第二版功能」。實測 Ollama 0.33.3：

| 請求方式 | 結果 |
|---|---|
| `images` 欄位送 WAV，開 thinking | 音訊確實進了模型（`prompt_eval_count=502`），但模型回「未提供音訊」 |
| `images` 欄位送 WAV，關 thinking | 輸出「अरे अरे अरे…」重複退化 |
| `audio` 欄位 | 被忽略，模型回「請提供音訊」 |

測資是最簡單的情況：19 秒、清晰英文、已知標準答案（YouTube 第一支影片「Me at the zoo」）。連它都失敗，日韓不可能可靠，且沒有時間戳。

### 2. faster-whisper：可行

在拋棄式環境（不動專案相依）實測：Python 3.13 Windows 可安裝；AMD Zen 5 16 執行緒 CPU 上，`small` 模型轉錄同一段 19 秒音訊只花 1.9 秒，僅錯一個詞，並有逐段時間戳與語言偵測（en，信心 0.97）。

**GPU 不可行**：`RuntimeError: Library cublas64_12.dll is not found`。本機未安裝 CUDA toolkit；可用 pip 安裝 NVIDIA DLL 套件補齊，但光 cuDNN 約 600 MB，且 ctranslate2 對 RTX 5060 Ti（Blackwell）的支援不確定。

### 3. 模型選擇（日韓品質基準）

測資：前一輪端到端用過的日文、韓文簡報演講，各取 130 秒音訊。AMD Zen 5 16 執行緒，CPU int8。

| 模型 | 日文 | 韓文 | 載入 |
|---|---|---|---|
| `small` | 4.7× 即時 | 9.1× | 0.9 秒 |
| `medium` | 2.0× | 3.6× | 37 秒 |
| **`large-v3-turbo`** | **3.9×** | **4.9×** | 40 秒 |

**Ruling：預設 `large-v3-turbo`。** 它比 `medium` 還快（解碼器較小）而品質是最大模型等級，`medium` 因此完全被壓制；比 `small` 只慢約兩成。10 分鐘影片約 2.5 分鐘轉錄；載入的 40 秒只在 app 啟動後第一次發生，之後模型快取在 adapter 裡。

品質：**韓文極佳**——完整、合文法、有標點，斷詞甚至比 YouTube 自己的辨識正確（turbo「아시겠지만 이」，YouTube「아시겠지만이」）。**日文抓得到大意但有詞錯**——講者是語速很快的口語，例如把「その内の1人目」聽成「住民のうちの1人目」；YouTube 自己的辨識同樣不完美。最終判準是摘要品質而非逐字正確率，見端到端驗證。

若錯了的代價：`small` 快約兩成但日文明顯更差；使用者可在設定檔改 `whisper_model`。

## 架構

### 備援放在 use case，不包成複合 gateway

曾考慮做一個實作 `SubtitleGateway` 的複合 adapter（先試字幕、失敗改語音），好處是 use case 完全不動。**不採用**：`SubtitleGateway.fetch(url, langs)` 沒有進度回呼、也沒有取消檢查，包在裡面會讓語音辨識變成「好幾分鐘沒反應、按取消也沒用」——正是上一輪整分支審查抓到的「LLM 階段取消失效」同一類問題。放在 use case 則兩者都有。

### 兩個新 port

```python
@dataclass(frozen=True)
class AudioClip:
    """下載好的音訊檔與其影片資訊。語音路徑沒有字幕 gateway 提供的 metadata。"""
    path: str
    video_id: str
    title: str
    duration: float

class AudioGateway(Protocol):
    def download_audio(self, url, dest_dir, progress, is_cancelled) -> AudioClip: ...
    def cleanup(self, dest_dir) -> None:
        """同 VideoSectionGateway.cleanup：必須容忍目錄不存在，絕不可 raise。"""

class SpeechTranscriber(Protocol):
    def transcribe(self, audio_path, duration, progress, is_cancelled) -> tuple[tuple[Cue, ...], str]:
        """回傳 (字幕, 偵測到的語言)。沒偵測到任何語音時回傳空 tuple。"""
```

一個 port 一件事，與既有六個 port 一致。

### use case 的備援流程

```python
try:
    transcript = self._subtitles.fetch(url, settings.subtitle_langs)
    note = "（使用自動字幕，品質可能較差）" if transcript.is_automatic else ""
except NoSubtitlesAvailable:
    if self._transcriber is None:
        raise                      # 未設定語音辨識：行為與原本相同
    transcript = self._transcribe(url, settings, cb, check)
    note = "（由語音辨識產生，可能有辨識錯誤）"
```

`BuildDeckUseCase` 的兩個新參數預設 `None`，既有以五個參數建構的地方與 18 個測試不受影響。

**只在「真的沒有可用字幕」時觸發。** 字幕存在但這次下載失敗（例如 HTTP 429）時，字幕 gateway 拋 `SubtitleDownloadFailed`——`NoSubtitlesAvailable` 的子類別，所以既有例外處理照常運作——use case 原樣拋出，不走語音。

> **此裁定經審查推翻。** 初稿寫的是「觸發條件是任何 `NoSubtitlesAvailable`」，理由是拿到結果勝過拿到錯誤。審查指出：使用者要的是「沒有字幕」的影片；有人工中文字幕卻暫時下載失敗時，會被無聲地降級成較慢、較差的語音辨識；字幕 gateway 特意寫好的「請過幾分鐘再試」被吞掉；後續失敗訊息會誤稱「沒有字幕」；而初稿聲稱的「音訊端點不受字幕限流影響」從未驗證。以上皆成立，已改。

### Transcript 的欄位語意

語音辨識的結果設 `is_automatic=False`。該欄位實際控制兩件事：`compress_cues` 的滾動去重，以及「使用自動字幕」提示。Whisper 的輸出是一句一句的獨立段落、不是 YouTube 那種滾動字幕，套用滾動去重只會誤刪內容；提示則由 use case 依走的路徑給出正確文字。不為此新增 domain 欄位。`is_automatic` 的 docstring 改寫為「是否為 YouTube 滾動式自動字幕」以反映實際語意。

### 進度與取消

語音路徑的進度以 `None`（UI 顯示忙碌動畫）搭配文字回報：「下載音訊… 45%」、「準備語音辨識…（解碼音訊、偵測人聲）」、「語音辨識中… 3:20 / 9:14」。不改動既有的進度區間劃分——那會連帶改掉兩個既有測試，換到的只是一條在 0–5% 間爬行好幾分鐘的進度條，文字進度反而更清楚。

faster-whisper 以 generator 逐段輸出，每段之間檢查取消。**但有兩段無法中斷**：模型載入（從磁碟約 12–40 秒；首次使用需下載約 1.6 GB），以及 `WhisperModel.transcribe` 在回傳 generator 之前一次做完的「解碼整個檔、跑 VAD、偵測語言」（長影片數十秒）。按取消時 UI 顯示「部分步驟無法中斷，會在該步驟完成後停止」；載入期間按了取消，載完後即停止、不會再去解碼。

### 內容來源註記

`Deck.source_note` 記錄內容來源（「由語音辨識產生，可能有辨識錯誤」、「使用 YouTube 自動字幕，品質可能較差」，人工字幕則為空），顯示在 HTML 標題區、UI 完成訊息與最後一則狀態。只在中途送出一次的提示會在幾毫秒內被後續狀態蓋掉——審查抓到初稿就是這樣，而且稍早加的自動字幕提示也有同樣問題。

### adapter

**`YtDlpAudioGateway`** — 下載 `bestaudio[protocol^=http]/best[protocol^=http]`，從 yt-dlp 的 info 取 id／title／duration。faster-whisper 透過 PyAV 直接解碼任何格式，**不需轉成 WAV**。

- **限定 http**：直播中的影片沒有字幕、會走到這裡；選到 HLS 音訊時 yt-dlp 會一直錄到直播結束。限定後只有 HLS 可用時直接報格式不可用。退路也限定 http，避免 `best` 抓回整支影音合併檔。
- **檔名帶影片 id**（`%(id)s.%(ext)s`）：yt-dlp 預設不覆寫既有檔案。初稿用固定檔名，審查抓到只要某次執行在 `finally` 之前中斷（例如轉錄時關視窗），下一支影片就會轉錄到上一支影片的聲音。
- **暫存資料夾 `.slidebox_audio_tmp`**，use case 在**下載前**先清空（清掉殘留的 `.part`，以免被續傳拼接），完成後於 `finally` 再清。名稱刻意取成明顯屬於本程式的樣子：cleanup 會整個刪除它。

**`FasterWhisperTranscriber`** — 延遲 import faster-whisper（它會載入 ctranslate2、onnxruntime、av，拖慢 app 啟動）；模型實例在第一次使用時載入並快取於 adapter，之後重複使用（從磁碟約 12–40 秒）。載入失敗不快取，下一次會重試。緊接著的重複句會被合併：Whisper large 家族偶爾卡在同一句話上重複輸出，而語音結果不走滾動去重。`vad_filter=True` 跳過靜音與純音樂段，避免 Whisper 在無人聲處幻覺出「Thanks for watching」之類的內容，也加快速度。語言交給 Whisper 自動偵測，不使用 yt-dlp 的 `language` 當提示——上傳者標錯語言時，強制指定會產出整份錯誤語言的轉錄，而 Whisper 以前 30 秒偵測語言既準又便宜。

模型實例透過注入的工廠函式建立，測試可以塞入假模型，驗證 adapter 本身的段落轉換、進度與取消邏輯而不必下載模型。

### 設定

`Settings` 新增 `whisper_model: str`，預設值見上方基準。可在 `slidebox_settings.json` 修改，UI 不新增控制項（YAGNI；語音辨識是自動備援，一般使用者無需選擇）。

## 錯誤處理

| 情況 | 行為 |
|---|---|
| 字幕存在但暫時下載失敗（HTTP 429 等） | `SubtitleDownloadFailed` 原樣拋出，顯示「請過幾分鐘再試」，**不走語音** |
| 未設定語音辨識 | 維持原本的 `NoSubtitlesAvailable` |
| faster-whisper 未安裝 | `NoSubtitlesAvailable`：「這部影片沒有字幕；語音辨識需要 faster-whisper，請執行 uv sync」 |
| 音訊下載失敗 | `NoSubtitlesAvailable`：「這部影片沒有字幕，音訊也下載失敗：…」 |
| 沒偵測到任何語音 | `NoSubtitlesAvailable`：「這部影片沒有字幕，也沒有偵測到語音」 |
| 使用者取消 | `OperationCancelled`，`finally` 清除暫存音訊 |

錯誤型別沿用 `NoSubtitlesAvailable`：從使用者角度，結局都是「拿不到這部影片的字幕」，不另增例外型別。

## 新增依賴

`faster-whisper`（連帶 ctranslate2、onnxruntime、av、tokenizers、huggingface-hub）。這推翻了原始 spec 的「不新增依賴」——那是首版範圍的約束，語音辨識本質上需要辨識引擎，且 Ollama 路線已實測不可行。

比照 musicbox 對 OpenCC 的處理：**宣告為相依，但程式碼延遲 import 並在缺套件時降級**。

模型於首次使用時從 Hugging Face 下載到本機快取，之後離線可用。

## 測試

- **use case**（假物件）：無字幕時走語音路徑並產出投影片；語音路徑的狀態訊息；未設定轉錄器時原例外照常拋出；語音路徑的暫存音訊於成功與取消時皆被清除；取消於轉錄中生效。
- **`FasterWhisperTranscriber`**（注入假模型，不下載）：段落轉 `Cue`、空白段落略過、偵測語言回傳、逐段檢查取消、未安裝時的錯誤訊息。
- **`YtDlpAudioGateway`**（替換 yt-dlp 網路層）：格式排除 HLS、metadata 取自 info、cleanup 容忍缺目錄。
- **端到端**：以真實無字幕影片跑完整 pipeline。

## 端到端驗證

| 影片 | 路徑 | 結果 |
|---|---|---|
| 日文簡報演講 `COopQRP6VYg`（554 秒） | 強制走語音 | 287 秒，8 頁 8 張截圖。語音辨識 120 秒（4.6× 即時）。摘要與同一支影片的字幕版主題、順序一致，日文辨識的詞錯未影響摘要 |
| Big Buck Bunny `aqz-KE-bpKQ`（真的沒有字幕、純音樂） | 真實字幕 gateway | 9 秒回報「這部影片沒有字幕，也沒有偵測到語音」 |

## 已知範圍界線

- **只用 CPU。** GPU 需要 CUDA DLL 且 Blackwell 支援不確定，見上方實測。
- 長影片的語音辨識時間與影片長度成正比：約 4 倍即時，1 小時影片約 15 分鐘。
- 模型載入與解碼階段無法中斷，見「進度與取消」。
- UI 不提供 Whisper 模型選單，改設定檔。
- 背景音樂很重、多人同時說話的影片，辨識品質會明顯下降。VAD 會濾掉純音樂段，但**歌詞會被當成內容**摘要進投影片。
