# 臺中市議會質詢片段來源 — 設計

日期：2026-09-28　狀態：已定案（使用者授權「全部交給你執行」，決策由本文記錄，可回頭推翻）

## 目標

在現有「立法院 IVOD → GPU 摘要 → 文章」的管線旁邊，接上第二個來源：
臺中市議會「議員個人質詢隨選視訊系統」（`vod.tccc.gov.tw`）。每位議員每場
質詢是一段獨立影片（15～60 分鐘），系統依議員分類、附會議名稱與日期。

做完之後：排程每天同時抓立法院與臺中市議會；文章卡片標示來源；可依來源篩選；
議員頁面的用語從「委員」變成「議員」。

## 探勘結論（皆實測，2026-09-28）

| 項目 | 結果 |
|---|---|
| 議員清單 | `GET /wb_region01.asp?url=11`，61 個 `index.asp?url=12&cno=<n>` 連結，連結文字是姓名 |
| 片段清單 | `GET /wb_region02.asp?url=12&cno=<n>[&ano=<m>&pageno=1]`，每列 `<a href="index.asp?url=12&cno=…&ano=…">會議名稱</a>` + 日期欄，每頁 10 筆、新到舊 |
| 選中片段 | 同一頁上方：`<iframe src="https://rds.ginnet.cloud/player/ncm/vod/…">`、「姓名 議員」、會議名稱、「會議日期：YYYY-MM-DD」、「影片長度：HH:MM」 |
| 串流 | 播放器頁 HTML 內嵌 `https://…akamaized.net/…/playlist.m3u8?iMda_seq=<n>`；純 curl、ffmpeg 預設 UA 皆 200，無 Referer 檢查 |
| 逐字稿 | `yishi.tccc.gov.tw` 的議事錄有依發言人拆的正式紀錄，但**沒有時間戳**、發言人清單要登入、明細端點回空；本版不用 |
| 片段長度 | 「00:50」是 **HH:MM**，實測 51:02。市政總質詢 ≈ 50 分鐘，業務質詢 ≈ 15 分鐘 |
| Whisper | large-v3-turbo CPU int8：3 分鐘音訊 42.5 秒（RTF 0.24），輸出正體中文，人名偶有同音錯字 |

推得的工作量：50 分鐘片段 ≈ 12 分鐘語音辨識 + 5 分鐘摘要；15 分鐘片段 ≈ 4 + 3 分鐘。

## 決策

1. **逐字稿用 Whisper**，不用議事錄。摘要與截圖都靠逐句時間戳，議事錄沒有；
   Whisper 已在 slidebox 裡（`FasterWhisperTranscriber`），只缺一條「從 HLS 抓音訊」的路。
   議事錄留作日後事實查核／校正人名的素材。
2. **CPU 跑 Whisper**（容器現況）。RTF 0.24 可接受；GPU 版（ctranslate2 + CUDA 函式庫）
   列為後續優化，不進本版。
3. **不改 `Article.ivod_id` 欄位名**，加 `source` 欄位區分來源。臺中片段的 id 寫成
   `tccc-<ano>`，避免與立法院的純數字撞。改名會動到 API、前端型別、測試與既有資料，
   對使用者沒有價值。
4. **每天遍歷 61 位議員的第一頁**來發現當天片段（61 次 HTTP，順序、逐一），
   不用議事錄或 YouTube。第一頁 10 筆 ≈ 兩個月的活動，回補幾天內都找得到。
5. **來源解析分兩處**：Pi（Django）只需清單與 metadata；GPU（slidebox）需要單一片段的
   播放器與 m3u8。兩邊各自解析同一支 ASP 頁，與現有 `ivod_source.py` / `ivod_api.py`
   的分工相同，不共用程式碼（兩個部署單位）。
6. **站名改成「質詢日報」**，報頭副標改成「立法院 · 臺中市議會」。原本的「立院質詢日報」
   在兩個來源並列後就不準了。若不喜歡改回一行字即可。
7. 篩選器的「委員」欄改稱「發言者」；文章與議員頁依 `source` 顯示「委員／議員」與機關名。
8. 法條引用照舊跑（議員也會引國家法律）；臺中市自治條例不在本版範圍。

## 架構

### GPU 側（`src/slidebox`）

新增 `usecases/sources.py::tccc_clip(url) -> TcccRef | None`：辨識
`vod.tccc.gov.tw/index.asp?url=12&cno=<n>&ano=<m>`（參數順序、大小寫不拘），回傳
`TcccRef(cno, ano)`；`tccc_id(url)` 回傳 `"tccc-<ano>"`。主機名完整比對，同 `ivod_id`。

新增 `infrastructure/tccc_api.py`：

- `TcccClient(fetch=_http_get, base="https://vod.tccc.gov.tw")`
  - `record(ref) -> TcccRecord(ano, cno, speaker, meeting, date, duration_seconds, player_url)`：
    抓 `wb_region02.asp?url=12&cno=&ano=&pageno=1`，解析「選中片段」區塊。快取最後一筆。
  - `video_url(ref) -> str`：抓 `player_url`，正規表示式取 `https://…m3u8?iMda_seq=…`。
    找不到丟 `NoSubtitlesAvailable`（與 IVOD 的 `video_url` 同一種失效語意，讓截圖階段降級成無圖）。
  - `TcccRecord.title`：`"{date} {speaker}議員－{meeting}"`，與 IVOD 標題同形。
- `TcccAudioGateway(client, runner=subprocess.run, ffmpeg_exe=None)`（實作 `AudioGateway`）：
  `download_audio(url, dest_dir, progress, is_cancelled)` 以 ffmpeg
  `-i <m3u8> -vn -ac 1 -ar 16000 audio.wav` 落地；長度由 wav 大小算（16-bit mono 16 kHz）；
  回傳 `AudioClip(path, video_id="tccc-<ano>", title, duration)`。ffmpeg 失敗轉成
  `NoSubtitlesAvailable`（訊息帶 stderr 前 200 字）。`cleanup` 刪目錄、不 raise。
  進度：ffmpeg 不好取百分比，只回報「下載音訊中…」文字。逾時 15 分鐘（60 分鐘片段的音訊
  串流下載實測遠低於此）。
- `NoSubtitlesGateway(reason)`（實作 `SubtitleGateway`）：一律丟 `NoSubtitlesAvailable`，
  讓 `BuildDeckUseCase` 走既有的語音辨識備援。

改 `infrastructure/ivod_sections.py`：把 `IvodSectionGateway` 的「由 url 取得串流」抽成
建構參數 `stream_for: Callable[[str], str]`，類別改名 `HlsSectionGateway`；保留
`IvodSectionGateway(client)` 為薄包裝（既有測試不動）。臺中用
`HlsSectionGateway(lambda url: client.video_url(tccc_clip(url)))`。

改 `infrastructure/routing.py`：三個 router（字幕、片段、**新增音訊**）都改成
`(default, ivod, tccc=None)`，`_pick(url)`：`ivod_id` → ivod；`tccc_clip` → tccc（未設定時退回
default）；否則 default。`cleanup` 對所有分支各呼叫一次（維持既有契約）。

改 `composition.py::build_usecase`：建 `TcccClient`，接上三個 router 的 tccc 分支。

改 `slidebox_api/runner.py::video_id_of`：`ivod_id(url) or tccc_id(url) or video_key(url)`。

改 `ollama_summarizer.py` 提示詞：「通常是立法委員的質詢」→「通常是立法委員或市議員的質詢」。

### Pi 側（`services/news`）

`models.py`：新增

```python
class ArticleSource(models.TextChoices):
    LY = "ly", "立法院"
    TCCC = "tccc", "臺中市議會"
```

`Article.source = CharField(max_length=16, choices=…, default=LY, db_index=True)`，
migration `0004_article_source`。既有資料全部落在 `ly`。

`ivod_source.py`：`IvodClip` 加 `source: str = "ly"`；`IvodUnavailable` 保留，
另取別名 `SourceUnavailable = IvodUnavailable`（臺中也丟它）。

新增 `tccc_source.py`：

- `TcccDailySource(base="https://vod.tccc.gov.tw", fetch=_http_get)`，`name = "臺中市議會"`
- `clips_for(day) -> list[IvodClip]`：
  1. `councilors()`：抓 `wb_region01.asp?url=11`，解析 `(cno, 姓名)`；抓不到丟 `SourceUnavailable`。
  2. 每位議員抓 `wb_region02.asp?url=12&cno=<n>` 第一頁，解析列 `(ano, meeting, date)`；
     單一議員頁失敗只記 log、跳過（不讓整天失敗）。
  3. 留下 `date == day` 的列；每一筆再抓一次 `…&ano=<m>&pageno=1` 取「影片長度」換算秒數
     （HH:MM → 分鐘精度）。取不到就 0。
  4. 組成 `IvodClip(ivod_id=f"tccc-{ano}", date, speaker=姓名, meeting, duration_seconds,
     ivod_url=f"{base}/index.asp?url=12&cno={cno}&ano={ano}", has_transcript=True, source="tccc")`。
- 解析全部用正規表示式（頁面是固定樣板的 ASP，沒有 JSON）。

`ingest.py`：`discover_days(days, sources: Sequence)`，對每個來源、每一天各跑一次
`discover`，錯誤訊息前綴來源名；`_upsert` 寫入 `source=clip.source`。
`IvodDailySource` 也加 `name = "立法院"`。

`management/commands/ingest_ivod.py`：組 `[IvodDailySource(...)] + ([TcccDailySource(...)] if settings.TCCC_ENABLED else [])`。
`settings.py`：`TCCC_ENABLED = _env_bool("TCCC_ENABLED", True)`、`TCCC_VOD_BASE`。

`api.py`：`/articles` 與 `/speakers` 加 `source` 查詢參數（值不合法回 422）；
卡片與詳情輸出 `source`；`SpeakerOut` 加 `source`（同名者依來源分列）。

### 前端（`web/news`）

- `lib/types.ts`：`ArticleSource = 'ly' | 'tccc'`；卡片、詳情、`Speaker` 加 `source`。
- `lib/sources.ts`：`SOURCE_LABEL`（立法院／臺中市議會）、`memberTitle(source)`（委員／議員）、
  `sourceSiteName(source)`（立法院 IVOD／臺中市議會隨選視訊）。
- `Filters.astro`：新增「來源」下拉（全部／立法院／臺中市議會）；「委員」欄改「發言者」，
  下拉選項顯示 `姓名（機關）`。
- `ArticleCard.astro`：新增來源小標籤（`SourceBadge.astro`）。
- `pages/article/[slug].astro`、`SlideBlock.astro`：原片按鈕文字依來源。
- `pages/speaker/[name].astro`：接受 `?source=`；文案用 `memberTitle`。
- `SiteHeader`/`SiteFooter`/`Base`/`index.astro`：站名「質詢日報」、副標「立法院 · 臺中市議會」、
  說明文字與資料來源致謝改成兩個來源。
- `lib/api.ts` 的離線假資料補 `source`。

### 部署

- `.env.docker.example`：`TCCC_ENABLED=true`。
- `compose.gpu.yaml` 已有 `whisper-cache` volume；README 新增「來源」一節，
  註明臺中片段長、每篇要 10～20 分鐘，`GPU_JOB_TIMEOUT_SECONDS` 預設 1800 對 60 分鐘片段偏緊，
  建議設 2700。

## 資料流

```
排程 04:00 ─▶ ingest_ivod ─▶ discover_days([LY, TCCC])
                                 │  TCCC：61 議員頁 → 當天列 → 每筆再抓長度
                                 ▼
                          Article(pending, source=tccc, ivod_id=tccc-14833)
                                 │ process_pending → GPU POST /jobs {url: index.asp?…&ano=14833}
                                 ▼
GPU  tccc_clip(url) ─▶ NoSubtitlesGateway ─▶ _transcribe:
       TcccAudioGateway: record → player → m3u8 → ffmpeg wav
       FasterWhisperTranscriber (CPU int8) → cues
     compress → 摘要 → HlsSectionGateway(m3u8) 截圖 → payload(video_id=tccc-14833)
                                 │
                                 ▼
Pi   save_result → READY → /api/articles?source=tccc → 前端卡片標「臺中市議會」
```

## 錯誤處理

| 情境 | 行為 |
|---|---|
| 議員清單頁抓不到 | `SourceUnavailable`，該來源該天記錯誤，立法院照常 |
| 單一議員頁抓不到 | log warning、跳過該議員 |
| 片段長度頁抓不到 | duration 0，仍登記 |
| GPU：播放器頁沒有 m3u8 | `NoSubtitlesAvailable` → 文章 failed（沒有音訊就沒有內容） |
| GPU：ffmpeg 抓音訊失敗／逾時 | 同上 |
| GPU：截圖階段 m3u8 失效 | 既有降級：無圖摘要 |
| Whisper 沒偵測到語音 | 既有：`NoSubtitlesAvailable` |

## 測試

- `tests/slidebox/test_sources.py`：`tccc_clip` 正反例（主機仿冒、缺參數、大小寫）。
- `tests/slidebox/test_tccc_api.py`：用 `tests/slidebox/fixtures/tccc_region02.html`、
  `tccc_player.html`（實頁裁剪）測 `record`、`video_url`、找不到 m3u8。
- `tests/slidebox/test_tccc_audio.py`：假 runner 驗證 ffmpeg 參數、wav 長度換算、失敗轉譯、cleanup。
- `tests/slidebox/test_routing.py`：三個 router 的 tccc 分支與未設定時退回 default。
- `tests/slidebox/test_ivod_sections.py`：`HlsSectionGateway` 用注入的 resolver；既有測試不變。
- `tests/slidebox_api/test_runner.py`：`video_id_of` 對臺中網址回 `tccc-<ano>`。
- `services/news/articles/tests/test_tccc_source.py`：FakeFetch 餵 fixture，驗證議員清單、當天過濾、
  長度換算、單頁失敗跳過、清單失敗丟 `SourceUnavailable`。
- `test_ingest.py`：多來源 discover、`source` 寫入。
- `test_api.py`：`?source=` 篩選、非法值 422、輸出含 `source`。
- 前端：`npm run build` 通過；手動在本機看首頁、篩選、文章頁、議員頁。

## 不做

- 議事錄（yishi）對齊、自治條例引用、GPU 版 Whisper、台北／新北議會、政黨標示
  （頁面上的政黨圖示沒有對照表）。
