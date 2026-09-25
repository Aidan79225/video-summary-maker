# 立院質詢日報（web/news）

立法院質詢的每日新聞網站前端。Astro + SSR（`@astrojs/node` standalone）+ Tailwind CSS 4 + TypeScript，
不含任何 UI 框架（沒有 React／Vue／Svelte），跑在 Raspberry Pi 上。

內容由後端（Django + django-ninja）提供，前端只負責取資料與排版。

---

## 需求

- **Node.js 22.12 以上**（Astro 7 的 `engines` 要求；開發機實測 v24.15.0）。
  Raspberry Pi OS 的 apt 給的是 Node 18，要用 [NodeSource](https://github.com/nodesource/distributions) 或 nvm 裝。
- npm（實測 11.12.1）

## 快速開始

```bash
cd web/news
npm install

# 開發（不需要後端，用內建假資料）
USE_FIXTURE=1 npm run dev          # http://localhost:4321

# 開發（連真的後端）
PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

Windows PowerShell 的環境變數寫法：

```powershell
$env:USE_FIXTURE = "1"; npm run dev
```

## 建置與正式啟動

```bash
npm run build                      # 產出 dist/server 與 dist/client
node ./dist/server/entry.mjs       # 等同 npm run preview / npm start
```

預設監聽 `http://localhost:4321`，可用 `HOST` / `PORT` 覆蓋。

## 文章頁的版面

- **摘要卡**在標題下方：一句話、關鍵數字、要求與回應。後端的 `brief` 是 `null` 時退回只顯示第一段導言。
- 每段的**完整敘述預設收合**，只露出小標與條列；段落列表上方的按鈕可以一次展開或收合全部。

## 環境變數

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `PUBLIC_API_BASE` | `http://localhost:8000` | 後端 API base，**SSR 在伺服器端用的**，可以填內網位址。結尾的斜線會自動去掉。 |
| `PUBLIC_MEDIA_BASE` | 同 `PUBLIC_API_BASE` | 圖片的 base。圖片網址會**原樣送到瀏覽器**，所以這裡要填手機連得到的位址；單機開發時兩者相同，不必設。 |
| `USE_FIXTURE` | `0` | 設為 `1` 時完全不連後端，改用 `src/fixtures/sample.json` 的假資料。 |
| `API_TIMEOUT_MS` | `8000` | 單一 API 請求的逾時毫秒數。後端沒起來時不會卡住整個頁面。 |
| `HOST` | `localhost` | 正式站監聽位址，對外服務要設 `0.0.0.0`。 |
| `PORT` | `4321` | 正式站監聽埠。 |

這些變數在**執行期**讀取（`process.env` 優先於建置時的 `import.meta.env`），
所以正式站改了 `PUBLIC_API_BASE` 只要 `systemctl restart`，**不需要重新 build**。

所有 API 呼叫都發生在伺服器端（SSR），瀏覽器不會直接連到 Django，
因此 `PUBLIC_API_BASE` 可以填內網位址（例如 `http://127.0.0.1:8000`）——但圖片是瀏覽器自己去抓的，那個位址手機連不到，所以要另外設 `PUBLIC_MEDIA_BASE`，或在 Pi 上用 nginx 同時代理 `/api` 與 `/media`。


## 假資料模式（USE_FIXTURE=1）

`src/fixtures/sample.json` 內含 4 篇完整的假文章（含分段重點、截圖路徑、逐字稿）。
啟用後：

- 完全不會對後端發出任何請求，`/api/articles`、`/api/articles/{slug}`、`/api/speakers`、`/api/health` 全部在本地模擬（含日期／委員／關鍵字篩選與分頁）。
- 頁面最上方會出現一條「示範資料模式」橫幅，避免把假資料當成真的。
- 截圖檔案實際上不存在，圖片會自動換成同尺寸的佔位方塊（版面不會塌）。

用途有兩個：後端還沒起來時仍可開發／驗收版面；以及 API 掛掉時還有一條可以 demo 的後路。

> 假資料中的人名、數字、發言內容**全部是虛構的**，只為了讓版面看起來像真的，請勿引用。

## 頁面

| 路徑 | 說明 |
| --- | --- |
| `/` | 最新報導。支援 `?date=`、`?speaker=`、`?q=`、`?page=`；第一頁無篩選時，最新一篇會以大版面呈現。 |
| `/article/[slug]` | 單篇報導。逐段排版（截圖＋小標＋條列＋完整敘述），每段可點時間戳連回 IVOD 原片，最後是可折疊的完整逐字稿。 |
| `/speaker/[name]` | 某位委員的報導列表（含分頁與其他委員快捷鍵）。 |
| 其他 | `src/pages/404.astro` |

IVOD 的網址不吃時間參數，所以時間戳只顯示 `mm:ss` 並連到 `ivod_url`（從頭播放），
連結的 `title` 有說明這件事。

## 韌性行為

後端連不上、回 5xx、回壞掉的 JSON、或回空清單，頁面都會 render 出可讀的中文訊息，
不會出現白畫面或未處理的 500。HTTP 狀態碼對照：

| 情況 | 狀態碼 | 畫面 |
| --- | --- | --- |
| 正常 | 200 | 內容 |
| 清單為空（有／無篩選條件） | 200 | 「目前還沒有任何報導」／「這個條件下沒有報導」 |
| 後端連不上、逾時、5xx、回應不是 JSON | 503 | 錯誤卡片（含「重新載入」與可展開的技術細節） |
| 文章不存在（後端回 404） | 404 | 「找不到這篇報導」 |
| 路由不存在 | 404 | 404 頁 |

回 503 而不是 200，是為了讓監控／健康檢查看得出後端掛了，同時使用者仍然看得到完整版面
（頁首、篩選器、錯誤說明都還在，可以直接改條件或重試）。

單篇文章即使少了 `slides` 或 `transcript_text` 也不會爆掉，會各自顯示對應的提示。

## 部署到 Raspberry Pi

### 方案 A：在 Pi 上建置（最單純）

```bash
cd /home/pi/yt-downloader/web/news
npm ci
npm run build
node ./dist/server/entry.mjs
```

### 方案 B：在別台機器建置，只把成品送上 Pi

```bash
# 開發機
npm ci && npm run build
rsync -av --delete dist package.json package-lock.json pi@raspberrypi:/srv/ly-news/

# Pi 上（只裝執行期需要的套件，tailwind／typescript 都不會裝）
cd /srv/ly-news && npm ci --omit=dev
node ./dist/server/entry.mjs
```

`dist/` 不是完全獨立的 bundle，執行時仍需要 `node_modules`（`astro` 與 `@astrojs/node` 的執行期相依），
所以兩種方案都要有 `node_modules`。建置才需要的套件（tailwindcss、typescript、@astrojs/check）
已經放在 `devDependencies`，`--omit=dev` 就不會裝到 Pi 上。

### systemd service 範例

`/etc/systemd/system/ly-news-web.service`：（後端那兩個叫 `ly-news-api` 與 `ly-news-scheduler`）

```ini
[Unit]
Description=立院質詢日報（Astro SSR 前端）
After=network-online.target
Wants=network-online.target
# 和 Django 裝在同一台時，讓它等後端先起來（沒有也不會壞，頁面會顯示「連不上內容伺服器」）
After=ly-news-api.service

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/srv/ly-news
Environment=NODE_ENV=production
Environment=HOST=0.0.0.0
Environment=PORT=4321
Environment=PUBLIC_API_BASE=http://127.0.0.1:8000
# 圖片網址會原樣送到瀏覽器，所以這一行要填**手機連得到**的位址，
# 而且那個 host 要加進 Django 的 DJANGO_ALLOWED_HOSTS。
Environment=PUBLIC_MEDIA_BASE=http://pi.local:8000
Environment=API_TIMEOUT_MS=8000
# 後端整個掛掉時要先撐住 demo，就把下面這行的註解拿掉
# Environment=USE_FIXTURE=1
ExecStart=/usr/bin/node /srv/ly-news/dist/server/entry.mjs
Restart=always
RestartSec=3
# Pi 記憶體有限，避免單一 process 吃爆
MemoryMax=512M
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ly-news-web

[Install]
WantedBy=multi-user.target
```

啟用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ly-news-web
sudo systemctl status ly-news-web
journalctl -u ly-news-web -f
```

改環境變數後只要 `sudo systemctl restart ly-news`，不必重新 build。

## 專案結構

```
web/news/
├─ astro.config.mjs          # output: 'server' + node standalone + tailwind vite plugin
├─ src/
│  ├─ layouts/Base.astro     # <head>、頁首頁尾、圖片載入失敗的全域 fallback
│  ├─ components/            # SiteHeader / SiteFooter / ArticleCard / Filters /
│  │                         # Pagination / SlideBlock / Transcript / Figure / Notice
│  ├─ lib/
│  │  ├─ api.ts              # 所有 API 呼叫、逾時、錯誤分類、假資料模式
│  │  ├─ types.ts            # 對應後端契約的型別
│  │  └─ format.ts           # mm:ss、日期、逐字稿拆行、query string
│  ├─ fixtures/sample.json   # 假資料（USE_FIXTURE=1）
│  ├─ pages/
│  │  ├─ index.astro
│  │  ├─ article/[slug].astro
│  │  ├─ speaker/[name].astro
│  │  └─ 404.astro
│  └─ styles/global.css      # 設計 token（深色優先）、中文排版、動態
└─ public/favicon.svg
```

## 設計

- 深色優先，淺色走 `prefers-color-scheme`；配色集中在 `global.css` 的 `--c-*` 變數。
- 標題用襯線字（Noto Serif TC → PingFang TC → Microsoft JhengHei），內文用黑體堆疊，行高 1.75～2。
- 手機優先，400px 寬度下完整可用。
- 進場淡入、hover 微上浮、圖片固定 `aspect-ratio` 佔位；全部尊重 `prefers-reduced-motion`。
- 所有圖片 `loading="lazy"`（首屏封面除外，用 `eager`）。

## 其他指令

```bash
npm run check    # astro check（TypeScript 型別檢查）
```
