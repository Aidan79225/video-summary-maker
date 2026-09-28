# 人物、任期與政黨 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 文章標上講者「當時」的政黨、可依政黨篩選；資料模型撐得住跨議會任期與換黨；Whisper 拿到講者姓名當提示。

**Architecture:** Pi 側新增 `Person`／`Membership` 與每週同步（LYAPI、臺中官網），登記文章時依日期查任期填 `party`；API 與前端加政黨標籤與篩選。GPU 側工作請求多一個 `speech_hint`，一路傳到 faster-whisper 的 `initial_prompt`。

**Tech Stack:** Django 6 + django-ninja、stdlib urllib、Astro 5、FastAPI、faster-whisper。

**Spec:** `docs/superpowers/specs/2026-09-28-members-and-parties-design.md`

## Global Constraints

- 政黨存來源給的全名；縮寫與顏色只在前端 `lib/parties.ts`。
- 認人只用姓名與別名；含糊一律 `needs_review`，不猜。
- 同步「來源這次沒出現的人」不下架。
- 每個 commit 結尾 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- 測試：`uv run pytest tests -q`；`cd services/news && uv run python manage.py test articles`；`cd web/news && npm run build && npm run check`。

---

### Task 1: GPU 側 `speech_hint` → `initial_prompt`

**Files:** `src/slidebox/domain/ports.py`（`SpeechTranscriber.transcribe` 加 `initial_prompt: str | None = None`）、`src/slidebox/infrastructure/whisper_transcriber.py`、`src/slidebox/usecases/build_deck.py`（`execute(..., speech_hint=None)` → `_transcribe(..., speech_hint)`）、`src/slidebox_api/{app,jobs,runner}.py`（`JobRequest.speech_hint`、`Job.speech_hint`、`JobStore.add(..., speech_hint)`、executor 傳給 `execute`）。
**Tests:** `tests/slidebox/fakes.py`（`FakeTranscriber` 記錄 `initial_prompt`）、`tests/slidebox/test_build_deck.py`、`tests/slidebox/test_whisper_transcriber.py`、`tests/slidebox_api/test_app.py`／`test_runner.py`。
**Interfaces produced:** `POST /jobs {speech_hint?: str ≤ 200}`；`BuildDeckUseCase.execute(url, settings, progress, is_cancelled, speech_hint=None)`。

- [ ] 測試：假 transcriber 收到 `initial_prompt == speech_hint`；沒給時是 None；API 請求帶 `speech_hint` 會存進 Job 並傳到 executor；超過 200 字回 422。
- [ ] 實作、跑 `uv run pytest tests -q`、commit `feat(slidebox): 工作可附語音辨識提示（講者姓名）`。

### Task 2: 資料模型 `Person`／`Membership`／`Article.party`

**Files:** `services/news/articles/models.py`、migration `0005`。
**Interfaces produced:** 見 spec；`Membership.covers(day) -> bool`；`ArticleSource` 沿用。

- [ ] 測試（`test_members.py`）：`covers` 的四種邊界；`Article.party` 預設空。
- [ ] 實作、makemigrations、commit `feat(news): Person／Membership 模型與 Article.party`。

### Task 3: 名單來源與同步

**Files:** `services/news/articles/members_sync.py`（`MemberRecord`、`LyMemberSource`、`TcccMemberSource`、`sync(records) -> SyncReport`、`link_article(article)`、`relink_all()`）、`management/commands/sync_members.py`、`newsroom/settings.py`（`LY_TERM`、`TCCC_WEB_BASE`）。
**Fixtures:** `tests/fixtures/lyapi_legislators_11.json`、`tccc_members_list.html`、`tccc_member_66.html`。
**Interfaces produced:** `MemberRecord(source, external_id, name, party, district, term, photo_url, start_date, end_date)`；`sync()` 回 `SyncReport(created_persons, created_memberships, updated, party_changes, review)`。

- [ ] 測試（`test_members_sync.py`）：LY 解析（含離職者 end_date、日期格式 `2026/02/01`）；臺中名單頁 124 個連結去重成 61 人、個人頁解析「黨藉」「第一選區」「第四屆」；`sync` 的認人三情況、換黨切段、不下架、重跑冪等；`link_article` 單人／聯合／查無。
- [ ] 實作、commit `feat(news): 議員名單同步與文章政黨連結`。

### Task 4: 登記時連結、排程、回填

**Files:** `ingest.py`（`_upsert` 後呼叫 `link_article`；`_resume_or_submit` 帶 `speech_hint`）、`gpu_client.py`（`submit(..., speech_hint=None)`）、`run_scheduler.py`（每週日 03:30 `sync_members`；啟動時表空就先跑）。
- [ ] 測試：`test_ingest.py` 登記後 `party` 有值、submit body 帶 `speech_hint`；`test_gpu_client.py` body 欄位。
- [ ] 實作、commit `feat(news): 登記時標政黨、送 GPU 時附講者提示、每週同步名單`。

### Task 5: API

**Files:** `api.py`（`party` 欄位、`?party=`、`/parties`、`SpeakerOut.party/district`）。
- [ ] 測試：`test_api.py`。
- [ ] 實作、commit `feat(news): API 輸出政黨、可依政黨篩選`。

### Task 6: Admin

**Files:** `admin.py`（`Person`＋inline、合併動作；`Membership`；`Article` 列表加 `party`）。
- [ ] 測試：`test_admin.py` 合併動作把任期與文章搬過去、別名合併。
- [ ] 實作、commit `feat(news): admin 管理人物與任期，可合併同一人`。

### Task 7: 前端

**Files:** `lib/parties.ts`、`components/PartyBadge.astro`、`Filters.astro`、`ArticleCard.astro`、`pages/article/[slug].astro`、`pages/speaker/[name].astro`、`pages/index.astro`、`lib/{types,api}.ts`、`fixtures/sample.json`。
- [ ] 實作、`npm run build && npm run check`、假資料目視、commit `feat(web): 政黨標籤與篩選`。

### Task 8: 文件與 PR

- [ ] `.env.docker.example`（`LY_TERM`、`TCCC_WEB_BASE`）、README「人物與政黨」一節、`sync_members --relink` 的合併後步驟。
- [ ] 全部測試、push、`gh pr create`。
