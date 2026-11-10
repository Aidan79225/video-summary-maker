"""說話者模式的守門：不認得的模式、或沒有裝多人零件的組裝，一律失敗，絕不默默走單人的路徑。

單人的路徑會把所有人寫成「講者」——對多人影片那是最糟的結果，比失敗還糟。
多人流程本身的測試在 test_build_deck_diarized.py。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Settings
from slidebox.domain.errors import DiarizationUnavailable
from slidebox.usecases.build_deck import BuildDeckUseCase

from .fakes import (
    FakeCheckpoints,
    FakeFrameExtractor,
    FakeRenderer,
    FakeSectionGateway,
    FakeSubtitleGateway,
    FakeSummarizer,
    make_slides,
)


def _single_mode_parts():
    subtitles, summarizer = FakeSubtitleGateway(), FakeSummarizer([make_slides(3)])
    sections, frames, renderer, checkpoints = (
        FakeSectionGateway(), FakeFrameExtractor(), FakeRenderer(), FakeCheckpoints())
    usecase = BuildDeckUseCase(subtitles, summarizer, sections, frames, renderer, checkpoints=checkpoints)
    return usecase, subtitles, summarizer, sections, frames, renderer, checkpoints


def _assert_nothing_was_touched(subtitles, summarizer, sections, frames, renderer, checkpoints):
    assert subtitles.calls == [] and summarizer.compressed == []
    assert sections.timestamps == [] and frames.calls == [] and renderer.rendered == []
    assert checkpoints.stored == {} and checkpoints.cleared == []


@pytest.mark.parametrize("mode", ["multi", "", "DIARIZE", "diarize ", "double", "off"])
def test_a_mode_other_than_single_or_diarize_is_refused_before_anything_is_touched(mode):
    usecase, *parts = _single_mode_parts()

    with pytest.raises(DiarizationUnavailable, match="不支援的說話者模式"):
        usecase.execute("URL", Settings(output_dir="OUT", min_slides=2, max_slides=5),
                        speaker_mode=mode, speaker_count=None)

    _assert_nothing_was_touched(*parts)


def test_the_error_names_the_refused_mode_and_says_it_will_not_fall_back():
    usecase, *_ = _single_mode_parts()
    with pytest.raises(DiarizationUnavailable) as raised:
        usecase.execute("URL", Settings(output_dir="OUT"), speaker_mode="multi")
    assert "multi" in str(raised.value) and "不會退回單人的寫法" in str(raised.value)


def test_diarize_on_a_build_without_the_multi_speaker_parts_is_refused_not_degraded():
    """Pi 或測試用的簡化組裝沒有音訊、說話者區分、逐字辨識、對談摘要：要求多人就失敗，
    絕不退回有字幕的單人路徑。"""
    usecase, *parts = _single_mode_parts()

    with pytest.raises(DiarizationUnavailable, match="多人模式需要"):
        usecase.execute("URL", Settings(output_dir="OUT", min_slides=2, max_slides=5),
                        speaker_mode="diarize", speaker_count=None)

    _assert_nothing_was_touched(*parts)


def test_single_mode_and_the_default_still_work_exactly_as_before():
    renderer = FakeRenderer()
    usecase = BuildDeckUseCase(FakeSubtitleGateway(), FakeSummarizer([make_slides(3), make_slides(3)]),
                               FakeSectionGateway(), FakeFrameExtractor(), renderer)
    settings = Settings(output_dir="OUT", min_slides=2, max_slides=5)

    default = usecase.execute("URL", settings)
    explicit = usecase.execute("URL", settings, speaker_mode="single", speaker_count=None)

    assert len(default.deck.slides) == len(explicit.deck.slides) == 3
    assert default.deck.speaker_mode == "single" and default.deck.speakers == ()
    assert default.deck.diarization is None and all(s.points == () for s in default.deck.slides)
