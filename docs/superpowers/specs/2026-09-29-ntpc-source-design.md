# 新北市議會質詢片段來源 — 設計

日期：2026-09-29　狀態：定案（使用者授權實作；決策依 2026-09-29 的四方向研究與批判，研究原始檔在
scratchpad `ntp/research/`，全部對真實回應驗證過）

## 目標

接上第三個來源：新北市議會「議事影音隨選視訊系統」（
`https://vod.ntp.gov.tw/VodCloudV2/`）。來源代碼 `ntpc`，顯示名「新北市議會」。
做完：排程每天查新北；可回補過去三個定期會（約 290 段）；文章標政黨；前端可篩選。

## 研究結論（皆已驗證）

**一段影片是什麼**
- 業務質詢（議程字樣 `…各機關聯合業務報告及質詢-<黨團或個人>`）：每段是一個黨團的時段
  （`-國民黨團發言`、`-民進黨團聯合發言`）或一位議員的個人時段（`-李翁議員月娥`、
  `-馬見Lahuy．Ipin議員`）。
- 市政總質詢（議程字樣正好是 `市政總質詢`）：一個黨團的聯合質詢跨好幾天，每段是其中一個休息
  到休息之間的時間切片；90 段裡 87 段只含一個黨團。
- `發言議員` = 該段實際發言的議員＋主席。議長蔣根煌、副議長陳鴻源從不質詢，一律移除。
  業務質詢由審查會召集人主持，召集人若不屬於該時段標示的黨團，也會出現在名單裡。
- 名單依筆畫排序，不是發言順序。同一位議員的發言可能跨兩段。
- 逐段拆到個人：做不到。會議紀錄（逐人逐段）要 2.5 個月以上才上線，且沒有時間戳；講者姓名
  幾乎不會被念出來。

**清單怎麼抓**
- `POST https://vod.ntp.gov.tw/VodCloudV2/VOD/Search`（form，**9 個欄位都要送**，缺一個就被
  靜默忽略）：`pageindex=1&MJ=&MP=&MType=&sMDate=2026/09/16&eMDate=2026/09/16&Keyword=&cEPName=&Sort=MDate`。
  日期只能是 `yyyy/MM/dd`（其他格式回 500，而且那個 session 之後一直 500，要換 cookie）。
- 回 302 到 `/VodCloudV2/VOD/Index`，篩選條件存在 `ASP.NET_SessionId` session 裡，**要 cookie jar**，
  重導用 GET。
- 頁面回顯 `總筆數:N`（以及日期），用來確認篩選有生效。每頁 12 筆；下一頁
  `GET /VodCloudV2/VOD/ToPage?ToPage=N`（同一個 cookie）。只有一頁時沒有分頁列。
- 每張卡片以 `<div class="col-md-3 col-lg-2" style="display:block;">` 分隔：
  `href=ViewDetailMetaData/<guid>`（不加引號）、`<span class="timecode">HH:MM:SS</span>`、
  `…>屆次會期：第4屆第8次定期會<`、`議　　程：市政總質詢`（標籤中間是兩個 U+3000，比對前去掉所有
  空白）、`發言議員：周雅玲,林裔綺,…`（ASCII 逗號）、`開會日期：115-09-16`（民國年）、
  `開始時間：`、`結束時間：`。冒號是全形 U+FF1A。
- `GET /VodCloudV2/VodStream/VideoPlayer?assetID=<guid>&type=Book_SD`（**不需要 cookie**）內含
  `https://vodwms.ntp.gov.tw:443/NTP/_definst_/mp4:PlayAgenda/Book_SD/{KEY}/{KEY}{SEQ}.mp4/playlist.m3u8?device=PC&amp;kind=Guest`
  （`&amp;` 要還原）。`KEY={屆2}{次2}{R|T}{民國yyyMMdd}`、`SEQ` 三碼。**同一個檔案可能有兩個
  GUID**（重複上架），以檔案 key 去重。
- 在串流網址後加 `&wowzaaudioonly=true` 得到純音訊（約 64 kbps AAC，2 小時約 58 MB）。
- 人看的連結：`https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=<guid>`（不需要 session）。
- TLS：Python 3.13+ 要清掉 `VERIFY_X509_STRICT`（同臺中）。
- 現況：第4屆第8次定期會的質詢在 9/17 結束，之後是委員會審查與三讀；第 4 屆 115-12-24 結束。
  每日排程近期會找不到東西，近期價值在回補：4-8R 約 88 段、4-7R 109 段、4-6R 91 段。

**名冊**
- `https://www.ntp.gov.tw/councilor-all?program=37`：一次拿到全部 64 位。每個選區一個
  `<div class="review-meeting all-list" id="area{N}">`，裡面每位
  `<a href="councilor-detail?program=37&A={N}&C={id} ">`（引號前有空白）與 `<p> {name} </p>`。
  選區取 `area{N}`（個人頁上的選區只是回顯網址參數，不可信）。
- 個人頁 `councilor-detail?program=37&A={N}&C={id}`：`<li>政黨：{party}</li>`；`現任` 清單裡有
  `新北市第4屆議長`／`新北市第4屆副議長`／`新北市第4屆議員`。
- 黨團：`party-group-detail?program=39&P=1`（中國國民黨黨團）、`P=2`（民主進步黨黨團）、
  `P=3`（無黨團結聯盟黨團），成員以 `councilor-detail?…&C=` 連結列出。
- `C` 是跨屆穩定的個人編號。政黨 30/28/無政黨 3/無黨團結聯盟 2/民眾黨 1；黨團 31/28/5。
  **政黨與黨團有 4 人不同**：文章標的是政黨，黨團只拿來判斷時段裡誰不屬於該黨團。
- 姓名：名冊 `宋雨蓁 Nikar‧Falong`（空白、U+2027）對影音 `宋雨蓁Nikar．Falong`（U+FF0E）。
  正規化：去空白，`‧ · ・` 換成 `．`。之後 64/64 對得上。`無政黨` 正規化成 `無黨籍`。

## 決策

1. **文章單位：一個媒體檔一篇**，`ivod_id = "ntpc-" + 檔案key`（例：`ntpc-0408R1150916020`，20 字元）。
   `ivod_url` = `ViewMetaData?assetID=<該檔案第一個看到的 guid>`。
2. **只收質詢**：議程去空白後
   - 等於 `市政總質詢`，或
   - 含 `各機關聯合業務報告及質詢-`，或
   - 以 `市長施政報告` 開頭、符合 `市長報告.*總預算`、或含 `專案報告`（多黨混合，`NTPC_INCLUDE_MIXED`，預設開），
   
   而且長度 ≥ 180 秒。其餘（報告事項、三讀、討論議案、預備會議…）略過。
3. **講者**（依序）：拆逗號、正規化姓名 → 移除議長／副議長（名冊 role）→
   - 個人時段（議程以 `-X議員Y` 或 `-X議員` 結尾）：只留 `X+Y` 那一位；
   - 黨團時段（`-國民黨團發言`、`-民進黨團聯合發言`）：只留該黨團成員（名冊 caucus）；
   - 多黨混合時段：另外移除當天開場「報告事項」那段的單一名字（當天主席）；
   - `市政總質詢`：不再過濾。
   
   過濾後沒有人就不登記（之後重跑再看）。多位以「、」串起（同臺中聯合質詢）。
   名冊還沒同步時：只做「個人時段」與「移除已知主席」，log 一次警告。
4. **名冊**：`NtpcMemberSource` 進 `sync_members`；`Membership` 加 `role`（議長／副議長／空）與
   `caucus`（國民黨團／民進黨團／無黨團結聯盟／空）兩欄，其他來源留空。`ingest_ivod` 查新北前，
   若新北名冊是空的就先同步一次（排程原本只在整張表空時才先同步，這裡補上）。
5. **GPU 側**：認得 `vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=<guid>`（也接受
   `ViewDetailMetaData/<guid>`）。沒有逐字稿 → 走語音辨識（音訊用 `&wowzaaudioonly=true`），
   截圖用一般串流（HLS）。把臺中的 `TcccAudioGateway` 抽成通用的 `HlsAudioGateway`，
   路由加新北分支。GPU 回傳的 `video_id` 用 `ntpc-<guid>`（Pi 不檢查這個值，文章主鍵用檔案 key）。
6. **禮貌**：新北兩個網站的請求一律循序，間隔 1 秒。
7. 不做：會議紀錄對齊逐人切分（之後的回補校正選項）、審查委員會影音（ExamVOD）、第 5 屆換屆
   （115-12-25 後名冊重同步即可，編號 `C` 跨屆穩定）。

## 介面約定（三個部分都要遵守）

| 項目 | 值 |
|---|---|
| 來源代碼 | `ntpc` |
| 顯示名 | `新北市議會`；原片系統名 `新北市議會議事影音`；稱謂 `議員`；英文小標 `City Councilor` |
| 文章 `ivod_id` | `ntpc-<檔案key>` |
| 文章 `ivod_url`（也是送 GPU 的網址） | `https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=<guid>` |
| 文章 `meeting` | `屆次會期 + " " + 議程`（例：`第4屆第8次定期會 市政總質詢`） |
| API `source` 參數 | 接受 `ly`、`tccc`、`ntpc` |
| 設定 | `NTPC_ENABLED`（預設 true）、`NTPC_VOD_BASE`（`https://vod.ntp.gov.tw`）、`NTPC_WEB_BASE`（`https://www.ntp.gov.tw`）、`NTPC_INCLUDE_MIXED`（預設 true） |

## 測試

- GPU：網址辨識正反例；播放器頁與 ViewMetaData 頁解析（研究存下的真實頁當 fixture）；
  `HlsAudioGateway` 的 ffmpeg 參數與失敗轉譯；路由的新北分支；`video_id_of`。
- Pi：卡片解析（含 U+3000 標籤、民國日期、時長）、分頁、回顯檢查與 500 換 cookie、篩選規則、
  講者規則（個人時段、黨團時段、混合時段、議長移除、名冊空的退化）、檔案 key 去重；名冊解析
  （清單、個人頁、黨團頁、姓名正規化、無政黨→無黨籍、role）；`ingest_ivod --source ntpc`；
  API `?source=ntpc`。
- 前端：`npm run build`、`astro check`，假資料加一篇新北。
- 端到端（實跑）：9/16 的清單、一段 1 小時片段的音訊下載＋Whisper＋截圖。
