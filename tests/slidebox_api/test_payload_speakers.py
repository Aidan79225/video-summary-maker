"""多人模式的成品：多出來的欄位、必須齊全的出處，以及單人結果完全不受影響。"""
from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from slidebox.domain.entities import Ask, Brief, Deck, Slide
from slidebox_api.payload import deck_payload

from .speaker_support import deck_from_result, load_contract

DIARIZED = deck_from_result(load_contract()["result"])


def payload_of(deck: Deck) -> dict:
    return deck_payload(deck, "abcdefghijk")


def test_a_diarized_payload_adds_exactly_the_contract_keys():
    payload = payload_of(DIARIZED)

    assert sorted(payload) == ["brief", "diarization", "slides", "source_note", "source_url",
                               "speaker_mode", "speakers", "title", "transcript_text", "video_id"]
    assert payload["speaker_mode"] == "diarize"
    assert payload["brief"] is None
    assert sorted(payload["slides"][0]) == ["bullets", "detail", "image_base64", "image_media_type",
                                            "index", "points", "timestamp", "title"]
    assert sorted(payload["slides"][0]["points"][0]) == ["evidence", "speaker", "start", "text", "turn"]
    assert sorted(payload["speakers"][0]) == ["first_seen", "flags", "id", "kind", "label", "last_seen",
                                              "samples", "seconds", "turns"]
    assert sorted(payload["speakers"][0]["samples"][0]) == ["start", "text"]
    assert sorted(payload["diarization"]) == [
        "engine", "fingerprint", "found_speakers", "kept_points", "minor_speakers", "model",
        "overlap_ratio", "proposed_points", "requested_speakers", "speech_seconds", "status",
        "unassigned_ratio", "version", "warnings"]


def test_the_diarized_payload_is_plain_json_with_lists_not_tuples():
    payload = payload_of(DIARIZED)
    assert json.loads(json.dumps(payload)) == payload
    assert isinstance(payload["speakers"], list) and isinstance(payload["diarization"]["warnings"], list)
    assert isinstance(payload["speakers"][0]["flags"], list)
    assert isinstance(payload["slides"][0]["points"], list)


def test_warnings_and_flags_are_carried_through():
    speaker = replace(DIARIZED.speakers[0], flags=("short_sample",))
    report = replace(DIARIZED.diarization, status="warn", warnings=("high_unassigned", "many_minor"))
    payload = payload_of(replace(DIARIZED, speakers=(speaker, *DIARIZED.speakers[1:]), diarization=report))
    assert payload["speakers"][0]["flags"] == ["short_sample"]
    assert payload["diarization"]["status"] == "warn"
    assert payload["diarization"]["warnings"] == ["high_unassigned", "many_minor"]


def test_sums_of_many_small_floats_are_rounded_but_positions_in_the_video_are_not():
    """秒數是一堆小數加起來的，不該在 JSON 裡看到 301.49999999999994；
    但「第幾秒」是被引用的位置，原樣交出去（&t= 連結與核對都靠它）。"""
    speaker = replace(DIARIZED.speakers[0], seconds=301.49999999999994, first_seen=5.123456,
                      last_seen=702.987654)
    report = replace(DIARIZED.diarization, speech_seconds=711.8999999999999,
                     unassigned_ratio=0.012345678, overlap_ratio=0.0314159)
    point = replace(DIARIZED.slides[0].points[0], start=5.123456)
    slide = replace(DIARIZED.slides[0], timestamp=5.123456, points=(point, *DIARIZED.slides[0].points[1:]))
    payload = payload_of(replace(DIARIZED, speakers=(speaker, *DIARIZED.speakers[1:]), diarization=report,
                                 slides=(slide, *DIARIZED.slides[1:])))

    assert payload["speakers"][0]["seconds"] == 301.5
    assert payload["diarization"]["speech_seconds"] == 711.9
    assert payload["diarization"]["unassigned_ratio"] == 0.012
    assert payload["diarization"]["overlap_ratio"] == 0.031
    assert payload["speakers"][0]["first_seen"] == 5.123456
    assert payload["speakers"][0]["last_seen"] == 702.987654
    assert payload["slides"][0]["timestamp"] == 5.123456
    assert payload["slides"][0]["points"][0]["start"] == 5.123456


def test_images_travel_with_a_diarized_deck_exactly_as_with_a_single_one(tmp_path):
    image = tmp_path / "01.webp"
    image.write_bytes(b"\x00fake\xff")
    slide = replace(DIARIZED.slides[0], image_path=str(image))
    payload = payload_of(replace(DIARIZED, slides=(slide, *DIARIZED.slides[1:])))
    assert payload["slides"][0]["image_base64"] == base64.b64encode(b"\x00fake\xff").decode("ascii")
    assert payload["slides"][0]["image_media_type"] == "image/webp"
    assert payload["slides"][1]["image_base64"] is None and payload["slides"][1]["image_media_type"] is None


# --- 缺任何一塊都不能交出去 ---


def test_a_diarized_deck_without_its_report_is_refused():
    with pytest.raises(ValueError, match="說話者區分報告"):
        payload_of(replace(DIARIZED, diarization=None))


def test_a_diarized_deck_without_a_speaker_list_is_refused():
    with pytest.raises(ValueError, match="說話者清單"):
        payload_of(replace(DIARIZED, speakers=()))


def test_a_diarized_deck_with_a_brief_is_refused():
    """多人模式不寫質詢卡；有一張就表示有人把單人的流程混了進來。"""
    with pytest.raises(ValueError, match="brief"):
        payload_of(replace(DIARIZED, brief=Brief("一句話", asks=(Ask("要求"),))))


def test_a_slide_whose_points_are_missing_is_refused_and_named():
    bare = replace(DIARIZED.slides[1], points=())
    with pytest.raises(ValueError, match="第 2 頁"):
        payload_of(replace(DIARIZED, slides=(DIARIZED.slides[0], bare)))


def test_every_slide_without_points_is_named():
    bare = tuple(replace(s, points=()) for s in DIARIZED.slides)
    with pytest.raises(ValueError, match="第 1、2 頁"):
        payload_of(replace(DIARIZED, slides=bare))


def test_a_mode_nobody_knows_is_refused_instead_of_being_taken_for_single():
    with pytest.raises(ValueError, match="speaker_mode"):
        payload_of(replace(DIARIZED, speaker_mode="multi"))


# --- 單人結果不受影響 ---


def test_a_single_deck_never_shows_speaker_fields_even_if_it_carries_them():
    """攔的 bug：判斷改成「有資料就輸出」，單人結果就會跟著多出鍵，新聞服務把它當多人結果處理。"""
    carrying = replace(DIARIZED, speaker_mode="single", brief=None)
    payload = payload_of(carrying)
    for key in ("speaker_mode", "speakers", "diarization"):
        assert key not in payload
    assert all("points" not in slide for slide in payload["slides"])
    assert sorted(payload) == ["brief", "slides", "source_note", "source_url", "title",
                               "transcript_text", "video_id"]


def test_a_single_deck_is_not_held_to_the_attribution_rules():
    """單人的頁面沒有 points、沒有說話者清單，也可以有質詢卡：這些規定只管多人。"""
    deck = Deck("https://x", "標題", (Slide(1, "章", ("點",), 0.0),),
                brief=Brief("一句話", asks=(Ask("要求"),)))
    payload = payload_of(deck)
    assert payload["brief"]["one_liner"] == "一句話"
    assert payload["slides"][0]["bullets"] == ["點"]
