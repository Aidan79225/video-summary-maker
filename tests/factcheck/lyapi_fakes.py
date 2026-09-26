"""用實測存下來的 LYAPI 回應當假資料。fixture 是真的回應，欄位名與形狀都對得上。"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeFetch:
    """依路徑回應。routes 的值可以是 dict，或吃 query（dict[str, str]）回 dict 的函式。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.removeprefix("/v2")
        if path not in self.routes:
            raise OSError(f"沒有這個路徑的假資料：{path}")
        route = self.routes[path]
        if callable(route):
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            return route(query)
        return route
