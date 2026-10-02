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
| `TOPIC_DAILY_LIMIT` | `200` | 每晚最多替幾篇文章分政策領域（議題分布；一篇幾秒） |
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
uv run python manage.py run_scheduler                   # 每天 04:10；每次都回補三天的清單，接著分政策領域、重算人物側寫
uv run python manage.py run_scheduler --backfill-days 0 # 不要回補

# 二、系統排程（crontab -e）：匯入 → 分政策領域 → 重算側寫；用 ; 而不是 &&，前一步失敗後一步照跑
10 4 * * * cd /home/pi/yt-downloader/services/news && /home/pi/.local/bin/uv run python manage.py ingest_ivod >> /var/log/ly-news-ingest.log 2>&1; /home/pi/.local/bin/uv run python manage.py classify_topics >> /var/log/ly-news-ingest.log 2>&1; /home/pi/.local/bin/uv run python manage.py compute_profiles >> /var/log/ly-news-ingest.log 2>&1
# 每週日：名單 → 立法院院內紀錄 → 重算側寫（常駐排程的 weekly 跑的就是這三步）
30 3 * * 0 cd /home/pi/yt-downloader/services/news && /home/pi/.local/bin/uv run python manage.py sync_members >> /var/log/ly-news-ingest.log 2>&1; /home/pi/.local/bin/uv run python manage.py sync_ly_records >> /var/log/ly-news-ingest.log 2>&1; /home/pi/.local/bin/uv run python manage.py compute_profiles >> /var/log/ly-news-ingest.log 2>&1
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

## 人物側寫：議題分布

設計見 `docs/superpowers/specs/2026-10-03-profile-topics-step2-design.md`。模型只做一件事：把一篇報導分到 12 個固定政策領域的其中一個（主領域，外加可有可無的次領域）；篇數、占比、聚焦度、廣度都是程式算的。**模型參與的指標要有人工標註集，準確率不到門檻就不上線**——所以這一節有三段：每晚分類、標註與評估、上線條件。

### 分類器

- 政策領域寫在 `articles/topics.py`（`TOPICS`），是唯一的來源：代碼用在資料庫與網址，名稱給模型看、給讀者看。GPU 端不寫死，每次送工作時把清單帶過去（`POST /jobs`，`kind="topic"`、`text`、`labels`）。
- 分類器讀的文字是摘要卡的**一句話＋各段小標**，**不放會議名稱**：「財政委員會」幾乎會直接決定答案，立委的委員會職掌比對就變成自己比自己。admin 的標註頁顯示的是同一段文字（同一個函式產生）。
- 只分**基礎文章**：已完成、單獨發言、有摘要卡（跟具體度同一批）。聯合質詢分不出每個人講了哪個議題，沒有摘要卡就沒有一句話可讀。
- 結果存在 `Topic`，連同 GPU 回來的 `classifier`（模型＋提示詞版本，例如 `qwen3:14b#topic-v1`）。文章重產時 `Topic` 會被刪掉，下一輪重分；人工標註不刪。
- 分類失敗只記 log、不動文章；GPU 連不上就整輪停止（同摘要）。

```bash
uv run python manage.py classify_topics                # 只分還沒有 Topic 的，上限 TOPIC_DAILY_LIMIT（預設 200）
uv run python manage.py classify_topics --limit 50
uv run python manage.py classify_topics --reclassify   # 連已經有的也重分：沒有的先、再來是分得最久的
```

### 標註與評估

1. **抽樣**：每個來源從基礎文章裡隨機抽，建立空白的標註。已經抽過的不重抽，只補到每個來源 N 篇；同樣的資料、同樣的種子抽到同樣的文章。

   ```bash
   uv run python manage.py sample_topic_labels                 # 每個來源補到 20 篇，種子 0
   uv run python manage.py sample_topic_labels --per-source 30 --seed 7
   ```

2. **標註**：到 admin 的「議題標註」頁（`/admin/articles/topiclabel/`）。清單上直接顯示分類器會讀到的那段文字與文章連結（admin 的文章頁有完整的摘要與逐字稿，旁邊還有原始影片），主領域用下拉選單在清單上直接選，選完按最下面的「儲存」。右邊可以篩「還沒標」。這一頁**刻意不顯示模型的分類**（盲標）：看得到模型的答案，人就會跟著它標。標註只能靠抽樣產生，不能在 admin 手動新增——人手挑的文章會偏向好分的。

3. **評估**：把已標註的文章用現在的模型與提示詞重分一次，逐來源比對主領域。

   ```bash
   uv run python manage.py eval_topics
   ```

   - 準確率 = 主領域相同的篇數 ÷ 已標註篇數（分類失敗的那篇算錯）。
   - 通過 = 已標註至少 20 篇（`TOPIC_MIN_LABELS`），而且準確率 ≥ 80%（`TOPIC_MIN_ACCURACY`）。
   - 每個有標註的來源存一筆 `TopicEvaluation`，印出各來源的結果與每一筆判錯的（文章、人工、模型）。
   - 一次評估裡 GPU 回來的分類器必須都一樣，不一樣就中止、什麼都不存（中途換了模型）。GPU 連不上也一樣。
   - 評估不會改動任何文章的 `Topic`。上線的分類器因此換了的話，會立刻重算一次人物側寫，分布與證據清單才不會兜不攏。

### 上線條件

某來源的議題指標**只用**「該來源最新一筆**通過的**評估」的分類器分出來的 `Topic`。沒有通過的評估，這個來源完全沒有議題指標、API 不給這個區塊。之後試一個新模型沒通過，不會讓已經驗過的舊版本下架；但新分出來的 `Topic` 版本對不上，就不算，直到重新評估通過。

所以**換模型或提示詞**（GPU 端的提示詞改了就要升 `topic-vN`）的順序是：

```bash
uv run python manage.py eval_topics                                 # 先用新版本評估，不動既有的 Topic
uv run python manage.py classify_topics --reclassify --limit 100000 # 通過了才把全部重分成新版本
uv run python manage.py compute_profiles
```

新版本評估通過的那一刻起，舊版本分的 `Topic` 就不算了（`eval_topics` 會當場重算）：重分跑完之前，側寫上的議題分布是每個領域 0 篇、n＝0。所以重分要一口氣跑完，不要交給每晚 200 篇的排程慢慢補——那樣每晚重算出來的都是只算了一部分的分布。反過來先重分更糟：重分過的那幾篇被蓋成還沒通過的新版本，就不算了，重分到一半時排程重算，出來的是只算了一部分、看起來卻像完整的分布，直到新版本評估通過為止。

在 admin 刪掉通過的評估（例如發現標註有誤）：上線條件因此變了——退回較舊的通過版本，或整個下架——的話，也會當場重算側寫。

### 指標（區塊 `topics`）

基礎文章：該會期、已完成、單獨發言、有摘要卡、而且有「通過版本」的 `Topic`。

| 指標 | 公式 | n |
|---|---|---|
| 分布（`topic:<代碼>` 共 12 列） | 各領域的篇數；占比 = 篇數 ÷ 基礎文章數 × 100。不給百分位 | 基礎文章數 |
| 聚焦度 `topic_focus` | 最大占比 × 100（%） | 基礎文章數 |
| 廣度 `topic_breadth` | 占比 ≥ 10% 的領域數（個；用整數比較，剛好 10% 的算） | 基礎文章數 |
| 委員會職掌內的比例 `committee_alignment`（只有立法院） | 主領域落在他「那個會期」所屬委員會職掌的篇數 ÷ 有委員會資料的基礎文章數 × 100（%） | 有委員會資料的基礎文章數 |

最小樣本（n < 5 不給百分位）、同儕不足 5 人誰都不比、mid-rank 百分位，都跟投入量與具體度一樣。

委員會職掌對照（`topics.COMMITTEE_AREAS`，方法頁公開）。委員會資料來自 LYAPI `/legislators` 的「委員會」（「第11屆第5會期：財政委員會」），`sync_members` 每週同步時存進任期的 `committees`；一個會期可能同時在好幾個委員會，職掌取聯集。程序、修憲、經費稽核委員會沒有政策職掌；那個會期沒有委員會資料（或只有這三個）的，不算進分母。

| 委員會 | 領域 |
|---|---|
| 內政委員會 | 內政治安 |
| 外交及國防委員會 | 國防外交 |
| 經濟委員會 | 財政經濟、農業、環境能源 |
| 財政委員會 | 財政經濟 |
| 教育及文化委員會 | 教育文化、數位科技 |
| 交通委員會 | 交通建設、數位科技 |
| 司法及法制委員會 | 司法法制 |
| 社會福利及衛生環境委員會 | 衛生福利、勞動、環境能源 |

### 部署這一版之後

**先更新 GPU 主機**（重建映像、重啟 `serve_api.py`），再部署新聞服務。舊的 GPU 不認得 `topic` 工作，每篇都回 422；分類那一輪開頭連續三篇被拒就會停下來並在 log 說明，不會整晚重複同一個錯誤。

```bash
uv run python manage.py migrate
uv run python manage.py sync_members --source ly   # 補上立委的委員會（不然要等週日）
uv run python manage.py classify_topics            # 積壓很多的話每晚的排程會分批補完
uv run python manage.py sample_topic_labels
# 到 admin 標完之後
uv run python manage.py eval_topics
```

## 人物側寫：院內紀錄（出席、提案、表決；只有立法院）

設計見 `docs/superpowers/specs/2026-10-03-profile-records-steps4-5-design.md`。**不打模型**：資料是 LYAPI 的結構化紀錄，指標是程式計數。市議會沒有結構化的出席與表決資料，不做。

### 同步（`articles/ly_records.py`，指令 `sync_ly_records`）

```bash
uv run python manage.py sync_ly_records               # 這一屆（LY_TERM）的全部會期：約 30 個請求、一分鐘左右
uv run python manage.py sync_ly_records --session 5   # 只同步第 5 會期
uv run python manage.py sync_ly_records --term 11
uv run python manage.py compute_profiles              # 同步完要重算，網站才看得到
```

- **會議**：`/meets` 的「院會」「委員會」「聯席會議」（聯席會議是另一個會議種類；存成委員會，單位是參加的全部委員會）。每個會議代碼一筆：一場會議可以開好幾天，任何一天在出席名單上就算出席那一場（實測每天的名單都一樣）。出席名單：院會取「會議資料」每一天的出席委員；**委員會與聯席會議取「議事錄」的出席委員**——LYAPI 的委員會「會議資料」沒有出席名單（2026-10 實測，歷屆都是），議事錄的「列席委員」（不是那個委員會的人）不算。還沒有議事錄的會議，出席是「不知道」，不算進任何人的分母。全院委員會、公聽會、黨團協商、考察不算。經費稽核委員會的會議在 LYAPI 標成會期 0，對不到會期，不收（報告會舉例列出）。
- **議案**：`/bills?提案來源=委員提案`：主提案人、連署人、議案狀態、提案日期、議事網連結。**一件提案算在一讀那一次院會的會期**（「會議代碼」，例如「院會-11-1-10」），不用「會期」欄位——那是最新進度的會期：第 1 會期提、第 5 會期撤案的案子寫 5（第 11 屆 7,402 件裡有 176 件不同）。LYAPI 的會期篩選也是看最新進度，所以議案抓整屆（只同步一個會期時，抓最新進度在那個會期以後的）、在這裡篩。連署人在清單加 `output_fields=連署人` 就拿得到，翻完全部委員提案（第 11 屆十來頁）就有每一件的連署人，不必逐人逐會期查 `連署人=<姓名>`（那要上百個請求；實測兩種做法的件數相同）。
- **表決**：`/votes`（第 11 屆全部是記名表決）：每位委員的贊成、反對、棄權。`/votes` 不支援用會期篩選，`--session` 時整屆抓回來在這裡篩。表決時間有幾筆沒有年（「中華民國年1月21日」），用那場會議的日期補。
- 一秒一個請求、429 退避（伺服器給 `Retry-After` 就照它）。**全部抓完才寫、在同一個 transaction 裡寫**：任何一頁失敗、或翻完的筆數比 LYAPI 說的少（翻頁途中資料有變動），整次失敗、資料庫不動、指令以非零結束，下次排程再來。以唯一鍵（會議代碼、議案編號、表決代碼）upsert，重跑是安全的；同步的範圍（整屆或那個會期）裡 LYAPI 已經沒有的紀錄一起刪掉——LYAPI 改了代碼的那一筆才不會被算兩次（這次一筆都沒抓到的種類不刪，多半是 LYAPI 出了狀況）。委員提案按 LYAPI 的「會期」分批抓：一次查詢最多翻到第 10,000 筆（再翻回 413），第 11 屆的委員提案到屆末會超過。
- **會期**：每筆紀錄的「第{屆}屆第{會期}會期」就是第 1 步的會期；還沒有就建立。涵蓋範圍（起訖）仍以文章為準，沒有文章的會期留空。會期超出 1～8 的是 LYAPI 的資料錯誤，不收。
- **名字**：比對一律去空白、去間隔號（`ly_records.name_key`）：會議資料寫「伍麗華Saidhai Tahovecahe」，名冊與表決寫「伍麗華Saidhai‧Tahovecahe」。報告會列出對不到任何一段立法院任期的姓名（黨團提案的「…立法院黨團」不算）；先跑 `sync_members`，還對不到就是寫法不同，到 admin 核對任期的名字。
- **黨團**：`sync_members` 把 LYAPI 名冊的「黨團」存進任期的 `caucus`（「0無」存成空字串）。黨團不一定等於政黨：第 11 屆有 2 位無黨籍參加國民黨團。
- **排程**：`run_scheduler` 每週日 03:30 依序跑 `sync_members`、`sync_ly_records`、`compute_profiles`，每一步各包各的，前一步失敗後一步照跑。

### 指標（區塊 `chamber`「院內紀錄」）

都以「該會期、他在任期間」為準：任期是名冊的到職日、離職日。中途遞補、中途離職的人，不在任的那幾場會議、那幾次表決不算進分母。母體＝任期跟這個會期的紀錄期間（會議與表決的最早到最晚；剛開議、還沒有會議與表決的會期用提案日期）有重疊的立委。提案日期不算進期間：休會期間提、或卡在程序委員會的案子，提案日期可能比一讀早好幾個月，算進去的話早就離職的人也會跑進這個會期的母體。百分位、最小樣本（n < 5 不給百分位）、同儕不足 5 人誰都不比，都跟投入量一樣。

| 指標 | 公式 | n | 最小樣本 |
|---|---|---|---|
| 院會出席率 `plenary_attendance` | 出席的院會 ÷ 在任期間、有出席紀錄的院會 × 100（%） | 分母（場） | 套 |
| 委員會出席率 `committee_attendance` | 出席的「他那個會期所屬委員會」的會議 ÷ 那些會議 × 100（%）；聯席會議只要單位裡有他的委員會就算 | 分母（場） | 套 |
| 主提案數 `bills_proposed` | 他是提案人的委員提案件數 | 同值 | 不套 |
| 連署數 `bills_cosigned` | 他是連署人的件數 | 同值 | 不套 |
| 三讀數 `bills_passed` | 他主提案、議案狀態含「三讀」的件數（「審查完畢(三讀)」也算） | 同值 | 不套 |
| 投票出席率 `vote_participation` | 他有投票的記名表決 ÷ 在任期間的記名表決 × 100（%） | 分母（次） | 套 |
| 與所屬黨團一致率 `caucus_agreement` | 他的票跟所屬黨團多數相同的表決 ÷ 他有投票、而且黨團有多數的表決 × 100（%） | 分母（次） | 套 |
| 跨黨投票數 `caucus_defections` | 他的票跟所屬黨團多數不同的表決數 | 同值 | 不套 |

- **黨團多數**：那次表決中，同黨團有投票的人裡票數最多的選項（贊成、反對、棄權）；並列就沒有多數，那次不算進一致率與跨黨投票。黨團看他「那一天」那段任期上的 `caucus`。**沒有參加黨團**的人，一致率與跨黨投票沒有值（n＝0），API 的 `reason` 是 `no_caucus`（compute 把他的跨黨投票數存成 null，有黨團、從不跨黨的人是 0）。
- 提案算在一讀的會期：休會期間提的案在下一個會期一讀。休會期間就離職的人不在下一個會期的母體裡，那幾件不會出現在任何人的側寫上（他在那個會期不是立委）。
- 只有院內紀錄、還沒有任何文章的會期（例如剛開議的），側寫只給 `chamber` 區塊：投入量不是 0，是沒有資料。
- 不加總、不排名照舊。出席、投票多不代表比較好，跟黨團不一致也不代表好或壞。
- **限制**：院長、副院長主持院會、依慣例不投票，名冊上卻掛在一個委員會——他們的投票出席率與委員會出席率會接近 0（第 11 屆第 5 會期：韓國瑜 0% 與 2.9%）。LYAPI 名冊沒有職位，目前沒有排除，要在方法頁說明。名冊只給現在的黨團：同一段任期內換黨團（但沒有換黨）的人，以前的表決也用現在的黨團算；這一版之前就因為換黨切段、已經結束的舊任期沒有黨團資料，那段期間的表決不算進一致率與跨黨投票。院內紀錄的清單是即時算的、指標是重算時存的：同步之後到下一次重算之間（每週日同步完會立刻重算），清單筆數可能跟指標差幾筆。LYAPI 的紀錄有延遲（委員會的議事錄常晚好幾週）。

### 紀錄清單（證據）

`GET /api/people/{id}/records?session=&kind=`：一個人、一個會期、一類紀錄，新的在前。每個院內紀錄指標的 `evidence_url` 是網站的 `/records/{person_id}?session={id}&kind=…`，網站照它呼叫這個端點。筆數 `count` 等於對應指標的 n 或值（兩邊用同一個 `chamber.SessionRecords` 算，測試逐人逐項驗）。

| kind | 對應指標 | 筆數等於 | 每筆附 |
|---|---|---|---|
| `plenary` | 院會出席率 | n | 會議代碼、日期、名稱、議事網的會議頁、`attended` |
| `committee` | 委員會出席率 | n | 同上 |
| `proposed` | 主提案數 | 值 | 議案編號、提案日期、名稱、議事網的議案頁、`status`、`proposers` |
| `cosigned` | 連署數 | 值 | 同上 |
| `passed` | 三讀數 | 值 | 同上 |
| `votes` | 投票出席率 | n | 表決代碼、日期、表決議題、會議代碼、那場院會的會議頁、`vote`（他的票，沒投是 null）、`caucus_majority`（他黨團的多數，並列或沒有黨團是 null） |
| `caucus_votes` | 與所屬黨團一致率 | n | 同上 |
| `defections` | 跨黨投票數 | 值 | 同上 |

`caucus_votes`（他有投票、黨團有多數的表決）是設計列的七類之外加的：一致率的 n 跟投票出席率的 n 不一樣，沒有這一類就點不回「n 筆」。回應另有 `label`（這一類的名稱）、`indicator`（對應的指標 key）、`caucus`（他在這個會期的黨團，空字串是沒有）。人或會期不存在、不是立法院的會期、這個會期沒有他的院內紀錄：404；`kind` 打錯：422。

### 部署這一版之後

```bash
uv run python manage.py migrate
uv run python manage.py sync_members --source ly   # 補上黨團（不然要等週日）
uv run python manage.py sync_ly_records
uv run python manage.py compute_profiles
```

## API

| 端點 | 說明 |
|---|---|
| `GET /api/health` | 文章數與最新日期 |
| `GET /api/articles?date=&speaker=&q=&source=&party=&session=&solo=&has_brief=&topic=&page=&page_size=` | 已完成的文章清單。`session`（會期 id）、`solo=1`（只要單獨發言）、`has_brief=1`（只要有摘要卡）、`topic`（領域代碼，或 `any`＝哪個領域都可以）是側寫的證據篩選：證據網址查出來的篇數等於指標的 n。`topic` 只認各來源通過評估的那個分類器分出來的領域；打錯的代碼回 422 |
| `GET /api/articles/{slug}` | 單篇，含摘要卡（`brief`：一句話、關鍵數字、要求與回應；GPU 端產不出來時為 `null`）、每段的條列與完整敘述、完整逐字稿 |
| `GET /api/speakers` | 委員與篇數；`person_id` 是同來源、同名任期所屬的人（查無任期為 `null`） |
| `GET /api/people/{person_id}/profile?source=&session=` | 人物側寫：一個會期的投入量與具體度，每項附 n、百分位、同儕人數與證據網址（網站的相對路徑）。那個來源有通過的議題評估時多一個 `topics` 區塊：`distribution`（12 個領域都列，依篇數由多到少、同數依領域表的順序；`share` 是 0～100）、`classifier`（`name`、`accuracy` 是 0～1、`labeled`、`evaluated_at`）。省略 `source` 用他最近一個有統計的會期的來源；省略 `session` 用他有發言的最近一個會期。沒有統計回 404。立法院同步過院內紀錄的會期多一個 `chamber` 區塊（見上面「院內紀錄」）；只算過院內紀錄的會期不給投入量與具體度 |
| `GET /api/people/{person_id}/records?session=&kind=` | 院內紀錄的證據清單（院會、委員會會議、主提案、連署、三讀、記名表決、黨團有多數的表決、跨黨投票），每筆附議事網連結；筆數等於對應指標的 n 或值 |

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
├─ models.py        實體：Article / Slide / Person / Membership / Session / ProfileStat / Topic / TopicLabel / TopicEvaluation / LyMeeting / LyBill / LyVote
├─ ivod_source.py   adapter：立法院開放資料
├─ gpu_client.py    adapter：GPU 主機上的摘要 API
├─ ingest.py        use case：發現 → 處理 → 落地（相依都用注入的）
├─ profiles.py      use case：會期解析、人物側寫指標的計算與快取
├─ topics.py        use case：政策領域、分類、標註集與評估、上線條件、委員會職掌
├─ ly_records.py    adapter：LYAPI 的會議出席、委員提案、記名表決（同步與寫入）
├─ chamber.py       use case：院內紀錄的指標與證據清單
├─ api.py           presentation：django-ninja 端點
└─ management/commands/
   ├─ ingest_ivod.py    composition root：從 settings 組出 adapter 再注入
   ├─ compute_profiles.py
   ├─ sync_ly_records.py
   ├─ classify_topics.py
   ├─ sample_topic_labels.py
   ├─ eval_topics.py
   ├─ backfill_meetings.py
   └─ run_scheduler.py
```

兩個 adapter 都可以注入替身，所以 `ingest.py` 的測試不碰網路。

## 測試

```bash
uv run python manage.py test articles
```
