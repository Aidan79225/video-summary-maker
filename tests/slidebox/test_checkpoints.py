"""存檔續跑的 JSON 實作：存得回來、對不上的設定不冒充、壞掉的檔案不讓工作失敗。"""
from __future__ import annotations

import os

from slidebox.domain.entities import Ask, Brief, Cue, KeyNumber, Slide, Transcript
from slidebox.infrastructure.checkpoint_store import JsonCheckpointStore

URL = "https://www.youtube.com/watch?v=f4r2yWk4LV0"
TRANSCRIPT = Transcript("vid1", "標題", 600.0, (Cue(0.0, 3.0, "第一句"), Cue(30.0, 33.5, "第二句")),
                        "zh-TW", is_automatic=True, source_note="來源註記")
SLIDES = (Slide(1, "第一段", ("講者指出", "第二點"), 12.5, detail="完整敘述"),
          Slide(2, "第二段", ("講者要求",), 40.0))
BRIEF = Brief("一句話", (KeyNumber("82.4", "億元", "三年累計", "三年編了 82.4 億", "醫療法", "第106條"),),
              (Ask("一個月內提出報告", "一個月內", "部長允諾"),))


def test_everything_comes_back_as_it_was_saved(tmp_path):
    store = JsonCheckpointStore()
    out = str(tmp_path)
    store.save_transcript(out, URL, "fp1", TRANSCRIPT, "由語音辨識產生")
    store.save_slides(out, URL, "fp2", SLIDES, "第 1 頁仍有第一人稱")
    store.save_brief(out, URL, "fp3", BRIEF)
    assert store.load_transcript(out, URL, "fp1") == (TRANSCRIPT, "由語音辨識產生")
    assert store.load_slides(out, URL, "fp2") == (SLIDES, "第 1 頁仍有第一人稱")
    assert store.load_brief(out, URL, "fp3") == BRIEF


def test_a_different_fingerprint_is_treated_as_missing(tmp_path):
    store = JsonCheckpointStore()
    store.save_slides(str(tmp_path), URL, "max=5", SLIDES, "")
    assert store.load_slides(str(tmp_path), URL, "max=6") is None


def test_each_url_has_its_own_checkpoint(tmp_path):
    store = JsonCheckpointStore()
    store.save_slides(str(tmp_path), URL, "fp", SLIDES, "")
    assert store.load_slides(str(tmp_path), URL + "&t=1", "fp") is None


def test_a_corrupt_file_is_treated_as_missing(tmp_path):
    """寫到一半當機留下的半個檔案，不能讓下一次的工作失敗。"""
    store = JsonCheckpointStore()
    store.save_slides(str(tmp_path), URL, "fp", SLIDES, "")
    for root, _, files in os.walk(tmp_path):
        for name in files:
            with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                f.write("{\"fingerprint\": \"fp\", \"data\": {")
    assert store.load_slides(str(tmp_path), URL, "fp") is None


def test_clear_removes_the_checkpoint_and_nothing_else(tmp_path):
    store = JsonCheckpointStore()
    keep = tmp_path / "影片 [vid1]" / "slides.html"
    keep.parent.mkdir()
    keep.write_text("成品", encoding="utf-8")
    store.save_slides(str(tmp_path), URL, "fp", SLIDES, "")
    store.clear(str(tmp_path), URL)
    assert store.load_slides(str(tmp_path), URL, "fp") is None
    assert keep.read_text(encoding="utf-8") == "成品"
    store.clear(str(tmp_path), URL)   # 清兩次也不會出錯


def test_a_save_that_cannot_be_written_does_not_raise(tmp_path):
    """存檔是加值：寫不進去（輸出資料夾其實是個檔案、沒有權限）時工作照常。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    store = JsonCheckpointStore()
    store.save_slides(str(blocker), URL, "fp", SLIDES, "")
    assert store.load_slides(str(blocker), URL, "fp") is None
    store.clear(str(blocker), URL)
