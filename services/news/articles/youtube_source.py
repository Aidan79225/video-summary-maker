"""YouTube 影片的網址解析與影片資料：add_video 在寫進資料庫之前要知道的事。

Pi 的相依刻意最少、映像裡沒有 yt-dlp，所以全部用標準函式庫（urllib、json、re、html.parser）：

- 網址：嚴格解析成 11 碼的影片 id，一律換成正規網址 https://www.youtube.com/watch?v=<id>。
  同一支影片用 youtu.be、&t=30s、shorts/ 貼都是同一篇；GPU 的檢查點以網址為鍵，正規化之後
  重試才接得回去。
- 影片資料：oEmbed（標題、頻道）→ 影片頁的 ytInitialPlayerResponse（日期、長度、播放狀態、
  是不是直播／不公開／音樂／Shorts）→ head 的 microdata → 文字裡的正規表示式，逐層補。
  **影片不存在時影片頁照樣回 200**（實測），所以一定要看 playabilityStatus。
- 預檢（preflight）：沒有救的、或會後悔的影片，在寫進資料庫之前就拒絕，並說出該加哪個旗標。

抓取用注入的 callable（fetch_oembed、fetch_page），所以測試不碰網路。解析函式都是純函式。
設計與每個決定的理由見 docs/superpowers/specs/2026-10-31-youtube-videos-design.md。
"""
from __future__ import annotations

import copy
import functools
import http.client
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ivod_source import SourceUnavailable

# Article.ivod_id 的前綴：yt-<11 碼影片 id>
ID_PREFIX = "yt-"
# 影片 id 剛好 11 個字。用 fullmatch，絕不截斷：yt-dlp 會把太長的 id 默默切成前 11 碼，
# 那等於處理一支沒人指名的影片。
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# 剛好 11 個字、卻是網址裡的固定字（嵌入的播放清單、直播頻道），不是影片
_NOT_IDS = frozenset({"videoseries", "live_stream"})

_WATCH_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                          "youtube-nocookie.com", "www.youtube-nocookie.com"})
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
# /<前綴>/<id>：Shorts、直播、嵌入，以及舊的 /v/ 與 /e/
_ID_PATH = re.compile(r"/(?:shorts|live|embed|v|e)/([^/]+)/?")
_CHANNEL_PATH = re.compile(r"/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/.*)?")

# 影片頁與 oEmbed 的請求。影片頁用瀏覽器的 UA（實測 urllib 的預設 UA 拿到一樣的資料，但這是
# 比較不容易被當成機器人的那個）；Accept-Language 固定英文，類別與標題才跟 yt-dlp 看到的一致；
# urllib 不會自己解壓縮，所以要 identity；SOCS=CAI 是 YouTube 的「已同意」cookie，歐洲的 IP
# 不帶的話會被轉去 consent 頁。
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/130.0.0.0 Safari/537.36")
_PAGE_HEADERS = {"User-Agent": _BROWSER_UA,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity",
                 "Cookie": "SOCS=CAI"}
_OEMBED_HEADERS = {"User-Agent": _BROWSER_UA, "Accept": "application/json",
                   "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity"}
_PAGE_TIMEOUT = 15.0
_OEMBED_TIMEOUT = 10.0
# 逾時只管「每一次讀取」：對方每隔幾秒送一個位元組，每次都沒逾時，卻永遠讀不完。所以另外給一個
# 整體的期限（單調時鐘，從開始讀本文算起）
_PAGE_DEADLINE = 30.0
_OEMBED_DEADLINE = 15.0
# 影片頁 0.9～1.6 MB；4 MB 擋住異常回應，播放器回應在最前面的 1 MB 內
_PAGE_MAX_BYTES = 4 * 1024 * 1024
_OEMBED_MAX_BYTES = 256 * 1024
_READ_BLOCK = 64 * 1024
# 轉址只能在這幾個主機之間跳（consent 頁在 consent.youtube.com，要跟過去才認得出是同意頁）。
# 離開這幾台的轉址（機器人檢查會轉去 www.google.com/sorry/）不跟，把它想去哪裡交給呼叫端判斷。
_REDIRECT_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                             "consent.youtube.com"})

# GPU 的 POST /jobs 只收 1～50 頁
_MIN_PAGES, _MAX_PAGES = 1, 50


class UrlRejected(ValueError):
    """這個網址不能用；訊息就是給使用者看的原因。"""


class VideoRefused(Exception):
    """預檢沒過；訊息就是給使用者看的原因。flag 是能蓋過它的旗標（沒有就是 None）。"""

    def __init__(self, message: str, flag: str | None = None):
        super().__init__(message)
        self.flag = flag


class MetadataUnavailable(SourceUnavailable):
    """oEmbed 與影片頁都讀不到：連不上、被 YouTube 擋下、或格式認不得。訊息列出每一層的原因。"""


# --- 網址 ---


def canonical_url(video_id: str) -> str:
    """一律存這個網址：同一支影片只有一種寫法，GPU 的檢查點才接得回去。"""
    return f"https://www.youtube.com/watch?v={video_id}"


def ivod_id_for(video_id: str) -> str:
    return f"{ID_PREFIX}{video_id}"


def video_id_from_ivod_id(ivod_id: str) -> str | None:
    """yt-<id> → <id>；不是這個形狀就是 None。"""
    if ivod_id.startswith(ID_PREFIX) and is_video_id(ivod_id[len(ID_PREFIX):]):
        return ivod_id[len(ID_PREFIX):]
    return None


def placeholder_title(video_id: str) -> str:
    """連標題都讀不到時登記用的佔位標題；處理完成後會被 GPU 的標題換掉（其他標題則保留）。"""
    return f"YouTube 影片 {video_id}"


def is_video_id(text: str) -> bool:
    return VIDEO_ID.fullmatch(text) is not None and text not in _NOT_IDS


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    # 網址裡值得告訴使用者的事（例如帶了播放清單，只處理 v= 那一支）
    notes: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return canonical_url(self.video_id)

    @property
    def ivod_id(self) -> str:
        return ivod_id_for(self.video_id)


def parse_video_url(text: str) -> VideoRef:
    """手打的網址 → 影片 id。接受的形狀與拒絕的理由見設計文件的「指令介面」。

    主機名單是精確比對（youtube.com.evil.com、evilyoutube.com、帳號＠主機的寫法都不過）；
    id 一定要剛好 11 碼。不是這支影片的東西——清單、頻道、搜尋、clip——一律拒絕並說明，
    不猜。拒絕時丟 UrlRejected，訊息就是原因。
    """
    raw = (text or "").strip()
    if not raw:
        raise UrlRejected("沒有給網址")
    if is_video_id(raw):
        return VideoRef(raw)
    if raw in _NOT_IDS:
        raise UrlRejected("這是嵌入的播放清單／直播頻道的固定字，不是單一影片")
    if re.search(r"\s", raw):
        raise UrlRejected("網址裡有空白或換行，看起來貼了不只一個網址；一次一個，各自加上引號")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", raw):
        raw = "https://" + raw
    try:
        url = urlsplit(raw)
        host, port = (url.hostname or "").lower(), url.port
    except ValueError:
        raise UrlRejected("網址的格式不正確") from None
    if url.scheme.lower() not in ("http", "https"):
        raise UrlRejected(f"只接受 http／https 的網址（這個是 {url.scheme}://）")
    if host not in _WATCH_HOSTS and host not in _SHORT_HOSTS:
        raise UrlRejected(f"不是 YouTube 的網址（主機：{host or '沒有'}）；只收 youtube.com 與 youtu.be 的影片")
    if url.username is not None or url.password is not None:
        raise UrlRejected("網址裡有帳號密碼的部分，不接受")
    if port not in (None, 80, 443):
        raise UrlRejected(f"網址指定了奇怪的連接埠 {port}，不接受")
    if host in _SHORT_HOSTS:
        return VideoRef(_checked_id(url.path.strip("/"), "youtu.be 網址後面"))
    return _from_youtube_path(url.path or "/", parse_qs(url.query))


def _from_youtube_path(path: str, query: dict[str, list[str]]) -> VideoRef:
    if path in ("/watch", "/watch/"):
        versions = set(query.get("v", []))
        if len(versions) > 1:
            raise UrlRejected("網址裡有不只一個 v= 參數，沒辦法判斷是哪一支影片")
        if versions:
            video_id = _checked_id(next(iter(versions)), "v= 後面")
            if "list" in query:
                return VideoRef(video_id, ("網址帶了播放清單（list=），只處理 v= 這一支影片，清單裡的其他影片不會處理",))
            return VideoRef(video_id)
        if "list" in query:
            raise UrlRejected(_PLAYLIST)
        raise UrlRejected("網址裡沒有 v= 影片 id")
    matched = _ID_PATH.fullmatch(path)
    if matched:
        return VideoRef(_checked_id(matched.group(1), "網址路徑裡"))
    if path.startswith("/playlist") or ("list" in query and not query.get("v")):
        raise UrlRejected(_PLAYLIST)
    if _CHANNEL_PATH.fullmatch(path):
        raise UrlRejected("這是頻道的網址，不是單一影片；請打開要處理的那支影片，貼影片自己的網址"
                          "（頻道的 /live 每次指向不同的影片）")
    if path.startswith(("/results", "/search")):
        raise UrlRejected("這是搜尋結果頁，不是單一影片；請貼影片自己的網址")
    if path.startswith("/clip/"):
        raise UrlRejected("這是 clip（剪輯片段）的連結，網址裡沒有影片 id；請貼原影片的網址")
    if path == "/live_stream":
        raise UrlRejected("這是直播頻道的嵌入網址，不是單一影片；請貼影片自己的網址")
    if path == "/" or path.startswith(("/feed", "/watch")):
        raise UrlRejected("網址裡找不到影片 id（要 watch?v=…、youtu.be/… 這種指到單一影片的網址）")
    raise UrlRejected("看不出這是哪一種 YouTube 影片網址；支援 watch?v=、youtu.be/、/shorts/、/live/、/embed/")


_PLAYLIST = "這是播放清單的網址，不是單一影片；請打開要處理的那支影片，貼影片自己的網址（watch?v=… 或 youtu.be/…）"


def _checked_id(value: str, where: str) -> str:
    if value in _NOT_IDS:
        raise UrlRejected("這是嵌入的播放清單／直播頻道的網址，不是單一影片；請貼影片自己的網址")
    if VIDEO_ID.fullmatch(value):
        return value
    raise UrlRejected(f"{where}不是剛好 11 碼的影片 id（可能少複製或多複製了字元）：{value[:40]!r}")


def dash_ids_last(args: list[str] | None) -> list[str] | None:
    """命令列上單獨的影片 id 若以「-」開頭（約 64 分之一），argparse 會把它當成選項，報一個看不懂的
    「少了網址」。把這種引數搬到最後面、前面補上「--」。其他引數的順序不變；已經有「--」就原樣不動。
    以「--」開頭的不碰：那是真正的選項（--traceback 剛好也是 11 個字）。"""
    if args is None or "--" in args:
        return args
    ids = [a for a in args if a.startswith("-") and not a.startswith("--") and VIDEO_ID.fullmatch(a)]
    if not ids:
        return args
    return [a for a in args if a not in ids] + ["--", *ids]


def allow_dash_ids(parser):
    """讓 argparse 的 parse_args 先過 dash_ids_last，並且讓位置引數可以散在選項之間（Django 指令的
    create_parser 用）。

    dash_ids_last 把以「-」開頭的 id 接在最後面的「--」之後。一般的 parse_args 遇到第一個選項時，前面的
    位置引數就算「全部」了，選項後面的網址一律變成多出來的引數：`add_video A -abcdefghij --speakers 1`
    會是 unrecognized arguments: -- -abcdefghij，連普通的 `A --speakers 1 B` 都不行（--speakers 1 現在是
    必填，幾乎每個批次都有選項）。parse_intermixed_args 先把選項解析掉，再解析剩下的位置引數。
    """
    def parse_args(args=None, namespace=None):
        return parser.parse_intermixed_args(dash_ids_last(args), namespace)

    parser.parse_args = parse_args
    return parser


# --- 抓取 ---


@dataclass(frozen=True)
class HttpResponse:
    status: int
    # 轉址跟完之後的最終網址；沒跟的轉址（離開 YouTube）則是它想去的網址
    url: str
    body: bytes


Fetch = Callable[[str], HttpResponse]


class _StayOnYoutube(urllib.request.HTTPRedirectHandler):
    """轉址只在 YouTube 的主機之間跳；離開的不跟，交回那個 3xx 讓呼叫端看它想去哪裡。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (target.hostname or "").lower() not in _REDIRECT_HOSTS or target.scheme not in ("http", "https"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_body(resp, max_bytes: int, started: float, deadline: float) -> bytes:
    """最多 max_bytes 個位元組；從 started（單調時鐘）算起，整個讀完不能超過 deadline 秒。

    一次只拿一次系統呼叫讀得到的量（read1）：read(n) 會等到湊滿 n 個位元組，對方一次只送一個位元組的話，
    兩次期限檢查之間可以隔上好幾個小時。連線在 Content-Length 之前就斷了要丟 IncompleteRead——
    http.client 自己只會默默回傳讀到的部分，缺掉的那一段可能剛好有「不公開」的旗標。
    """
    chunks: list[bytes] = []
    total = 0
    while total < max_bytes:
        if time.monotonic() - started > deadline:
            raise TimeoutError(f"讀取超過 {deadline:g} 秒還沒讀完")
        block = resp.read1(min(_READ_BLOCK, max_bytes - total))
        if not block:
            missing = getattr(resp, "length", None)
            if missing:
                raise http.client.IncompleteRead(b"".join(chunks), missing)
            break
        chunks.append(block)
        total += len(block)
    return b"".join(chunks)


def _http_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int,
              deadline: float = _PAGE_DEADLINE) -> HttpResponse:
    """GET。HTTP 的錯誤狀態（404、429…）當成回應交回去，連不上、逾時才丟 OSError。

    timeout 是每一次讀取的逾時，deadline 是讀本文的整體期限（見 _read_body）。被截斷的回應
    （http.client.IncompleteRead）與 urllib 自己解析轉址網址時丟的 ValueError 都不是 OSError，
    呼叫端只接 OSError，所以在這裡轉。
    """
    opener = urllib.request.build_opener(_StayOnYoutube)
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.geturl(), _read_body(resp, max_bytes, started, deadline))
    except urllib.error.HTTPError as e:
        where = getattr(e, "filename", "") or url
        location = e.headers.get("Location") if e.headers is not None else None
        if location and 300 <= e.code < 400:
            try:
                where = urljoin(where, location)
            except ValueError:
                where = location          # 格式不對的轉址：原樣交給呼叫端（_page_problem 認得出來）
        try:
            body = _read_body(e, max_bytes, started, deadline)
        except (OSError, http.client.HTTPException):
            body = b""
        return HttpResponse(e.code, where, body)
    except http.client.HTTPException as e:
        raise OSError(f"連線被中斷：{e}") from e
    except ValueError as e:
        raise OSError(f"網址的格式不正確：{e}") from e


def http_page(url: str) -> HttpResponse:
    return _http_get(url, _PAGE_HEADERS, _PAGE_TIMEOUT, _PAGE_MAX_BYTES, _PAGE_DEADLINE)


def http_oembed(url: str) -> HttpResponse:
    return _http_get(url, _OEMBED_HEADERS, _OEMBED_TIMEOUT, _OEMBED_MAX_BYTES, _OEMBED_DEADLINE)


def oembed_url(video_id: str) -> str:
    return "https://www.youtube.com/oembed?url=" + quote(canonical_url(video_id), safe="") + "&format=json"


# --- 影片資料 ---


@dataclass(frozen=True)
class VideoMeta:
    """一支影片登記前要知道的事。None／False／0 都是「不知道或沒有」，preflight 會分辨。"""

    video_id: str
    title: str | None = None
    channel: str | None = None
    channel_url: str | None = None
    # 直播開始（或上傳）那一刻的**台北時間**日期；不知道是 None
    date: date | None = None
    duration_seconds: int = 0
    is_live: bool = False
    is_upcoming: bool = False
    # 剛結束的直播：YouTube 還在轉檔，沒有字幕、下載很慢
    is_post_live: bool = False
    # unlisted 與 shorts_eligible：True／False＝播放器回應的 microformat 說的；None＝不知道（沒讀到影片頁，或
    # microformat 裡沒有這個欄位）。真實的影片頁，能播放的每一頁都帶這兩個旗標（固定資料裡六支都是），所以影片能播
    # 卻是 None 是「沒檢查」，不是「沒問題」：預檢要求 --allow-unverified（見 preflight）。category 不一樣，
    # 沒有類別就是沒有類別，只是少了一個音樂的提示
    unlisted: bool | None = None
    category: str | None = None
    shorts_eligible: bool | None = None
    # True＝可以播；False＝不能（不存在、私人、地區限制、尚未首播…），原因在 playability_reason；
    # None＝沒讀到影片頁，不知道
    playable: bool | None = None
    playability_reason: str = ""
    # 影片頁有沒有讀到（認得出某個欄位就算）：沒有的話不公開、直播、音樂、Shorts 與長度都沒辦法檢查
    page_read: bool = False
    # 影片頁裡的播放器回應有沒有解出來，而且帶著 playabilityStatus。只讀到 <title> 或 microdata 時是
    # False：不公開、直播中、Shorts 與能不能播放都只在播放器回應裡，沒有它就是「沒檢查」，不是「沒問題」
    player_read: bool = False
    # 讀取過程中遇到的問題（連不上、被擋下…），給使用者看
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class OEmbed:
    title: str
    author_name: str
    author_url: str


def parse_oembed(body: bytes | str | None) -> OEmbed | None:
    """oEmbed 的 JSON。認不得（空的、不是物件、沒有標題也沒有頻道）就是 None。

    錯誤回應的本文是純文字（「Not Found」），所以呼叫端不能對非 200 的回應呼叫這個。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError, OverflowError, RecursionError):
        # 不是 JSON、型別不對、數字大到轉不了、巢狀深到超過遞迴上限：都是「認不得」
        return None
    if not isinstance(data, dict):
        return None

    def text(key: str) -> str:
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    if not text("title") and not text("author_name"):
        return None
    return OEmbed(text("title"), text("author_name"), _https(text("author_url")))


def _https(url: str) -> str:
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


@functools.cache
def _taipei():
    """台北時區。沒有時區資料庫（精簡的容器）時退回固定 +8：台北沒有夏令時間，答案一樣。"""
    try:
        return ZoneInfo("Asia/Taipei")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), "Asia/Taipei")


def taipei_date(text: str | None) -> date | None:
    """ISO 時間字串 → 台北時間的日期。

    影片頁的日期帶太平洋時區（-07:00）：台北時間白天上傳的影片會差一天（動物園那支：太平洋
    2005-04-23、台北 2005-04-24）。沒有時區的只有日期可靠，原樣取用；讀不懂是 None——
    連「換成台北時間之後超出 date 的範圍」（0001-01-01+08:00、9999-12-31-07:00，OverflowError）也是。
    """
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
        return moment.date() if moment.tzinfo is None else moment.astimezone(_taipei()).date()
    except (ValueError, OverflowError):
        return None


# 長度的各部分只收 ASCII 數字且位數有上限：\d 會收「²」之外的各國數字，int() 又會在 4300 位數丟 ValueError
_ISO_DURATION = re.compile(r"PT(?:([0-9]{1,6})H)?(?:([0-9]{1,9})M)?(?:([0-9]{1,9})S)?")
_SECONDS = re.compile(r"[0-9]{1,9}")


def _seconds(value: object) -> int | None:
    """秒數：只收 1～9 位的 ASCII 數字字串（不是、或大到不可能就是不知道），不丟例外。"""
    text = value.strip() if isinstance(value, str) else ""
    return int(text) if _SECONDS.fullmatch(text) else None


def _iso_duration(text: str | None) -> int | None:
    """microdata 的長度（PT95M16S，分鐘可以超過 59）。"""
    matched = _ISO_DURATION.fullmatch((text or "").strip())
    if not matched or not any(matched.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in matched.groups())
    return hours * 3600 + minutes * 60 + seconds


# 別人的網站改版、被截斷、型別不對、被刻意餵怪東西時，解析函式會丟的例外。每一層各自攔下、記成問題，
# 換下一層；不攔的話，一個壞欄位就是整個指令（連同一次給的其他網址）的 traceback
_PARSE_ERRORS = (AttributeError, TypeError, ValueError, OverflowError, RecursionError)


class _Facts:
    """三層解析共用的草稿：每一層只補前面沒填的欄位。"""

    def __init__(self) -> None:
        self.status: str | None = None
        self.reason = ""
        self.title: str | None = None
        self.channel: str | None = None
        self.channel_url: str | None = None
        self.upload: str | None = None
        self.start: str | None = None
        self.live_content = False
        self.duration: int | None = None
        self.is_live = False
        self.is_upcoming = False
        self.is_post_live = False
        self.unlisted: bool | None = None
        self.category: str | None = None
        self.shorts: bool | None = None

    def known(self) -> bool:
        return any((self.status, self.title, self.channel, self.upload, self.duration))


_PLAYER_RESPONSE = re.compile(r"ytInitialPlayerResponse\s*=\s*")


def _player_response(text: str) -> dict | None:
    """影片頁裡的 `var ytInitialPlayerResponse = {...};`。raw_decode 懂字串與括號，解完一個物件就停。

    標題與描述會原樣出現在真正的腳本之前（<title>、<meta>；實測 <title> 在 781,736、腳本在 789,733）。
    上傳者把影片標題取成 `var ytInitialPlayerResponse = {};` 的話，第一個解得出來的物件就是 {}，
    不公開、直播、Shorts 的檢查全部悄悄失效。所以只認長得像播放器回應的物件（有 playabilityStatus 或
    videoDetails）；不是的跳過、接著找下一個。反射進 HTML 或 JSON 字串的文字，引號都已經被跳脫，
    做不出有鍵的物件。
    """
    decoder = json.JSONDecoder()
    for matched in _PLAYER_RESPONSE.finditer(text):
        start = matched.end()
        if text[start:start + 1] != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict) and (isinstance(obj.get("playabilityStatus"), dict)
                                      or isinstance(obj.get("videoDetails"), dict)):
            return obj
    return None


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dig(value: object, *keys: str) -> object:
    """巢狀字典裡的值；中間任何一層不是字典（別人的網站改了形狀）就是 None，不丟例外。"""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _simple_text(value: object) -> str | None:
    if isinstance(value, dict):
        runs = value.get("runs")
        joined = "".join(run["text"] for run in runs if isinstance(run, dict) and isinstance(run.get("text"), str)
                         ) if isinstance(runs, list) else ""
        return _str(value.get("simpleText")) or _str(joined)
    return _str(value)


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _from_player_response(text: str, facts: _Facts) -> bool:
    """第一層：影片頁的 JSON。裡面什麼都有，所以優先。解得出來回 True。"""
    pr = _player_response(text)
    if pr is None:
        return False
    status = _dict(pr.get("playabilityStatus"))
    details = _dict(pr.get("videoDetails"))
    micro = _dict(_dig(pr, "microformat", "playerMicroformatRenderer"))
    live = _dict(micro.get("liveBroadcastDetails"))

    facts.status = _str(status.get("status"))
    facts.reason = (_str(status.get("reason"))
                    or _simple_text(_dig(status, "errorScreen", "playerErrorMessageRenderer", "reason")) or "")
    facts.title = _str(details.get("title")) or _simple_text(micro.get("title"))
    facts.channel = _str(details.get("author")) or _str(micro.get("ownerChannelName"))
    channel_id = _str(details.get("channelId")) or _str(micro.get("externalChannelId"))
    profile = _str(micro.get("ownerProfileUrl"))
    if profile:
        facts.channel_url = _https(profile)
    elif channel_id:
        facts.channel_url = f"https://www.youtube.com/channel/{channel_id}"
    facts.upload = _str(micro.get("uploadDate")) or _str(micro.get("publishDate"))
    facts.start = _str(live.get("startTimestamp"))
    facts.live_content = details.get("isLiveContent") is True
    facts.duration = next((n for n in (_seconds(details.get("lengthSeconds")), _seconds(micro.get("lengthSeconds")))
                           if n is not None), None)
    facts.is_live = bool(details.get("isLive") or live.get("isLiveNow"))
    facts.is_upcoming = bool(details.get("isUpcoming"))
    facts.is_post_live = bool(details.get("isPostLiveDvr"))
    facts.unlisted = _flag(micro.get("isUnlisted"))
    facts.category = _str(micro.get("category"))
    facts.shorts = _flag(micro.get("isShortsEligible"))
    return True


class _Head(HTMLParser):
    """head 的 <title>、og:title 與 schema.org 的 microdata（<meta|link itemprop=…>）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.props: dict[str, list[str]] = {}
        self.og_title: str | None = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in ("meta", "link"):
            prop = a.get("itemprop")
            if prop:
                value = a.get("content") if a.get("content") is not None else a.get("href")
                self.props.setdefault(prop, []).append(value or "")
            if a.get("property") == "og:title" and self.og_title is None:
                self.og_title = a.get("content")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _head_of(text: str) -> _Head:
    head = _Head()
    end = text.find("</head>")
    head.feed(text if end < 0 else text[:end])
    return head


def _from_microdata(head: _Head, facts: _Facts) -> None:
    """第二層：播放器回應不見了（改版、被截斷）時，head 的 microdata 還有標題、日期、長度與頻道。"""
    props = head.props
    names = [n for n in props.get("name", []) if n.strip()]

    def first(key: str) -> str | None:
        return next((v.strip() for v in props.get(key, []) if v.strip()), None)

    facts.title = facts.title or (names[0].strip() if names else None)
    # 頻道的名字是第二個 name（第一個是影片）
    facts.channel = facts.channel or (names[1].strip() if len(names) > 1 else None)
    facts.upload = facts.upload or first("uploadDate") or first("datePublished")
    broadcast = (first("isLiveBroadcast") or "").lower() == "true"
    facts.live_content = facts.live_content or broadcast
    facts.start = facts.start or (first("startDate") if broadcast else None)
    if facts.duration is None:
        facts.duration = _iso_duration(first("duration"))
    facts.category = facts.category or first("genre")


_RE_LENGTH = re.compile(r'"lengthSeconds"\s*:\s*"([0-9]+)"')
_RE_UPLOAD = re.compile(r'"(?:uploadDate|publishDate)"\s*:\s*"([^"]+)"')
_RE_START = re.compile(r'"startTimestamp"\s*:\s*"([^"]+)"')
_RE_CHANNEL = re.compile(r'"ownerChannelName"\s*:\s*"((?:[^"\\]|\\.)*)"')
_RE_LIVE_CONTENT = re.compile(r'"isLiveContent"\s*:\s*(true|false)')


def _from_regex(text: str, facts: _Facts) -> None:
    """第三層：JSON 解不出來、microdata 也沒有，直接在文字裡找欄位。

    只補日期、長度、頻道與是不是直播過；直播中、尚未首播這些旗標不從這裡讀——整個頁面還有推薦影片
    的 JSON，抓到的可能是別支的。播放器回應解得出來時完全不用這一層（見 parse_watch_page）。
    """
    def first(pattern: re.Pattern) -> str | None:
        matched = pattern.search(text)
        return matched.group(1) if matched else None

    if facts.duration is None:
        facts.duration = _seconds(first(_RE_LENGTH))
    facts.upload = facts.upload or first(_RE_UPLOAD)
    facts.start = facts.start or first(_RE_START)
    channel = first(_RE_CHANNEL)
    if not facts.channel and channel:
        try:
            facts.channel = _str(json.loads(f'"{channel}"'))
        except ValueError:
            pass
    if first(_RE_LIVE_CONTENT) == "true":
        facts.live_content = True


def parse_watch_page(text: str, video_id: str) -> VideoMeta | None:
    """影片頁 → 影片資料。三層：播放器回應 → microdata → 正規表示式，最後才是 <title>。

    一個欄位都認不得（不是影片頁）回 None。不看 JSON-LD（結束的直播沒有它，也沒有長度與頻道，
    裡面還混著使用者留言）與 og:description（描述是空的時是一段制式廣告文字）。

    影片頁是別人的網站：每一層各自攔下預期中的例外（型別不對、數字太大、巢狀太深…），整層作廢
    （做到一半的欄位不留）、記成問題，換下一層，不讓一個壞欄位變成整個指令的 traceback。
    player_read 只在播放器回應解出來、而且帶著 playabilityStatus 時成立；page_read 則是認得出任何欄位。
    """
    facts = _Facts()
    problems: list[str] = []

    def layer(name: str, run: Callable[[_Facts], object]) -> object:
        nonlocal facts
        trial = copy.copy(facts)
        try:
            result = run(trial)
        except _PARSE_ERRORS as e:
            problems.append(f"影片頁的{name}認不得（{type(e).__name__}），這一層略過")
            return None
        facts = trial
        return result

    decoded = bool(layer("播放器回應", lambda f: _from_player_response(text, f)))
    head: _Head | None = None
    try:
        head = _head_of(text)
    except _PARSE_ERRORS as e:
        problems.append(f"影片頁的標頭認不得（{type(e).__name__}），這一層略過")
    if head is not None:
        layer("microdata", lambda f: _from_microdata(head, f))
    if not decoded:
        # 播放器回應解不出來才在文字裡撈：解得出來的話，缺的欄位就是沒有，不去猜別支影片的數字
        layer("正規表示式", lambda f: _from_regex(text, f))
    if head is not None:
        facts.title = (facts.title or _str(head.og_title)
                       or _str(re.sub(r"\s*-\s*YouTube\s*$", "", head.title.strip())))
    if not facts.known():
        return None
    moment = facts.start if (facts.live_content and facts.start) else facts.upload
    return VideoMeta(
        video_id=video_id, title=facts.title, channel=facts.channel, channel_url=facts.channel_url,
        date=taipei_date(moment) or taipei_date(facts.upload), duration_seconds=facts.duration or 0,
        is_live=facts.is_live, is_upcoming=facts.is_upcoming or facts.status == "LIVE_STREAM_OFFLINE",
        is_post_live=facts.is_post_live, unlisted=facts.unlisted, category=facts.category,
        shorts_eligible=facts.shorts,
        playable=None if facts.status is None else facts.status == "OK",
        playability_reason="" if facts.status in (None, "OK") else (facts.reason or facts.status),
        page_read=True, player_read=facts.status is not None, problems=tuple(problems))


def _page_problem(response: HttpResponse) -> str | None:
    """影片頁沒拿到的原因（被擋下、轉走、錯誤狀態）；拿到了是 None。看的是最終網址的主機與路徑。"""
    try:
        where = urlsplit(response.url)
        host, path = (where.hostname or "").lower(), where.path
    except ValueError:
        # 轉址的網址格式不對（Location: http://[::1/x）
        return "YouTube 把請求轉去一個格式不對的網址，沒有拿到影片頁"
    if path.startswith("/sorry/"):
        return "YouTube 的機器人檢查（/sorry/）擋下了這個連線，換個網路或晚點再試"
    if host.startswith("consent."):
        return "YouTube 要求先同意 cookie 條款（被轉到 consent 頁）"
    if response.status == 429:
        return "YouTube 回 429（請求太頻繁，或這個網路被當成機器人），晚點再試"
    if 300 <= response.status < 400:
        return f"YouTube 把請求轉去 {host}{path}，沒有拿到影片頁"
    if response.status != 200:
        return f"YouTube 回應 HTTP {response.status}，沒有拿到影片頁"
    return None


_BOT_CHECK = re.compile(r"\bbot\b", re.IGNORECASE)


def fetch_metadata(video_id: str, fetch_oembed: Fetch = http_oembed, fetch_page: Fetch = http_page) -> VideoMeta:
    """oEmbed → 影片頁，最多兩個請求。影片不存在也是回傳（playable=False），由 preflight 拒絕。

    oEmbed：400（id 格式不對）、404（不存在）直接定案，不再讀影片頁；401／403（可能只是不允許
    嵌入）、429、5xx、逾時都不代表影片有問題，改讀影片頁。影片頁被擋下（同意頁、機器人檢查、
    429）但 oEmbed 讀到了，就用 oEmbed 的標題與頻道、其餘欄位不知道（page_read=False）；preflight 對這種
    結果預設拒絕（要 --allow-unverified）。兩邊都讀不到才丟 MetadataUnavailable。

    任何一層的解析丟了預期中的例外（見 _PARSE_ERRORS）都記成問題、換下一層，不會是 traceback。
    """
    problems: list[str] = []
    oembed: OEmbed | None = None
    try:
        response = fetch_oembed(oembed_url(video_id))
    except OSError as e:
        problems.append(f"oEmbed 連不上（{e}），改讀影片頁")
    else:
        if response.status in _OEMBED_VERDICTS:
            return VideoMeta(video_id, playable=False, playability_reason=_OEMBED_VERDICTS[response.status])
        if response.status == 200:
            try:
                oembed = parse_oembed(response.body)
            except _PARSE_ERRORS:
                oembed = None
            if oembed is None:
                problems.append("oEmbed 的回應認不得，改讀影片頁")
        else:
            problems.append(f"oEmbed 回應 {response.status}（不代表影片有問題），改讀影片頁")

    page: VideoMeta | None = None
    try:
        response = fetch_page(canonical_url(video_id))
    except OSError as e:
        problems.append(f"影片頁連不上（{e}）")
    else:
        problem = _page_problem(response)
        if problem is None:
            try:
                page = parse_watch_page(response.body.decode("utf-8", "replace"), video_id)
            except _PARSE_ERRORS as e:
                problem = f"影片頁解析失敗（{type(e).__name__}）"
            else:
                if page is None:
                    problem = "影片頁的格式認不得（YouTube 可能改版了）"
                elif page.playable is False and _BOT_CHECK.search(page.playability_reason):
                    # 「請登入以確認你不是機器人」是這個網路被擋，不是這支影片的狀態
                    problem, page = "YouTube 要求登入以確認不是機器人（機器人檢查），換個網路或晚點再試", None
        if problem:
            problems.append(problem)

    if oembed is None and page is None:
        raise MetadataUnavailable("；".join(problems) or "讀不到影片資料")
    return _merge(video_id, oembed, page, tuple(problems))


_OEMBED_VERDICTS = {
    400: "YouTube 說這不是有效的影片 id（oEmbed 回 400）；確認網址有沒有少複製或多複製字元",
    404: "影片不存在或已被移除（oEmbed 回 404）",
}


def _merge(video_id: str, oembed: OEmbed | None, page: VideoMeta | None, problems: tuple[str, ...]) -> VideoMeta:
    base = page or VideoMeta(video_id)
    return VideoMeta(
        video_id=video_id,
        title=(oembed.title if oembed and oembed.title else None) or base.title,
        channel=(oembed.author_name if oembed and oembed.author_name else None) or base.channel,
        channel_url=(oembed.author_url if oembed and oembed.author_url else None) or base.channel_url,
        date=base.date, duration_seconds=base.duration_seconds, is_live=base.is_live,
        is_upcoming=base.is_upcoming, is_post_live=base.is_post_live, unlisted=base.unlisted,
        category=base.category, shorts_eligible=base.shorts_eligible, playable=base.playable,
        playability_reason=base.playability_reason, page_read=base.page_read, player_read=base.player_read,
        # 取得過程的問題在前，影片頁某一層解析失敗的問題接在後面
        problems=problems + base.problems)


# --- 預檢 ---


@dataclass(frozen=True)
class PreflightOptions:
    max_minutes: int = 180
    force: bool = False
    allow_unlisted: bool = False
    allow_music: bool = False
    # 影片頁沒讀到、或讀到了卻沒解出播放器回應：不公開、直播、Shorts、音樂與長度都檢查不了。
    # 預設拒絕；這個旗標是操作者說「那些檢查我自己負責」
    allow_unverified: bool = False


def _unverified_because(meta: VideoMeta) -> str:
    """為什麼不公開、直播、Shorts、音樂與長度檢查不了；檢查得了就是空字串。"""
    if not meta.page_read:
        return "沒有讀到影片頁"
    if not meta.player_read:
        return "讀到的影片頁裡找不到播放器回應（playabilityStatus；YouTube 可能改版了）"
    return ""


def _missing_flags(meta: VideoMeta) -> list[tuple[str, str]]:
    """播放器回應讀到了、影片也能播，卻缺了真實頁面每一頁都有的旗標：回 [(欄位名稱, 因此沒檢查的事)]，沒缺是空的。

    只看不公開（isUnlisted）與 Shorts（isShortsEligible）：檢查本身靠它們，而且固定資料裡每一支能播放的真實頁面
    都帶布林值。缺了的原因是 microformat 整段不見或被改名（改版、被截斷），不是這支影片「不是不公開」。
    真實頁面本來就可能沒有的欄位（類別、日期、長度、頻道連結…）不算：少了它們頂多少一個警告。
    影片播不了（不存在、私人…）的頁面本來就沒有 microformat，預檢會先因為播不了拒絕，不在這裡叫人加旗標。
    """
    if not (meta.player_read and meta.playable):
        return []
    flags = (("isUnlisted", meta.unlisted, "是不是「不公開」"), ("isShortsEligible", meta.shorts_eligible, "是不是 Shorts"))
    return [(name, what) for name, value, what in flags if value is None]


def preflight(meta: VideoMeta, options: PreflightOptions) -> list[str]:
    """寫進資料庫之前的檢查：沒有救的影片不佔一個重試次數，會後悔的影片先說清楚。

    不能處理的丟 VideoRefused（訊息是給使用者看的原因，能用旗標蓋過的會寫出旗標）；
    能處理但要提醒的以警告字串回傳。

    **檢查不了也是拒絕**：影片頁沒讀到、或讀到了卻沒解出播放器回應（YouTube 被擋下、改版、頁面被截斷），
    不公開、直播中、Shorts、音樂與長度就全是「不知道」。oEmbed 連不公開的影片也回 200，所以標題讀得到
    不代表這些檢查過了；以前只印一行警告就照樣登記、送 GPU、發佈。要放行得明確加 --allow-unverified
    （只給 --title、--speaker、--date 不算：那只是不需要影片頁就能登記，不代表檢查過了）。
    播放器回應解出來、影片也能播，卻缺了 isUnlisted 或 isShortsEligible（microformat 不見了或被改名）也一樣：
    真實的頁面每一頁都有這兩個旗標，缺了的話 unlisted 與 shorts 停在 None，不能讀成「不是不公開」「不是 Shorts」。
    訊息說缺的是哪一個欄位。已經知道的壞消息（不公開、Shorts…）永遠比「檢查不了」先說。
    """
    if meta.is_upcoming:
        raise VideoRefused("這是尚未首播（預約中）的直播，還沒有內容可以摘要；等它播完、YouTube 轉檔好之後再加")
    if meta.playable is False:
        raise VideoRefused(f"這支影片現在無法播放：{meta.playability_reason or '原因不明'}")
    if meta.is_live:
        raise VideoRefused("這是直播中的影片，播完才能摘要；等直播結束、YouTube 轉檔好之後再加")
    if meta.shorts_eligible:
        raise VideoRefused("這是 Shorts（直式短影片）：截圖會被網站 16:9 的框切掉，這版不收 Shorts")
    if meta.unlisted and not options.allow_unlisted:
        raise VideoRefused("這支影片是「不公開」（unlisted）：只有拿到連結的人看得到，但摘要、逐字稿和截圖會公開在網站上。"
                           "確定要公開的話，加上 --allow-unlisted", "--allow-unlisted")
    if meta.category and meta.category.strip().lower() in _MUSIC and not options.allow_music:
        raise VideoRefused("這支影片的類別是「音樂」：整首歌詞會被當成逐字稿公開在網站上。確定要的話，加上 --allow-music",
                           "--allow-music")
    minutes = meta.duration_seconds / 60
    if meta.duration_seconds > options.max_minutes * 60:
        raise VideoRefused(
            f"影片長 {minutes:.0f} 分鐘，超過上限 {options.max_minutes} 分鐘。要處理的話，加上 "
            f"--max-minutes {-(-meta.duration_seconds // 60)}；影片越長，GPU_JOB_TIMEOUT_SECONDS（兩個容器都要）也要跟著調大",
            "--max-minutes")
    unverified = _unverified_because(meta)
    missing = _missing_flags(meta)
    names = "、".join(name for name, _ in missing)
    unchecked = "、".join(what for _, what in missing)
    if unverified and not options.allow_unverified:
        raise VideoRefused(
            f"{unverified}，所以不公開、直播中、Shorts、音樂與長度都檢查不了，而摘要、逐字稿和截圖會公開在網站上。"
            "多半是影片頁暫時被 YouTube 擋下（429、同意頁、機器人檢查），晚一點再試；"
            "確定現在就要處理的話，加上 --allow-unverified，那些檢查就由你自己負責"
            "（只給 --title、--speaker、--date 不能代替它）", "--allow-unverified")
    if missing and not options.allow_unverified:
        raise VideoRefused(
            f"影片頁的播放器回應裡沒有 {names}（microformat 不見了或被改名；真實的影片頁每一頁都有，"
            f"YouTube 可能改版了），所以沒辦法確認這支影片{unchecked}，而摘要、逐字稿和截圖會公開在網站上。"
            "晚一點再試；確定現在就要處理的話，加上 --allow-unverified，"
            f"{'那些檢查' if len(missing) > 1 else '那項檢查'}就由你自己負責（只給 --title、--speaker、--date 不能代替它）",
            "--allow-unverified")
    warnings: list[str] = []
    if meta.is_post_live:
        if not options.force:
            raise VideoRefused("這支直播剛結束，YouTube 還在轉檔：沒有字幕、下載很慢，常常失敗。建議幾小時後再加；"
                               "要現在處理，加上 --force", "--force")
        warnings.append("這支直播剛結束，YouTube 還在轉檔（沒有字幕、下載很慢）；照 --force 處理，失敗的話過幾小時再試")
    if unverified:
        # 照 --allow-unverified 處理：警告照舊，操作者要知道自己跳過了什麼
        what = "沒有讀到影片頁" if not meta.page_read else "影片頁裡沒有解出播放器回應"
        warnings.append(f"{what}，所以沒辦法確認這支影片是不是公開、是不是直播中、是不是音樂或 Shorts，"
                        "也不一定知道長度與日期；摘要和逐字稿會公開在網站上，請自己確認過")
    else:
        if missing:
            warnings.append(f"影片頁的播放器回應裡沒有 {names}，所以沒辦法確認這支影片{unchecked}；"
                            "摘要和逐字稿會公開在網站上，請自己確認過")
        if not meta.duration_seconds:
            warnings.append("影片頁沒有給長度，沒辦法檢查 --max-minutes，頁數範圍也交給 GPU 的預設")
    return warnings


_MUSIC = frozenset({"music", "音樂"})


# --- 頁數與講者 ---


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def slide_range(duration_seconds: int, min_override: int | None = None,
                max_override: int | None = None) -> tuple[int | None, int | None]:
    """依長度算頁數範圍：最少 clamp(⌈分鐘/6⌉, 3, 8)、最多 clamp(⌈分鐘/2⌉, 6, 50)。

    GPU 預設要 8～15 頁：19 秒的影片只產得出 3 頁，會因「少於下限」失敗；一小時的影片只有
    十頁又太少。不知道長度（0）時交給 GPU 的預設（回 None）。兩個覆寫值（--min-slides、
    --max-slides）蓋過算出來的；範圍互相矛盾、或超出 GPU 收的 1～50 就丟 ValueError。
    """
    if duration_seconds and duration_seconds > 0:
        # ⌈秒/360⌉ 與 ⌈秒/120⌉：用整數運算，避免浮點數把剛好的分鐘數進位
        low = _clamp(-(-duration_seconds // 360), 3, 8)
        high = _clamp(-(-duration_seconds // 120), 6, _MAX_PAGES)
    else:
        low = high = None
    low = low if min_override is None else min_override
    high = high if max_override is None else max_override
    for name, value in (("--min-slides", min_override), ("--max-slides", max_override)):
        if value is not None and not _MIN_PAGES <= value <= _MAX_PAGES:
            raise ValueError(f"{name} 要在 {_MIN_PAGES}～{_MAX_PAGES} 之間（GPU 只收這個範圍），不是 {value}")
    if low is not None and high is not None and low > high:
        raise ValueError(f"頁數範圍互相矛盾：最少 {low} 頁、最多 {high} 頁；請用 --min-slides 與 --max-slides 一起指定")
    return low, high


# 不能留在標題與頻道名稱裡的字元類別：Cc（控制字元：ESC、NUL、BEL、DEL、C1…，終端機會當成指令）、
# Cf（格式字元：雙向覆寫與隔離 U+202A–202E、U+2066–2069，零寬字元、BOM…，會讓文字重排或看不見）、
# Cs（孤立的代理字元：存進資料庫或印出來會 UnicodeEncodeError）
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
# Cf 裡唯二要留下的：ZWNJ（U+200C）與 ZWJ（U+200D）。表情符號的組合（家庭、職業、膚色）靠 ZWJ 接成一個，波斯文、
# 印地文等的字形與字義靠 ZWNJ／ZWJ；拿掉的話家庭會拆成三個人、波斯文會變成另一種寫法。它們不會移動游標，也不會讓
# 旁邊的字重排，留著沒有風險（雙向覆寫與隔離、零寬空格、BOM 等其餘的 Cf 照舊拿掉）
_JOINERS = "\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}"


def clean_text(text: str | None) -> str:
    """上傳者寫的單行文字（標題、頻道名稱）→ 可以安全印出、存進資料庫、放進網址的一行。

    標題與頻道名稱會原樣印在操作者的終端機（ESC 序列能清掉前面的警告行、換掉視窗標題）、存進 Article
    （講者欄位是公開的 /speaker/<名稱>，NUL 會變成 %00、RLO 會讓旁邊的字重排）。
    空白類字元（換行、tab、U+2028、全形空白…）換成一個空格，其餘的 Cc／Cf／Cs 拿掉（ZWNJ 與 ZWJ 除外，見
    _JOINERS），最後把連續的空白收成一個。只剩連接符的文字看起來是空的，當成空字串：呼叫端才會走預設值
    （佔位標題、「YouTube」頻道），不會存下一個看不見的標題。
    """
    kept = []
    for char in text or "":
        if char.isspace():
            kept.append(" ")
        elif char in _JOINERS or unicodedata.category(char) not in _UNSAFE_CATEGORIES:
            kept.append(char)
    cleaned = " ".join("".join(kept).split())
    return cleaned if cleaned.strip(" " + _JOINERS) else ""


def clean_speaker(name: str | None) -> str:
    """頻道名稱 → 講者欄位：先過 clean_text；「、」是聯合質詢的分隔字元（API 會把「甲、乙」拆成兩個講者），
    換成「・」；不超過欄位的 300 字。"""
    return clean_text(name).replace("、", "・")[:300]
