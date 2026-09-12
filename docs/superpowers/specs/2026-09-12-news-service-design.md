# 立法院新聞服務（2026-09-12）

使用者要求：把既有的 slidebox 變成「新聞服務的材料」（可以是 FastAPI 的形式），另外開兩個專案——Django + django-ninja 的後端（提供 news API，並定時把立法院每日的內容轉成**詳細**摘要），以及 Astro 的前端（新聞頁，要夠 modern 夠 fancy）。

部署形狀是使用者指定的：

```
┌─ GPU 主機（Windows，已有 Ollama / ffmpeg / slidebox）
│    FastAPI  ── 吃一個 IVOD 網址，吐出結構化的摘要材料
│                （這是唯一需要 GPU 的東西）
│
└─ Raspberry Pi
     Django + django-ninja ── 每日排程抓立法院 → 呼叫 GPU 主機 → 存成文章 → 開 news API
     Astro                 ── 讀 news API，產出新聞頁
```

## 裁定

**裁定：FastAPI 不另開專案，放進既有 repo 的 `src/slidebox_api/`。**

它跑在 GPU 主機上，而那台機器就是桌面 app 所在的機器——所有相依（Ollama、ffmpeg、yt-dlp、faster-whisper、slidebox 本身）都已經裝好了。另開一個 uv 專案只是為了把同一台機器上的同一份程式碼再裝一次。使用者說的「2 個 project」是 Django 與 Astro；FastAPI 是「把前面做的東西變成材料」。

若錯，代價是日後要把 API 搬到另一台機器時得先拆出套件。

**裁定：Pi 上不放 slidebox 的任何程式碼。**

Django 只透過 HTTP 跟 GPU 主機說話。這讓 Pi 不必裝 PySide6／faster-whisper／yt-dlp（在 arm 上都是重量級），也讓兩邊可以各自演進。代價是立法院 API 的查詢程式碼在兩邊各有一份——但兩邊要的東西不同（slidebox 要單筆的逐字稿，Django 要每日清單），不是真正的重複。

**裁定：GPU API 是非同步工作佇列，不是同步呼叫。**

一支影片要 2～5 分鐘（詳細模式更久）。同步 HTTP 會在任何反向代理、任何 client 逾時下爆掉。改成 `POST /jobs` 回 job id、`GET /jobs/{id}` 查進度——這也剛好對應桌面 app 既有的進度回報。

**裁定：同一時間只跑一個 job。**

理由與桌面 app 的佇列完全相同：兩個生成會互搶 Ollama 與 ffmpeg。GPU 主機只有一張卡。

**裁定：圖片以 base64 隨 JSON 回傳，由 Pi 落地成檔案。**

每頁約 20～40 KB 的 WebP，一篇 4～10 頁＝100～400 KB。另外開一個圖片端點要處理保存期限、清理與授權，為了省下這幾百 KB 不值得。Pi 收到後存成 media 檔案，之後由 Django 直接服務。

**裁定：Astro 用 SSR（`@astrojs/node`）而不是 SSG。**

新聞每天增加，SSG 表示每天要重新建置整站；Pi 上那是分鐘級的浪費。SSR 讓 Astro 直接讀 Django API，新文章一進資料庫就看得到。流量是自用等級，Pi 撐得住。

**裁定：排程用 Django management command，並另外提供一個 APScheduler 常駐版本。**

`python manage.py ingest_ivod` 是可以獨立重跑、可以手動補跑的單位，也方便用 cron／systemd timer 排。另外給 `python manage.py run_scheduler` 讓不想碰 systemd 的人一個指令搞定。兩者跑的是同一段程式碼。

**裁定：摘要一律用詳細模式。**

使用者明確說「要有詳細內容的摘要」。新聞頁需要的正是每段的完整敘述——條列在網頁上讀起來太單薄。代價是生成時間約兩倍。

## GPU API（`src/slidebox_api/`）

```
GET    /health              → {ok, model, ollama_host, ollama_reachable, queued, busy}
POST   /jobs                → 202 {id, status}
GET    /jobs/{id}           → {id, status, progress_fraction, progress_status, error, result}
GET    /jobs                → 最近的工作
DELETE /jobs/{id}           → 取消（排隊中直接標記，執行中送取消訊號；兩者都查得到結果）
```

`POST /jobs` 的 body：`{url, detailed=true, min_slides, max_slides, model?}`。

`result` 就是新聞服務要的材料：

```json
{
  "video_id": "171180",
  "source_url": "https://ivod.ly.gov.tw/Play/Clip/1M/171180",
  "title": "2026-08-27 洪毓祥－第11屆第5會期第23次會議",
  "source_note": "逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
  "transcript_text": "00:00 主席 各位同仁…",
  "slides": [
    {"index": 1, "title": "…", "bullets": ["…"], "detail": "…",
     "timestamp": 0.0, "image_base64": "UklGR…", "image_media_type": "image/webp"}
  ]
}
```

認證：`X-API-Key` 對 `SLIDEBOX_API_KEY`；環境變數沒設就不驗（區網開發用）。

## Django（`services/news/`）

- `Article`：ivod_id（唯一）、slug、title、speaker、meeting、date、duration、ivod_url、source_note、transcript_text、status、error、時間戳
- `Slide`：FK Article、index、title、bullets(JSON)、detail、timestamp、image(FileField)

`ingest_ivod` 指令做三件事：查立法院當日的 Clip → 建立 pending 的 Article → 逐篇送去 GPU 主機並落地。每一步都可獨立重跑（以 ivod_id 為準的 upsert），失敗的留在 `failed` 狀態附錯誤訊息，下次會重試。

ninja API：

```
GET /api/health
GET /api/articles?date=&speaker=&q=&page=&page_size=
GET /api/articles/{slug}
GET /api/speakers
```

## Astro（`web/news/`）

讀 Django API；首頁是文章網格（日期／委員篩選），內頁是「每頁截圖 + 標題 + 條列 + 完整敘述」的時間軸，每個時間點可以跳回 IVOD 原片。深色優先、Tailwind。

## 不做的事

- 不做使用者系統、不做留言、不做全文檢索引擎（`q` 用資料庫的 icontains 就夠）。
- 不做完整會議（Full）：8 小時壓成十幾頁不是新聞，而且影片主機連不上、沒有截圖。只收 Clip。
- 不在 Pi 上跑任何模型。

## 審查後的修正

### C1：`/media` 在預設設定下根本沒被掛上，整站圖片全 404

`django.conf.urls.static.static()` 的第一行就是 `if not settings.DEBUG: return []`，而本專案的 `DEBUG` 預設是 `False`——照 README 部署的結果是每一張截圖都 404，而註解、settings、README、`.env.example` 四處都宣稱相反。

三邊的測試都沒抓到：pytest 不碰 Django，Django 的測試只斷言字串以 `/media/` 開頭，Astro 沒有測試。**斷言 URL 的形狀，不等於斷言 URL 能用。** 現在改用 `django.views.static.serve` 明確掛上，並補了真的去 GET 那張圖、比對位元組的測試。

### C2：在交易裡刪檔案，回滾後留下永久破圖

`save_result` 先刪舊截圖再刪 DB 列。之後只要有任何例外（重複的 slide index、SD 卡寫滿、SQLite 被鎖），DB 回滾把列救回來，但**檔案已經沒了**。結果是一批「欄位有值、檔案不存在」的破圖，而且 `imageless()` 看的是欄位是否為空——欄位非空，所以永遠不會被重試撿到；狀態也還是 READY。沒有任何路徑會修好它。

改成新截圖寫進一個新的子資料夾，舊檔案掛在 `transaction.on_commit` 上等提交成功才刪。留下孤兒檔比留下破圖便宜得多。順帶修掉一個假的保證：Django 的 storage 從不覆寫同名檔案，原本「重跑會覆蓋」的註解只是因為先刪掉了。

### 其餘

- `?date=abc` 會讓 Django 500（前端把訪客網址原樣轉手過來，任何爬蟲都能觸發）→ 參數改宣告成日期型別，交給 ninja 回 422。
- `_process` 只接兩種例外，其他錯誤會炸掉整批且靜悄悄 → 每篇各自接住，標記失敗後繼續。
- `PROCESSING` 不是鎖：排程與手動指令同時跑會把同一支影片送去 GPU 兩次 → 改用資料庫的條件式 UPDATE 搶所有權，並用時間判準回收卡住的項目。
- `--retry-imageless` 失敗會把已發佈的好文章打成 FAILED、立刻從站上消失 → 那條路徑失敗不降級。
- 沒有重試上限，永久失敗的文章每天燒一次 GPU → 加 `attempts` 與上限。
- `job_id` 沒有留下來，等待途中斷線就整支重跑 → 存進 `Article.gpu_job_id`，下一輪先問問看那個工作是不是已經好了。
- `JobStore` 先寫狀態再寫結果，而讀取端在鎖外逐一取屬性 → 會讀到「done 但沒有 result」，Pi 判定失敗、幾分鐘的 GPU 成品報銷。改成結果先寫、狀態最後寫，並提供鎖內快照。
- 每個工作都重建一次 use case，等於每次重付 40 秒的語音模型載入 → 整個行程只建一次。
- GPU API：金鑰改用 `compare_digest`（`==` 會由回應時間洩漏前綴）、網址限定 http(s)（否則 `file://` 就是讀本機檔案的管道）、監聽非 loopback 又沒設金鑰時啟動警告。
- 前端：圖片的 base 與 API 的 base 分開（SSR 用 127.0.0.1 沒問題，但那個位址送到手機上就是破圖）；外部連結先過 scheme 檢查（上游回 `javascript:` 就是點擊型 XSS）。
- Pi 停機跨過排程時間 = 那天的內容永久消失（記憶體排程沒有補跑的概念）→ 排程啟動時回補最近三天。
- 立法院 API 改 schema 與「今天休會」在下游長得一模一樣（都是發現 0 篇）→ 少了 `ivods` 欄位就明確報錯。

## 撰碼慣例（使用者要求）

- **狀態不用裸字串**：`ArticleStatus`（Django `TextChoices`）、`JobStatus`、`ItemStatus`、`VideoKind`、`Feature`、`Field` 都是列舉。立法院 API 的欄位名也集中在一個列舉裡，上游改名只要動一個地方。
- **嚴格 DI**：中間層不自己 `new` 相依、也不讀環境變數。`create_app` 的每個相依都是必填參數，組裝全部集中在 `serve_api.py`；Django 這邊的 composition root 是 management command。
- **註解與 docstring 說「為什麼」**：能從程式碼讀出來的事就不寫。
