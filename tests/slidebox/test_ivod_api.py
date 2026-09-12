"""IVOD 字幕來源：用假的 API 回應，不碰網路。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.errors import SubtitleDownloadFailed
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
    with pytest.raises(SubtitleDownloadFailed):
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


# --- 審查後補強 ---


def test_a_record_without_a_transcript_is_not_cached():
    """攔的 bug：無條件快取會讓「立法院產生逐字稿後再試一次」永遠失敗——
    錯誤訊息明明就叫使用者稍後重試，佇列也有重試按鈕。"""
    empty = {**RECORD, "transcript": {}}
    fetch = FakeFetch({"data": empty})
    gateway = IvodSubtitleGateway(IvodClient(fetch=fetch))
    with pytest.raises(SubtitleDownloadFailed):
        gateway.fetch(URL, ())
    # 立法院這時把逐字稿產出來了
    fetch._payload = {"data": RECORD}
    assert len(gateway.fetch(URL, ()).cues) == 2


def test_a_complete_record_is_still_cached():
    fetch = FakeFetch()
    client = IvodClient(fetch=fetch)
    client.record("171180")
    client.record("171180")
    assert len(fetch.urls) == 1


def test_a_segment_with_an_unparseable_time_is_skipped_not_fatal():
    """攔的 bug：裸 float() 遇到 "00:01:02" 會 ValueError，一路衝到 UI 顯示
    「could not convert string to float」——使用者完全看不懂，也繞過了
    「要不要改走語音」的判斷。"""
    record = {**RECORD, "transcript": {"whisperx": [
        {"start": "00:01:02", "end": 5.0, "text": "壞掉的一段"},
        {"start": 10.0, "end": 12.0, "text": "正常的一段"},
    ]}}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    assert [c.text for c in gateway.fetch(URL, ()).cues] == ["正常的一段"]


def test_a_transcript_that_is_a_list_does_not_crash():
    """空物件被序列化成 [] 是常見的 API 行為；非空 list 也要擋住。"""
    record = {**RECORD, "transcript": [{"whisperx": []}]}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    with pytest.raises(SubtitleDownloadFailed):
        gateway.fetch(URL, ())


def test_a_response_whose_data_is_not_an_object_is_reported_clearly():
    gateway, _ = _gateway(FakeFetch({"data": ["不是物件"]}))
    with pytest.raises(SubtitleDownloadFailed) as e:
        gateway.fetch(URL, ())
    assert "格式" in str(e.value)


def test_a_non_ivod_url_is_refused_instead_of_hitting_the_list_endpoint():
    gateway, fetch = _gateway()
    with pytest.raises(SubtitleDownloadFailed):
        gateway.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ())
    assert fetch.urls == []


def test_the_duration_falls_back_to_the_last_segment_when_it_is_missing():
    record = {**RECORD}
    del record["影片長度"]
    gateway, _ = _gateway(FakeFetch({"data": record}))
    assert gateway.fetch(URL, ()).duration == pytest.approx(22.0)


def test_a_full_session_still_gets_a_usable_title():
    """完整會議沒有委員名稱以外的線索；標題不能只剩下會議名。"""
    record = {**RECORD, "委員名稱": "完整會議"}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    title = gateway.fetch(URL, ()).title
    assert "完整會議" in title and "2026-08-27" in title


def test_a_record_with_no_names_at_all_still_has_a_title():
    record = {k: v for k, v in RECORD.items() if k not in ("委員名稱", "日期", "會議資料")}
    gateway, _ = _gateway(FakeFetch({"data": record}))
    assert gateway.fetch(URL, ()).title.strip() != ""


def test_a_record_with_no_video_url_is_reported():
    record = {k: v for k, v in RECORD.items() if k != "video_url"}
    client = IvodClient(fetch=FakeFetch({"data": record}))
    with pytest.raises(SubtitleDownloadFailed):
        client.video_url("171180")
