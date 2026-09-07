"""BuildDeckUseCase 的編排行為。"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Settings, Slide
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
