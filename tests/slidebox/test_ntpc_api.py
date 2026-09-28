"""新北市議會 metadata 頁與播放器頁的解析：用存下來的真實頁面，不碰網路。"""
from __future__ import annotations

import os

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable
from slidebox.infrastructure.ntpc_api import (
    NtpcClient,
    NtpcRecord,
    audio_only,
    parse_metadata_page,
    parse_stream_url,
)
from slidebox.usecases.sources import NtpcRef

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
GUID = "ebc80ece-7491-4288-be73-7c59f6b4815c"
REF = NtpcRef(GUID)
M3U8 = ("https://vodwms.ntp.gov.tw:443/NTP/_definst_/mp4:PlayAgenda/Book_SD/0408R1150916/"
        "0408R1150916020.mp4/playlist.m3u8?device=PC&kind=Guest")
M3U8_0915 = ("https://vodwms.ntp.gov.tw:443/NTP/_definst_/mp4:PlayAgenda/Book_SD/0408R1150915/"
             "0408R1150915020.mp4/playlist.m3u8?device=PC&kind=Guest")
SPEAKERS = ("周雅玲", "林裔綺", "張嘉玲", "許昭興", "陳鴻源", "彭佳芸", "廖宜琨", "鄭宇恩", "鍾宏仁")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _field(label, value):
    """真實頁面上一欄的樣子。"""
    return (f'<span onclick="showTip(this,event)" type="button" class="control-label " '
            f'data-tip="" style="">{label}：{value}</span>')


def _page(**overrides):
    fields = {"屆次會期": "第4屆第8次定期會", "議　　程": "市政總質詢",
              "發言議員": "周雅玲,林裔綺", "開會日期": "115-09-16", "影片長度": "01:00:10"}
    fields.update(overrides)
    return "<html><body>" + "".join(_field(k, v) for k, v in fields.items() if v is not None) \
        + "</body></html>"


class FakeFetch:
    def __init__(self, pages: dict[str, str], error: Exception | None = None):
        self.pages = pages
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        for key, page in self.pages.items():
            if key in url:
                return page
        raise AssertionError(f"沒準備這個網址的頁面：{url}")


def test_the_metadata_page_yields_session_agenda_speakers_date_and_duration():
    record = parse_metadata_page(_read("ntpc_viewmetadata_ebc80ece.html"), REF)
    assert record.asset_id == GUID
    assert record.session == "第4屆第8次定期會"
    assert record.agenda == "市政總質詢"
    assert record.speakers == SPEAKERS
    assert record.date == "2026-09-16"                  # 民國 115 年
    assert record.duration_seconds == 1 * 3600 + 59 * 60 + 26   # 「01:59:26」是 HH:MM:SS
    assert record.title == ("2026-09-16 " + "、".join(SPEAKERS)
                            + "－第4屆第8次定期會 市政總質詢")


def test_only_the_label_loses_its_ideographic_spaces():
    """「議　　程」的標籤中間是兩個 U+3000，比對前要去掉；值裡的空白是內容，
    只修頭尾。攔的 bug：用 `議程` 直接比對標籤，議程永遠抓不到，整頁判定解析失敗。"""
    page = _page(**{"議　　程": "  第4屆第8次定期會各機關聯合業務報告及質詢-國民黨團發言 "})
    record = parse_metadata_page(page, REF)
    assert record.agenda == "第4屆第8次定期會各機關聯合業務報告及質詢-國民黨團發言"


def test_the_og_title_meta_tag_is_not_mistaken_for_a_field():
    """頁首 <meta og:title> 也寫著「屆次會期：…」，但那不是欄位；頁尾的
    「機關地址：…」也不是。只有 control-label 的 span 才算。"""
    page = ('<meta property="og:title" content="屆次會期：錯的">'
            + _page() + "<footer>機關地址： 板橋區</footer>")
    assert parse_metadata_page(page, REF).session == "第4屆第8次定期會"


def test_speaker_names_are_split_on_ascii_commas_only():
    """「馬見Lahuy．Ipin」的「．」是名字的一部分；名單只用半形逗號分隔。"""
    page = _page(發言議員="馬見Lahuy．Ipin, 李翁月娥 ,,")
    assert parse_metadata_page(page, REF).speakers == ("馬見Lahuy．Ipin", "李翁月娥")


def test_an_empty_speaker_list_falls_back_to_the_council_in_the_title():
    record = parse_metadata_page(_page(發言議員=""), REF)
    assert record.speakers == ()
    assert record.title == "2026-09-16 新北市議會－第4屆第8次定期會 市政總質詢"
    assert parse_metadata_page(_page(發言議員=None), REF).speakers == ()


def test_html_entities_in_values_are_decoded():
    assert parse_metadata_page(_page(**{"議　　程": "A&amp;B"}), REF).agenda == "A&B"


def test_an_unparseable_duration_is_zero_not_a_failure():
    assert parse_metadata_page(_page(影片長度="1:02"), REF).duration_seconds == 0
    assert parse_metadata_page(_page(影片長度=None), REF).duration_seconds == 0


@pytest.mark.parametrize("override", [
    {"屆次會期": None},
    {"議　　程": None},
    {"議　　程": "   "},
    {"開會日期": None},
    {"開會日期": "2026/09/16"},
])
def test_a_page_missing_a_required_field_is_reported(override):
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會.*解析"):
        parse_metadata_page(_page(**override), REF)


def test_the_player_page_yields_the_hls_url_with_ampersands_restored():
    """播放器頁把網址寫成 `?device=PC&amp;kind=Guest`；原樣交給 ffmpeg 的話
    伺服器看到的參數是 `amp;kind`。"""
    assert parse_stream_url(_read("ntpc_player_ebc80ece.html")) == M3U8
    assert parse_stream_url(_read("ntpc_player_00926153.html")) == M3U8_0915


def test_a_player_page_without_a_stream_gives_none():
    assert parse_stream_url("<html><body>nothing here</body></html>") is None


@pytest.mark.parametrize("src", [
    "https://evil.example/vodwms.ntp.gov.tw/x.mp4/playlist.m3u8?a=1",
    "https://vodwms.ntp.gov.tw.evil.example/x.mp4/playlist.m3u8?a=1",
    "https://vodwms.ntp.gov.tw@evil.example/x.mp4/playlist.m3u8?a=1",
])
def test_a_stream_on_another_host_is_ignored(src):
    """這個網址會直接交給 ffmpeg；播放器頁被竄改或改版時不該跟著去抓別的主機。"""
    assert parse_stream_url(f"<source src='{src}' />") is None


def test_audio_only_appends_the_wowza_switch():
    assert audio_only(M3U8) == M3U8 + "&wowzaaudioonly=true"
    assert audio_only("https://vodwms.ntp.gov.tw/a/playlist.m3u8") == \
        "https://vodwms.ntp.gov.tw/a/playlist.m3u8?wowzaaudioonly=true"


def test_the_client_fetches_the_public_metadata_page():
    fetch = FakeFetch({"ViewMetaData": _read("ntpc_viewmetadata_ebc80ece.html")})
    record = NtpcClient(fetch=fetch).record(REF)
    assert fetch.urls == [f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}"]
    assert record.speakers == SPEAKERS


def test_the_record_is_cached_for_the_same_clip():
    fetch = FakeFetch({"ViewMetaData": _read("ntpc_viewmetadata_ebc80ece.html")})
    client = NtpcClient(fetch=fetch)
    client.record(REF)
    client.record(NtpcRef(GUID))
    assert len(fetch.urls) == 1


def test_video_and_audio_urls_come_from_the_player_page():
    fetch = FakeFetch({"VideoPlayer": _read("ntpc_player_ebc80ece.html")})
    client = NtpcClient(fetch=fetch, base="https://vod.ntp.gov.tw/")
    assert client.video_url(REF) == M3U8
    assert client.audio_url(REF) == M3U8 + "&wowzaaudioonly=true"
    assert fetch.urls[0] == \
        f"https://vod.ntp.gov.tw/VodCloudV2/VodStream/VideoPlayer?assetID={GUID}&type=Book_SD"
    # 找串流不需要 metadata 頁
    assert not any("ViewMetaData" in url for url in fetch.urls)


def test_a_metadata_page_that_cannot_be_fetched_is_reported():
    client = NtpcClient(fetch=FakeFetch({}, error=OSError("connection refused")))
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會"):
        client.record(REF)


def test_a_player_page_that_cannot_be_fetched_is_reported():
    client = NtpcClient(fetch=FakeFetch({}, error=OSError("timed out")))
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會.*播放器"):
        client.video_url(REF)
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會"):
        client.audio_url(REF)


def test_a_metadata_page_without_the_fields_is_reported():
    client = NtpcClient(fetch=FakeFetch({"ViewMetaData": "<html>改版了</html>"}))
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會.*解析"):
        client.record(REF)


def test_a_player_without_a_stream_is_reported():
    client = NtpcClient(fetch=FakeFetch({"VideoPlayer": "<html>no stream</html>"}))
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會.*串流"):
        client.video_url(REF)


def test_a_missing_ref_is_reported_without_touching_the_network():
    """composition 是 `client.video_url(ntpc_clip(url))`：路由接錯時 ref 是 None。
    要翻成 NoSubtitlesAvailable，截圖那一步才會降級成「沒有截圖」而不是整份失敗。"""
    fetch = FakeFetch({}, error=AssertionError("不該打到"))
    client = NtpcClient(fetch=fetch)
    for call in (client.record, client.video_url, client.audio_url):
        with pytest.raises(NoSubtitlesAvailable, match="新北市議會"):
            call(None)
    assert fetch.urls == []


def test_the_record_title_follows_the_shared_shape():
    record = NtpcRecord(asset_id=GUID, session="第4屆第8次定期會",
                        agenda="第4屆第8次定期會各機關聯合業務報告及質詢-李翁議員月娥",
                        speakers=("李翁月娥",), date="2026-08-18", duration_seconds=900)
    assert record.title == ("2026-08-18 李翁月娥－第4屆第8次定期會 "
                            "第4屆第8次定期會各機關聯合業務報告及質詢-李翁議員月娥")


def test_the_council_fetch_keeps_verification_but_drops_the_strict_flag():
    """攔的 bug：Python 3.13 的預設 context 會拒絕新北市議會的憑證鏈，
    整個來源一篇都抓不到。只能關 strict，不能關驗證。"""
    import ssl

    from slidebox.infrastructure.ntpc_api import _ssl_context

    context = _ssl_context()
    assert not (context.verify_flags & ssl.VERIFY_X509_STRICT)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_a_truncated_response_degrades_instead_of_failing_the_job():
    """攔的 bug：IncompleteRead 不是 OSError，截圖階段（整篇最後一步）遇到它會讓
    已經跑完語音辨識與摘要的工作整個失敗；應該跟連線錯誤一樣退回「沒有截圖」。"""
    import http.client

    from slidebox.infrastructure.ivod_sections import HlsSectionGateway
    from slidebox.usecases.sources import ntpc_clip

    def truncated(url):
        raise http.client.IncompleteRead(b"partial")

    client = NtpcClient(fetch=truncated)
    url = "https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=ebc80ece-7491-4288-be73-7c59f6b4815c"
    with pytest.raises(NoSubtitlesAvailable):
        client.video_url(ntpc_clip(url))
    gateway = HlsSectionGateway(lambda u: client.video_url(ntpc_clip(u)), ffmpeg_exe="ffmpeg")
    assert gateway.download_sections(url, [3.0, 60.0], None, "unused", lambda f, s: None,
                                     lambda: False) == [None, None]


@pytest.mark.parametrize("page", ["ViewMetaData", "VideoPlayer"])
def test_an_incomplete_read_on_either_page_is_reported(page):
    """IncompleteRead 不是 OSError：metadata 頁與播放器頁都要轉成「抓不到」。"""
    import http.client

    client = NtpcClient(fetch=FakeFetch({}, error=http.client.IncompleteRead(b"partial")))
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會"):
        if page == "ViewMetaData":
            client.record(REF)
        else:
            client.video_url(REF)
