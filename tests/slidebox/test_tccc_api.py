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


def test_the_council_fetch_keeps_verification_but_drops_the_strict_flag():
    """攔的 bug：Python 3.13 的預設 context 會拒絕市議會的憑證鏈（CA 憑證缺
    Subject Key Identifier），整個來源在容器裡一篇都抓不到。只能關 strict，
    不能關驗證。"""
    import ssl

    from slidebox.infrastructure.tccc_api import _ssl_context

    context = _ssl_context()
    assert not (context.verify_flags & ssl.VERIFY_X509_STRICT)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_the_json_style_player_page_also_yields_the_hls_url():
    r"""攔的 bug：`/player/vod/`（沒有 ncm）的播放器頁把串流放在 JSON 裡、斜線跳脫成
    `\/`，舊的正規表示式找不到，這些片段在 GPU 端一律失敗。"""
    assert parse_stream_url(_read("tccc_player_json.html")) == (
        "https://streamak0128.akamaized.net/vod0128vh-67eb/_definst_/04A08/08_11509xx/"
        "1150921_1300_8_03_05_2_1.mp4/playlist.m3u8?iMda_seq=153178")
