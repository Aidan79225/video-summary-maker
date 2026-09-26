"""LYAPI（ly.govapi.tw）的 HTTP 客戶端。

LYAPI 是公民科技社群整理的立法院資料，不是立法院官方——所以證據同時留
官方網址與這裡的 API 網址。對外的錯誤契約只有 SourceUnavailable：它讓
那一則主張變成無法查證，而不是讓整篇查核失敗。
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from ..domain.errors import SourceUnavailable

DEFAULT_BASE = "https://ly.govapi.tw/v2"
_TIMEOUT = 30.0
_PAGE_LIMIT = 100
# 一部法律最多兩三百條；翻頁上限只是防上游給了怪的 total_page 而無限迴圈
_MAX_PAGES = 10
# 打太快會被 LYAPI 429（實測：「LYAPI 取不到 /bills/202110224810000：HTTP Error
# 429」），重試幾次通常就過了；指數退避的秒數，長度就是最多重試次數
_RETRY_DELAYS = (2.0, 4.0, 8.0)
_MAX_RETRY_AFTER = 10.0


def _http_get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _total_pages(payload: dict) -> int:
    try:
        return int(payload.get("total_page") or 1)
    except (TypeError, ValueError):
        return 1


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    # Retry-After 是伺服器主動告知的等待秒數，比我們猜的退避更準；沒有這個標頭
    # 才退回固定的指數退避。封頂是防伺服器叫我們等一個離譜的秒數
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
        url = self.url(path, params)
        payload = self._fetch_with_retry(url, path)
        if not isinstance(payload, dict):
            raise SourceUnavailable(f"LYAPI 的 {path} 回應格式不符預期")
        return payload

    def _fetch_with_retry(self, url: str, path: str) -> object:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return self._fetch(url)
            # HTTPError 是 OSError 的子類別，要排在下面那個 except 之前才攔得到，
            # 這樣才能只針對 429 重試，其他狀態碼跟今天一樣直接算查不到
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < len(_RETRY_DELAYS):
                    self._sleep(_retry_delay(e, attempt))
                    continue
                raise SourceUnavailable(f"LYAPI 取不到 {path}：{str(e)[:200]}") from e
            # HTTPException（例如 IncompleteRead：連線中途斷掉）不是 OSError，要另外接
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise SourceUnavailable(f"LYAPI 取不到 {path}：{str(e)[:200]}") from e
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
            if page >= _total_pages(payload):
                break
        return rows

    def bills_search(self, keyword: str, term: int) -> list[dict]:
        # 常見法律一屆就有上百件相關議案（例如「醫療法」94 件），30 筆的舊上限
        # 會把要找的那件擠到下一頁去
        payload = self.get("/bills", {"q": f'"{keyword}"', "屆": term, "limit": 100})
        return list(payload.get("bills") or [])

    def bill(self, bill_id: str) -> dict:
        data = self.get(f"/bills/{bill_id}").get("data")
        return data if isinstance(data, dict) else {}
