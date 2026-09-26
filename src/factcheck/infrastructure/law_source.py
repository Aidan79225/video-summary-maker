"""法條取證：法律名稱 → 發言當日有效的版本 → 指定條文與提到它的條文。"""
from __future__ import annotations

import urllib.parse
from collections import OrderedDict
from collections.abc import Sequence
from datetime import date

from ..domain.entities import Evidence, ExtractedClaim, SourceKind
from ..usecases.numbers import article_label, article_number
from .lyapi import LyApi

# 指定條文之外，最多再取幾條「內文提到它」的條文。委員常引義務條文，
# 罰則卻在後面另一條（醫療法第 24 條 → 第 106 條）。
MAX_RELATED = 2
EXCERPT_LIMIT = 1200
# 常駐服務會連續跑好幾個月；每部法律一兩百條，快取要有上限
_CACHE_SIZE = 16
# 罰則條文優先：罰鍰、罰金、有期徒刑、拘役通常包含具體數字，更容易比對
_PENALTY_TERMS = ("罰鍰", "罰金", "有期徒刑", "拘役")


def official_law_url(name: str) -> str:
    """全國法規資料庫的名稱查詢頁。實測可用；它沒有穩定的「依名稱直達」網址。"""
    return ("https://law.moj.gov.tw/Law/LawSearchResult.aspx?ty=ONEBAR&kw="
            + urllib.parse.quote(name))


def version_on(versions: Sequence[dict], on: date) -> dict | None:
    """發言當日有效的版本：日期不晚於發言日的最新一版。

    不能直接用現行版：醫療法最近一次修正是 2026-05-08，查錯版本會把
    「修法前說的是對的」判成錯。
    """
    day = on.isoformat()
    eligible = [v for v in versions if str(v.get("日期") or "") and str(v["日期"]) <= day]
    return max(eligible, key=lambda v: str(v["日期"]), default=None)


def _matches(law: dict, name: str) -> bool:
    names = [law.get("名稱") or ""]
    names += list(law.get("其他名稱") or []) + list(law.get("別名") or [])
    return name in names


def _is_penalty_article(row: dict) -> bool:
    """檢查是否為罰則條文。"""
    content = str(row.get("內容") or "")
    return any(term in content for term in _PENALTY_TERMS)


class LawSource:
    def __init__(self, api: LyApi):
        self._api = api
        self._contents: OrderedDict[str, list[dict]] = OrderedDict()

    def find(self, claim: ExtractedClaim, on: date) -> list[Evidence]:
        number = article_number(claim.article)
        if not claim.law or number is None:
            return []
        law = next((row for row in self._api.laws_by_name(claim.law)
                    if _matches(row, claim.law)), None)
        if law is None:
            return []
        law_id = law.get("法律編號")
        if not law_id:
            return []
        version = version_on(self._api.law_versions(str(law_id)), on)
        if version is None:
            return []
        version_id = version.get("版本編號")
        if not version_id:
            return []
        rows = self._rows(str(version_id))
        try:
            label = article_label(number)
        except ValueError:
            return []
        target = [r for r in rows if r.get("條號") == label]
        # 罰則條文優先：罰鍰、罰金等通常有具體數字，更容易比對
        referencing = [r for r in rows
                       if r.get("條號") and r.get("條號") != label
                       and label in str(r.get("內容") or "")]
        # 穩定排序：罰則優先，其餘保持文件順序
        referencing.sort(key=lambda r: (not _is_penalty_article(r), rows.index(r)))
        related = referencing[:MAX_RELATED]
        return [self._evidence(law, version, row) for row in target + related]

    def _rows(self, version_id: str) -> list[dict]:
        if version_id in self._contents:
            self._contents.move_to_end(version_id)
            return self._contents[version_id]
        rows = self._api.law_contents(version_id)
        self._contents[version_id] = rows
        if len(self._contents) > _CACHE_SIZE:
            self._contents.popitem(last=False)
        return rows

    def _evidence(self, law: dict, version: dict, row: dict) -> Evidence:
        name = str(law.get("名稱") or "")
        article = str(row.get("條號") or "")
        version_id = str(version.get("版本編號") or "")
        return Evidence(
            source=SourceKind.LAW,
            title=f"{name} {article}（{version.get('日期')} {version.get('動作')}版）",
            official_url=official_law_url(name),
            api_url=self._api.url("/law_contents", {"版本編號": version_id,
                                                    "條號": article}),
            excerpt=str(row.get("內容") or "")[:EXCERPT_LIMIT],
        )
