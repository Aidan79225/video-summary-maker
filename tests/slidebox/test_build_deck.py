"""BuildDeckUseCase 的編排行為。"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Cue, Settings, Slide, Transcript
from slidebox.domain.errors import (
    NoSubtitlesAvailable,
    OperationCancelled,
    SummarizerOutputInvalid,
)
from slidebox.usecases.build_deck import BuildDeckUseCase

from .fakes import (
    FakeFrameExtractor,
    FakeRenderer,
    FakeSectionGateway,
    FakeSubtitleGateway,
    FakeSummarizer,
    RaisingThenSucceedingSummarizer,
    make_slides,
)


def _settings(**kw) -> Settings:
    base = dict(output_dir="OUT", min_slides=2, max_slides=5)
    base.update(kw)
    return Settings(**base)


def _build(subs=None, summ=None, secs=None, frames=None, rend=None):
    return BuildDeckUseCase(
        subs or FakeSubtitleGateway(),
        summ or FakeSummarizer([make_slides(3)]),
        secs or FakeSectionGateway(),
        frames or FakeFrameExtractor(),
        rend or FakeRenderer(),
    )


def test_happy_path_returns_deck_with_images_and_renders_once():
    rend = FakeRenderer()
    result = _build(rend=rend).execute("URL", _settings())
    assert len(result.deck.slides) == 3
    assert result.deck.missing_images == 0
    assert all(s.image_path is not None for s in result.deck.slides)
    assert len(rend.rendered) == 1
    assert result.html_path.endswith(".html")


def test_html_path_lives_under_output_dir_and_video_id():
    result = _build().execute("URL", _settings(output_dir="OUT"))
    assert "OUT" in result.html_path
    assert "vid1" in result.html_path


def test_compressed_text_is_passed_to_summarizer():
    """壓縮是純邏輯的責任，summarizer 收到的是壓好的字串。"""
    summ = FakeSummarizer([make_slides(3)])
    _build(summ=summ).execute("URL", _settings())
    assert summ.compressed[0].startswith("[0] ")


def test_timestamps_from_slides_drive_the_download():
    secs = FakeSectionGateway()
    _build(secs=secs).execute("URL", _settings())
    assert secs.timestamps == [30.0, 60.0, 90.0]


def test_no_subtitles_propagates():
    with pytest.raises(NoSubtitlesAvailable):
        _build(subs=FakeSubtitleGateway(fail=True)).execute("URL", _settings())


def test_retries_once_when_validation_fails_then_succeeds():
    """第一批只有 1 頁（低於下限 2），重試後給合格的 3 頁。"""
    summ = FakeSummarizer([make_slides(1), make_slides(3)])
    result = _build(summ=summ).execute("URL", _settings())
    assert len(result.deck.slides) == 3
    assert summ.hints[0] == ""
    assert "少於下限" in summ.hints[1]


def test_raises_when_retry_also_fails():
    summ = FakeSummarizer([make_slides(1), make_slides(1)])
    with pytest.raises(SummarizerOutputInvalid):
        _build(summ=summ).execute("URL", _settings())


def test_summarizer_is_called_at_most_twice():
    summ = FakeSummarizer([make_slides(1), make_slides(1)])
    with pytest.raises(SummarizerOutputInvalid):
        _build(summ=summ).execute("URL", _settings())
    assert len(summ.hints) == 2


def test_out_of_range_timestamp_is_clamped_not_retried():
    """時間戳越界只夾取，不該觸發重試。"""
    slides = (
        Slide(1, "A", ("x",), 99999.0),
        Slide(2, "B", ("y",), 10.0),
    )
    summ = FakeSummarizer([slides])
    result = _build(summ=summ).execute("URL", _settings())
    assert result.deck.slides[0].timestamp == 599.0
    assert len(summ.hints) == 1


def test_failed_section_download_leaves_that_slide_without_image():
    secs = FakeSectionGateway(results=["a.mp4", None, "c.mp4"])
    result = _build(secs=secs).execute("URL", _settings())
    assert result.deck.missing_images == 1
    assert result.deck.slides[1].image_path is None
    assert result.deck.slides[0].image_path is not None


def test_failed_frame_extraction_leaves_that_slide_without_image():
    secs = FakeSectionGateway(results=["a.mp4", "b.mp4", "c.mp4"])
    frames = FakeFrameExtractor(fail_on={"b.mp4"})
    result = _build(secs=secs, frames=frames).execute("URL", _settings())
    assert result.deck.missing_images == 1
    assert result.deck.slides[1].image_path is None


def test_still_renders_when_every_image_fails():
    """全部截圖失敗仍要出純文字 HTML。"""
    secs = FakeSectionGateway(results=[None, None, None])
    rend = FakeRenderer()
    result = _build(secs=secs, rend=rend).execute("URL", _settings())
    assert result.deck.missing_images == 3
    assert len(rend.rendered) == 1


def test_cleanup_runs_on_success():
    secs = FakeSectionGateway()
    _build(secs=secs).execute("URL", _settings())
    assert len(secs.cleaned) == 1


def test_cleanup_runs_even_when_cancelled():
    secs = FakeSectionGateway()
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3      # 讓前幾個階段通過，之後才取消

    with pytest.raises(OperationCancelled):
        _build(secs=secs).execute("URL", _settings(), None, cancel)
    assert len(secs.cleaned) == 1


def test_cancel_before_anything_skips_the_subtitle_fetch():
    subs = FakeSubtitleGateway()
    with pytest.raises(OperationCancelled):
        _build(subs=subs).execute("URL", _settings(), None, lambda: True)
    assert subs.calls == []


def test_progress_reaches_one():
    seen: list[float | None] = []
    _build().execute("URL", _settings(), lambda f, s: seen.append(f), None)
    assert seen[-1] == 1.0


def test_sub_step_progress_is_mapped_into_the_summary_band():
    """子步驟的 0..1 進度要映射到整條 pipeline 的 5%..40% 區間。"""
    seen: list[float | None] = []
    _build().execute("URL", _settings(), lambda f, s: seen.append(f), None)
    assert 0.05 in seen                              # 子步驟 0.0 → 區間下緣
    assert any(v == pytest.approx(0.225) for v in seen if v is not None)  # 子步驟 0.5 → 區間中點
    assert None in seen          # 不確定進度原樣傳遞，不得被算成數字


def test_automatic_transcript_warns_about_quality_in_progress_status():
    """FakeSubtitleGateway 的預設 transcript 是 is_automatic=True。"""
    seen: list[str] = []
    _build().execute("URL", _settings(), lambda f, s: seen.append(s), None)
    assert any("自動字幕" in s for s in seen)


def test_manual_transcript_does_not_warn_in_progress_status():
    manual = Transcript(
        video_id="vid1",
        title="測試影片",
        duration=600.0,
        cues=(Cue(0.0, 3.0, "第一句"), Cue(30.0, 33.0, "第二句")),
        language="zh-TW",
        is_automatic=False,
    )
    seen: list[str] = []
    _build(subs=FakeSubtitleGateway(transcript=manual)).execute(
        "URL", _settings(), lambda f, s: seen.append(s), None
    )
    assert not any("自動字幕" in s for s in seen)


def test_retries_once_when_summarizer_output_is_not_json():
    """完全不是 JSON 的回應（SummarizerOutputInvalid）跟驗證失敗一樣要拿到重試。"""
    summ = RaisingThenSucceedingSummarizer(make_slides(3))
    result = _build(summ=summ).execute("URL", _settings())
    assert len(result.deck.slides) == 3
    assert summ.calls == 2


def test_cancel_during_frame_attachment_still_cleans_up():
    """取消發生在抽幀迴圈時，clips 已經落地，cleanup 才真的有事情要做。"""
    secs = FakeSectionGateway()
    frames = FakeFrameExtractor()
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 5      # 通過下載，進到抽幀迴圈才取消

    with pytest.raises(OperationCancelled):
        _build(secs=secs, frames=frames).execute("URL", _settings(), None, cancel)
    assert len(secs.cleaned) == 1
    assert len(frames.calls) < 3   # 沒有把三張圖都做完
