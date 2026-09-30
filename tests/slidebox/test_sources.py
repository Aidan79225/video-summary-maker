"""來源判別：純字串處理，不碰網路。"""
from __future__ import annotations

import pytest

from slidebox.usecases.sources import ivod_id


@pytest.mark.parametrize("url,expected", [
    ("https://ivod.ly.gov.tw/Play/Clip/1M/171180", "171180"),
    ("https://ivod.ly.gov.tw/Play/Full/1M/17704", "17704"),
    ("https://ivod.ly.gov.tw/Play/Clip/300K/154164", "154164"),
    ("http://ivod.ly.gov.tw/Play/Clip/1M/171180/", "171180"),
    ("https://ivod.ly.gov.tw/Play/Clip/1M/171180?x=1", "171180"),
    ("  https://ivod.ly.gov.tw/Play/Clip/1M/171180  ", "171180"),
])
def test_recognises_the_ivod_forms(url, expected):
    assert ivod_id(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://ivod.ly.gov.tw/",
    "https://ivod.ly.gov.tw/Play/Clip/1M/",
    "",
])
def test_everything_else_is_not_ivod(url):
    assert ivod_id(url) is None


def test_a_youtube_video_whose_id_looks_like_a_path_is_not_mistaken_for_ivod():
    """攔的 bug：只比對「網址裡有數字」會把一般影片誤判成 IVOD，
    接著整條 pipeline 會去打立法院 API 找一個不存在的 id。"""
    assert ivod_id("https://www.youtube.com/watch?v=12345678901") is None


def test_a_lookalike_host_is_not_accepted():
    """攔的 bug：用 in 比對主機名，evil-ivod.ly.gov.tw.attacker.com 會被當成
    自己人，程式就會把使用者貼的網址拿去對別人的伺服器組 API 請求。"""
    assert ivod_id("https://ivod.ly.gov.tw.attacker.com/Play/Clip/1M/1") is None


# --- 臺中市議會 ---

from slidebox.usecases.sources import TcccRef, tccc_clip, tccc_id  # noqa: E402


@pytest.mark.parametrize("url", [
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833",
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833&pageno=1",
    "https://vod.tccc.gov.tw/index.asp?ano=14833&cno=85&url=12",
    "http://VOD.TCCC.GOV.TW/index.asp?url=12&cno=85&ano=14833",
    "  https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833  ",
])
def test_recognises_taichung_council_clip_pages(url):
    assert tccc_clip(url) == TcccRef("85", "14833")
    assert tccc_id(url) == "tccc-14833"


@pytest.mark.parametrize("url", [
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=85",          # 只有議員，沒有片段
    "https://vod.tccc.gov.tw/index.asp?url=11",
    "https://vod.tccc.gov.tw/wb_region02.asp?url=12&cno=85&ano=14833",  # 內頁不是公開網址
    "https://vod.tccc.gov.tw.attacker.com/index.asp?url=12&cno=85&ano=14833",
    "https://ivod.ly.gov.tw/Play/Clip/1M/171180",
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=abc&ano=14833",
    "",
])
def test_everything_else_is_not_a_taichung_clip(url):
    assert tccc_clip(url) is None
    assert tccc_id(url) is None


# --- 新北市議會 ---

from slidebox.usecases.sources import NtpcRef, ntpc_clip, ntpc_id  # noqa: E402

GUID = "ebc80ece-7491-4288-be73-7c59f6b4815c"


@pytest.mark.parametrize("url", [
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetid={GUID}",
    f"https://vod.ntp.gov.tw/vodcloudv2/vod/viewmetadata?ASSETID={GUID.upper()}",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?x=1&assetID={GUID}",
    f"http://VOD.NTP.GOV.TW/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"https://vod.ntp.gov.tw:443/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"  https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}  ",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewDetailMetaData/{GUID}",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewDetailMetaData/{GUID.upper()}/",
])
def test_recognises_new_taipei_council_clip_pages(url):
    """GUID 一律轉小寫：同一段影片不該因為大小寫不同變成兩個 id。"""
    assert ntpc_clip(url) == NtpcRef(GUID)
    assert ntpc_id(url) == f"ntpc-{GUID}"


@pytest.mark.parametrize("url", [
    "https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData",                     # 沒有 assetID
    "https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=",
    "https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID=12345",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID[:-1]}",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}x",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}%0A",  # 結尾換行
    f"https://vod.ntp.gov.tw/VodCloudV2/VodStream/VideoPlayer?assetID={GUID}&type=Book_SD",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaDataX?assetID={GUID}",
    f"https://vod.ntp.gov.tw/Other/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewDetailMetaData/{GUID}/extra",
    f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewDetailMetaData?assetID={GUID}",
    "https://vod.ntp.gov.tw/VodCloudV2/VOD/Index",
    f"https://vod.ntp.gov.tw.attacker.com/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"https://evil-vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"https://www.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    f"https://attacker.com/VodCloudV2/VOD/ViewMetaData?assetID={GUID}&h=vod.ntp.gov.tw",
    f"https://vod.ntp.gov.tw@attacker.com/VodCloudV2/VOD/ViewMetaData?assetID={GUID}",
    "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833",
    "https://ivod.ly.gov.tw/Play/Clip/1M/171180",
    "",
])
def test_everything_else_is_not_a_new_taipei_clip(url):
    """攔的 bug：GUID 會被拼進送給市議會的請求，只比對前綴或用 `$` 結尾，
    `…%0A` 這種尾巴就會跟著混進去；主機名用 in 比對則會把仿冒網域當自己人。"""
    assert ntpc_clip(url) is None
    assert ntpc_id(url) is None


def test_the_other_sources_do_not_claim_new_taipei_urls():
    """三個來源的判別互斥，路由才不必擔心先後順序以外的事。"""
    url = f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID}"
    assert ivod_id(url) is None
    assert tccc_clip(url) is None
