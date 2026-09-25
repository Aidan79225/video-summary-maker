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
| `GPU_API_BASE` | `http://localhost:8800` | GPU 主機上的摘要 API |
| `GPU_API_KEY` | 空 | 對應 GPU 端的 `SLIDEBOX_API_KEY` |
| `GPU_JOB_TIMEOUT_SECONDS` | `1800` | 等單一支影片的上限 |
| `INGEST_DAILY_LIMIT` | `20` | **每次執行**最多處理幾段（一段約 3～5 分鐘）。回補多天只是多查幾天的清單，處理上限不變。 |
| `INGEST_HOUR` | `4` | 常駐排程每天幾點跑 |

## 每日匯入

```bash
# 預設抓「昨天」——立法院的 AI 逐字稿不是即時產生的，當天去抓通常是空的
uv run python manage.py ingest_ivod

uv run python manage.py ingest_ivod --date 2026-08-27
uv run python manage.py ingest_ivod --days 7        # 多查七天的清單（處理上限仍是 INGEST_DAILY_LIMIT）
uv run python manage.py ingest_ivod --discover-only # 只登記，不送去產生摘要
uv run python manage.py ingest_ivod --process-only  # 不查立法院，只把待處理的送出去
uv run python manage.py ingest_ivod --retry-imageless  # 重跑「一張截圖都沒有」的文章
```

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
uv run python manage.py run_scheduler                   # 每天 04:10；每次都回補三天的清單
uv run python manage.py run_scheduler --backfill-days 0 # 不要回補

# 二、系統排程（crontab -e）
10 4 * * * cd /home/pi/yt-downloader/services/news && /home/pi/.local/bin/uv run python manage.py ingest_ivod >> /var/log/ly-news-ingest.log 2>&1
```

## API

| 端點 | 說明 |
|---|---|
| `GET /api/health` | 文章數與最新日期 |
| `GET /api/articles?date=&speaker=&q=&page=&page_size=` | 已完成的文章清單 |
| `GET /api/articles/{slug}` | 單篇，含摘要卡（`brief`：一句話、關鍵數字、要求與回應；GPU 端產不出來時為 `null`）、每段的條列與完整敘述、完整逐字稿 |
| `GET /api/speakers` | 委員與篇數 |

清單裡每張卡片的 `teaser` 優先用摘要卡的一句話，沒有卡片才退回第一段的完整敘述。

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
├─ models.py        實體：Article / Slide
├─ ivod_source.py   adapter：立法院開放資料
├─ gpu_client.py    adapter：GPU 主機上的摘要 API
├─ ingest.py        use case：發現 → 處理 → 落地（相依都用注入的）
├─ api.py           presentation：django-ninja 端點
└─ management/commands/
   ├─ ingest_ivod.py    composition root：從 settings 組出 adapter 再注入
   └─ run_scheduler.py
```

兩個 adapter 都可以注入替身，所以 `ingest.py` 的測試不碰網路。

## 測試

```bash
uv run python manage.py test articles
```
