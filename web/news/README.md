# 立院質詢日報（web/news）

立法院質詢的每日新聞網站前端。Astro + SSR（`@astrojs/node` standalone）+ Tailwind CSS 4 + TypeScript，
不含任何 UI 框架（沒有 React／Vue／Svelte），跑在 Raspberry Pi 上。

內容由後端（Django + django-ninja）提供，前端只負責取資料與排版。

---

## 需求

- **Node.js 22.12 以上**（Astro 7 的 `engines` 要求；開發機實測 v24.15.0）。
  Raspberry Pi OS 的 apt 給的是 Node 18，要用 [NodeSource](https://github.com/nodesource/distributions) 或 nvm 裝。
- npm（實測 11.12.1）
- 跑測試（`npm test`）要 **Node.js 22.18 以上**：測試直接載入 `.ts`，靠 Node 內建的型別抹除（22.18 起預設開啟），不加任何套件。

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

- **摘要卡**在標題下方：一句話、關鍵數字、要求與回應。後端的 `brief` 是 `null` 時退回只顯示第一段導言。關鍵數字若帶條文來源（`sources`），數字下方有「醫療法 第一百零六條」的連結，卡片底下收合著條文原文與官方連結；只附來源，不判對錯。
- 每段的**完整敘述預設收合**，只露出小標與條列；段落列表上方的按鈕可以一次展開或收合全部。
- **影片**（來源 `youtube`，YouTube 手動加入的）跟議會的文章差在幾處：沒有「會議」欄，長度是 0（只拿到標題與頻道）時也不顯示長度；摘要卡只留一句話與關鍵數字，**不顯示「要求與回應」**（那是議會質詢的版型：「未定期限」「未明確回應」是網站替沒有期限、沒有回應的要求補的字樣，影片裡沒有被質詢的對象，印出來就變成一句沒有根據的指控）；標題、頻道名稱與頻道晶片可以在任何字之間斷行（YouTube 的標題常有很長的網址與標籤，不斷行的話手機的版面會左右捲動）；標題原樣顯示，不套 `displayTitle`（它會把開頭的日期拿掉，影片標題開頭的日期是標題的一部分）；頁首固定一則備註「由 AI 依影片自動整理，可能有誤，以原影片為準」並連回 YouTube；每段的時間戳連到 `ivod_url` 加上 `t=<秒>s`，點了直接從那一段開始播；頁首的返回鍵回 `/videos`；整頁帶 `<meta name="robots" content="noindex">`。標籤、「觀看YouTube原片」與時間戳的提示都從來源表（`lib/sources.ts`）查，不寫在頁面裡。
- **多人影片**（用聲音區分說話者的 YouTube 影片）：後端多給 `speakers`、`diarization` 與每段的 `points`，網站**只顯示、不推測**——名字與標籤都是後端代好的，沒有人確認過的名字不會出現；不認得的、缺的一律往「少說」退（不認得的身分依據當「未確認身分」、看不懂的診斷狀態當 `warn`）。
  - 頁首：頻道晶片（上傳者，連到頻道頁）前面寫「頻道」，跟說話者分開；那兩個字不加透明度，字色跟頻道名稱一樣（淺色底 5.7:1、深色底 10.3:1，AA 要 4.5:1；`tests/contrast.test.ts` 直接讀 `global.css` 的 token 算兩種配色，之後改了色碼讓對比掉下去就會紅）。往下依序是 **出場**（參與者在前、短暫的「其他聲音」在後；每位附編輯設的角色，與一枚文字的身分依據：`未確認身分`／`已由編輯確認`）與一則**說明列**（固定一句「說話者由聲音自動區分，可能分錯；名稱未經確認時只以標籤稱呼」；狀態是 `warn` 時，每個警告碼再加一句白話：人數與指定的不同、有不少片段無法判斷是誰在說話、出現多個短暫的聲音、只偵測到一位說話者；有不認得的碼，或 `warn` 卻沒有任何認得的碼，補一句通用的，警告不會因為看不懂而消失）。**不顯示任何數字或百分比。**
  - 說話者的記號是「形狀＋文字」（顏色只是輔助，從不單靠顏色；文字就是無障礙名稱）。短暫的聲音與後端代入的「（無法判斷）」「（未知發言者）」畫成空心形狀＋虛線框。**影片的說話者不連到 `/speaker/`**（「發言者 A」不是人，編輯設的名字也沒有名冊可以對）。
  - 每段：有 `points` 的段落把每個重點標上說話者與帶 `t=` 的時間連結（跳到那一點，不是整段開頭），取代平的條列；重點的文字可以在任何字之間斷行（雜湊、很長的網址不會把整頁撐寬，手機 320 px 也不會左右捲動）；`start` 超過一天（契約的 `MAX_SECONDS`）的重點當作沒有時間：不顯示時間、不產生連結，重點本身還在；截圖下面寫「畫面僅為該時間點的截圖」（畫面上的人不一定是在說話的人）。沒有 `points` 的段落照舊畫條列。某一點的說話者名稱若跟出場名單裡那個編號的顯示名對不上（後端自己矛盾），那一點畫成中性的「（未知發言者）」，不借用任何一位的記號——不替可能不對的人背書。
  - 逐字稿：上方有說話者對照；相鄰、同一位說話者的行併成一塊，晶片只畫一次。一行是 `mm:ss 顯示名：內文`（分鐘可以超過 99），**只有行首剛好是出場名單裡的顯示名（或後端代入的兩個固定字樣）加上全形冒號才拆**（顯示名自己含全形冒號也認得）；兩個顯示名都吻合行首（「A」與「A：B」）時，「A：B：你好」分不出是 A 說了「B：你好」還是「A：B」說了「你好」，整行當內文，不猜——議會的「主席：」、內文裡的冒號、對不上名字的一律整行當內文。
  - 列表卡片：API 給 `participant_count`（正整數，不超過契約的 24 位說話者）時寫「2 位說話者」，純文字；沒給、不是正整數或超過 24 就跟以前一樣。那一格自己帶全強度的 `text-muted`（頁尾其他格繼承的 `text-muted/80` 在淺色底只有 3.3:1，新加的這一格要 AA：4.8:1）。
  - 舊版 API 沒有這幾個欄位、型別壞掉、或還留著 `[[Sn]]` 佔位符時，`lib/api.ts` 的 `normalizeArticleDetail` 都會補成安全的形狀（每個字串先清過；殘留的完整佔位符換成「（未知發言者）」、尾端的殘片拿掉），頁面照舊版的樣子 render，原始佔位符到不了頁面。**議會文章與單人影片完全不變。**

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

`src/fixtures/sample.json` 內含 9 篇完整的假文章（立法院 3 篇、臺中 1 篇、新北 1 篇、YouTube 4 篇；含分段重點、截圖路徑、逐字稿）。
啟用後：

- 完全不會對後端發出任何請求，`/api/articles`、`/api/articles/{slug}`、`/api/speakers`、`/api/people/{id}/profile`、`/api/health` 全部在本地模擬（含日期／委員／關鍵字／會期／單獨發言／有摘要卡／主領域篩選與分頁）。
- 人物側寫：`people`（名冊）、`sessions`（會期）、`profiles`（每人每會期的指標）三段，文章多兩個假資料才有的欄位：`session_id`（掛在哪個會期）與 `topic`（評估通過的分類器給的主領域；聯合質詢與沒有摘要卡的沒有）。議題分布在 `profiles` 裡只寫有篇數的領域，其餘領域、占比與連結由 `api.ts` 照後端的規則補齊。看版面用這幾頁：
  - `/speaker/範例一?source=ly`：預設會期（第11屆第5會期）照假文章算，投入量有百分位、具體度與議題分布的指標全是「樣本不足」；議題分布只有一條長條（國防外交 1 篇），其餘 11 個領域收成「其他領域都是 0 篇」一行。切到第11屆第4會期可以看到有百分位的具體度、「同儕不足」，以及五個領域的分布與有百分位的聚焦度、廣度、委員會職掌內的比例（這一期沒有對應的假文章，「看這 N 篇」與每個領域的連結都是空清單）。
  - 追問（`/speaker/範例一?source=ly&session=2#followups`）：判斷器通過評估的版面——有百分位的追問率、已追問／未追問／觀察中／待追蹤四組、已追問附再提的那篇與引用、「期限寫法換算不出日期」的項數。這一期的出處文章都是編的，點進去是 404（只有「無人機交機進度」那一項的再提指到真的假文章）。預設會期（第11屆第5會期）只有一項觀察中、追問率沒有分母。
  - `/speaker/範例二?source=ly`：判斷器**沒通過**評估的追問區塊——沒有追問率，只有「待追蹤」一組並寫明比率要等人工驗證。真的後端只有一個追問判斷器，兩種版面並存只是為了看版面。
  - `/speaker/範例五?source=ntpc`：新北的聯合質詢，時長排在次數前面、具體度沒有分母；沒有議題分布（示範分類器沒通過評估的議會整塊不顯示）；追問區塊是空的——聯合質詢裡的要求不算給任何人，追問率 n＝0。
  - `/speaker/範例六?source=ntpc#followups`：換了判斷器之後的追問區塊——有兩項還是舊的判斷器判的，列在「待重判」一組，追問率卡片沒有值、寫明正在重新判斷（API 的 `reason` 是 `rejudging`）。這幾項要求與出處文章都是編的，點進去是 404。
  - `/speaker/範例三`（有 person_id、沒有側寫）與 `/speaker/範例四`（不在名冊）：側寫整段不顯示。
- 院內紀錄（立委）：`src/fixtures/records.json` 存範例一兩個會期的假紀錄（會議出席、主提案、連署、記名表決），`/api/people/{id}/records` 也在本地模擬，回的是後端 `RecordListOut` 的形狀（`id`、`title`、中文的票），走的是正式站的整理路徑。三讀、黨團有多數與跨黨投票的清單、各指標的值與 n 都由 `src/lib/records.ts` 照後端的公式從這些紀錄算，所以證據頁的筆數一定等於指標的 n；只有同儕人數與百分位是編的。
  提案與質詢一致率：主提案多一個假資料才有的 `area`（議案分類器給的領域代碼；沒有的是還沒分類），清單照後端的形狀放在每件的 `topic`（`{key, label}`，只有主提案清單標），並附 `bill_classifier`；質詢那一邊拿 `sample.json` 同一個會期的議題分布，一致率照後端的重疊公式算，卡片的 `speech_n`（質詢篇數）也照後端給。`records.json` 的 `bill_classifier` 是假資料的議案分類器與它的評估，照後端只認過門檻的：拿掉它或把 `accuracy` 改到 0.8 以下，就看得到議案沒有領域、也沒有一致率卡片的版面；沒有議題分布的側寫（新北）也沒有這張卡。看版面用：
  - `/speaker/範例一?source=ly` 側寫最後的「院內紀錄」：出席、提案、記名表決三組卡片，每張卡連到 `/records/1?session=3&kind=…`。提案組最後是「提案與質詢一致率」：這一期他只有 1 篇分過議題的質詢，卡片沒有值，寫「他這個會期分過議題的質詢不到 5 篇，不算一致率」（API 的 `reason` 是 `few_speeches`）。
  - `/records/1?session=3&kind=votes`：記名表決清單，有他的票、黨團多數、一次黨團並列（不算進一致率）與兩次「跟黨團不同」；切換列可以換成院會、委員會（含一場聯席會議）、主提案、連署、三讀、黨團有多數（一致率的分母，12 次）、跨黨投票。
  - 第11屆第4會期（`/speaker/範例一?source=ly&session=2`）：他沒有參加黨團，一致率與跨黨投票寫「沒有參加黨團」；委員會只有 3 場，出席率是「樣本不足」；三讀 0 件。提案與質詢一致率有值與百分位（72.2%，提案 6 件、質詢 9 篇）。
  - `/records/1?session=2&kind=proposed`：主提案 7 件，6 件標了領域、1 件標「還沒分類」；上方除了主提案數，也列出提案與質詢一致率與它的 n（分過領域的 6 件）。
- YouTube 影片：`source` 是 `youtube` 的四篇（`yt-FIXTURE0001`、`yt-FIXTURE0002` 是**沒有任何說話者欄位的舊樣子**，頻道「範例頻道」「範例影音」；`yt-FIXTURE0003`、`yt-FIXTURE0004` 是多人影片，頻道「範例對談」「範例座談」；網址指向 `youtube.example.invalid`，沒有名冊也沒有側寫）。假資料的 API 跟後端用同一個預設範圍：沒帶 `source` 只回議會，所以首頁與發言者、政黨的選單看不到它們，`/videos` 與 `?source=youtube` 才有（手打 `/?source=youtube` 首頁會當成沒篩）。看版面用：
  - `/videos`：卡片格線、紅色的 YouTube 來源標籤、頻道連結；第二篇長度是 0（卡片不顯示長度）、標題以日期開頭（原樣顯示）。多人影片的卡片寫「2 位說話者」（`participant_count`：假資料由 `speakers` 算，只算參與者）；舊樣子的兩篇沒有。
  - `/article/2026-11-02-yt-FIXTURE0003`：多人影片（診斷狀態 `ok`）。三位說話者：「範例甲」（編輯設了名字與角色「主持人」，身分依據 `已由編輯確認`）、「發言者 B」（`未確認身分`）、「其他聲音 1」（短暫的聲音，排在最後、空心記號）；五段都有 `points`，最後一點在 125:30（7530 秒，連結是 `t=7530s`，時間顯示 `2:05:30`）；逐字稿 20 行，其中一行是後端代入的「（無法判斷）」（中性的晶片，不是某位說話者），分鐘超過 99。
  - `/article/2026-10-20-yt-FIXTURE0004`：多人影片（診斷狀態 `warn`，四個警告碼各一句）。一位參與者與四個短暫的聲音；逐字稿有一行開頭是「現場字幕：」（不是任何一位說話者，原樣顯示、不拆冒號），另一行內文裡還有一個冒號（只用第一個）。
  - `/article/2026-10-28-yt-FIXTURE0001`：有摘要卡（`brief` 裡有兩項要求，但影片頁不顯示「要求與回應」，看這頁確認它沒有冒出來）、固定的 AI 備註、每段時間戳帶 `t=`（滑鼠停上去的提示是「從 02:23 開始播放」）。
  - `/article/2026-09-14-yt-FIXTURE0002`：沒有摘要卡（退回導言）、沒有「會議」與長度、時間戳超過一小時（`1:40:12`）、逐字稿跨過 100 分鐘（三位數的分鐘）。
  - `/speaker/範例頻道?source=youtube`：頻道頁，沒有側寫、用語是「頻道」與「影片」、返回鍵回 `/videos`；不帶 `?source=` 的 `/speaker/範例頻道` 找不到，跟後端的預設範圍一樣。
- 頁面最上方會出現一條「示範資料模式」橫幅，避免把假資料當成真的。
- 截圖檔案實際上不存在，圖片會自動換成同尺寸的佔位方塊（版面不會塌）。

用途有兩個：後端還沒起來時仍可開發／驗收版面；以及 API 掛掉時還有一條可以 demo 的後路。

> 假資料中的人名、數字、發言內容**全部是虛構的**，只為了讓版面看起來像真的，請勿引用。

## 頁面

| 路徑 | 說明 |
| --- | --- |
| `/` | 最新報導（只有議會來源：後端沒帶 `source` 時只回議會）。支援 `?date=`、`?speaker=`、`?q=`、`?page=`；第一頁無篩選時，最新一篇會以大版面呈現。`?source=` 只認議會，手打 `?source=youtube` 的當成沒篩。 |
| `/videos` | 影片（YouTube 手動加入的）。版型沿用首頁，但沒有篩選器與頭條大卡，一次一個 API 呼叫（`source=youtube`，依加入的先後、新的在前），只支援 `?page=`；沒有影片時說「還沒有加入任何影片」並寫出加入影片的指令。API 錯誤的處理與 HTTP 狀態碼跟首頁一樣；頁面帶 `noindex`。頁首的「影片」連結在所有寬度都顯示，在這一頁時有標示。多人影片的卡片寫「N 位說話者」（API 給 `participant_count` 才有）。 |
| `/article/[slug]` | 單篇報導。逐段排版（截圖＋小標＋條列＋完整敘述），每段可點時間戳連回原片（YouTube 影片的時間戳帶 `t=`，直接跳到那一段），最後是可折疊的完整逐字稿。影片的版面差異（含多人影片的出場、說明列、每個重點的說話者）見上面「文章頁的版面」。 |
| `/speaker/[name]` | 某位委員的報導列表（含分頁與其他委員快捷鍵）。有 `person_id` 且側寫 API 成功時，清單上方是人物側寫；側寫失敗或 404 就整段不顯示。支援 `?source=`、`?session=`（會期 id）、`?solo=1`（只看單獨發言）、`?brief=1`（只看有摘要卡的）、`?topic=`（主領域代碼，只認 `src/lib/topics.ts` 列舉裡的；`any` 是不限領域、只要分過類，議題指標的「看這 N 篇」帶的是它），有篩選時清單上方顯示條件與「清除」，翻頁時條件跟著走。影片的頻道頁用 `?source=youtube`：沒有側寫、用語是「頻道」與「影片」、返回鍵回 `/videos`、帶 `noindex`。網址裡的名稱從原始網址解碼剛好一次，不用 `Astro.params`：Astro 會把路徑反覆解碼到穩定，卻不解 `%2F`、`%26` 這幾個保留字元，頻道「50%25off」給到頁面時已經是 `50%off`，「Q&A? #1」則留著 `%26`。 |
| `/method` | 方法說明。`#profile` 寫人物側寫的原則、公式、會期與同儕、百分位、最小樣本、資料限制與還沒做的指標；`#profile-topics` 寫議題分布的 12 個領域、分類的輸入、人工標註與門檻、上線條件、公式、委員會對照與限制；`#profile-followups` 寫追問率的期限換算表、候選與篩選、判斷與落地檢查、各種狀態（含換了判斷器之後的待重判）、公式、標註集與門檻、限制（規則與門檻在 `src/lib/followups.ts`）；`#profile-chamber` 寫院內紀錄，其中 `#profile-chamber-alignment` 寫提案與質詢一致率的定義、重疊公式與算好的例子（例子用 `lib/records.ts` 的 `distributionOverlap` 算，改了件數不會算錯）、兩道門檻、為什麼只送議案名稱與限制。改後端的計算時這一頁要跟著改。 |
| `/records/[id]` | 院內紀錄清單（只有立委），側寫「院內紀錄」每張卡的證據。`[id]` 是 person_id；`?session=`（會期 id，沒帶就用側寫預設的會期）、`?kind=`（`plenary`、`committee`、`proposed`、`cosigned`、`passed`、`votes`、`caucus_votes`、`defections`，跟後端的 `chamber.KIND_KEYS` 一樣，清單在 `lib/records.ts` 的 `RECORD_KIND_KEYS`；不認得的當成 `plenary`；`caucus_votes` 是與所屬黨團一致率的分母——他有投票、黨團也有多數的表決，設計只列了七類，但一致率的 n 跟投票出席率不同，要有自己的清單筆數才對得上）。標題寫是誰、哪個會期、哪一類，上方是類別切換列與那一類的指標，底下每一筆附官方連結，另有回發言者頁的連結。主提案清單也是提案與質詢一致率的證據：清單附了議案分類器（後端的 `bill_classifier`）、而且它過了網站再擋一次的門檻（至少 20 件人工標註、準確率至少 80%，`lib/topics.ts` 的 `BILL_TOPIC_MIN_*`）時，議案帶了領域（後端每件的 `topic`，`{key, label}`）就標在每一件旁邊，沒帶的標「還沒分類」，上方多列一致率與它的 n，並寫出分類器的準確率；分類器沒附或沒過門檻時整份清單的領域都拿掉，就不標。那個會期沒有院內紀錄（側寫沒有 `chamber` 區塊）時回 404。公式與資料限制寫在 `/method#profile-chamber`，一致率在 `/method#profile-chamber-alignment`。 |
| 其他 | `src/pages/404.astro` |

IVOD 的網址不吃時間參數，所以時間戳只顯示 `mm:ss` 並連到 `ivod_url`（從頭播放），
連結的 `title` 有說明這件事。YouTube 的網址吃 `t=`：來源表裡 `timeParam` 為真的來源，
時間戳會連到 `ivod_url` 加上 `t=<秒>s`（`timestampHref`），提示文字也改成「從 02:23 開始播放」。

## 韌性行為

後端連不上、回 5xx、回壞掉的 JSON、或回空清單，頁面都會 render 出可讀的中文訊息，
不會出現白畫面或未處理的 500。HTTP 狀態碼對照：

| 情況 | 狀態碼 | 畫面 |
| --- | --- | --- |
| 正常 | 200 | 內容 |
| 清單為空（有／無篩選條件） | 200 | 「目前還沒有任何報導」／「這個條件下沒有報導」；`/videos` 是「還沒有加入任何影片」 |
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
│  │                         # Pagination / SlideBlock / Transcript / Figure / Notice /
│  │                         # ProfilePanel / ProfileIndicator / ProfileTopics /
│  │                         # ProfileFollowups / FollowupItem（人物側寫）/ MethodFollowups（方法頁）
│  │                         # ProfileChamber / RecordMeeting / RecordBill / RecordVote /
│  │                         # OfficialLink（院內紀錄）/ SpeakerChip（多人影片的說話者晶片）
│  ├─ lib/
│  │  ├─ api.ts              # 所有 API 呼叫、逾時、錯誤分類、假資料模式
│  │  ├─ types.ts            # 對應後端契約的型別
│  │  ├─ topics.ts           # 議題的 12 個領域與委員會對照（方法頁用；跟後端一起改）
│  │  ├─ records.ts          # 院內紀錄的類別與指標對照、紀錄清單的整理、一致率的重疊公式與門檻、假資料
│  │  ├─ followups.ts        # 追問的期限換算表、狀態、門檻與區塊整理（跟後端一起改）
│  │  ├─ sources.ts          # 來源表：議會或影片（kind）、各來源的用語、時間戳連結（timeParam）；COUNCIL_SOURCES 是首頁與篩選器用的議會清單
│  │  ├─ speakers.ts         # 多人影片的資料層：容錯的正規化、[[Sn]] 清除、出場名單與記號（不 import 其他本地模組，測試才載得進來）
│  │  └─ format.ts           # mm:ss、日期、逐字稿拆行與依說話者分塊、query string
│  ├─ fixtures/sample.json   # 假資料（USE_FIXTURE=1）
│  ├─ fixtures/records.json  # 院內紀錄的假資料（USE_FIXTURE=1）
│  ├─ pages/
│  │  ├─ index.astro
│  │  ├─ videos.astro        # 影片（YouTube 手動加入的）清單；沒有篩選器與頭條大卡
│  │  ├─ article/[slug].astro
│  │  ├─ speaker/[name].astro
│  │  ├─ records/[id].astro
│  │  ├─ method.astro
│  │  └─ 404.astro
│  └─ styles/global.css      # 設計 token（深色優先）、中文排版、動態、說話者晶片
├─ tests/                    # node:test（沒有額外套件）：speakers／transcript／contrast（讀 global.css 的 token 算深淺兩種配色的對比）是單元測試，pages 起建置好的站對 HTML 下斷言
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
npm run check      # astro check（TypeScript 型別檢查）
npm test           # 先 build，再跑 tests/ 全部（單元測試＋頁面斷言）
npm run test:unit  # 只跑單元測試（不需要 build，不到一秒）
```

`tests/pages.test.ts` 起兩種伺服器，全綁在 `127.0.0.1` 的暫用埠、不碰外部網路：
`USE_FIXTURE=1` 的站（斷言 `sample.json` 的多人影片、舊樣子的影片、議會文章、`/videos` 卡片；每個新出現的畫面都有掛鉤 `data-*`，只加在新的標記上，舊的標記一個字都沒動），
以及一個假的後端（站走正式的 API 路徑）餵舊版 API、型別壞掉、留著 `[[Sn]]` 的回應。

### 議會頁面「改版前後逐字相同」的檢查

動到共用的元件（`SlideBlock`、`Transcript`、`ArticleCard`、`global.css`）之後，確認議會與舊樣子影片的 HTML 沒被改到：
**改之前**先 build、用 `USE_FIXTURE=1` 起站（loopback、埠 ≥ 18000），把 `/`、`/?source=constructor`、一篇議會的 `/article/<slug>`、`/speaker/<名稱>`、`/videos` 等路由的 HTML 存到 repo 外的暫存資料夾；
改完再 build、存一份，逐檔 diff。`_astro/Base.<雜湊>.css` 的檔名會因為 CSS 變動而不同，比對前先正規化。只應該有「新增的畫面」不同：新增的假資料影片造成的 `/videos` 卡片與頻道頁的其他頻道清單、新的影片文章頁。

## 假資料與測試的關係

`sample.json` 的多人影片（`yt-FIXTURE0003`、`yt-FIXTURE0004`）與舊樣子的影片（`0001`、`0002`）是 `tests/pages.test.ts` 的輸入：改假資料時要一起看那邊的斷言（說話者的順序、時間、警告碼、逐字稿的行數）。
