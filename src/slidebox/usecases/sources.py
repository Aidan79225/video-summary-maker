"""判別影片來源：純字串處理，不碰網路。

目前只需要回答一個問題——「這是不是立法院 IVOD，是的話 id 多少」。
其餘一律走 YouTube（yt-dlp）那條路。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# 立法院 IVOD 的播放頁：/Play/Clip/1M/171180、/Play/Full/300K/17704
_PATH_RE = re.compile(r"^/Play/(?:Clip|Full)/[^/]+/(\d+)/?$", re.IGNORECASE)

_HOST = "ivod.ly.gov.tw"


def ivod_id(url: str) -> str | None:
    """IVOD 播放網址回傳它的 id，其餘回傳 None。

    主機用完整比對而不是 `in`：`ivod.ly.gov.tw.attacker.com` 也含有這串字，
    誤判的話程式會拿使用者貼的網址去對別人的伺服器組 API 請求。
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != _HOST:
        return None
    match = _PATH_RE.match(parsed.path)
    return match.group(1) if match else None
