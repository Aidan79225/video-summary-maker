"""判別影片來源：純字串處理，不碰網路。

目前只需要回答一個問題——「這是不是立法院 IVOD，是的話 id 多少」。
其餘一律走 YouTube（yt-dlp）那條路。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

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


# --- 臺中市議會 ---

_TCCC_HOST = "vod.tccc.gov.tw"


@dataclass(frozen=True)
class TcccRef:
    """臺中市議會的一段質詢：議員編號 + 片段編號。兩個都要，頁面是照議員分的。"""
    cno: str
    ano: str


def tccc_clip(url: str) -> TcccRef | None:
    """臺中市議會「議員個人質詢隨選視訊」的片段網址回傳 (cno, ano)，其餘 None。

    只認公開的 `index.asp`：內頁 `wb_region02.asp` 是 iframe 的內容，
    不該當成使用者貼的網址。主機名同 ivod_id 用完整比對。
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
