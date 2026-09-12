# SlideBox 支援立法院 IVOD（2026-09-12）

使用者要求：支援台灣立法院 IVOD，維持 Clean Architecture，**改動越少越好**。長期目標是把核心開成 API，讓另一個服務每天跑、產出新聞網頁。

## 調查結果（實測，非推測）

| 問題 | 實測 |
|---|---|
| 有沒有逐字稿？ | 有。`ly.govapi.tw/v2/ivods/{id}` 直接給 WhisperX 結果，含 `start`/`end`/`text`——正好是 `Cue` 的形狀 |
| Clip 171180（3 分 17 秒） | 50 段、791 字，完整覆蓋到 197 秒 |
| Full 17704（8 小時 36 分） | 2409 段、32,247 字，完整覆蓋到 30,975 秒 |
| 影片能不能抽幀？ | 能。`ffmpeg -ss T -i playlist.m3u8 -t 4 -c copy` **1 秒**切出片段，抽幀 <1 秒 |
| 完整會議的影片主機 | `h264media01.ly.gov.tw:443` 連不上（timeout）；Clip 走的 HiNet CDN 回 200 |

## 裁定

**裁定：沿用現有的兩個 port（下載片段 → 抽幀），不為 IVOD 發明新的抽象。**

原本以為 IVOD 應該「直接從 m3u8 抽幀」，那會需要把 `VideoSectionGateway` + `FrameExtractor` 合併成一個新 port，連帶改寫 `BuildDeckUseCase`、暫存清理與所有既有測試。實測發現 ffmpeg 從 HLS 切 4 秒片段只要 1 秒，現有形狀完全夠用——於是 **use case 一行都不用改**，IVOD 只是 `VideoSectionGateway` 的第二個實作。代價是多寫一個約 3 MB 的暫存片段，而那個暫存目錄現有的 `cleanup` 已經在管。

若錯，代價是每頁多花約 1 秒與一次磁碟寫入。

**裁定：用兩個「依來源分派」的 adapter 做路由，而不是兩個 use case 或在 UI 判斷網址。**

`BuildDeckUseCase` 持有一組 gateway。要支援第二個來源有三條路：讓 UI 選 use case（把來源知識洩進 presentation 層）、包一個 router use case（多一層同介面的東西）、或讓 gateway 自己分派。選第三個：兩個約 20 行的 adapter，摘要、渲染、設定、取消、佇列全部維持單一條 pipeline，只有「去哪裡拿字幕／片段」這件事分岔。

若錯，代價是來源變多時這兩個 adapter 會長成 if-else 串。

**裁定：`Transcript` 多一個 `source_note` 欄位，讓來源自己說出品質註記。**

現在的註記是 use case 從 `is_automatic` 推出來的。但 `is_automatic` 的真正意思是「YouTube 的滾動字幕」（決定要不要做滾動去重），不是品質標籤。IVOD 的逐字稿既不是滾動字幕、品質又確實需要註記（實測看到「朝野黨壇協商」，應為「黨團」）。新欄位讓來源自己講，use case 只在來源沒講時退回原本的判斷——YouTube 那條路行為完全不變。

**裁定：composition 抽出 `build_usecase(settings)`。**

為長期的 API 目標鋪路：未來的每日排程服務 import 這一個函式就能跑，不必碰 PySide6。這是純粹的搬移，沒有行為改變。

**裁定：完整會議（Full）照樣支援，只是會沒有截圖。**

影片主機連不上是立法院那邊的事，我們擋不住。現有的「部分截圖失敗仍然出片」策略剛好涵蓋這種情況——會產出一份沒有圖的摘要，並標明缺圖頁數。不特別為它寫程式碼。

若錯，代價是使用者貼完整會議時會等一輪失敗的片段下載（每頁數秒）才拿到無圖成品。

## 元件

```
usecases/sources.py         純函式 ivod_id(url)：認得出 IVOD 網址與其 id
infrastructure/ivod_api.py  IvodClient（單筆快取）+ IvodSubtitleGateway
infrastructure/ivod_sections.py  IvodSectionGateway：ffmpeg 從 m3u8 切片段
infrastructure/routing.py   BySourceSubtitleGateway / BySourceSectionGateway
composition.py              build_usecase(settings) + 路由接線
```

`IvodClient` 做單筆快取：字幕與片段兩個 gateway 都需要同一筆 record（一個要逐字稿、一個要 `video_url`），不快取就會對同一個 id 打兩次 API。

## 已知限制

- **逐字稿是 AI 產的**，有錯字；台語發言品質更差。成品會標註來源。
- **沒有 `ai-transcript` 的影片**會落到既有的語音辨識備援，但 yt-dlp 不認得 IVOD 網址，那條路會失敗並回報錯誤——不無聲吞掉。
- **畫質設定對 IVOD 無效**：API 只給一個串流網址。
- **8 小時的完整會議**逐字稿 32k 字，超過預設 `char_budget` 會被等比壓縮。IVOD 的 Clip（一位委員的一段發言）才是自然的摘要單位。
