# 人物、任期與政黨 — 設計

日期：2026-09-28　狀態：草稿，待審

## 目標

讓讀者看得出每篇質詢的講者屬於哪個政黨，能依政黨篩選；資料模型要撐得住
「同一個人先當議員、後當立委、再回議會」與「任內換黨」，並讓之後的新北市議會
（第三個來源）直接接上，不必回頭改。

本版不做政黨總覽頁與主題分類（等三個議會累積一兩個月資料再做）。

## 探勘結論（2026-09-28 實測）

| 來源 | 名單與政黨 |
|---|---|
| 立法院 | LYAPI `GET /v2/legislators?屆=11`：123 位，欄位有 `委員姓名`、`黨籍`（民主進步黨 53、中國國民黨 52、台灣民眾黨 16、無黨籍 2）、`選區名稱`、`照片位址`、`歷屆立法委員編號`（跨屆穩定）、`到職日`、`是否離職`、`離職日期` |
| 臺中市議會 | 官網 `wb_introduction01.asp` 列全部議員，連結 `main.asp?uno=14&cno=<官網編號>`；個人頁 `wb_introduction02.asp?cno=<官網編號>` 內文有「黨藉 中國國民黨」（官網錯字）、「第一選區」、「直轄市第四屆議員」。**官網編號與影音系統的 cno 不同**（楊啓邦官網 66、影音 85），只能用姓名對 |
| 兩邊 | 都只給「目前」黨籍，沒有異動日期 |

## 決策

1. **人與任期分開**：`Person`（真人）與 `Membership`（一段任期：議會、政黨、選區、屆次、起訖）。
   同一個人跨議會、跨屆、換黨都是多筆 Membership 掛在同一個 Person 下。
2. **文章存「當時」的政黨**：`Article.party` 是登記時依文章日期查到的政黨，之後換黨不回溯改舊文章。
   聯合質詢多人時存去重後的多個黨（「國民黨、民進黨」）。另有 `Article.membership`（單一講者時才填）。
3. **跨議會認人用姓名，含糊就交給人**：同步時同名且只有一個 Person 就連上；沒有就建新的；
   同名多個或姓名寫法不同（別名）就建新的並標 `needs_review`，admin 提供「合併」動作。
   61 位議員加 123 位立委，重疊的人數一隻手數得完，人工確認負擔小。
4. **換黨用「偵測到就切一段」**：同步發現同一筆任期的政黨變了，就把舊 Membership 的
   `end_date` 設為今天、新開一筆 `start_date` 今天，並標 `needs_review` 提醒到 admin 補正確日期。
5. **政黨名稱存來源給的全名**（民主進步黨、中國國民黨、台灣民眾黨、無黨籍），顯示時前端縮寫
   （民進黨／國民黨／民眾黨）並上固定色；沒對到縮寫表的照原樣顯示、用中性色。
6. **同步每週一次**（排程週日 03:30），排程啟動時若從未同步過就先跑一次；也可手動 `sync_members`。
7. **順便做 Whisper 姓名提示**：Pi 送 GPU 工作時附 `speech_hint`（講者姓名與議會名），GPU 把它當
   faster-whisper 的 `initial_prompt`，「楊啟邦」就會寫成「楊啓邦」。只對走語音辨識的來源有作用。

## 資料模型（`services/news/articles/models.py`）

```python
class Person(models.Model):
    name = CharField(100, db_index=True)          # 主要姓名
    aliases = JSONField(default=list)             # 其他寫法：["楊啟邦"]
    note = TextField(blank=True)
    needs_review = BooleanField(default=False)    # 同步時認人有疑慮
    created_at / updated_at

class Membership(models.Model):
    person = ForeignKey(Person, related_name="memberships")
    source = CharField(16, choices=ArticleSource.choices, db_index=True)
    external_id = CharField(64, blank=True, db_index=True)   # LY：歷屆立法委員編號；臺中：官網 cno
    name = CharField(100, db_index=True)          # 來源上的寫法（可能與 Person.name 不同）
    party = CharField(100, blank=True)            # 來源給的全名
    district = CharField(100, blank=True)
    term = CharField(32, blank=True)              # "第11屆" / "第4屆"
    photo_url = URLField(500, blank=True)
    start_date = DateField(null=True)             # LY 到職日；臺中不知道就 null（視為無限早）
    end_date = DateField(null=True)               # null = 現任
    needs_review = BooleanField(default=False)    # 換黨自動切段、日期待補
    synced_at = DateTimeField(null=True)
    class Meta: indexes on (source, name), (source, external_id)

Article.membership = ForeignKey(Membership, null=True, on_delete=SET_NULL)
Article.party = CharField(200, blank=True, db_index=True)   # 「民主進步黨」或「中國國民黨、民主進步黨」
```

migration `0005_person_membership_article_party`。

`Membership.covers(day)`：`(start_date is None or start_date <= day) and (end_date is None or day <= end_date)`。

## 同步（`articles/members_sync.py` + 指令 `sync_members`）

介面：每個來源一個 `MemberSource`，`fetch() -> list[MemberRecord(source, external_id, name, party, district, term, photo_url, start_date, end_date)]`。

- `LyMemberSource(base=LYAPI_BASE, term=LY_TERM)`：`/legislators?屆={term}&limit=200`，分頁到 `total_page`；
  `是否離職` 為真的填 `end_date=離職日期`。429 退避比照 `law_source`。
- `TcccMemberSource(base=TCCC_WEB_BASE)`：`wb_introduction01.asp` 解析 `(官網 cno, 姓名)`（61 位），
  逐頁抓 `wb_introduction02.asp?cno=` 解析「黨藉」「第○選區」「第○屆」。單頁失敗只 log。

`sync(records)` 對每筆：
1. 找 `Membership(source, external_id)`；沒有就找 `Membership(source, name, end_date=None)`（臺中沒有穩定編號時的退路）。
2. 沒有 Membership → 認人：`Person.name == name` 或 `name in aliases`。恰一個 → 連上；零個 → 建 Person；
   多個 → 建新 Person 並 `needs_review=True`，log warning。建 Membership。
3. 有 Membership 且 `party` 不同 → 舊的 `end_date=今天`、`needs_review=True`；新建一筆 `start_date=今天`、
   `needs_review=True`；log warning「{name} 黨籍由 A 變 B，請到 admin 補日期」。
4. 其他欄位（選區、照片、屆次、離職）直接更新，`synced_at=now`。
5. 這次沒出現、且 `end_date` 為空的現任 Membership：**不動**（來源暫時漏人不該把人下架），只 log。

指令：`sync_members [--source ly|tccc] [--relink]`；`--relink` 把所有文章依現有 Membership 重算 `party`／`membership`（回填既有文章用）。
排程：`run_scheduler` 加每週日 03:30 的 `sync_members`；啟動時若 `Membership` 表是空的就先跑一次。

## 文章連結（`ingest._upsert` 與 `relink`）

`link_article(article)`：
- 講者字串依「、」拆成姓名列表。
- 每個姓名找 `Membership.objects.filter(source=article.source, name=姓名)` 中 `covers(article.date)` 的；
  多筆時取 `start_date` 最晚的。找不到就跳過。
- `party` = 找到的政黨去重、保留順序、「、」串接；單一講者且找到時 `membership` 設為它。
- 在 `_upsert` 建立文章時呼叫；`save_result` 不動它。

## API

- `ArticleCardOut` 加 `party: str`；`/articles?party=<全名>` 篩選（比對方式同講者：等於／頓號開頭／夾住／結尾）。
- `SpeakerOut` 加 `party: str`、`district: str`（取該講者在該來源現任的 Membership；沒有就空）。
- 新增 `GET /parties?source=`：`[{name, count, latest_date}]`，從 READY 文章的 `party` 拆頓號彙總，給篩選器用。
- `GpuApiClient.submit(..., speech_hint: str | None)`：body 多 `speech_hint`；`_process_one` 傳
  `f"{議會名} {會議名}。發言者：{講者}"`（≤ 200 字）。舊 GPU 若不認得這個欄位會忽略（FastAPI 預設不擋多餘欄位）。

## GPU 側（`src/slidebox`、`src/slidebox_api`）

- `JobRequest.speech_hint: str | None = Field(default=None, max_length=200)`；`Job` 帶著它；
  `SlideboxExecutor` 透過 `usecase.execute(url, settings, progress, cancelled, speech_hint=...)` 傳入。
- `SpeechTranscriber.transcribe(..., initial_prompt: str | None = None)`；`FasterWhisperTranscriber` 傳給
  `model.transcribe(initial_prompt=...)`。桌面 app 不傳，行為不變。

## Admin

- `Person`：列表（姓名、任期數、needs_review）、篩選 needs_review、`Membership` inline、動作「合併到…」
  （把選取的多個 Person 併成第一個：任期與文章都搬過去、別名合併、刪掉其餘）。
- `Membership`：列表（來源、姓名、政黨、屆次、起訖、needs_review）、篩選來源／政黨／needs_review。
- `Article`：列表加 `party`，篩選加 `party`。

## 前端（`web/news`）

- `lib/parties.ts`：`PARTY_SHORT`（民主進步黨→民進黨、中國國民黨→國民黨、台灣民眾黨→民眾黨、無黨籍→無黨籍）、
  `partyTone`（民進黨綠、國民黨藍、民眾黨青、無黨籍灰、其他中性）、`splitParties("A、B")`。
- `PartyBadge.astro`：縮寫 + 色；多黨時並排多個。
- 卡片：講者旁加 `PartyBadge`。文章頁：講者旁加；`dl` 多一列「政黨」。
- 篩選器：加「政黨」下拉（選項來自 `/api/parties`，顯示縮寫、值用全名）；`index.astro` 讀 `?party=`。
- 講者頁：標題下顯示「民進黨 · 臺中市第一選區」。
- `types.ts`：`ArticleCard.party`、`Speaker.party`、`Speaker.district`、`Party` 型別；`api.ts` 的 `getParties()`
  與假資料補 `party`。

## 部署

- `.env.docker.example`：`LY_TERM=11`、`TCCC_WEB_BASE=https://www.tccc.gov.tw`（皆有預設，不填也行）。
- 合併後在 pi5 跑一次 `sync_members --relink` 回填既有文章；之後排程每週自動。

## 錯誤處理

| 情境 | 行為 |
|---|---|
| LYAPI 或臺中官網抓不到 | 該來源這次不同步，log warning，既有資料不動 |
| 臺中單一議員頁抓不到 | 跳過該人，其餘照常 |
| 同名多人 | 建新 Person 標 needs_review，不猜 |
| 換黨 | 自動切段並標 needs_review |
| 文章講者查無任期 | `party` 空、`membership` 空，前端不顯示標籤 |
| 舊 GPU 不認得 `speech_hint` | 忽略，照常產生 |

## 測試

- `test_members_sync.py`：LY 與臺中的解析（fixture：LYAPI JSON 節錄、臺中名單頁與一張個人頁）；
  認人三種情況；換黨切段；離職填 end_date；來源漏人不下架。
- `test_ingest.py`：`_upsert` 填 `party`／`membership`；聯合質詢多黨去重；查無任期留空。
- `test_api.py`：`?party=` 篩選、`/parties`、講者帶政黨與選區。
- `test_admin.py`：合併 Person 動作。
- GPU 側：`speech_hint` 從請求一路傳到 `initial_prompt`（假 transcriber 驗證）。
- 前端：`npm run build`、`astro check`，假資料模式目視。

## 不做

政黨總覽頁、主題分類、歷屆（第 10 屆以前）名單、照片下載、新北市議會。
