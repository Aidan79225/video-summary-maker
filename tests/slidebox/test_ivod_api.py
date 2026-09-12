"""IVOD 字幕來源：用假的 API 回應，不碰網路。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable, SubtitleDownloadFailed
from slidebox.infrastructure.ivod_api import IvodClient, IvodSubtitleGateway

RECORD = {
    "IVOD_ID": 171180,
    "日期": "2026-08-27",
    "影片長度": "00:03:17",
    "委員名稱": "洪毓祥",
    "會議資料": {"標題": "立法院第11屆第4會期第1次會議"},
    "video_url": "https://cdn.example/playlist.m3u8",
    "支援功能": ["ai-transcript"],
    "transcript": {
        "whisperx": [
            {"start": 0.189, "end": 12.152, "text": "主席 各位同仁"},
            {"start": 14.0, "end": 22.0, "text": "我們支持國防自主"},
        ]
    },
}


class FakeFetch:
    def __init__(self, payload=None, error=None):
        self._payload = payload if payload is not None else {"data": RECORD}
        self._error = error
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        return json.dumps(self._payload, ensure_ascii=False)


def _gateway(fetch=None) -> tuple[IvodSubtitleGateway, FakeFetch]:
    fetch = fetch or FakeFetch()
    return IvodSubtitleGateway(IvodClient(fetch=fetch)), fetch


URL = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"


def test_the_whisperx_segments_become_cues():
    gateway, _ = _gateway()
    transcript = gateway.fetch(URL, ("zh-TW",))
    assert [c.text for c in transcript.cues] == ["主席 各位同仁", "我們支持國防自主"]
    assert transcript.cues[0].start == pytest.approx(0.189)
    assert transcript.cues[0].end == pytest.approx(12.152)


def test_the_duration_comes_from_the_declared_length():
    gateway, _ = _gateway()
    assert gateway.fetch(URL, ()).duration == pytest.approx(197.0)


def test_the_title_says_who_spoke_and_at_which_meeting():
    """資料夾名稱用的就是這個標題。只有「完整會議」四個字分不出是哪一天
    哪一場，等於回到用 hash 當名字的舊問題。"""
    gateway, _ = _gateway()
    title = gateway.fetch(URL, ()).title
    assert "洪毓祥" in title
    assert "2026-08-27" in title
    assert "第11屆第4會期第1次會議" in title


def test_the_id_is_the_ivod_id_so_reruns_land_in_the_same_folder():
    gateway, _ = _gateway()
    assert gateway.fetch(URL, ()).video_id == "171180"


def test_the_transcript_carries_its_own_quality_note():
    """立法院的逐字稿是 AI 產的，實測看得到錯字（「朝野黨壇協商」）。
    成品必須說出來源，否則使用者會以為那是官方紀錄。"""
    gateway, _ = _gateway()
    note = gateway.fetch(URL, ()).source_note
    assert "AI" in note or "辨識" in note


def test_rolling_dedupe_is_off_for_whisperx_output():
    """攔的 bug：WhisperX 是一句一句的獨立段落，不是 YouTube 的滾動字幕；
    套用滾動去重會把正常內容當重複刪掉。"""
    gateway, _ = _gateway()
    assert gateway.fetch(URL, ()).is_automatic is False


def test_a_clip_with_no_ai_transcript_says_so_instead_of_falling_back_to_speech():
    """攔的 bug：raise 一般的 NoSubtitlesAvailable 會啟動語音備援，而語音
    備援是 yt-dlp，它根本不認得 IVOD 網址——使用者最後看到的錯誤訊息
    會是無關的「無法下載音訊」。"""
    record = {**RECORD, "transcript": {}, "支援功能": []}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    with pytest.raises(SubtitleDownloadFailed) as e:
        gateway.fetch(URL, ())
    assert "逐字稿" in str(e.value)


def test_an_api_failure_is_reported_as_a_subtitle_failure():
    gateway, _ = _gateway(FakeFetch(error=OSError("連線逾時")))
    with pytest.raises(SubtitleDownloadFailed) as e:
        gateway.fetch(URL, ())
    assert "連線逾時" in str(e.value)


def test_segments_with_no_text_are_dropped():
    record = {**RECORD, "transcript": {"whisperx": [
        {"start": 0.0, "end": 1.0, "text": "   "},
        {"start": 1.0, "end": 2.0, "text": "有內容"},
    ]}}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    assert [c.text for c in gateway.fetch(URL, ()).cues] == ["有內容"]


def test_a_record_whose_segments_are_all_empty_is_not_silently_accepted():
    record = {**RECORD, "transcript": {"whisperx": [
        {"start": 0.0, "end": 1.0, "text": ""}]}}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    with pytest.raises(NoSubtitlesAvailable):
        gateway.fetch(URL, ())


def test_the_record_is_fetched_once_even_when_two_gateways_need_it():
    """字幕要逐字稿、片段要 video_url，是同一筆資料。不共用就會對同一個
    id 打兩次 API。"""
    fetch = FakeFetch()
    client = IvodClient(fetch=fetch)
    client.record("171180")
    client.record("171180")
    assert len(fetch.urls) == 1
    assert "171180" in fetch.urls[0]


def test_a_different_id_is_fetched_again():
    fetch = FakeFetch()
    client = IvodClient(fetch=fetch)
    client.record("171180")
    client.record("17704")
    assert len(fetch.urls) == 2
