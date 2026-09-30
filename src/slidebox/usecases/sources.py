"""判別影片來源：純字串處理，不碰網路。

回答「這是不是立法院 IVOD／臺中市議會／新北市議會的片段，是的話 id 多少」。
其餘一律走 YouTube（yt-dlp）那條路。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, urlparse

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


# --- 新北市議會 ---

_NTPC_HOST = "vod.ntp.gov.tw"
_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# 一律用 fullmatch 而不是 `^…$`：`$` 會放過結尾的換行（%0A 解碼後就是）。
_GUID_RE = re.compile(_GUID, re.IGNORECASE)
_NTPC_META_RE = re.compile(r"/VodCloudV2/VOD/ViewMetaData/?", re.IGNORECASE)
# 清單頁卡片的連結形狀：/VodCloudV2/VOD/ViewDetailMetaData/<guid>
_NTPC_DETAIL_RE = re.compile(rf"/VodCloudV2/VOD/ViewDetailMetaData/({_GUID})/?", re.IGNORECASE)


@dataclass(frozen=True)
class NtpcRef:
    """新北市議會的一段影片：網站用 GUID 當 assetID，一律存小寫。"""
    asset_id: str


def ntpc_clip(url: str) -> NtpcRef | None:
    """新北市議會「議事影音隨選視訊系統」的片段網址回傳 NtpcRef，其餘 None。

    認兩種形狀：公開的 `ViewMetaData?assetID=<guid>`（新聞服務送來的就是這個），
    與清單頁卡片連到的 `ViewDetailMetaData/<guid>`（使用者從清單點進去時網址列
    是這個）。網站是 ASP.NET，路徑與參數名稱不分大小寫，這裡也不分。
    GUID 要完整比對：它之後會被拼進送給市議會的請求，不能讓任意字串混進去。
    主機名同 ivod_id 用完整比對。
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if (parsed.hostname or "").lower() != _NTPC_HOST:
        return None
    detail = _NTPC_DETAIL_RE.fullmatch(parsed.path)
    if detail:
        return NtpcRef(asset_id=detail.group(1).lower())
    if not _NTPC_META_RE.fullmatch(parsed.path):
        return None
    for name, value in parse_qsl(parsed.query):
        if name.lower() == "assetid":
            return NtpcRef(asset_id=value.lower()) if _GUID_RE.fullmatch(value) else None
    return None


def ntpc_id(url: str) -> str | None:
    """GPU 回傳的識別碼：`ntpc-<guid>`。

    新聞服務的文章主鍵用的是檔案 key（同一個檔案可能被上架成兩個 GUID），
    那個值只有清單頁看得到，這裡拿不到；Pi 不檢查這個值，所以用 GUID 就好。
    """
    ref = ntpc_clip(url)
    return f"ntpc-{ref.asset_id}" if ref else None
