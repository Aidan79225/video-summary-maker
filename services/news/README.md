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
| `GPU_API_BASE` | `http://localhost:8800` | GPU 主機上的摘要 API |
| `GPU_API_KEY` | 空 | 對應 GPU 端的 `SLIDEBOX_API_KEY` |
| `GPU_JOB_TIMEOUT_SECONDS` | `1800` | 等單一支影片的上限 |
| `INGEST_DAILY_LIMIT` | `20` | 每次最多處理幾段（一段約 3～5 分鐘） |
| `INGEST_HOUR` | `4` | 常駐排程每天幾點跑 |

## 每日匯入

```bash
# 預設抓「昨天」——立法院的 AI 逐字稿不是即時產生的，當天去抓通常是空的
uv run python manage.py ingest_ivod

uv run python manage.py ingest_ivod --date 2026-08-27
uv run python manage.py ingest_ivod --days 7        # 補跑最近七天
uv run python manage.py ingest_ivod --discover-only # 只登記，不送去產生摘要
uv run python manage.py ingest_ivod --process-only  # 不查立法院，只把待處理的送出去
uv run python manage.py ingest_ivod --retry-imageless  # 重跑「一張截圖都沒有」的文章
```

### 關於截圖

立法院的影片 CDN（`ivod-lyvod.cdn.hinet.net`）會**間歇性回 5xx**——實測同一批片段前一小時還好好的，下一小時三個全部連不上。逐字稿走的是另一個端點，所以那種時候摘要照樣產得出來，只有畫面會全缺，文章仍然會發佈（內文才是主體）。

CDN 恢復之後可以手動補：

```bash
uv run python manage.py ingest_ivod --retry-imageless --limit 5
```

刻意不放進每日排程：重跑會連摘要一起重做，每篇要花幾分鐘的 GPU 時間，而 CDN 什麼時候恢復沒人知道。

整個流程以 `ivod_id` 為準做 upsert，所以**重跑是安全的**：已完成的不會重做，失敗的下一輪會再試（立法院的逐字稿有時晚幾小時才出現）。

### 排程

兩種都可以，跑的是同一段程式碼：

```bash
# 一、常駐排程（不想碰 systemd 的話）
uv run python manage.py run_scheduler          # 每天 04:10
uv run python manage.py run_scheduler --now    # 啟動時先跑一次

# 二、系統排程（crontab -e）
10 4 * * * cd /home/pi/yt-downloader/services/news && /home/pi/.local/bin/uv run python manage.py ingest_ivod >> /var/log/ly-news.log 2>&1
```

## API

| 端點 | 說明 |
|---|---|
| `GET /api/health` | 文章數與最新日期 |
| `GET /api/articles?date=&speaker=&q=&page=&page_size=` | 已完成的文章清單 |
| `GET /api/articles/{slug}` | 單篇，含每段的條列與完整敘述、完整逐字稿 |
| `GET /api/speakers` | 委員與篇數 |

圖片欄位回的是**相對路徑**（`/media/articles/171180/01.webp`），由前端接上自己的 API base——回絕對網址要猜對外主機名，在反向代理後面很容易猜錯。

## 部署到 Raspberry Pi

```ini
# /etc/systemd/system/ly-news.service
[Unit]
Description=立法院質詢摘要 API
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/yt-downloader/services/news
Environment=DJANGO_SECRET_KEY=換成一串隨機字元
Environment=DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,pi.local
Environment=GPU_API_BASE=http://192.168.1.50:8800
ExecStart=/home/pi/.local/bin/uv run python manage.py runserver 0.0.0.0:8000 --noreload
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/ly-news-scheduler.service
[Unit]
Description=立法院質詢摘要每日匯入
After=ly-news.service

[Service]
User=pi
WorkingDirectory=/home/pi/yt-downloader/services/news
Environment=GPU_API_BASE=http://192.168.1.50:8800
ExecStart=/home/pi/.local/bin/uv run python manage.py run_scheduler
Restart=always

[Install]
WantedBy=multi-user.target
```

`runserver` 是開發伺服器。自用流量沒問題，但要更穩的話換成 gunicorn：

```bash
uv add gunicorn
uv run gunicorn newsroom.wsgi:application --bind 0.0.0.0:8000 --workers 2
```

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
