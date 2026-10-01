# 立法院質詢每日摘要 — Django 後端

跑在 Raspberry Pi 上。做三件事：

1. 每天向立法院開放資料要當天的質詢片段
2. 把每一段送到 **GPU 主機上的摘要 API**（`serve_api.py`，在這個 repo 的根目錄），拿回詳細摘要與截圖
3. 用 django-ninja 把成品開成 news API，給 Astro 前端讀

Pi 上不跑任何模型，也不裝 slidebox——兩邊只透過 HTTP 說話。

## 跑起來

```bash
cd services/news
uv sync
uv run python manage.py migrate
uv run python manage.py runserver 0.0.0.0:8000
```

API 在 <http://localhost:8000/api/health>，互動式文件在 <http://localhost:8000/api/docs>。

管理後台（可選）：`uv run python manage.py createsuperuser`，然後開 `/admin/`。

`DEBUG=False` 時 `runserver` 不會服務靜態檔，admin 會是一頁沒有樣式的 HTML。開發時設 `DJANGO_DEBUG=1`（WhiteNoise 會直接從各 app 的 static 目錄找），或加 `--insecure`——admin 是把 `attempts` 歸零的唯一介面。

## 正式部署前一定要做的一件事

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

把結果設成 `DJANGO_SECRET_KEY`。這個專案的原始碼是公開的，預設值等於全世界都知道——用它簽 session 與 CSRF 等於沒簽，而且**不會有任何症狀**。所以 `DEBUG=False` 時只要金鑰還是預設值，wsgi/asgi 會直接拒絕啟動（`newsroom/guards.py`）。

離線工作（`migrate`、`ingest_ivod`、測試）不受影響——那些不對外服務，沒有這個風險。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `DJANGO_SECRET_KEY` | 開發用的假值 | **一定要設**；`DEBUG=False` 時沒設會直接拒絕啟動 |
| `DJANGO_DEBUG` | `False` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | 逗號分隔；要加 Pi 的位址 |
| `DJANGO_DB_PATH` | `services/news/db.sqlite3` | |
| `DJANGO_MEDIA_ROOT` | `services/news/media` | 截圖放這裡 |
| `DJANGO_SERVE_MEDIA` | `True` | 由 Django 直接服務 `/media`；前面有 nginx 時設 `False` |
| `DJANGO_MEDIA_CACHE_SECONDS` | `86400` | `/media` 回應的 `Cache-Control: max-age`。設 `0` 就不加快取標頭 |
| `CITE_LAWS` | `True` | 摘要卡的關鍵數字若講到某部法律某一條，從 LYAPI 抓條文原文附在旁邊。只附來源、不判對錯；關掉只是沒有條文連結 |
| `LYAPI_BASE` | `https://ly.govapi.tw/v2` | 法條資料來源（公民科技社群整理的立法院資料，非官方；每筆來源同時附全國法規資料庫的連結） |
| `GPU_API_BASE` | `http://localhost:8800` | GPU 主機上的摘要 API |
| `GPU_API_KEY` | 空 | 對應 GPU 端的 `SLIDEBOX_API_KEY` |
| `GPU_JOB_TIMEOUT_SECONDS` | `1800` | 等單一支影片的上限 |
| `INGEST_DAILY_LIMIT` | `20` | **每次執行**最多處理幾段（一段約 3～5 分鐘）。回補多天只是多查幾天的清單，處理上限不變。 |
| `INGEST_HOUR` | `4` | 常駐排程每天幾點跑 |
| `NTPC_ENABLED` | `True` | 每天也查新北市議會的質詢片段（`ingest_ivod --source ntpc` 不看這個設定） |
| `NTPC_INCLUDE_MIXED` | `True` | 新北的多黨混合時段（市長施政報告、總預算報告、專案報告）也收 |
| `NTPC_VOD_BASE` | `https://vod.ntp.gov.tw` | 新北市議會議事影音系統 |
| `NTPC_WEB_BASE` | `https://www.ntp.gov.tw` | 新北市議會官網（名冊：政黨、議長／副議長、黨團） |

## 每日匯入

```bash
# 預設抓「昨天」——立法院的 AI 逐字稿不是即時產生的，當天去抓通常是空的
uv run python manage.py ingest_ivod

uv run python manage.py ingest_ivod --date 2026-08-27
uv run python manage.py ingest_ivod --days 7        # 多查七天的清單（處理上限仍是 INGEST_DAILY_LIMIT）
uv run python manage.py ingest_ivod --discover-only # 只登記，不送去產生摘要
uv run python manage.py ingest_ivod --process-only  # 不查立法院，只把待處理的送出去
uv run python manage.py ingest_ivod --retry-imageless  # 重跑「一張截圖都沒有」的文章

# 新北市議會：回補第 4 屆第 6～8 次定期會（質詢在 2026-09-17 結束）
uv run python manage.py ingest_ivod --source ntpc --date 2026-09-17 --days 400 --discover-only
```

新北怎麼挑片段、講者怎麼認，見根目錄 README 的「新北市議會」一節與 `articles/ntpc_source.py`。

### 關於截圖

截圖是 GPU 主機上的 ffmpeg 直接從立法院的影片 CDN（`ivod-lyvod.cdn.hinet.net`）切片段。逐字稿走的是另一個端點，所以 CDN 拿不到畫面時摘要照樣產得出來，只有畫面會全缺，文章仍然會發佈（內文才是主體）。

**CDN 會擋 ffmpeg 預設的 User-Agent（`Lavf/…`），一律回 403**，換成瀏覽器的 UA 就是 200（2026-09-19 實測）。截圖程式已經改送瀏覽器 UA（`src/slidebox/infrastructure/ivod_sections.py`）。之前以為是 CDN「間歇性回 5xx」，很可能就是這個。如果又出現**每一篇都整批沒圖**，先比對這兩個指令的狀態碼，看是不是 CDN 改了規則：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -A "Lavf/61.7.100" "<m3u8 網址>"
curl -s -o /dev/null -w "%{http_code}\n" -A "Mozilla/5.0"   "<m3u8 網址>"
```

m3u8 網址是 `https://ly.govapi.tw/v2/ivods/<IVOD_ID>` 回應裡的 `video_url`。

問題排除之後可以手動補：

```bash
uv run python manage.py ingest_ivod --retry-imageless --limit 5
```

刻意不放進每日排程：重跑會連摘要一起重做，每篇要花幾分鐘的 GPU 時間，而截圖失敗的原因不見得等一等就會自己好。

整個流程以 `ivod_id` 為準做 upsert，所以**重跑是安全的**：已完成的不會重做，失敗的下一輪會再試（立法院的逐字稿有時晚幾小時才出現）。

### 排程

兩種都可以，跑的是同一段程式碼：

```bash
# 一、常駐排程（不想碰 systemd 的話）
uv run python manage.py run_scheduler                   # 每天 04:10；每次都回補三天的清單，接著重算人物側寫
uv run python manage.py run_scheduler --backfill-days 0 # 不要回補

# 二、系統排程（crontab -e）：匯入之後接著重算側寫；用 ; 而不是 &&，匯入失敗照樣重算
10 4 * * * cd /home/pi/yt-downloader/services/news && /home/pi/.local/bin/uv run python manage.py ingest_ivod >> /var/log/ly-news-ingest.log 2>&1; /home/pi/.local/bin/uv run python manage.py compute_profiles >> /var/log/ly-news-ingest.log 2>&1
```

## 人物側寫（投入量、具體度）

設計見 issue #24 與 `docs/superpowers/specs/2026-10-01-profile-indicators-step1-design.md`。原則：不加總、不排名、只跟同一議會同一會期的人比、樣本不足不給百分位、每個數字都能點回那幾篇文章。

```bash
uv run python manage.py compute_profiles
```

做三件事，整批重算、重跑是安全的：

1. 替還沒掛會期的文章掛上會期（從 `meeting` 解析：立法院 `第11屆第5會期`，臨時會併入所屬會期；議會 `第4屆第8次定期會`／`第4屆第2次臨時會`），並更新每個會期的**資料涵蓋範圍**（掛在那個會期的文章最早與最晚的日期，不是官方起訖）。解析不出來的（例如「立法院朝野黨團協商」）不掛、不計入，報告會列出各來源有幾篇已完成的文章因此沒算到。新文章在匯入登記時就會掛上。
2. 逐會期算每個人的指標，存進 `ProfileStat`（同一個 transaction 裡刪掉舊的、寫入新的）。母體是**同一屆**、任期與會期涵蓋範圍重疊的所有人，去掉新北名冊上那一屆的議長、副議長（只主持、不質詢）；母體裡沒發言的人投入量是 0。議會的任期沒有起訖日期，所以要看屆別：`sync_members` 遇到換屆（連任也算）會結束舊的一段、開新的一段，不會改寫舊任期的屆別與職位。新的一段從來源給的到職日開始，沒有就用那一屆的就職日（直轄市議會 12 月 25 日、立法院 2 月 1 日，每四年一屆）；已經提前離職的人，原本的離職日不會被改掉。立法院換屆（下一次是 2028-02-01）時要把 `LY_TERM` 改成新的屆別，否則新屆的名冊不會同步，新會期沒有同儕。只算已完成的文章。
3. 印出每個會期的人數，以及**對不到任期的講者**與篇數——講者是用「同來源、任期的姓名、任期涵蓋文章日期」對的（人物的別名不參與），對不到通常是名冊的寫法跟影音系統不同、或任期起訖沒涵蓋那篇的日期，要到 admin 的任期核對，再跑一次。對得到人、卻不在那個會期母體裡的講者（通常是任期的屆別或起訖不對）也會列出來。

立法院有一部分片段（全院委員會、公聽會，約 6%）沒有「會議資料」，舊版匯入時會議名稱是空的、掛不上會期。新匯入的會改用頂層的「會議名稱」；既有的文章跑一次回補：

```bash
uv run python manage.py backfill_meetings   # 逐篇向 LYAPI 查，一秒一個請求；補完再跑 compute_profiles
```

| 區塊 | 指標 | 公式 | n |
|---|---|---|---|
| 投入量 | 發言次數 | 講者包含他的文章數（聯合質詢每人各算一次） | 同值 |
| 投入量 | 發言總時長 | Σ（時長 ÷ 該篇講者人數）÷ 60，四捨五入到小數一位（用分數精確計算，同樣長的人才會同值） | 發言次數 |
| 具體度 | 每篇落地數字數 | 摘要卡 `key_numbers` 總數 ÷ 基礎文章數 | 基礎文章數 |
| 具體度 | 每篇帶期限的要求數 | `asks` 中期限非空的項數 ÷ 基礎文章數 | 基礎文章數 |
| 具體度 | 有來源的數字占比 | 有 `sources` 的數字 ÷ 數字總數 × 100 | 數字總數 |

具體度的基礎文章：單獨發言、而且有摘要卡（聯合質詢的卡片分不出是誰講的）。具體度 n < 5 時不給百分位；任何一項的同儕（母體中這一項樣本夠的人）少於 5 人時，誰都不給百分位。百分位 =（比他低的人數 ＋ 0.5 × 同值人數）÷ 同儕人數 × 100。

排程：`run_scheduler` 每晚匯入之後接著跑一次，包在自己的 try 裡，失敗只記 log、不影響匯入。部署這一版之後先手動跑一次，把既有的文章掛上會期。

## API

| 端點 | 說明 |
|---|---|
| `GET /api/health` | 文章數與最新日期 |
| `GET /api/articles?date=&speaker=&q=&source=&party=&session=&solo=&has_brief=&page=&page_size=` | 已完成的文章清單。`session`（會期 id）、`solo=1`（只要單獨發言）、`has_brief=1`（只要有摘要卡）是側寫的證據篩選：證據網址查出來的篇數等於指標的 n |
| `GET /api/articles/{slug}` | 單篇，含摘要卡（`brief`：一句話、關鍵數字、要求與回應；GPU 端產不出來時為 `null`）、每段的條列與完整敘述、完整逐字稿 |
| `GET /api/speakers` | 委員與篇數；`person_id` 是同來源、同名任期所屬的人（查無任期為 `null`） |
| `GET /api/people/{person_id}/profile?source=&session=` | 人物側寫：一個會期的投入量與具體度，每項附 n、百分位、同儕人數與證據網址（網站的相對路徑）。省略 `source` 用他最近一個有統計的會期的來源；省略 `session` 用他有發言的最近一個會期。沒有統計回 404 |

清單裡每張卡片的 `teaser` 優先用摘要卡的一句話，沒有卡片才退回第一段的完整敘述。

摘要卡裡的關鍵數字若帶 `law` 與 `article`（GPU 端已確認講者自己講了法律名稱與條號），落地後會從 LYAPI 抓發言當天有效版本的條文原文，放進 `sources`：講者引的那一條，加上最多兩條「內文提到它」的罰則條文（委員說第 24 條，罰鍰其實在第 106 條）。LYAPI 取不到時文章照常發佈，只是沒有 `sources`。

圖片欄位回的是**相對路徑**（`/media/articles/<ivod_id>/<批次>/01.webp`），由前端接上自己的 API base——回絕對網址要猜對外主機名，在反向代理後面很容易猜錯。

## 部署到 Raspberry Pi

三個 unit 各自獨立，**名字不要重複**（前端那份文件用的是 `ly-news-web`）：

```ini
# /etc/systemd/system/ly-news-api.service
[Unit]
Description=立法院質詢摘要 API（Django）
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/yt-downloader/services/news
Environment=DJANGO_SECRET_KEY=換成一串隨機字元
Environment=DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,pi.local
Environment=GPU_API_BASE=http://192.168.1.50:8800
ExecStart=/home/pi/.local/bin/uv run gunicorn newsroom.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 30
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/ly-news-scheduler.service
[Unit]
Description=立法院質詢摘要每日匯入
After=ly-news-api.service

[Service]
User=pi
WorkingDirectory=/home/pi/yt-downloader/services/news
Environment=GPU_API_BASE=http://192.168.1.50:8800
ExecStart=/home/pi/.local/bin/uv run python manage.py run_scheduler
Restart=always

[Install]
WantedBy=multi-user.target
```

啟動前跑一次 `uv run python manage.py collectstatic --noinput`：admin 的 CSS／JS 由 WhiteNoise 從 `staticfiles/` 服務，不必再用 `runserver --insecure`。Docker 映像在建置時已經做了這一步。

對外流量真正碰到 Django 的是截圖（Cloudflare 把 `media/*` 轉到這裡）。`/media` 的回應帶 `Cache-Control: public, max-age=86400, immutable`（`DJANGO_MEDIA_CACHE_SECONDS` 可調，設 0 關掉）：每次重跑都寫進新的子資料夾、網址跟著換，所以同一個網址的內容永遠不變，Cloudflare 邊緣快取一天之後 Pi 幾乎不再被圖片打到。

## 架構

```
articles/
├─ models.py        實體：Article / Slide / Person / Membership / Session / ProfileStat
├─ ivod_source.py   adapter：立法院開放資料
├─ gpu_client.py    adapter：GPU 主機上的摘要 API
├─ ingest.py        use case：發現 → 處理 → 落地（相依都用注入的）
├─ profiles.py      use case：會期解析、人物側寫指標的計算與快取
├─ api.py           presentation：django-ninja 端點
└─ management/commands/
   ├─ ingest_ivod.py    composition root：從 settings 組出 adapter 再注入
   ├─ compute_profiles.py
   ├─ backfill_meetings.py
   └─ run_scheduler.py
```

兩個 adapter 都可以注入替身，所以 `ingest.py` 的測試不碰網路。

## 測試

```bash
uv run python manage.py test articles
```
