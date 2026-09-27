"""法條原文：法律名稱 → 發言當日有效的版本 → 指定條文與提到它的條文。

摘要卡上的關鍵數字若是在講某部法律某一條（GPU 端已經確認講者自己講了
法律名稱與條號），這裡把條文原文抓回來附在數字旁邊。只附來源、不判對錯：
「他說 3 到 5 萬，條文在這裡」讓讀者自己看，沒有判定就沒有誹謗的風險，
也不需要人工審核。

資料來自 LYAPI（ly.govapi.tw），是公民科技社群整理的立法院資料，不是
官方——所以每筆來源同時留官方網址（全國法規資料庫）與 LYAPI 的網址。
只用 stdlib，與 ivod_source 同一個風格。
"""
from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import date

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://ly.govapi.tw/v2"
_TIMEOUT = 30.0
_PAGE_LIMIT = 100
# 一部法律最多兩三百條；翻頁上限只是防上游給了怪的 total_page 而無限迴圈
_MAX_PAGES = 10
# 打太快會被 LYAPI 429，重試幾次通常就過了；退避秒數的長度就是最多重試次數
_RETRY_DELAYS = (2.0, 4.0, 8.0)
_MAX_RETRY_AFTER = 10.0
# 指定條文之外，最多再取幾條「內文提到它」的條文。委員常引義務條文，
# 罰則卻在後面另一條（醫療法第 24 條 → 第 106 條）。
MAX_RELATED = 2
EXCERPT_LIMIT = 1200
# 常駐服務會連續跑好幾個月；每部法律一兩百條，快取要有上限
_CACHE_SIZE = 16
# 罰則條文優先：罰鍰、罰金、有期徒刑、拘役通常就是委員引用的數字所在
_PENALTY_TERMS = ("罰鍰", "罰金", "有期徒刑", "拘役")
_CN_ORDINAL = "零一二三四五六七八九"


class LawUnavailable(Exception):
    """LYAPI 這次拿不到。屬於暫時性問題，這一篇先沒有來源，不影響文章本身。"""


def _http_get(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    # Retry-After 是伺服器主動告知的等待秒數，比我們猜的退避更準
    headers = getattr(error, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            return min(float(retry_after), _MAX_RETRY_AFTER)
        except ValueError:
            pass
    return _RETRY_DELAYS[attempt]


class LyApi:
    def __init__(self, base: str = DEFAULT_BASE,
                 fetch: Callable[[str], object] = _http_get,
                 sleep: Callable[[float], None] = time.sleep):
        self._base = base.rstrip("/")
        self._fetch = fetch
        self._sleep = sleep

    def url(self, path: str, params: Mapping[str, object] | None = None) -> str:
        query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        return f"{self._base}{path}{query}"

    def get(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        payload = self._fetch_with_retry(self.url(path, params), path)
        if not isinstance(payload, dict):
            raise LawUnavailable(f"LYAPI 的 {path} 回應格式不符預期")
        return payload

    def _fetch_with_retry(self, url: str, path: str) -> object:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return self._fetch(url)
            # HTTPError 是 OSError 的子類別，要排在前面才能只針對 429 重試
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < len(_RETRY_DELAYS):
                    self._sleep(_retry_delay(e, attempt))
                    continue
                raise LawUnavailable(f"LYAPI 取不到 {path}：{str(e)[:200]}") from e
            # IncompleteRead（連線中途斷掉）不是 OSError，要另外接
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise LawUnavailable(f"LYAPI 取不到 {path}：{str(e)[:200]}") from e
        raise AssertionError("unreachable：迴圈每次都會 return 或 raise")

    def laws_by_name(self, name: str) -> list[dict]:
        # 引號是必要的：不加的話是模糊搜尋，搜「醫療法」會先回所得稅法
        payload = self.get("/laws", {"q": f'"{name}"', "limit": 20})
        return list(payload.get("laws") or [])

    def law_versions(self, law_id: str) -> list[dict]:
        payload = self.get(f"/laws/{law_id}/versions", {"limit": 100})
        return list(payload.get("lawversions") or [])

    def law_contents(self, version_id: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            payload = self.get("/law_contents", {"版本編號": version_id,
                                                 "limit": _PAGE_LIMIT, "page": page})
            rows.extend(payload.get("lawcontents") or [])
            try:
                pages = int(payload.get("total_page") or 1)
            except (TypeError, ValueError):
                pages = 1
            if page >= pages:
                break
        return rows


def official_law_url(name: str) -> str:
    """全國法規資料庫的名稱查詢頁。它沒有穩定的「依名稱直達」網址。"""
    return ("https://law.moj.gov.tw/Law/LawSearchResult.aspx?ty=ONEBAR&kw="
            + urllib.parse.quote(name))


def version_on(versions: Sequence[dict], on: date) -> dict | None:
    """發言當日有效的版本：日期不晚於發言日的最新一版。

    不能直接用現行版：醫療法最近一次修正是 2026-05-08，委員在修法前講的
    數字要對照修法前的條文。
    """
    day = on.isoformat()
    eligible = [v for v in versions if str(v.get("日期") or "") and str(v["日期"]) <= day]
    return max(eligible, key=lambda v: str(v["日期"]), default=None)


def to_chinese(n: int) -> str:
    """1～999 轉成法條用的中文數字：106 → 一百零六、110 → 一百一十。"""
    if not 0 < n < 1000:
        raise ValueError(f"條號超出範圍：{n}")
    hundreds, rest = divmod(n, 100)
    tens, ones = divmod(rest, 10)
    tail = _CN_ORDINAL[ones] if ones else ""
    if hundreds:
        head = _CN_ORDINAL[hundreds] + "百"
        if rest == 0:
            return head
        if tens == 0:
            return head + "零" + tail
        return head + _CN_ORDINAL[tens] + "十" + tail
    if tens:
        return ("" if tens == 1 else _CN_ORDINAL[tens]) + "十" + tail
    return tail


def article_number(text: str) -> int | None:
    """GPU 端給的條號是正規化過的「第N條」；這裡只認阿拉伯數字。"""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits and 0 < int(digits) < 1000 else None


def _matches(law: dict, name: str) -> bool:
    names = [law.get("名稱") or ""]
    names += list(law.get("其他名稱") or []) + list(law.get("別名") or [])
    return name in names


def _is_penalty(row: dict) -> bool:
    content = str(row.get("內容") or "")
    return any(term in content for term in _PENALTY_TERMS)


class LawSource:
    """回傳的是給 JSON 用的 dict，直接存進 Article.brief。"""

    def __init__(self, api: LyApi):
        self._api = api
        self._contents: OrderedDict[str, list[dict]] = OrderedDict()

    def find(self, law_name: str, article: str, on: date) -> list[dict]:
        number = article_number(article)
        if not law_name or number is None:
            return []
        law = next((row for row in self._api.laws_by_name(law_name)
                    if _matches(row, law_name)), None)
        law_id = (law or {}).get("法律編號")
        if not law_id:
            return []
        version = version_on(self._api.law_versions(str(law_id)), on)
        version_id = (version or {}).get("版本編號")
        if not version_id:
            return []
        rows = self._rows(str(version_id))
        label = f"第{to_chinese(number)}條"
        target = [r for r in rows if r.get("條號") == label]
        referencing = [r for r in rows
                       if r.get("條號") and r.get("條號") != label
                       and label in str(r.get("內容") or "")]
        # 穩定排序：罰則優先，其餘保持條文順序
        referencing.sort(key=lambda r: (not _is_penalty(r), rows.index(r)))
        return [self._source(law, version, row)
                for row in target + referencing[:MAX_RELATED]]

    def _rows(self, version_id: str) -> list[dict]:
        if version_id in self._contents:
            self._contents.move_to_end(version_id)
            return self._contents[version_id]
        rows = self._api.law_contents(version_id)
        self._contents[version_id] = rows
        if len(self._contents) > _CACHE_SIZE:
            self._contents.popitem(last=False)
        return rows

    def _source(self, law: dict, version: dict, row: dict) -> dict:
        name = str(law.get("名稱") or "")
        article = str(row.get("條號") or "")
        version_id = str(version.get("版本編號") or "")
        return {
            "law": name,
            "article": article,
            "title": f"{name} {article}（{version.get('日期')} {version.get('動作')}版）",
            "excerpt": str(row.get("內容") or "")[:EXCERPT_LIMIT],
            "official_url": official_law_url(name),
            "api_url": self._api.url("/law_contents", {"版本編號": version_id,
                                                       "條號": article}),
        }
