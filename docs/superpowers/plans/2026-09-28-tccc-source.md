# 臺中市議會來源 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓排程每天同時抓立法院 IVOD 與臺中市議會的質詢片段，臺中片段在 GPU 主機上用 Whisper 產逐字稿、照既有流程摘要與截圖，前端標示來源並可篩選。

**Architecture:** GPU 側（`src/slidebox`）加一個來源分支：網址辨識 → `TcccClient` 解析 ASP 頁與播放器頁拿 m3u8 → 沒有字幕所以走既有的語音辨識備援（新增 `TcccAudioGateway` 用 ffmpeg 抓音訊）→ 截圖改用可注入串流解析的 `HlsSectionGateway`。Pi 側（`services/news`）加 `Article.source` 欄位、`TcccDailySource` 爬 61 位議員頁發現當天片段、API 加 `source` 篩選。前端加來源標籤與篩選、用語依來源切換。

**Tech Stack:** Python 3.12 / stdlib urllib + re（不加相依）、faster-whisper（已在）、ffmpeg（已在容器）、Django 6 + django-ninja、Astro 5 + Tailwind。

**Spec:** `docs/superpowers/specs/2026-09-28-tccc-source-design.md`

## Global Constraints

- 不新增 Python 相依；Pi 側只用 stdlib 抓網頁。
- 主機名一律完整比對（`hostname == "vod.tccc.gov.tw"`），不能用 `in`。
- 臺中片段的識別碼一律是 `tccc-<ano>`（Django `ivod_id`、GPU payload `video_id`、`AudioClip.video_id` 三處相同）。
- `cleanup()` 類方法絕不可 raise、要容忍目錄不存在。
- 所有 commit 結尾加 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- 測試指令：GPU 側 `uv run pytest tests/slidebox tests/slidebox_api -q`；Pi 側 `cd services/news && uv run python manage.py test articles -v 1`；前端 `cd web/news && npm run build`。
- 註解與訊息用正體中文，風格比照既有檔案（說明「為什麼」而不是「做什麼」）。

---

## File map

| 檔案 | 責任 |
|---|---|
| `src/slidebox/usecases/sources.py` | 網址辨識：`ivod_id`（既有）、`tccc_clip`、`tccc_id`（新） |
| `src/slidebox/infrastructure/tccc_api.py` | 抓臺中 ASP 頁與播放器頁：`TcccRecord`、`TcccClient` |
| `src/slidebox/infrastructure/tccc_audio.py` | `TcccAudioGateway`（ffmpeg 抓音訊）、`NoSubtitlesGateway` |
| `src/slidebox/infrastructure/ivod_sections.py` | `HlsSectionGateway`（通用）、`IvodSectionGateway`（薄包裝） |
| `src/slidebox/infrastructure/routing.py` | 三個 router 加 tccc 分支；新增 `BySourceAudioGateway` |
| `src/slidebox/composition.py` | 接線 |
| `src/slidebox_api/runner.py` | `video_id_of` 認得臺中 |
| `services/news/articles/models.py` + migration 0004 | `ArticleSource`、`Article.source` |
| `services/news/articles/ivod_source.py` | `IvodClip.source`、`SourceUnavailable` 別名、`IvodDailySource.name` |
| `services/news/articles/tccc_source.py` | `TcccDailySource` |
| `services/news/articles/ingest.py` | `discover_days(days, sources)`、`_upsert` 寫 source |
| `services/news/articles/management/commands/ingest_ivod.py` | 組來源清單 |
| `services/news/newsroom/settings.py` | `TCCC_ENABLED`、`TCCC_VOD_BASE` |
| `services/news/articles/api.py` | `source` 參數與輸出 |
| `web/news/src/lib/sources.ts` | 來源標籤、稱謂 |
| `web/news/src/components/SourceBadge.astro` | 來源小標籤 |
| 其餘前端檔 | 用語與篩選 |

---

### Task 1: 網址辨識 `tccc_clip` / `tccc_id`

**Files:**
- Modify: `src/slidebox/usecases/sources.py`
- Test: `tests/slidebox/test_sources.py`

**Interfaces:**
- Produces: `TcccRef(cno: str, ano: str)` frozen dataclass；`tccc_clip(url: str) -> TcccRef | None`；`tccc_id(url: str) -> str | None`（回 `"tccc-<ano>"`）。

- [ ] **Step 1: 寫失敗的測試**（加在 `tests/slidebox/test_sources.py` 末尾）

```python
from slidebox.usecases.sources import TcccRef, tccc_clip, tccc_id


@pytest.mark.parametrize("url,expected", [
    ("https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833", TcccRef("85", "14833")),
    ("https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833&pageno=1", TcccRef("85", "14833")),
    ("https://vod.tccc.gov.tw/index.asp?ano=14833&cno=85&url=12", TcccRef("85", "14833")),
    ("http://VOD.TCCC.GOV.TW/index.asp?url=12&cno=85&ano=14833", TcccRef("85", "14833")),
    ("  https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833  ", TcccRef("85", "14833")),
])
def test_recognises_taichung_council_clip_pages(url, expected):
    assert tccc_clip(url) == expected
    assert tccc_id(url) == "tccc-14833"


@pytest.mark.parametrize("url", [
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=85",          # 只有議員，沒有片段
    "https://vod.tccc.gov.tw/index.asp?url=11",
    "https://vod.tccc.gov.tw/wb_region02.asp?url=12&cno=85&ano=14833",  # 內頁不是公開網址
    "https://vod.tccc.gov.tw.attacker.com/index.asp?url=12&cno=85&ano=14833",
    "https://ivod.ly.gov.tw/Play/Clip/1M/171180",
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=abc&ano=14833",
    "",
])
def test_everything_else_is_not_a_taichung_clip(url):
    assert tccc_clip(url) is None
    assert tccc_id(url) is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/slidebox/test_sources.py -q`
Expected: ImportError `TcccRef`

- [ ] **Step 3: 實作**（加在 `src/slidebox/usecases/sources.py` 末尾）

```python
from dataclasses import dataclass
from urllib.parse import parse_qs

_TCCC_HOST = "vod.tccc.gov.tw"


@dataclass(frozen=True)
class TcccRef:
    """臺中市議會的一段質詢：議員編號 + 片段編號。兩個都要，頁面是照議員分的。"""
    cno: str
    ano: str


def tccc_clip(url: str) -> TcccRef | None:
    """臺中市議會「議員個人質詢隨選視訊」的片段網址回傳 (cno, ano)，其餘 None。

    只認公開的 `index.asp`：內頁 `wb_region02.asp` 是 iframe 的內容，
    不該當成使用者貼的網址。
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if (parsed.hostname or "").lower() != _TCCC_HOST:
        return None
    if parsed.path.lower() != "/index.asp":
        return None
    query = parse_qs(parsed.query)
    cno = (query.get("cno") or [""])[0]
    ano = (query.get("ano") or [""])[0]
    if not (cno.isdigit() and ano.isdigit()):
        return None
    return TcccRef(cno=cno, ano=ano)


def tccc_id(url: str) -> str | None:
    """新聞服務用的識別碼：`tccc-<ano>`，加前綴才不會跟立法院的純數字撞。"""
    ref = tccc_clip(url)
    return f"tccc-{ref.ano}" if ref else None
```

（`urlparse` 已 import；把 `dataclass`／`parse_qs` 的 import 移到檔頭。）

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/slidebox/test_sources.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/slidebox/usecases/sources.py tests/slidebox/test_sources.py
git commit -m "feat(slidebox): 認得臺中市議會的片段網址"
```

---

### Task 2: `TcccClient`：解析片段頁與播放器頁

**Files:**
- Create: `src/slidebox/infrastructure/tccc_api.py`
- Create: `tests/slidebox/fixtures/tccc_region02_85_14833.html`（實頁 `wb_region02.asp?url=12&cno=85&ano=14833&pageno=1`，去掉 `<script>`）
- Create: `tests/slidebox/fixtures/tccc_player.html`（播放器頁裁剪，保留含 `src: 'https://….m3u8?iMda_seq=153345'` 的那段）
- Test: `tests/slidebox/test_tccc_api.py`

**Interfaces:**
- Consumes: `TcccRef` (Task 1)
- Produces:
  - `TcccRecord(ano, cno, speaker, meeting, date, duration_seconds: int, player_url)` frozen dataclass，`title` property = `f"{date} {speaker}議員－{meeting}"`。
  - `TcccClient(fetch=_http_get, base="https://vod.tccc.gov.tw")`：`record(ref: TcccRef) -> TcccRecord`（快取最後一筆）、`video_url(ref) -> str`。
  - 兩者失敗都丟 `NoSubtitlesAvailable`（訊息說明抓不到哪一頁）。
  - 模組層 `parse_clip_page(html: str, ref: TcccRef) -> TcccRecord`、`parse_stream_url(html: str) -> str | None`、`_hhmm_seconds("00:50") -> 3000`。

- [ ] **Step 1: 放 fixture**

把探勘存下的 `wb_region02.asp?url=12&cno=85&ano=14833&pageno=1` 頁面另存為 fixture；播放器頁只留下列骨架：

```html
<!DOCTYPE html><html><head><title>player</title></head><body>
<script>
    var player = videojs('gplayer', {
        sources: [{
            src: 'https://streamak0128.akamaized.net/vod0128vh-67eb/_definst_/04A08/08_11509xx/1150924_0930_8_01_01_1_1.mp4/playlist.m3u8?iMda_seq=153345',
            type: 'application/x-mpegURL'
        }]
    });
</script>
</body></html>
```

- [ ] **Step 2: 寫失敗的測試** `tests/slidebox/test_tccc_api.py`

```python
"""臺中市議會片段頁與播放器頁的解析：用存下來的真實頁面，不碰網路。"""
from __future__ import annotations

import os

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable
from slidebox.infrastructure.tccc_api import TcccClient, parse_clip_page, parse_stream_url
from slidebox.usecases.sources import TcccRef

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
REF = TcccRef("85", "14833")
M3U8 = ("https://streamak0128.akamaized.net/vod0128vh-67eb/_definst_/04A08/08_11509xx/"
        "1150924_0930_8_01_01_1_1.mp4/playlist.m3u8?iMda_seq=153345")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeFetch:
    def __init__(self, pages: dict[str, str], error: Exception | None = None):
        self.pages = pages
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        for key, html in self.pages.items():
            if key in url:
                return html
        raise AssertionError(f"沒準備這個網址的頁面：{url}")


def test_the_clip_page_yields_speaker_meeting_date_duration_and_player():
    record = parse_clip_page(_read("tccc_region02_85_14833.html"), REF)
    assert record.speaker == "楊啓邦"
    assert record.meeting == "第4屆第8次定期會 市政總質詢"
    assert record.date == "2026-09-24"
    assert record.duration_seconds == 50 * 60          # 「00:50」是 HH:MM
    assert record.player_url.startswith("https://rds.ginnet.cloud/player/")
    assert record.title == "2026-09-24 楊啓邦議員－第4屆第8次定期會 市政總質詢"
    assert (record.cno, record.ano) == ("85", "14833")


def test_the_player_page_yields_the_hls_url():
    assert parse_stream_url(_read("tccc_player.html")) == M3U8


def test_a_player_page_without_a_stream_gives_none():
    assert parse_stream_url("<html><body>nothing here</body></html>") is None


def test_the_client_fetches_the_inner_frame_with_the_right_query():
    fetch = FakeFetch({"wb_region02.asp": _read("tccc_region02_85_14833.html")})
    client = TcccClient(fetch=fetch)
    record = client.record(REF)
    assert fetch.urls == ["https://vod.tccc.gov.tw/wb_region02.asp?url=12&cno=85&ano=14833&pageno=1"]
    assert record.speaker == "楊啓邦"


def test_the_record_is_cached_for_the_same_clip():
    fetch = FakeFetch({"wb_region02.asp": _read("tccc_region02_85_14833.html")})
    client = TcccClient(fetch=fetch)
    client.record(REF)
    client.record(REF)
    assert len(fetch.urls) == 1


def test_video_url_goes_through_the_player_page():
    fetch = FakeFetch({
        "wb_region02.asp": _read("tccc_region02_85_14833.html"),
        "rds.ginnet.cloud": _read("tccc_player.html"),
    })
    assert TcccClient(fetch=fetch).video_url(REF) == M3U8


def test_a_clip_page_that_cannot_be_fetched_is_reported():
    client = TcccClient(fetch=FakeFetch({}, error=OSError("connection refused")))
    with pytest.raises(NoSubtitlesAvailable, match="臺中市議會"):
        client.record(REF)


def test_a_clip_page_without_the_expected_block_is_reported():
    client = TcccClient(fetch=FakeFetch({"wb_region02.asp": "<html>改版了</html>"}))
    with pytest.raises(NoSubtitlesAvailable, match="解析"):
        client.record(REF)


def test_a_player_without_a_stream_is_reported():
    fetch = FakeFetch({
        "wb_region02.asp": _read("tccc_region02_85_14833.html"),
        "rds.ginnet.cloud": "<html>no stream</html>",
    })
    with pytest.raises(NoSubtitlesAvailable, match="串流"):
        TcccClient(fetch=fetch).video_url(REF)
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/slidebox/test_tccc_api.py -q`
Expected: ModuleNotFoundError `tccc_api`

- [ ] **Step 4: 實作** `src/slidebox/infrastructure/tccc_api.py`

```python
"""臺中市議會「議員個人質詢隨選視訊系統」（vod.tccc.gov.tw）。

沒有 API：片段清單是舊式 ASP 頁 + iframe，影片在第三方播放器（Ginnet）
的頁面裡以 HLS 提供。這裡只做兩件事——解析片段頁拿 metadata 與播放器網址、
解析播放器頁拿 m3u8。逐字稿系統（yishi.tccc.gov.tw）沒有時間戳，不用；
逐字稿交給語音辨識。
"""
from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass

from ..domain.errors import NoSubtitlesAvailable
from ..usecases.sources import TcccRef

_BASE = "https://vod.tccc.gov.tw"
_TIMEOUT = 30.0

_PLAYER_RE = re.compile(r'<iframe[^>]+src="(https://rds\.ginnet\.cloud/player/[^"]+)"', re.I)
_SPEAKER_RE = re.compile(r">\s*([^<>\s]+)\s+議員\s*<")
_MEETING_RE = re.compile(r'<font color="#0D57BB">([^<]+)</font>')
_DATE_RE = re.compile(r"會議日期：</td>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2})")
_DURATION_RE = re.compile(r"影片長度：</td>\s*<td[^>]*>\s*(\d{1,2}:\d{2})")
# 播放器頁把來源寫在 JS 裡：src: 'https://….m3u8?iMda_seq=…'
_STREAM_RE = re.compile(r"""https://[^'"\s]+\.m3u8(?:\?[^'"\s]*)?""")


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _hhmm_seconds(text: str) -> int:
    """頁面的「影片長度」是 HH:MM（「00:50」= 50 分鐘，實測 51 分）。分鐘精度。"""
    try:
        hours, minutes = text.split(":")
        return int(hours) * 3600 + int(minutes) * 60
    except ValueError:
        return 0


@dataclass(frozen=True)
class TcccRecord:
    ano: str
    cno: str
    speaker: str
    meeting: str
    date: str
    duration_seconds: int
    player_url: str

    @property
    def title(self) -> str:
        """與立法院文章同形（日期 姓名－會議），前端才不用分兩套排版。"""
        return f"{self.date} {self.speaker}議員－{self.meeting}"


def parse_clip_page(page: str, ref: TcccRef) -> TcccRecord:
    """從 wb_region02.asp 的「選中片段」區塊取 metadata。缺任何一項都算解析失敗。"""
    player = _PLAYER_RE.search(page)
    speaker = _SPEAKER_RE.search(page)
    meeting = _MEETING_RE.search(page)
    day = _DATE_RE.search(page)
    if not (player and speaker and meeting and day):
        raise NoSubtitlesAvailable("臺中市議會的片段頁解析失敗，頁面格式可能改了")
    duration = _DURATION_RE.search(page)
    return TcccRecord(
        ano=ref.ano,
        cno=ref.cno,
        speaker=html.unescape(speaker.group(1)).strip(),
        meeting=html.unescape(meeting.group(1)).strip(),
        date=day.group(1),
        duration_seconds=_hhmm_seconds(duration.group(1)) if duration else 0,
        player_url=html.unescape(player.group(1)),
    )


def parse_stream_url(page: str) -> str | None:
    match = _STREAM_RE.search(page)
    return match.group(0) if match else None


class TcccClient:
    """一段片段兩個 gateway 都要用（音訊要串流、截圖也要串流），快取最後一筆。"""

    def __init__(self, fetch=_http_get, base: str = _BASE):
        self._fetch = fetch
        self._base = base.rstrip("/")
        self._cached: tuple[TcccRef, TcccRecord] | None = None

    def clip_page_url(self, ref: TcccRef) -> str:
        return f"{self._base}/wb_region02.asp?url=12&cno={ref.cno}&ano={ref.ano}&pageno=1"

    def record(self, ref: TcccRef) -> TcccRecord:
        if self._cached and self._cached[0] == ref:
            return self._cached[1]
        try:
            page = self._fetch(self.clip_page_url(ref))
        except OSError as e:
            raise NoSubtitlesAvailable(f"臺中市議會的片段頁抓不到：{e}") from e
        record = parse_clip_page(page, ref)
        self._cached = (ref, record)
        return record

    def video_url(self, ref: TcccRef) -> str:
        record = self.record(ref)
        try:
            page = self._fetch(record.player_url)
        except OSError as e:
            raise NoSubtitlesAvailable(f"臺中市議會的播放器頁抓不到：{e}") from e
        stream = parse_stream_url(page)
        if not stream:
            raise NoSubtitlesAvailable("臺中市議會的播放器頁裡找不到串流網址")
        return stream
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/slidebox/test_tccc_api.py -q`
Expected: PASS（若 `_SPEAKER_RE` 對 fixture 抓不到，先用 `grep -n "議員" fixture` 對照實際標記調整正規表示式，不要改 fixture）

- [ ] **Step 6: Commit**

```bash
git add src/slidebox/infrastructure/tccc_api.py tests/slidebox/test_tccc_api.py tests/slidebox/fixtures/
git commit -m "feat(slidebox): 臺中市議會片段頁與播放器頁的解析"
```

---

### Task 3: `HlsSectionGateway`：截圖 gateway 改成可注入串流解析

**Files:**
- Modify: `src/slidebox/infrastructure/ivod_sections.py`
- Test: `tests/slidebox/test_ivod_sections.py`（既有測試不動，加一個）

**Interfaces:**
- Produces: `HlsSectionGateway(stream_for: Callable[[str], str], clip_seconds=..., runner=..., ffmpeg_exe=...)`；`stream_for(url)` 回 m3u8，找不到丟 `NoSubtitlesAvailable`（或 `OSError`）→ 全部 None。`IvodSectionGateway(client, ...)` 保留，等於 `HlsSectionGateway(lambda url: client.video_url(ivod_id(url)))` 但保留「不是 IVOD 網址就不打 API」的檢查。

- [ ] **Step 1: 寫失敗的測試**（加在 `tests/slidebox/test_ivod_sections.py` 末尾）

```python
def test_a_generic_hls_gateway_takes_any_stream_resolver():
    from slidebox.infrastructure.ivod_sections import HlsSectionGateway

    seen = []

    def resolve(url):
        seen.append(url)
        return M3U8

    runner = FakeRunner()
    gateway = HlsSectionGateway(resolve, runner=runner, ffmpeg_exe="ffmpeg")
    paths = gateway.download_sections("https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=1",
                                      [3.0], None, os.path.join(TMP(), "hls"), _noop, lambda: False)
    assert seen == ["https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=1"]
    assert paths[0] is not None
    assert M3U8 in runner.commands[0]
```

（`TMP()` 若檔內沒有這個 helper，用 `tmp_path` fixture 改寫成 `def test_…(tmp_path)` 並用 `str(tmp_path / "hls")`。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/slidebox/test_ivod_sections.py -q`
Expected: ImportError `HlsSectionGateway`

- [ ] **Step 3: 重構**

把 `IvodSectionGateway` 改名為 `HlsSectionGateway`，建構子第一個參數改成 `stream_for: Callable[[str], str]`；`download_sections` 裡原本的

```python
        video_id = ivod_id(url)
        if video_id is None:
            progress(1.0, f"這不是 IVOD 的播放網址（{url[:60]}），將產出無截圖的摘要")
            return [None] * len(timestamps)
        try:
            stream = self._client.video_url(video_id)
        except (NoSubtitlesAvailable, OSError) as e:
```

改成

```python
        try:
            stream = self._stream_for(url)
        except (NoSubtitlesAvailable, OSError) as e:
            progress(1.0, f"這段影片沒有可用的串流（{str(e)[:80]}），將產出無截圖的摘要")
            return [None] * len(timestamps)
```

然後在檔尾加薄包裝，保住既有行為（不是 IVOD 網址就不打 API）：

```python
def _ivod_stream(client: IvodClient):
    def resolve(url: str) -> str:
        video_id = ivod_id(url)
        if video_id is None:
            # 走到這裡表示路由接錯了。不打 API——少了 id 會變成打清單端點，
            # 一次無謂的請求換一個看不懂的錯。
            raise NoSubtitlesAvailable(f"這不是 IVOD 的播放網址（{url[:60]}）")
        return client.video_url(video_id)
    return resolve


class IvodSectionGateway(HlsSectionGateway):
    def __init__(self, client: IvodClient, clip_seconds: float = DEFAULT_CLIP_SECONDS,
                 runner=subprocess.run, ffmpeg_exe: str | None = None):
        super().__init__(_ivod_stream(client), clip_seconds, runner, ffmpeg_exe)
```

模組 docstring 補一句：「`HlsSectionGateway` 通用於任何 m3u8 來源，臺中市議會也用它」。

- [ ] **Step 4: 跑全部 slidebox 測試**

Run: `uv run pytest tests/slidebox -q`
Expected: PASS（既有 `test_ivod_sections.py` 裡檢查「不是 IVOD 網址」訊息的測試若比對字串，改成比對「將產出無截圖的摘要」即可）

- [ ] **Step 5: Commit**

```bash
git add src/slidebox/infrastructure/ivod_sections.py tests/slidebox/test_ivod_sections.py
git commit -m "refactor(slidebox): 截圖 gateway 改成可注入串流來源的 HlsSectionGateway"
```

---

### Task 4: `TcccAudioGateway` 與 `NoSubtitlesGateway`

**Files:**
- Create: `src/slidebox/infrastructure/tccc_audio.py`
- Test: `tests/slidebox/test_tccc_audio.py`

**Interfaces:**
- Consumes: `TcccClient.record/video_url` (Task 2)、`tccc_clip` (Task 1)、`AudioClip`、`AudioGateway`／`SubtitleGateway` port。
- Produces: `TcccAudioGateway(client, runner=subprocess.run, ffmpeg_exe=None, timeout=900)`：`download_audio(url, dest_dir, progress, is_cancelled) -> AudioClip(path=<dest_dir>/tccc-<ano>.wav, video_id="tccc-<ano>", title=record.title, duration=<wav 秒數>)`；`cleanup(dest_dir)`。`NoSubtitlesGateway(reason: str)`：`fetch()` 一律 raise `NoSubtitlesAvailable(reason)`。

- [ ] **Step 1: 寫失敗的測試** `tests/slidebox/test_tccc_audio.py`

```python
"""臺中片段的音訊：假的 ffmpeg runner，不碰網路。"""
from __future__ import annotations

import os
import subprocess

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable, OperationCancelled
from slidebox.infrastructure.tccc_api import TcccRecord
from slidebox.infrastructure.tccc_audio import NoSubtitlesGateway, TcccAudioGateway

URL = "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833"
M3U8 = "https://cdn.example/playlist.m3u8?iMda_seq=1"
RECORD = TcccRecord(ano="14833", cno="85", speaker="楊啓邦", meeting="市政總質詢",
                    date="2026-09-24", duration_seconds=3000, player_url="https://p")


class FakeClient:
    def __init__(self, error=None):
        self.error = error

    def record(self, ref):
        if self.error:
            raise self.error
        return RECORD

    def video_url(self, ref):
        if self.error:
            raise self.error
        return M3U8


class FakeRunner:
    def __init__(self, fail=False, stderr="boom", seconds=2.0, timeout=False):
        self.fail, self.stderr, self.seconds, self.timeout = fail, stderr, seconds, timeout
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        if not self.fail:
            with open(command[-1], "wb") as f:
                f.write(b"\0" * int(16000 * 2 * self.seconds))  # 16 kHz、16-bit、單聲道

        class Result:
            returncode = 1 if self.fail else 0
            stderr = self.stderr if self.fail else ""
        return Result()


def _noop(frac, status):
    pass


def test_downloads_mono_16k_wav_from_the_stream(tmp_path):
    runner = FakeRunner(seconds=2.0)
    clip = TcccAudioGateway(FakeClient(), runner=runner, ffmpeg_exe="ffmpeg").download_audio(
        URL, str(tmp_path / "a"), _noop, lambda: False)
    command = runner.commands[0]
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == M3U8
    assert ["-vn", "-ac", "1", "-ar", "16000"] == command[command.index("-vn"):command.index("-vn") + 5]
    assert clip.path.endswith("tccc-14833.wav") and os.path.exists(clip.path)
    assert clip.video_id == "tccc-14833"
    assert clip.title == RECORD.title
    assert clip.duration == pytest.approx(2.0)


def test_ffmpeg_failure_is_reported_as_no_subtitles(tmp_path):
    gateway = TcccAudioGateway(FakeClient(), runner=FakeRunner(fail=True, stderr="403 Forbidden"),
                               ffmpeg_exe="ffmpeg")
    with pytest.raises(NoSubtitlesAvailable, match="403 Forbidden"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_ffmpeg_timeout_is_reported(tmp_path):
    gateway = TcccAudioGateway(FakeClient(), runner=FakeRunner(timeout=True), ffmpeg_exe="ffmpeg")
    with pytest.raises(NoSubtitlesAvailable, match="逾時"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_a_non_taichung_url_is_rejected_without_touching_the_network(tmp_path):
    gateway = TcccAudioGateway(FakeClient(error=AssertionError("不該打到")), runner=FakeRunner(),
                               ffmpeg_exe="ffmpeg")
    with pytest.raises(NoSubtitlesAvailable):
        gateway.download_audio("https://youtu.be/x", str(tmp_path / "a"), _noop, lambda: False)


def test_cancel_before_download_raises(tmp_path):
    gateway = TcccAudioGateway(FakeClient(), runner=FakeRunner(), ffmpeg_exe="ffmpeg")
    with pytest.raises(OperationCancelled):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: True)


def test_cleanup_tolerates_a_missing_dir(tmp_path):
    TcccAudioGateway(FakeClient(), runner=FakeRunner(), ffmpeg_exe="ffmpeg").cleanup(
        str(tmp_path / "nope"))


def test_no_subtitles_gateway_always_declines():
    with pytest.raises(NoSubtitlesAvailable, match="語音辨識"):
        NoSubtitlesGateway("臺中市議會沒有逐字稿，改用語音辨識").fetch(URL, ("zh",))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/slidebox/test_tccc_audio.py -q`
Expected: ModuleNotFoundError

- [ ] **Step 3: 實作** `src/slidebox/infrastructure/tccc_audio.py`

```python
"""臺中市議會片段的音訊與字幕 gateway。

臺中沒有逐字稿可抓（議事錄沒有時間戳），所以字幕 gateway 一律說「沒有」，
讓 use case 走語音辨識；音訊則用 ffmpeg 直接從 HLS 抓成 16 kHz 單聲道 wav——
faster-whisper 內部就是重採樣到這個格式，先做好可以少一次解碼。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence

from ..domain.entities import AudioClip, Transcript
from ..domain.errors import NoSubtitlesAvailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.sources import tccc_clip
from .ffmpeg import get_ffmpeg_exe
from .tccc_api import TcccClient

# 一小時的片段實測音訊串流幾分鐘就抓完；15 分鐘是「網路壞了」的上限，
# 不是正常情況會碰到的值。
_TIMEOUT_SECONDS = 900
_SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2  # pcm_s16le 單聲道


class NoSubtitlesGateway:
    """字幕 gateway 的「一律沒有」實作，讓 BuildDeckUseCase 走語音辨識備援。"""

    def __init__(self, reason: str):
        self._reason = reason

    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        raise NoSubtitlesAvailable(self._reason)


class TcccAudioGateway:
    def __init__(self, client: TcccClient, runner=subprocess.run,
                 ffmpeg_exe: str | None = None, timeout: float = _TIMEOUT_SECONDS):
        self._client = client
        self._runner = runner
        self._exe = ffmpeg_exe or get_ffmpeg_exe()
        self._timeout = timeout

    def download_audio(self, url: str, dest_dir: str, progress: ProgressCallback,
                       is_cancelled: CancelCheck) -> AudioClip:
        ref = tccc_clip(url)
        if ref is None:
            raise NoSubtitlesAvailable(f"這不是臺中市議會的片段網址（{url[:60]}）")
        if is_cancelled():
            raise OperationCancelled()
        record = self._client.record(ref)
        stream = self._client.video_url(ref)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"tccc-{ref.ano}.wav")
        progress(None, f"下載音訊…（{record.title[:40]}）")
        command = [
            self._exe, "-hide_banner", "-loglevel", "error", "-y",
            "-i", stream,
            "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE), "-c:a", "pcm_s16le",
            dest,
        ]
        try:
            result = self._runner(command, capture_output=True, text=True, timeout=self._timeout)
        except subprocess.TimeoutExpired as e:
            raise NoSubtitlesAvailable(f"音訊下載逾時（超過 {self._timeout:.0f} 秒）") from e
        except OSError as e:
            raise NoSubtitlesAvailable(f"無法執行 ffmpeg：{e}") from e
        if result.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
            stderr = (getattr(result, "stderr", "") or "").strip()[:200]
            raise NoSubtitlesAvailable(f"音訊下載失敗：{stderr or 'ffmpeg 失敗'}")
        # wav 是固定位元率，長度直接由檔案大小算，不必再開一次 ffprobe。
        duration = os.path.getsize(dest) / (_SAMPLE_RATE * _BYTES_PER_SAMPLE)
        return AudioClip(path=dest, video_id=f"tccc-{ref.ano}", title=record.title,
                         duration=float(duration))

    def cleanup(self, dest_dir: str) -> None:
        shutil.rmtree(dest_dir, ignore_errors=True)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/slidebox/test_tccc_audio.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/slidebox/infrastructure/tccc_audio.py tests/slidebox/test_tccc_audio.py
git commit -m "feat(slidebox): 臺中片段用 ffmpeg 抓音訊給語音辨識"
```

---

### Task 5: 路由、接線、`video_id_of`、提示詞

**Files:**
- Modify: `src/slidebox/infrastructure/routing.py`
- Modify: `src/slidebox/composition.py:90-113`
- Modify: `src/slidebox_api/runner.py:91-97`
- Modify: `src/slidebox/infrastructure/ollama_summarizer.py`（「通常是立法委員的質詢」那一句）
- Test: `tests/slidebox/test_routing.py`、`tests/slidebox_api/test_runner.py`、`tests/slidebox/test_composition.py`

**Interfaces:**
- Produces: `BySourceSubtitleGateway(default, ivod, tccc=None)`、`BySourceSectionGateway(default, ivod, tccc=None)`、`BySourceAudioGateway(default, tccc=None)`（實作 `AudioGateway`：`download_audio`、`cleanup`）。`video_id_of(tccc_url) == "tccc-<ano>"`。

- [ ] **Step 1: 寫失敗的測試**（加在 `tests/slidebox/test_routing.py`）

```python
TCCC = "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833"


class AudioSpy:
    def __init__(self, name):
        self.name = name
        self.downloaded = []
        self.cleaned = []

    def download_audio(self, url, dest_dir, progress, is_cancelled):
        self.downloaded.append(url)
        return self.name

    def cleanup(self, dest_dir):
        self.cleaned.append(dest_dir)


def test_taichung_urls_go_to_the_taichung_branch():
    yt, ivod, tccc = Spy("yt"), Spy("ivod"), Spy("tccc")
    assert BySourceSubtitleGateway(yt, ivod, tccc).fetch(TCCC, ()) == "tccc"
    assert BySourceSectionGateway(yt, ivod, tccc).download_sections(TCCC, [], None, "d", None, None) == ["tccc"]
    assert BySourceSubtitleGateway(yt, ivod, tccc).fetch(IVOD, ()) == "ivod"


def test_without_a_taichung_branch_the_default_handles_it():
    yt, ivod = Spy("yt"), Spy("ivod")
    assert BySourceSubtitleGateway(yt, ivod).fetch(TCCC, ()) == "yt"


def test_audio_routes_taichung_and_falls_back_to_the_default():
    from slidebox.infrastructure.routing import BySourceAudioGateway
    yt, tccc = AudioSpy("yt"), AudioSpy("tccc")
    router = BySourceAudioGateway(yt, tccc)
    assert router.download_audio(TCCC, "d", None, None) == "tccc"
    assert router.download_audio(YOUTUBE, "d", None, None) == "yt"
    router.cleanup("x")
    assert yt.cleaned == ["x"] and tccc.cleaned == ["x"]


def test_section_cleanup_reaches_the_taichung_branch_too():
    yt, ivod, tccc = Spy("yt"), Spy("ivod"), Spy("tccc")
    BySourceSectionGateway(yt, ivod, tccc).cleanup("d")
    assert tccc.cleaned == ["d"]
```

並把 `test_the_routers_take_exactly_what_the_ports_declare` 的 `pairs` 加上
`(AudioGateway.download_audio, BySourceAudioGateway.download_audio)`、`(AudioGateway.cleanup, BySourceAudioGateway.cleanup)`（import `AudioGateway`）。

在 `tests/slidebox_api/test_runner.py` 加：

```python
def test_a_taichung_clip_is_keyed_by_its_prefixed_ano():
    assert video_id_of("https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833") == "tccc-14833"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/slidebox/test_routing.py tests/slidebox_api/test_runner.py -q`
Expected: FAIL（TypeError：多給一個參數；ImportError `BySourceAudioGateway`；assert `tccc-14833`）

- [ ] **Step 3: 實作 routing**

`routing.py` 改成：

```python
from ..domain.entities import AudioClip, Transcript
from ..domain.ports import AudioGateway, CancelCheck, ProgressCallback, SubtitleGateway, VideoSectionGateway
from ..usecases.sources import ivod_id, tccc_clip


def _pick(url: str, default, ivod, tccc):
    """三選一：IVOD → 臺中 → 其餘。臺中分支沒接時退回 default，讓桌面 app
    （沒有臺中那一套）不必知道這個來源存在。"""
    if ivod_id(url):
        return ivod
    if tccc is not None and tccc_clip(url):
        return tccc
    return default


class BySourceSubtitleGateway:
    def __init__(self, default: SubtitleGateway, ivod: SubtitleGateway,
                 tccc: SubtitleGateway | None = None):
        self._default, self._ivod, self._tccc = default, ivod, tccc

    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        return _pick(url, self._default, self._ivod, self._tccc).fetch(url, langs)


class BySourceSectionGateway:
    def __init__(self, default: VideoSectionGateway, ivod: VideoSectionGateway,
                 tccc: VideoSectionGateway | None = None):
        self._default, self._ivod, self._tccc = default, ivod, tccc

    def download_sections(self, url, timestamps, max_height, dest_dir, progress, is_cancelled):
        chosen = _pick(url, self._default, self._ivod, self._tccc)
        return chosen.download_sections(url, timestamps, max_height, dest_dir, progress, is_cancelled)

    def cleanup(self, dest_dir: str) -> None:
        _cleanup_all((self._default, self._ivod, self._tccc), dest_dir)


class BySourceAudioGateway:
    """音訊只有兩條路：臺中用 ffmpeg 抓 HLS，其餘交給 yt-dlp。IVOD 不會走到
    這裡（它有逐字稿，不會觸發語音備援）。"""

    def __init__(self, default: AudioGateway, tccc: AudioGateway | None = None):
        self._default, self._tccc = default, tccc

    def download_audio(self, url: str, dest_dir: str, progress: ProgressCallback,
                       is_cancelled: CancelCheck) -> AudioClip:
        chosen = self._tccc if (self._tccc is not None and tccc_clip(url)) else self._default
        return chosen.download_audio(url, dest_dir, progress, is_cancelled)

    def cleanup(self, dest_dir: str) -> None:
        _cleanup_all((self._default, self._tccc), dest_dir)


def _cleanup_all(gateways, dest_dir: str) -> None:
    """每個分支都清一次（既有 docstring 的理由：cleanup 沒有 url 可判斷）。"""
    for gateway in gateways:
        if gateway is None:
            continue
        try:
            gateway.cleanup(dest_dir)
        except Exception:  # noqa: BLE001
            pass
```

保留原本 `cleanup` 上的長 docstring（搬到 `_cleanup_all`），簽章名稱與 port 完全一致（有測試比對參數名）。

- [ ] **Step 4: 接線** `composition.py::build_usecase`

```python
    ivod_client = IvodClient()
    tccc_client = TcccClient()
    return BuildDeckUseCase(
        BySourceSubtitleGateway(
            YtDlpSubtitleGateway(), IvodSubtitleGateway(ivod_client),
            tccc=NoSubtitlesGateway("臺中市議會沒有逐字稿，改用語音辨識")),
        CurrentSettingsSummarizer(settings),
        BySourceSectionGateway(
            YtDlpSectionGateway(), IvodSectionGateway(ivod_client),
            tccc=HlsSectionGateway(lambda url: tccc_client.video_url(tccc_clip(url)))),
        FfmpegFrameExtractor(),
        HtmlDeckRenderer(),
        audio=BySourceAudioGateway(YtDlpAudioGateway(), tccc=TcccAudioGateway(tccc_client)),
        transcriber=FasterWhisperTranscriber(settings.whisper_model),
        brief_writer=CurrentSettingsBriefWriter(settings),
    )
```

補 import；docstring 加一句「臺中市議會沒有逐字稿：字幕 gateway 直接說沒有，音訊用 ffmpeg 抓 HLS 後交給 Whisper」。

`runner.py`：

```python
from slidebox.usecases.sources import ivod_id, tccc_id
...
    return ivod_id(url) or tccc_id(url) or video_key(url)
```

`ollama_summarizer.py`：「通常是立法委員的質詢」→「通常是立法委員或市議員的質詢」。若 `tests/slidebox/test_ollama_prompt.py` 有比對整句，一併更新。

- [ ] **Step 5: 跑全部 GPU 側測試**

Run: `uv run pytest tests/slidebox tests/slidebox_api -q`
Expected: PASS（`test_composition.py` 若用假 factory 檢查 gateway 型別，依新型別調整）

- [ ] **Step 6: Commit**

```bash
git add src/slidebox src/slidebox_api tests/slidebox tests/slidebox_api
git commit -m "feat(slidebox): 臺中市議會片段走語音辨識與 HLS 截圖"
```

---

### Task 6: Pi 側資料模型與多來源 discover

**Files:**
- Modify: `services/news/articles/models.py`（`ArticleSource`、`Article.source`）
- Create: `services/news/articles/migrations/0004_article_source.py`（`makemigrations` 產生）
- Modify: `services/news/articles/ivod_source.py`（`IvodClip.source`、`SourceUnavailable`、`IvodDailySource.name`）
- Modify: `services/news/articles/ingest.py`（`discover_days(days, sources)`、`_upsert`）
- Modify: `services/news/articles/management/commands/ingest_ivod.py`
- Modify: `services/news/newsroom/settings.py`
- Test: `services/news/articles/tests/test_ingest.py`

**Interfaces:**
- Produces: `ArticleSource.LY = "ly"`、`ArticleSource.TCCC = "tccc"`；`Article.source`（default `ly`）；`IvodClip(..., source: str = "ly")`；`SourceUnavailable`（= `IvodUnavailable`）；每個來源物件有 `name: str` 與 `clips_for(day)`；`discover_days(days: Sequence[date], sources: Sequence) -> IngestReport`。

- [ ] **Step 1: 寫失敗的測試**（`test_ingest.py`，`FakeSource` 加 `name = "假來源"` 屬性）

```python
    def test_discover_days_runs_every_source_and_keeps_going_after_one_fails(self):
        from articles.ingest import discover_days
        from articles.models import ArticleSource

        ok = FakeSource([_clip("900001"), _clip_tccc("14833")])
        ok.name = "立法院"
        broken = FakeSource(error=IvodUnavailable("掛了"))
        broken.name = "臺中市議會"
        report = discover_days([date(2026, 8, 27)], [broken, ok])
        self.assertEqual((report.discovered, report.created), (2, 2))
        self.assertEqual(len(report.errors), 1)
        self.assertIn("臺中市議會", report.errors[0])
        self.assertEqual(Article.objects.get(ivod_id="tccc-14833").source, ArticleSource.TCCC)
        self.assertEqual(Article.objects.get(ivod_id="900001").source, ArticleSource.LY)
```

加 helper：

```python
def _clip_tccc(ano="14833", speaker="楊啓邦"):
    return IvodClip(
        ivod_id=f"tccc-{ano}", date="2026-08-27", speaker=speaker,
        meeting="第4屆第8次定期會 市政總質詢", duration_seconds=3000,
        ivod_url=f"https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano={ano}",
        has_transcript=True, source="tccc",
    )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd services/news && uv run python manage.py test articles.tests.test_ingest -v 1`
Expected: TypeError（`source` 不是 `IvodClip` 的欄位）

- [ ] **Step 3: 實作**

`models.py`：

```python
class ArticleSource(models.TextChoices):
    LY = "ly", "立法院"
    TCCC = "tccc", "臺中市議會"
```

`Article` 在 `ivod_id` 下方加：

```python
    # 哪個議會。ivod_id 這個名字是歷史包袱——現在是「來源給的識別碼」，
    # 臺中片段寫成 tccc-<ano>；改欄位名要動 API、前端與既有資料，不值得。
    source = models.CharField(max_length=16, choices=ArticleSource.choices,
                              default=ArticleSource.LY, db_index=True)
```

`cd services/news && uv run python manage.py makemigrations articles -n article_source`。

`ivod_source.py`：`IvodClip` 加 `source: str = "ly"`（放最後，有預設值）；`IvodUnavailable` 下方加 `SourceUnavailable = IvodUnavailable`（docstring：「兩個來源共用同一個例外，discover 才能一視同仁」）；`IvodDailySource` 加類別屬性 `name = "立法院"`。

`ingest.py`：

```python
def discover_days(days: Sequence[date], sources: Sequence) -> IngestReport:
    """登記多天、多來源的片段。某一天或某個來源失敗不影響其他。"""
    report = IngestReport()
    for source in sources:
        for day in days:
            try:
                found, created = discover(day, source)
            except SourceUnavailable as e:
                report.errors.append(f"{source.name} {day}: {e}")
                logger.warning("%s清單取得失敗（%s）：%s", source.name, day, e)
                continue
            report.discovered += found
            report.created += created
    return report
```

`_upsert` 的 defaults 加 `"source": clip.source`。`discover(day, source)` 的型別註記改成 duck-typed（拿掉 `IvodDailySource`）。

`ingest_ivod.py`：

```python
        sources = [IvodDailySource(settings.IVOD_API_BASE)]
        if settings.TCCC_ENABLED:
            sources.append(TcccDailySource(settings.TCCC_VOD_BASE))
        self.stdout.write(f"=== {days[-1]} ～ {days[0]}（{'、'.join(s.name for s in sources)}）===")
        report = discover_days(days, sources)
```

（`TcccDailySource` 在 Task 7 才建；本 task 先 import 會失敗，所以本 task 先寫成只有 `IvodDailySource`，Task 7 再加那兩行。）

`settings.py`（`CITE_LAWS` 下方）：

```python
# 臺中市議會「議員個人質詢隨選視訊」。片段長（15～60 分鐘）、要在 GPU 主機跑
# 語音辨識，一篇 10～20 分鐘。不想抓就關掉。
TCCC_ENABLED = _env_bool("TCCC_ENABLED", True)
TCCC_VOD_BASE = os.environ.get("TCCC_VOD_BASE", "https://vod.tccc.gov.tw")
```

- [ ] **Step 4: 跑 Pi 側全部測試**

Run: `cd services/news && uv run python manage.py test articles -v 1`
Expected: PASS（`test_ingest.py` 既有呼叫 `discover_days(days, source)` 的地方改成傳 `[source]`）

- [ ] **Step 5: Commit**

```bash
git add services/news
git commit -m "feat(news): Article 加來源欄位，discover 支援多個來源"
```

---

### Task 7: `TcccDailySource`

**Files:**
- Create: `services/news/articles/tccc_source.py`
- Create: `services/news/articles/tests/fixtures/tccc_region01.html`（`wb_region01.asp?url=11` 實頁）
- Create: `services/news/articles/tests/fixtures/tccc_region02_85.html`（`wb_region02.asp?url=12&cno=85` 不帶 ano 的實頁）
- Create: `services/news/articles/tests/fixtures/tccc_region02_85_14745.html`（帶 `ano=14745` 的實頁）
- Modify: `services/news/articles/management/commands/ingest_ivod.py`（接上）
- Test: `services/news/articles/tests/test_tccc_source.py`

**Interfaces:**
- Consumes: `IvodClip`、`SourceUnavailable` (Task 6)
- Produces: `TcccDailySource(base, fetch=_http_get)`，`name = "臺中市議會"`，`clips_for(day: date, only_with_transcript=True) -> list[IvodClip]`；模組層 `parse_councilors(html) -> list[tuple[str, str]]`（`(cno, name)`）、`parse_clip_rows(html) -> list[ClipRow(ano, meeting, date)]`（含被選中那一筆，其 ano 來自分頁連結）、`parse_selected_duration(html) -> int`。

頁面行為（探勘實測）：
- 不帶 `ano` 時，最新一筆是「選中片段」，它在清單裡**沒有連結**，ano 只出現在分頁連結 `…&ano=14833&PageNo=1`。若沒有分頁（片段 ≤10 筆），再用清單第一個有連結的 ano 重抓一次，最新那筆就會變成有連結的列。
- 每列：`<a href="index.asp?url=12&cno=85&ano=14745&pageno=1" …>會議名稱</a>` … `<td valign="top" nowrap="nowrap">2026-09-02</td>`。
- 選中片段區塊：`會議日期：</td><td …>2026-09-24</td>`、`影片長度：</td><td>00:50</td>`（HH:MM）。

- [ ] **Step 1: 放 fixture**（三個實頁，去掉 `<script>` 區塊即可）

- [ ] **Step 2: 寫失敗的測試** `test_tccc_source.py`

```python
"""臺中市議會清單來源：用存下來的真實頁面，不碰網路。"""
from __future__ import annotations

import os
from datetime import date

from django.test import SimpleTestCase

from articles.ivod_source import SourceUnavailable
from articles.tccc_source import (TcccDailySource, parse_clip_rows, parse_councilors,
                                  parse_selected_duration)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeFetch:
    """依網址片段回頁面；沒準備的網址回 error 或 AssertionError。"""

    def __init__(self, pages, error_for=()):
        self.pages = pages
        self.error_for = tuple(error_for)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if any(e in url for e in self.error_for):
            raise OSError("connection refused")
        for key, html in self.pages.items():
            if key in url:
                return html
        raise AssertionError(f"沒準備這個網址的頁面：{url}")


class ParseTests(SimpleTestCase):
    def test_councilors_are_read_with_their_ids(self):
        rows = parse_councilors(_read("tccc_region01.html"))
        self.assertEqual(len(rows), 61)
        self.assertIn(("85", "楊啓邦"), rows)

    def test_clip_rows_include_the_selected_clip_via_the_pager(self):
        rows = parse_clip_rows(_read("tccc_region02_85.html"))
        self.assertEqual(rows[0].ano, "14833")
        self.assertEqual(rows[0].meeting, "第4屆第8次定期會 市政總質詢")
        self.assertEqual(rows[0].date, "2026-09-24")
        self.assertEqual(rows[1].ano, "14745")
        self.assertEqual(rows[1].date, "2026-09-02")
        self.assertEqual(len(rows), 10)

    def test_when_another_clip_is_selected_the_newest_is_a_plain_link(self):
        rows = parse_clip_rows(_read("tccc_region02_85_14745.html"))
        self.assertEqual([r.ano for r in rows[:2]], ["14833", "14745"])

    def test_selected_duration_is_hours_and_minutes(self):
        self.assertEqual(parse_selected_duration(_read("tccc_region02_85.html")), 50 * 60)
        self.assertEqual(parse_selected_duration(_read("tccc_region02_85_14745.html")), 15 * 60)


class SourceTests(SimpleTestCase):
    def _source(self, error_for=()):
        pages = {
            "wb_region01.asp": _read("tccc_region01.html"),
            "cno=85&ano=14745": _read("tccc_region02_85_14745.html"),
            "cno=85": _read("tccc_region02_85.html"),
        }
        # 其餘 60 位議員：空頁（沒有片段列）
        fetch = _Fetch(pages, error_for)
        return TcccDailySource("https://vod.example", fetch=fetch), fetch

    def test_only_clips_on_that_day_become_clips(self):
        source, fetch = self._source()
        clips = source.clips_for(date(2026, 9, 2))
        self.assertEqual([c.ivod_id for c in clips], ["tccc-14745"])
        clip = clips[0]
        self.assertEqual(clip.speaker, "楊啓邦")
        self.assertEqual(clip.meeting, "第4屆第8次定期會 業務質詢：都發建設水利部分")
        self.assertEqual(clip.date, "2026-09-02")
        self.assertEqual(clip.duration_seconds, 15 * 60)
        self.assertEqual(clip.ivod_url, "https://vod.example/index.asp?url=12&cno=85&ano=14745")
        self.assertEqual(clip.source, "tccc")
        self.assertTrue(clip.has_transcript)

    def test_every_councilor_page_is_visited_once(self):
        source, fetch = self._source()
        source.clips_for(date(2026, 1, 1))
        listing = [u for u in fetch.urls if "wb_region02.asp" in u and "ano=" not in u]
        self.assertEqual(len(listing), 61)

    def test_a_quiet_day_yields_nothing(self):
        source, _ = self._source()
        self.assertEqual(source.clips_for(date(2026, 1, 1)), [])

    def test_one_broken_councilor_page_does_not_kill_the_day(self):
        source, _ = self._source(error_for=["cno=64"])
        self.assertEqual([c.ivod_id for c in source.clips_for(date(2026, 9, 2))], ["tccc-14745"])

    def test_a_broken_councilor_list_is_unavailable(self):
        source, _ = self._source(error_for=["wb_region01.asp"])
        with self.assertRaises(SourceUnavailable):
            source.clips_for(date(2026, 9, 2))

    def test_a_missing_duration_page_still_registers_the_clip(self):
        source, _ = self._source(error_for=["ano=14745"])
        clips = source.clips_for(date(2026, 9, 2))
        self.assertEqual(clips[0].duration_seconds, 0)


class _Fetch(FakeFetch):
    """沒準備的議員頁回一張沒有片段的空頁，模擬「這位議員那天沒發言」。"""

    def __call__(self, url):
        try:
            return super().__call__(url)
        except AssertionError:
            return "<html><body><table></table></body></html>"
```

注意 `test_one_broken_councilor_page…` 裡 `cno=64` 也會匹配 `cno=64` 的議員頁（fixture 裡確有 cno=64）。`FakeFetch` 的比對是「網址包含 key」，`"cno=85"` 也會匹配 `cno=85&ano=14745`，所以 `pages` 的順序要讓較長的 key 先比對——上面已經這樣排。

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd services/news && uv run python manage.py test articles.tests.test_tccc_source -v 1`
Expected: ImportError

- [ ] **Step 4: 實作** `tccc_source.py`

```python
"""臺中市議會「議員個人質詢隨選視訊系統」：查某一天有哪些片段。

沒有 API，只有依議員分類的 ASP 頁。所以「某一天有誰發言」要逐一翻 61 位
議員的第一頁（10 筆、新到舊）——每天 61 次 HTTP，順序、逐一，對方是舊系統。
只用 stdlib。
"""
from __future__ import annotations

import html
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import date

from .ivod_source import IvodClip, SourceUnavailable

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
SOURCE = "tccc"

_COUNCILOR_RE = re.compile(r'href="index\.asp\?url=12&cno=(\d+)"[^>]*>(?:<font[^>]*>)?\s*([^<]+?)\s*<')
_ROW_RE = re.compile(
    r'href="index\.asp\?url=12&cno=\d+&ano=(\d+)&pageno=\d+"[^>]*>([^<]+)</a>'
    r'.*?<td valign="top" nowrap="nowrap">(\d{4}-\d{2}-\d{2})</td>',
    re.S | re.I)
_SELECTED_ANO_RE = re.compile(r"&ano=(\d+)&PageNo=\d+", re.I)
_SELECTED_MEETING_RE = re.compile(r'<font color="#0D57BB">([^<]+)</font>')
_SELECTED_DATE_RE = re.compile(r"會議日期：</td>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2})")
_SELECTED_DURATION_RE = re.compile(r"影片長度：</td>\s*<td[^>]*>\s*(\d{1,2}):(\d{2})")


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ClipRow:
    ano: str
    meeting: str
    date: str


def parse_councilors(page: str) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for cno, name in _COUNCILOR_RE.findall(page):
        seen.setdefault(cno, html.unescape(name).strip())
    return [(cno, name) for cno, name in seen.items() if name]


def parse_clip_rows(page: str) -> list[ClipRow]:
    """清單裡每一列；被選中的那一筆沒有連結，改從分頁連結拿 ano、從上方區塊拿
    會議名稱與日期，放在最前面（它是最新的）。"""
    rows = [ClipRow(ano, html.unescape(meeting).strip(), day)
            for ano, meeting, day in _ROW_RE.findall(page)]
    selected = _SELECTED_ANO_RE.search(page)
    meeting = _SELECTED_MEETING_RE.search(page)
    day = _SELECTED_DATE_RE.search(page)
    if selected and meeting and day and all(r.ano != selected.group(1) for r in rows):
        rows.insert(0, ClipRow(selected.group(1), html.unescape(meeting.group(1)).strip(),
                               day.group(1)))
    return rows


def parse_selected_duration(page: str) -> int:
    """「影片長度：00:50」是 HH:MM（實測 51 分鐘），分鐘精度。"""
    match = _SELECTED_DURATION_RE.search(page)
    if not match:
        return 0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60


class TcccDailySource:
    name = "臺中市議會"

    def __init__(self, base: str, fetch=_http_get):
        self._base = base.rstrip("/")
        self._fetch = fetch

    def clips_for(self, day: date, only_with_transcript: bool = True) -> list[IvodClip]:
        """某一天的片段。only_with_transcript 只為了跟立法院同介面：臺中一律走
        語音辨識，沒有「有沒有逐字稿」這回事。"""
        try:
            councilors = parse_councilors(self._fetch(f"{self._base}/wb_region01.asp?url=11"))
        except OSError as e:
            raise SourceUnavailable(f"臺中市議會的議員清單抓不到：{e}") from e
        if not councilors:
            raise SourceUnavailable("臺中市議會的議員清單解析不出任何議員，頁面格式可能改了")
        wanted = day.isoformat()
        clips: list[IvodClip] = []
        for cno, name in councilors:
            for row in self._rows_for(cno, name):
                if row.date != wanted:
                    continue
                clips.append(IvodClip(
                    ivod_id=f"{SOURCE}-{row.ano}",
                    date=row.date,
                    speaker=name,
                    meeting=row.meeting,
                    duration_seconds=self._duration(cno, row.ano),
                    ivod_url=f"{self._base}/index.asp?url=12&cno={cno}&ano={row.ano}",
                    has_transcript=True,
                    source=SOURCE,
                ))
        return sorted(clips, key=lambda c: int(c.ivod_id.split("-")[1]))

    def _rows_for(self, cno: str, name: str) -> list[ClipRow]:
        try:
            page = self._fetch(f"{self._base}/wb_region02.asp?url=12&cno={cno}")
        except OSError as e:
            logger.warning("臺中市議會 %s（cno=%s）的片段頁抓不到：%s", name, cno, e)
            return []
        rows = parse_clip_rows(page)
        if rows and not _SELECTED_ANO_RE.search(page):
            # 沒有分頁就拿不到選中那一筆的 ano：改選另一筆，最新的就會變成有連結的列
            try:
                page = self._fetch(
                    f"{self._base}/wb_region02.asp?url=12&cno={cno}&ano={rows[0].ano}&pageno=1")
                rows = parse_clip_rows(page)
            except OSError:
                pass
        return rows

    def _duration(self, cno: str, ano: str) -> int:
        try:
            page = self._fetch(f"{self._base}/wb_region02.asp?url=12&cno={cno}&ano={ano}&pageno=1")
        except OSError as e:
            logger.warning("臺中市議會片段 %s 的長度抓不到：%s", ano, e)
            return 0
        return parse_selected_duration(page)
```

`ingest_ivod.py`：import `TcccDailySource`，依 `settings.TCCC_ENABLED` 加進 `sources`（Task 6 預留）。

- [ ] **Step 5: 跑測試確認通過**

Run: `cd services/news && uv run python manage.py test articles -v 1`
Expected: PASS（正規表示式對不上 fixture 時，`grep -n` fixture 對照調整正規表示式；不要改 fixture）

- [ ] **Step 6: Commit**

```bash
git add services/news/articles
git commit -m "feat(news): 臺中市議會每日片段來源"
```

---

### Task 8: API 的 `source` 篩選與輸出

**Files:**
- Modify: `services/news/articles/api.py`
- Test: `services/news/articles/tests/test_api.py`

**Interfaces:**
- Produces: `GET /api/articles?source=ly|tccc`、`GET /api/speakers?source=…`；`ArticleCardOut.source: str`；`SpeakerOut.source: str`（同名不同來源分列）。非法值 422。

- [ ] **Step 1: 寫失敗的測試**（`test_api.py`，`_article()` 加參數 `source="ly"` 並寫入 `Article.objects.create(..., source=source)`）

```python
    def test_cards_carry_their_source_and_can_be_filtered_by_it(self):
        _article("900001", speaker="範例一")
        _article("tccc-14833", speaker="楊啓邦", source="tccc")
        res = self.client.get("/api/articles").json()
        self.assertEqual({i["source"] for i in res["items"]}, {"ly", "tccc"})
        res = self.client.get("/api/articles?source=tccc").json()
        self.assertEqual([i["ivod_id"] for i in res["items"]], ["tccc-14833"])
        self.assertEqual(self.client.get("/api/articles?source=nope").status_code, 422)

    def test_speakers_are_listed_per_source(self):
        _article("900001", speaker="範例一")
        _article("tccc-14833", speaker="楊啓邦", source="tccc")
        items = self.client.get("/api/speakers").json()["items"]
        self.assertEqual({(i["name"], i["source"]) for i in items},
                         {("範例一", "ly"), ("楊啓邦", "tccc")})
        items = self.client.get("/api/speakers?source=tccc").json()["items"]
        self.assertEqual([i["name"] for i in items], ["楊啓邦"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd services/news && uv run python manage.py test articles.tests.test_api -v 1`
Expected: KeyError `source`

- [ ] **Step 3: 實作**

```python
from .models import Article, ArticleSource, ArticleStatus

SourceParam = Literal["ly", "tccc"]   # from typing import Literal；ninja 會對非法值回 422

class ArticleCardOut(Schema):
    ...
    source: str

class SpeakerOut(Schema):
    name: str
    source: str
    count: int
    latest_date: date_type | None
```

`_card` 加 `"source": article.source`。`list_articles` 加參數 `source: SourceParam | None = None`，`if source: queryset = queryset.filter(source=source)`。`speakers` 加同樣參數，`.values("speaker", "source")`，輸出 `"source": r["source"]`。`NinjaAPI(title=...)` 改成「議會質詢摘要 API」。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd services/news && uv run python manage.py test articles -v 1`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/news/articles/api.py services/news/articles/tests/test_api.py
git commit -m "feat(news): API 輸出來源並可依來源篩選"
```

---

### Task 9: 前端型別、來源標籤、篩選

**Files:**
- Modify: `web/news/src/lib/types.ts`
- Create: `web/news/src/lib/sources.ts`
- Create: `web/news/src/components/SourceBadge.astro`
- Modify: `web/news/src/components/Filters.astro`、`ArticleCard.astro`
- Modify: `web/news/src/lib/api.ts`（query 傳 `source`、fixture 補 `source`、`getSpeakers(source?)`）
- Modify: `web/news/src/pages/index.astro`（讀 `?source=`、傳給 Filters 與 getArticles）
- Modify: `web/news/src/fixtures/sample.json`（每篇加 `"source": "ly"`）

**Interfaces:**
- Produces:

```ts
// lib/sources.ts
export type ArticleSource = 'ly' | 'tccc';
export const SOURCES: ArticleSource[] = ['ly', 'tccc'];
export const SOURCE_LABEL: Record<ArticleSource, string> = { ly: '立法院', tccc: '臺中市議會' };
/** 發言者的稱謂：立委是「委員」、市議員是「議員」 */
export function memberTitle(source: string | undefined): string {
  return source === 'tccc' ? '議員' : '委員';
}
export function sourceLabel(source: string | undefined): string {
  return source === 'tccc' ? SOURCE_LABEL.tccc : SOURCE_LABEL.ly;
}
/** 原片所在的系統名稱，給「觀看原片」按鈕用 */
export function sourceSiteName(source: string | undefined): string {
  return source === 'tccc' ? '臺中市議會隨選視訊' : '立法院 IVOD';
}
export function isSource(v: string): v is ArticleSource {
  return v === 'ly' || v === 'tccc';
}
```

`types.ts`：`ArticleCard.source: ArticleSource`、`Speaker.source: ArticleSource`、`ArticleQuery.source?: string`。

- [ ] **Step 1: 型別與 sources.ts**（照上面寫）

- [ ] **Step 2: `SourceBadge.astro`**

```astro
---
import { sourceLabel } from '../lib/sources';
interface Props { source: string | undefined; class?: string }
const { source, class: extra = '' } = Astro.props;
const label = sourceLabel(source);
const tone = source === 'tccc'
  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
  : 'border-line bg-bg-soft text-muted';
---
<span class={`inline-flex items-center rounded-full border px-2 py-0.5 text-[0.68rem] tracking-wide ${tone} ${extra}`}>{label}</span>
```

（若專案的 Tailwind 主題沒有 `emerald`，Tailwind v4 內建色票一定有；`dark:` 若主題用 `prefers-color-scheme` 也可用。）

- [ ] **Step 3: `ArticleCard.astro`**：日期那一列的最後加 `<SourceBadge source={article.source} />`；speaker 連結加上 `?source=${article.source}`。

- [ ] **Step 4: `Filters.astro`**：Props 加 `source?: string`；chips 加 `source ? { key: 'source', label: `來源：${sourceLabel(source)}` }`；grid 改成五欄（`lg:grid-cols-[9rem_11rem_13rem_minmax(0,1fr)_auto]`），第一欄放：

```astro
<div>
  <label class={label} for="f-source">來源</label>
  <select id="f-source" name="source" class={field}>
    <option value="">全部</option>
    {SOURCES.map((s) => <option value={s} selected={s === source}>{SOURCE_LABEL[s]}</option>)}
  </select>
</div>
```

「委員」欄改：label 「發言者」、placeholder 「輸入姓名」、預設選項「全部發言者」、每個選項 `{s.name}（{SOURCE_LABEL[s.source]}，{s.count}）`，`value` 仍是姓名。

- [ ] **Step 5: `api.ts`**：`getArticles` 加 `if (query.source) params.set('source', query.source)`；fixture 篩選加 `if (query.source && a.source !== query.source) return false`；`getSpeakers(source?: string)` 帶 `?source=`，fixture 版 map 的 key 改成 `${a.source}:${a.speaker}` 並輸出 `source`。

- [ ] **Step 6: `index.astro`**：`const source = url.searchParams.get('source')?.trim() ?? ''`，只有 `isSource(source)` 才使用；傳給 `getArticles({..., source})`、`getSpeakers(source)`、`<Filters source={source} …/>`、`params`、`hasFilter`。

- [ ] **Step 7: Build**

Run: `cd web/news && npm run build`
Expected: 成功；`npm run check` 沒有新的型別錯誤。

- [ ] **Step 8: Commit**

```bash
git add web/news/src
git commit -m "feat(web): 卡片標示來源，篩選器可依來源與發言者"
```

---

### Task 10: 前端用語依來源切換、站名

**Files:**
- Modify: `web/news/src/pages/article/[slug].astro`（「觀看 IVOD 原片」→ `觀看${sourceSiteName(article.source)}原片`；speaker 連結帶 `?source=`）
- Modify: `web/news/src/components/SlideBlock.astro`（Props 加 `source?: string`；title 改 `在${sourceSiteName(source)}開啟原始影片（原片網址不支援時間參數，會從頭播放）`）
- Modify: `web/news/src/pages/speaker/[name].astro`（讀 `?source=`；`getArticles({ speaker, source })`、`getSpeakers(source)`；`Legislator` → `source === 'tccc' ? 'Councilor' : 'Legislator'`；description `${name}${memberTitle(source)}在${sourceLabel(source)}的質詢報導整理。`；「這位委員」「其他委員」→ 用 `memberTitle`；`others` 過濾同來源；連結帶 `?source=`）
- Modify: `web/news/src/components/SiteHeader.astro`（「立院質詢日報」→「質詢日報」；「Legislative Yuan Daily」→「Council Daily」）
- Modify: `web/news/src/components/SiteFooter.astro`（站名；說明改「內容整理自立法院 IVOD 與臺中市議會隨選視訊的公開影音。立法院逐字稿由院方 AI 產生、臺中市議會逐字稿由本站語音辨識產生，都可能有辨識錯誤；摘要為自動整理，正式引用請以原始影音與公報為準。」；連結列加 `https://vod.tccc.gov.tw/` 「臺中市議會隨選視訊」；資料來源那行改「資料來源：立法院 IVOD、臺中市議會隨選視訊」）
- Modify: `web/news/src/layouts/Base.astro`（`立院質詢日報` → `質詢日報`；description 「立法院與臺中市議會質詢的每日重點整理：逐段摘要、原片時間戳、完整逐字稿。」）
- Modify: `web/news/src/pages/index.astro`（「Legislative Yuan · Daily」→「立法院 · 臺中市議會」；lead 文案「每天彙整立法院 IVOD 與臺中市議會隨選視訊的質詢影音…」；「改看全部委員」→「改看全部發言者」；「等當天的 IVOD 處理完成後」→「等當天的影片處理完成後」）

- [ ] **Step 1: 逐檔修改**（照上表）

- [ ] **Step 2: Build 並本機看**

Run: `cd web/news && npm run build && USE_FIXTURE=1 npm run preview`，用瀏覽器看首頁、`/?source=tccc`、一篇文章、一個議員頁；截圖存到 scratchpad。
Expected: 沒有「立院」「IVOD」硬字串殘留在臺中文章上（`grep -rn "立院\|IVOD" web/news/src` 只剩 `sources.ts`、footer 與 LY 分支）。

- [ ] **Step 3: Commit**

```bash
git add web/news/src
git commit -m "feat(web): 站名與用語依來源切換"
```

---

### Task 11: 部署與文件

**Files:**
- Modify: `.env.docker.example`（加 `TCCC_ENABLED=true`、`GPU_JOB_TIMEOUT_SECONDS=2700` 與註解）
- Modify: `README.md`（新增「資料來源」一節：立法院／臺中市議會、臺中片段長度與每篇時間、`TCCC_ENABLED`、Whisper 在 CPU 跑、`ollama` 與 whisper 記憶體；`ingest_ivod` 說明改「抓立法院與臺中市議會」）
- Modify: `services/news/articles/management/commands/ingest_ivod.py` 的 `help`／docstring
- Modify: `compose.gpu.yaml` 註解（whisper-cache 那行補「臺中市議會片段全靠它」）

- [ ] **Step 1: 改檔**

- [ ] **Step 2: 全部測試**

Run: `uv run pytest tests -q && cd services/news && uv run python manage.py test articles -v 1 && cd ../../web/news && npm run build`
Expected: 全部 PASS

- [ ] **Step 3: 真實冒煙測試（不佔 GPU 的部分）**

```bash
cd services/news && uv run python manage.py shell -c "
from datetime import date
from articles.tccc_source import TcccDailySource
clips = TcccDailySource('https://vod.tccc.gov.tw').clips_for(date(2026, 9, 24))
print(len(clips)); [print(c.ivod_id, c.speaker, c.duration_seconds, c.meeting) for c in clips[:5]]
"
```

Expected: 列出 9/24 市政總質詢的幾位議員（含 tccc-14833 楊啓邦 3000）。

GPU 端到端（等 8 月批次跑完再做）：`curl -X POST http://localhost:8800/jobs -H "X-API-Key: …" -d '{"url":"https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14745","detailed":true}'`，14745 是 15 分鐘的片段，預期 6～8 分鐘完成、payload `video_id == "tccc-14745"`、有截圖。

- [ ] **Step 4: Commit、推上去、開 PR**

```bash
git add -A && git commit -m "docs: 臺中市議會來源的部署說明"
git push -u origin feat/tccc-source
gh pr create --title "feat: 新增臺中市議會質詢片段來源" --body "..."
```

---

## Self-review

- 規格覆蓋：網址辨識（T1）、TcccClient（T2）、HlsSectionGateway（T3）、音訊與字幕 gateway（T4）、路由／接線／video_id_of／提示詞（T5）、模型與多來源 discover（T6）、TcccDailySource（T7）、API（T8）、前端篩選與標籤（T9）、用語與站名（T10）、部署文件（T11）。規格「錯誤處理」表每一列都對應到 T2/T4/T7 的測試。
- 型別一致：`TcccRef(cno, ano)` 在 T1 定義、T2/T4/T5 使用；`tccc_id` 回 `"tccc-<ano>"` 與 T4 的 `AudioClip.video_id`、T7 的 `ivod_id` 同形；`SourceUnavailable` 在 T6 定義、T7 使用；`source` 值 `"ly"/"tccc"` 貫穿 T6–T10。
- 無佔位：每個步驟都有程式碼或明確的改法。
