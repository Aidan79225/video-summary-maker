"""多人模式的存檔階段：說話者區段、語音辨識的字、通過檢查的摘要。

共同的原則：只存原始而完整的產出；空的、沒有說話者的、時間不是有限數字的一律不存，
壞檔一律當作沒有。單人模式的階段與檔案不變（見 test_checkpoints.py）。
"""
from __future__ import annotations

import json
import os

import pytest

from slidebox.domain.entities import DiarizerOutput, Point, Slide, SpeakerTurn, Word, WordTranscript
from slidebox.infrastructure.checkpoint_store import JsonCheckpointStore

URL = "https://www.youtube.com/watch?v=abcdefghijk"
DIARIZATION = DiarizerOutput((SpeakerTurn(0.0, 2.5, "SPEAKER_00"), SpeakerTurn(2.5, 6.0, "SPEAKER_01")), 0.75)
WORDS = WordTranscript("abcdefghijk", "少子化對談", 120.5, "zh", (
    Word(0.0, 0.4, "今", 0, 0.98), Word(0.4, 0.8, "天", 0, 0.5), Word(1.0, 1.5, " hello", 1, 0.0)))
POINTS = (Point("S1", 0, 5.0, "詢問目前補助政策的成效", "先請教您怎麼看目前的補助政策"),
          Point("S2", 1, 14.0, "現金補助有幫助", "現金補助有幫助，但不能只靠現金"))
SLIDES = (Slide(1, "補助政策", ("[[S1]]：詢問目前補助政策的成效", "[[S2]]：現金補助有幫助"), 5.0,
                detail="[[S1]]：詢問目前補助政策的成效。 [[S2]]：現金補助有幫助。", points=POINTS),)


def _files(tmp_path):
    return sorted(name for _, _, names in os.walk(tmp_path) for name in names)


def test_every_multi_stage_comes_back_as_it_was_saved(tmp_path):
    store, out = JsonCheckpointStore(), str(tmp_path)
    store.save_diarization(out, URL, "d", DIARIZATION)
    store.save_words(out, URL, "w", WORDS)
    store.save_conversation(out, URL, "c", SLIDES, 3)
    assert store.load_diarization(out, URL, "d") == DIARIZATION
    assert store.load_words(out, URL, "w") == WORDS
    assert store.load_conversation(out, URL, "c") == (SLIDES, 3)
    assert _files(tmp_path) == ["conversation.json", "diarization.json", "words.json"]


def test_a_different_fingerprint_is_treated_as_missing(tmp_path):
    """攔的 bug：換了說話者區分的模型、指定人數或辨識設定，卻拿舊的結果來用。"""
    store, out = JsonCheckpointStore(), str(tmp_path)
    store.save_diarization(out, URL, "d", DIARIZATION)
    store.save_words(out, URL, "w", WORDS)
    store.save_conversation(out, URL, "c", SLIDES, 3)
    assert store.load_diarization(out, URL, "other") is None
    assert store.load_words(out, URL, "other") is None
    assert store.load_conversation(out, URL, "other") is None


def test_each_stage_is_independent_of_the_others(tmp_path):
    """說話者區分做完、語音辨識失敗時，重跑不必再區分一次（反之亦然）。"""
    store, out = JsonCheckpointStore(), str(tmp_path)
    store.save_diarization(out, URL, "d", DIARIZATION)
    assert store.load_words(out, URL, "d") is None and store.load_conversation(out, URL, "d") is None
    store.save_words(out, URL, "w", WORDS)
    assert store.load_diarization(out, URL, "d") == DIARIZATION


def test_each_url_has_its_own_stages(tmp_path):
    store, out = JsonCheckpointStore(), str(tmp_path)
    store.save_diarization(out, URL, "d", DIARIZATION)
    assert store.load_diarization(out, URL + "&t=1", "d") is None


def test_the_multi_stages_do_not_touch_the_single_mode_stage_files(tmp_path):
    """單人模式的 slides.json 格式不變；同一個網址先失敗在單人、再跑多人，兩邊不會互相蓋掉。"""
    store, out = JsonCheckpointStore(), str(tmp_path)
    store.save_slides(out, URL, "single", (Slide(1, "第一段", ("講者指出",), 12.5),), "")
    store.save_conversation(out, URL, "multi", SLIDES, 2)
    assert store.load_slides(out, URL, "single") is not None
    assert store.load_conversation(out, URL, "multi") is not None


@pytest.mark.parametrize("empty", [DiarizerOutput((), 0.0)])
def test_a_diarization_without_any_turn_is_never_saved(tmp_path, empty):
    """攔的 bug：存下一份「沒有說話者」的結果，下次接著用它，整支影片就寫成沒有人說話。"""
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "d", empty)
    assert store.load_diarization(str(tmp_path), URL, "d") is None
    assert _files(tmp_path) == []


@pytest.mark.parametrize("turns", [
    (SpeakerTurn(0.0, float("nan"), "A"),),
    (SpeakerTurn(float("inf"), 5.0, "A"),),
    (SpeakerTurn(5.0, 5.0, "A"),),
    (SpeakerTurn(5.0, 1.0, "A"),),
    (SpeakerTurn(0.0, 1.0, ""),),
    (SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(1.0, float("nan"), "B")),
])
def test_a_diarization_with_unusable_turns_is_never_saved(tmp_path, turns):
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "d", DiarizerOutput(turns, 0.0))
    assert store.load_diarization(str(tmp_path), URL, "d") is None
    assert _files(tmp_path) == []


def test_a_diarization_with_a_non_finite_overlap_is_never_saved(tmp_path):
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "d", DiarizerOutput(DIARIZATION.turns, float("nan")))
    assert store.load_diarization(str(tmp_path), URL, "d") is None


def test_words_without_any_word_are_never_saved(tmp_path):
    """攔的 bug：語音辨識沒聽到任何聲音的空結果被存下來，下次直接沿用，永遠做不出東西。"""
    store = JsonCheckpointStore()
    store.save_words(str(tmp_path), URL, "w", WordTranscript("v", "t", 10.0, "zh", ()))
    assert store.load_words(str(tmp_path), URL, "w") is None
    assert _files(tmp_path) == []


def test_words_with_non_finite_times_are_never_saved(tmp_path):
    store = JsonCheckpointStore()
    bad = WordTranscript("v", "t", 10.0, "zh", (Word(0.0, float("nan"), "字", 0, 1.0),))
    store.save_words(str(tmp_path), URL, "w", bad)
    assert store.load_words(str(tmp_path), URL, "w") is None


def test_a_conversation_with_a_pointless_page_is_never_saved(tmp_path):
    """沒有重點（沒有說話者）的一頁，整份都不存：存下去，下次接著用就是發佈一頁沒人說的話。"""
    store = JsonCheckpointStore()
    pointless = (*SLIDES, Slide(2, "沒有重點", ("[[S1]]：x",), 30.0))
    store.save_conversation(str(tmp_path), URL, "c", pointless, 5)
    assert store.load_conversation(str(tmp_path), URL, "c") is None
    store.save_conversation(str(tmp_path), URL, "c", (), 0)
    assert store.load_conversation(str(tmp_path), URL, "c") is None
    assert _files(tmp_path) == []


def test_a_conversation_cannot_have_kept_more_points_than_the_model_proposed(tmp_path):
    store = JsonCheckpointStore()
    store.save_conversation(str(tmp_path), URL, "c", SLIDES, 1)
    assert store.load_conversation(str(tmp_path), URL, "c") is None


def test_the_image_path_is_not_stored(tmp_path):
    """截圖每次都重抽，存了也是指向上一次的暫存位置。"""
    store = JsonCheckpointStore()
    with_image = (Slide(1, "補助政策", ("a",), 5.0, image_path="OUT/01.webp", points=POINTS),)
    store.save_conversation(str(tmp_path), URL, "c", with_image, 2)
    loaded, _ = store.load_conversation(str(tmp_path), URL, "c")
    assert loaded[0].image_path is None and loaded[0].points == POINTS


@pytest.mark.parametrize("stage,load", [
    ("diarization", lambda s, out: s.load_diarization(out, URL, "fp")),
    ("words", lambda s, out: s.load_words(out, URL, "fp")),
    ("conversation", lambda s, out: s.load_conversation(out, URL, "fp")),
])
@pytest.mark.parametrize("data", [
    None, [], "x", 5, {}, {"turns": 5}, {"turns": [[1, 2]]}, {"words": [[1, 2, "a"]]},
    {"slides": [{"index": 1}], "proposed": 1}, {"slides": "x", "proposed": 1},
    {"turns": [[0, 1, 5]], "overlap_seconds": 0},                       # 說話者標籤不是字串
    {"turns": [[0, 1, "A"]], "overlap_seconds": None},
])
def test_a_stored_file_with_the_wrong_shape_is_treated_as_missing(tmp_path, stage, load, data):
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "fp", DIARIZATION)       # 先建好資料夾
    folder = next(p for p, _, names in os.walk(tmp_path) if "diarization.json" in names)
    with open(os.path.join(folder, f"{stage}.json"), "w", encoding="utf-8") as f:
        json.dump({"url": URL, "fingerprint": "fp", "data": data}, f)
    assert load(store, str(tmp_path)) is None


NAN = float("nan")
GOOD_POINT = {"speaker": "S1", "turn": 0, "start": 5.0, "text": "詢問補助政策", "evidence": "請教補助政策"}
GOOD_SLIDE = {"index": 1, "title": "補助", "bullets": ["a"], "timestamp": 5.0, "detail": "d", "points": [GOOD_POINT]}


@pytest.mark.parametrize("stage,load,data", [
    # 形狀對，但內容不可用：就算檔案是別人（或舊版本）寫的，也不能拿來接著做
    ("diarization", "load_diarization", {"turns": [], "overlap_seconds": 0.0}),
    ("diarization", "load_diarization", {"turns": [[0.0, 1.0, ""]], "overlap_seconds": 0.0}),
    ("diarization", "load_diarization", {"turns": [[2.0, 1.0, "A"]], "overlap_seconds": 0.0}),
    ("diarization", "load_diarization", {"turns": [[0.0, NAN, "A"]], "overlap_seconds": 0.0}),
    ("diarization", "load_diarization", {"turns": [[0.0, 1.0, "A"]], "overlap_seconds": NAN}),
    ("words", "load_words", {"video_id": "v", "title": "t", "duration": 10.0, "language": "zh", "words": []}),
    ("words", "load_words", {"video_id": "v", "title": "t", "duration": 10.0, "language": "zh",
                             "words": [[0.0, NAN, "字", 0, 1.0]]}),
    ("words", "load_words", {"video_id": "v", "title": "t", "duration": NAN, "language": "zh",
                             "words": [[0.0, 1.0, "字", 0, 1.0]]}),
    ("conversation", "load_conversation", {"slides": [], "proposed": 0}),
    ("conversation", "load_conversation", {"slides": [{**GOOD_SLIDE, "points": []}], "proposed": 3}),
    ("conversation", "load_conversation", {"slides": [{**GOOD_SLIDE, "points": [{**GOOD_POINT, "speaker": ""}]}],
                                           "proposed": 3}),
    ("conversation", "load_conversation", {"slides": [GOOD_SLIDE], "proposed": 0}),
    ("conversation", "load_conversation", {"slides": [{**GOOD_SLIDE, "timestamp": NAN}], "proposed": 3}),
])
def test_a_well_formed_file_with_unusable_content_is_treated_as_missing(tmp_path, stage, load, data):
    """攔的 bug：只檢查存的時候、不檢查讀的時候。檔案可能是舊版本寫的，或被改過。"""
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "seed", DIARIZATION)     # 先建好資料夾
    folder = next(p for p, _, names in os.walk(tmp_path) if "diarization.json" in names)
    with open(os.path.join(folder, f"{stage}.json"), "w", encoding="utf-8") as f:
        json.dump({"url": URL, "fingerprint": "fp", "data": data}, f)
    assert getattr(store, load)(str(tmp_path), URL, "fp") is None


def test_the_planted_file_shapes_used_above_are_loadable_when_the_content_is_fine(tmp_path):
    """上面那些「不可用」的例子，換成可用的內容就讀得回來——證明它們是因為內容被擋，不是因為形狀。"""
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "seed", DIARIZATION)
    folder = next(p for p, _, names in os.walk(tmp_path) if "diarization.json" in names)
    for stage, data in (
        ("diarization", {"turns": [[0.0, 1.0, "A"]], "overlap_seconds": 0.0}),
        ("words", {"video_id": "v", "title": "t", "duration": 10.0, "language": "zh",
                   "words": [[0.0, 1.0, "字", 0, 1.0]]}),
        ("conversation", {"slides": [GOOD_SLIDE], "proposed": 1}),
    ):
        with open(os.path.join(folder, f"{stage}.json"), "w", encoding="utf-8") as f:
            json.dump({"url": URL, "fingerprint": "fp", "data": data}, f)
    assert store.load_diarization(str(tmp_path), URL, "fp") is not None
    assert store.load_words(str(tmp_path), URL, "fp") is not None
    assert store.load_conversation(str(tmp_path), URL, "fp") is not None


def test_a_corrupt_file_is_treated_as_missing(tmp_path):
    store = JsonCheckpointStore()
    store.save_diarization(str(tmp_path), URL, "d", DIARIZATION)
    store.save_words(str(tmp_path), URL, "w", WORDS)
    for root, _, names in os.walk(tmp_path):
        for name in names:
            with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                f.write('{"fingerprint": "d", "data": {')
    assert store.load_diarization(str(tmp_path), URL, "d") is None
    assert store.load_words(str(tmp_path), URL, "w") is None


def test_clear_removes_every_multi_stage_and_nothing_else(tmp_path):
    store, out = JsonCheckpointStore(), str(tmp_path)
    keep = tmp_path / "影片 [abcdefghijk]" / "slides.html"
    keep.parent.mkdir()
    keep.write_text("成品", encoding="utf-8")
    store.save_diarization(out, URL, "d", DIARIZATION)
    store.save_words(out, URL, "w", WORDS)
    store.save_conversation(out, URL, "c", SLIDES, 2)
    store.clear(out, URL)
    assert store.load_diarization(out, URL, "d") is None
    assert store.load_words(out, URL, "w") is None
    assert store.load_conversation(out, URL, "c") is None
    assert keep.read_text(encoding="utf-8") == "成品"


def test_a_save_that_cannot_be_written_does_not_raise_and_leaves_no_temp_file(tmp_path):
    """存檔是加值：輸出資料夾其實是個檔案時，工作照常；中途失敗也不留半個檔案。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    store = JsonCheckpointStore()
    store.save_diarization(str(blocker), URL, "d", DIARIZATION)
    store.save_words(str(blocker), URL, "w", WORDS)
    store.save_conversation(str(blocker), URL, "c", SLIDES, 2)
    assert store.load_diarization(str(blocker), URL, "d") is None
    store.clear(str(blocker), URL)


def test_the_stored_files_are_compact_arrays_not_one_object_per_word(tmp_path):
    """兩小時的影片有幾萬個字：一個字一個物件的 JSON 會有好幾 MB，存讀都慢。"""
    store = JsonCheckpointStore()
    many = WordTranscript("v", "t", 7200.0, "zh", tuple(
        Word(round(i * 0.3, 2), round(i * 0.3 + 0.2, 2), "字", i // 20, 0.9) for i in range(20000)))
    store.save_words(str(tmp_path), URL, "w", many)
    path = next(os.path.join(root, n) for root, _, names in os.walk(tmp_path) for n in names)
    with open(path, encoding="utf-8") as f:
        stored = json.load(f)["data"]["words"]
    assert isinstance(stored[0], list) and len(stored) == 20000
    assert os.path.getsize(path) < 20000 * 40
    assert store.load_words(str(tmp_path), URL, "w") == many
