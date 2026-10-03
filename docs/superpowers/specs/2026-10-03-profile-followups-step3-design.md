# 人物側寫 第三步：追問率 — 實作設計

日期：2026-10-03　依據：issue #24 的「順序」第 3 步（追問率），接在第 2 步（PR #26，議題分布）之後，沿用它的 GPU 工作種類、標註與評估的做法。

原則照 issue：模型只判斷「這篇有沒有再提同一件事」並附一句引用，引用必須在新文章的逐字稿裡；比率由程式算；標註集門檻沒過就**只顯示待追蹤清單、不顯示比率**。

## 範圍

做：期限換算、追問的候選與判斷、GPU 工作種類 `followup`、`FollowUp` 表、標註集與評估、追問率指標與清單、API、網站、方法頁。

不做：人工標註本身（使用者標）。

## 哪些要求算

- 來源文章：已完成、**單獨發言**、有摘要卡（跟具體度同一批基礎文章）。聯合質詢分不出要求是誰提的。
- 摘要卡 `asks` 裡 `deadline.strip()` 非空的每一項。用 `(文章, asks 的序號)` 當鍵。

## 期限換算（程式，`articles/followups.py`）

從發言日期算出 `due_date`，換不出來就是 None（不計入追問率，也不進待追蹤清單；頁面另外列「期限寫法無法換算」的數量）。規則（全形數字、中文數字都要能讀）：

| 寫法 | 到期日 |
|---|---|
| N 天內／N 日內 | 發言日 + N 天 |
| N 週內／N 周內／N 星期內／N 個禮拜內 | + 7N 天 |
| N 個月內／N 月內 | + N 個月（同日，月底夾住） |
| 半年內 | + 6 個月 |
| N 年內 | + N 年 |
| 本月底／月底前 | 發言當月最後一天 |
| 下個月 | 次月最後一天 |
| 年底前／今年底／今年內 | 當年 12 月 31 日 |
| X 月 X 日前、X 月底前（沒寫年） | 今年那一天；已經過了就是明年 |
| 本會期／這個會期／會期內／會期結束前（只有立法院） | 單數會期 5 月 31 日、雙數會期 12 月 31 日（發言那年）；議會不換算 |
| 儘快、盡速、立即、馬上、下次、預算審查前…… | 無法換算 |

## 找追問

- 候選：**同一個人**、同一個來源、已完成、單獨發言、有摘要卡、日期在 (發言日, due_date + 90 天] 的文章。發言之後、期限之前再提，也算追問。
- 先用程式篩：要求的文字（request）跟候選文章的文字（一句話＋各段小標＋它自己的要求）比字元雙字組，取重疊最高的**前 3 篇**、而且至少有 1 個雙字組重疊（停用字除外）。依重疊高到低送判斷，**第一篇判定有追問就停**。
- 每一對（要求, 候選文章）只判斷一次，記下來；之後只判斷新出現的候選。
- 狀態（計算時依日期決定）：
  - `pending` 待追蹤：還沒到期、也還沒找到追問。
  - `watching` 觀察中：已到期、在 90 天觀察期內、還沒找到追問。
  - `followed` 已追問：找到追問（不管在觀察期的哪個時候）。
  - `not_followed` 未追問：觀察期結束、沒有找到。
- 追問率 =`followed` ÷（`followed` + `not_followed`），**只算通過評估的判斷器判出來的**。

## GPU 端：工作種類 `followup`

- `POST /jobs`，`kind: "followup"`，帶 `request`（舊的要求）、`response`（當時官員的回應，可空）、`card`（新文章的一句話＋要求＋各段小標）、`excerpt`（新文章逐字稿裡跟要求最相關的一段，≤ 1500 字，由新聞服務挑：重疊雙字組最多的視窗）。
- Ollama JSON schema：`{"followed_up": boolean, "quote": string}`。溫度 0、關掉思考。提示詞：只有**同一件具體的事**（同一個要求、同一個案子）才算追問，同一個領域的別件事不算；`quote` 必須是 `excerpt` 裡的原文，沒有追問就留空。
- 成品：`{"followed_up": bool, "quote": str, "classifier": "<模型>#followup-v1#<提示指紋>"}`，指紋做法同 topic。
- 跟其他工作同一個佇列；保留上限照 topic 的做法（輕量工作另外算）。

## 落地檢查（新聞服務）

`followed_up` 為真時，`quote` 去掉空白與標點後必須至少 6 個字、而且出現在新文章的 `transcript_text`（同樣正規化）裡；否則當成沒有追問，並記下 `ungrounded`。

## 資料表

```
FollowUp         一項要求：article、ask_index、request、deadline_text、due_date（可空）、
                 followed_by（文章，可空）、quote、classifier、checked（JSON：判斷過的候選文章 id）、checked_at
FollowUpLabel    人工標註的一對：article、ask_index、candidate（文章）、followed（null＝還沒標）、note、labeled_at
FollowUpEvaluation 評估：classifier、labeled、correct、accuracy、passed、mistakes（JSON）、ran_at
```

文章重產（`save_result`）時刪掉它當來源的 FollowUp，也把它從別人的 `checked` 與 `followed_by` 拿掉（內容變了要重判）。

## 指令與排程

- `check_followups [--limit N]`：替基礎文章建立／更新 FollowUp（期限換算），再判斷新的候選對，上限 N 個判斷工作（預設 `FOLLOWUP_DAILY_LIMIT` = 200）。GPU 連不上整輪停；開頭連續 3 個被拒也停（同 topic）。
- `sample_followup_labels [--pairs 30] [--seed 0]`：從「通過程式篩選的候選對」抽 30 對，一半取重疊最高的那一篇、一半隨機，建立空白標註。已經抽過的不重抽。
- admin「追問標註」：清單上直接用下拉選單標「有追問／沒有追問」，顯示舊要求、當時回應、新文章的一句話與挑出來的那段逐字稿、兩篇文章的連結；**不顯示模型判斷**。
- `eval_followups`：把已標註的對重新送判斷（含落地檢查），準確率 ≥ 85% 而且至少 30 對才通過。存 `FollowUpEvaluation`，印出判錯的。判斷器版本規則同 topic：每個判斷器只看自己最新的評估。
- 排程：匯入 → 議題分類 → 追問判斷 → 重算側寫，各自 try。

## 指標與清單（區塊 key `followup`，標題「追問」）

- `followup_rate` 追問率（%）：該會期他當來源的要求（依來源文章的會期）裡，`followed` ÷（`followed` + `not_followed`）× 100；n ＝ 分母；套最小樣本、給百分位（同儕＝母體中 n ≥ 5 的人）。只有判斷器通過評估時才有；存進 ProfileStat，`classifier` 欄記下判斷器。
- 清單（不靠模型的部分永遠給）：該會期他的要求，各自的狀態、期限原文、到期日、來源文章；`followed` 的附上追問的文章與引用。判斷器沒通過時，只給 `pending` 與無法換算的數量，不給 `followed`／`not_followed`／`watching`（那些要靠判斷）。

## API

- 側寫 `blocks` 多一個 `{"key": "followup", "title": "追問", "indicators": [followup_rate]（判斷器沒通過就是空清單）, "asks": [...], "unparsed": N, "judge": {name, accuracy, labeled, evaluated_at} | null}`。
- `asks` 每項：`{article: {slug, title, date}, request, deadline, due_date, state, followed_by: {slug, title, date} | null, quote}`，依到期日排序。只要文章有頁面（READY）。
- 追問率的 `evidence_url` 指到發言者頁的 `#followups`（清單就在側寫裡）。

## 網站

- 側寫的「追問」區塊：一行說明（判斷器準確率、只算單獨發言的要求、期限換算規則連到方法頁）；追問率卡片（沿用指標卡）；清單分成「已追問」「未追問」「觀察中」「待追蹤」四組，每項是要求、期限、來源文章連結；已追問的附追問文章連結與引用。判斷器沒通過時只有「待追蹤」一組，並寫明比率要等人工驗證。
- 方法頁 `#profile-followups`：哪些要求算、期限換算表、候選與篩選、判斷與落地檢查、四種狀態、公式、標註集與門檻、限制（篩選可能漏掉換了說法的追問；只算單獨發言）。
- 假資料模式要有含追問區塊的側寫（通過與沒通過各一）。

## 測試

- 期限換算：表上每一種寫法、中文與全形數字、月底夾住、跨年、會期、無法換算。
- 候選與篩選、每對只判一次、第一個有追問就停、四種狀態的日期邊界、重產時的清理。
- GPU：請求驗證、schema、解析、分派、成品、保留上限。
- 落地檢查、標註抽樣、admin 盲標、評估與上線條件、指標公式與最小樣本、API 形狀（通過／沒通過）、排程順序。
- 網站：check、build、假資料模式的兩種狀態與方法頁。
