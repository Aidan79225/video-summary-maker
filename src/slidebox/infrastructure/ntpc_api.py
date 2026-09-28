"""新北市議會「議事影音隨選視訊系統」（vod.ntp.gov.tw/VodCloudV2）。

沒有 API：影片的 metadata 在公開的 ViewMetaData 頁（不需要 session），
串流網址在播放器頁（VideoPlayer，也不需要 cookie）的 <source> 裡，是 Wowza
的 HLS。這裡只做兩件事——解析 metadata 頁、解析播放器頁拿 m3u8。
會議紀錄要兩個多月後才上線而且沒有時間戳，逐字稿交給語音辨識；
Wowza 的串流網址加 `wowzaaudioonly=true` 就只給音訊，語音辨識不必下載影像。
"""
from __future__ import annotations

import html
import re
import ssl
import urllib.request
from dataclasses import dataclass

from ..domain.errors import NoSubtitlesAvailable
from ..usecases.sources import NtpcRef

_BASE = "https://vod.ntp.gov.tw"
_TIMEOUT = 30.0

# metadata 頁每一欄都是 <span … class="control-label " …>標籤：值</span>，冒號是全形。
# 只認 control-label：<meta og:title> 也有「屆次會期：…」，頁尾也有「地址：…」。
_FIELD_RE = re.compile(
    r'<span[^>]*class="control-label\s*"[^>]*>([^<>：]*)：([^<>]*)</span>', re.I)
# 播放器頁同一個網址出現三次（<source> 與兩段 JS），而且 & 被寫成 &amp;。
# 主機限定 vodwms.ntp.gov.tw：這個網址會直接交給 ffmpeg 去抓。
_STREAM_RE = re.compile(
    r"""https://vodwms\.ntp\.gov\.tw(?::\d+)?/[^'"\s<>]+?\.m3u8(?:\?[^'"\s<>]*)?""")
# 民國年：115-09-16
_ROC_DATE_RE = re.compile(r"(\d{2,3})-(\d{1,2})-(\d{1,2})")

_AUDIO_ONLY = "wowzaaudioonly=true"


def _ssl_context() -> ssl.SSLContext:
    """新北市議會的兩個網站跟臺中一樣，憑證鏈在 Python 3.13 預設的
    VERIFY_X509_STRICT 下會被拒絕（3.12 以前是接受的）。這裡只關掉 strict 旗標，
    主機名與信任鏈照常驗證，不是關掉驗證。"""
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT, context=_ssl_context()) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _hhmmss_seconds(text: str) -> int:
    """「影片長度」是 HH:MM:SS（「01:59:26」）。臺中是 HH:MM，別搞混。"""
    try:
        hours, minutes, seconds = text.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    except ValueError:
        return 0


def _roc_to_iso(text: str) -> str | None:
    """「開會日期」是民國年（115-09-16）；文章與標題一律用西元。"""
    match = _ROC_DATE_RE.fullmatch(text.strip())
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


@dataclass(frozen=True)
class NtpcRecord:
    asset_id: str
    session: str                  # 屆次會期：第4屆第8次定期會
    agenda: str                   # 議程：市政總質詢
    speakers: tuple[str, ...]     # 發言議員（含主席，依筆畫排序，不是發言順序）
    date: str
    duration_seconds: int

    @property
    def title(self) -> str:
        """與立法院、臺中的文章同形（日期 姓名－會議），前端才不用分兩套排版。

        發言議員是整個時段的名單而不是一位，多位以「、」串起（同臺中聯合質詢）。
        名單空的時候用院名頂上，標題才不會出現「日期 －會議」這種斷頭。
        """
        who = "、".join(self.speakers) or "新北市議會"
        return f"{self.date} {who}－{self.session} {self.agenda}"


def _fields(page: str) -> dict[str, str]:
    """標籤去掉所有空白再比對：「議　　程」中間是兩個 U+3000，值不動。"""
    fields: dict[str, str] = {}
    for label, value in _FIELD_RE.findall(page):
        key = "".join(html.unescape(label).split())
        fields.setdefault(key, html.unescape(value).strip())
    return fields


def parse_metadata_page(page: str, ref: NtpcRef) -> NtpcRecord:
    """從 ViewMetaData 頁取 metadata。屆次會期、議程、開會日期缺一項都算解析失敗；
    發言議員可以空（標題會退回院名），影片長度解析不了就是 0。"""
    fields = _fields(page)
    session = fields.get("屆次會期", "")
    agenda = fields.get("議程", "")
    date = _roc_to_iso(fields.get("開會日期", ""))
    if not (session and agenda and date):
        raise NoSubtitlesAvailable("新北市議會的影片資訊頁解析失敗，頁面格式可能改了")
    # 名單用半形逗號分隔；姓名本身可能含「．」（馬見Lahuy．Ipin），不能拿來切。
    speakers = tuple(name.strip() for name in fields.get("發言議員", "").split(",")
                     if name.strip())
    return NtpcRecord(
        asset_id=ref.asset_id,
        session=session,
        agenda=agenda,
        speakers=speakers,
        date=date,
        duration_seconds=_hhmmss_seconds(fields.get("影片長度", "")),
    )


def parse_stream_url(page: str) -> str | None:
    match = _STREAM_RE.search(page)
    return html.unescape(match.group(0)) if match else None


def audio_only(stream: str) -> str:
    """Wowza 的串流網址加 `wowzaaudioonly=true` 就只給音訊（約 64 kbps AAC，
    兩小時約 58 MB）：語音辨識用不到影像，不必把整支影片拉下來。"""
    return f"{stream}{'&' if '?' in stream else '?'}{_AUDIO_ONLY}"


class NtpcClient:
    """音訊與截圖兩個 gateway 共用一個 client。

    跟臺中不同，找串流不必先拿 metadata（播放器頁直接用 assetID），所以
    metadata 只有音訊那一步要用（標題）；仍快取最後一筆，同一段影片重跑時
    不必再打一次市議會。
    """

    def __init__(self, fetch=_http_get, base: str = _BASE):
        self._fetch = fetch
        self._base = base.rstrip("/")
        self._cached: tuple[NtpcRef, NtpcRecord] | None = None

    def metadata_url(self, ref: NtpcRef) -> str:
        return f"{self._base}/VodCloudV2/VOD/ViewMetaData?assetID={ref.asset_id}"

    def player_url(self, ref: NtpcRef) -> str:
        # 參數照抄 metadata 頁 iframe 的 src（type=Book_SD），研究時驗證過的就是這個形狀。
        return f"{self._base}/VodCloudV2/VodStream/VideoPlayer?assetID={ref.asset_id}&type=Book_SD"

    def record(self, ref: NtpcRef | None) -> NtpcRecord:
        ref = _require(ref)
        if self._cached and self._cached[0] == ref:
            return self._cached[1]
        try:
            page = self._fetch(self.metadata_url(ref))
        except OSError as e:
            raise NoSubtitlesAvailable(f"新北市議會的影片資訊頁抓不到：{e}") from e
        record = parse_metadata_page(page, ref)
        self._cached = (ref, record)
        return record

    def video_url(self, ref: NtpcRef | None) -> str:
        ref = _require(ref)
        try:
            page = self._fetch(self.player_url(ref))
        except OSError as e:
            raise NoSubtitlesAvailable(f"新北市議會的播放器頁抓不到：{e}") from e
        stream = parse_stream_url(page)
        if not stream:
            raise NoSubtitlesAvailable("新北市議會的播放器頁裡找不到串流網址")
        return stream

    def audio_url(self, ref: NtpcRef | None) -> str:
        return audio_only(self.video_url(ref))


def _require(ref: NtpcRef | None) -> NtpcRef:
    """composition 是 `client.video_url(ntpc_clip(url))`：路由接錯時 ref 會是 None。
    在這裡翻成 NoSubtitlesAvailable，截圖那一步才會降級成「沒有截圖」，而不是
    一個 AttributeError 讓整份摘要失敗。"""
    if ref is None:
        raise NoSubtitlesAvailable("這不是新北市議會的影片網址")
    return ref
