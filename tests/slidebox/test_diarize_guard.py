"""多人模式的流程還沒接上時（G2 才接）：要求 diarize 一定失敗，絕不默默走單人的路徑。

單人的路徑會把所有人寫成「講者」——對多人影片那是最糟的結果，比失敗還糟。
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


@pytest.mark.parametrize("mode", ["diarize", "multi", "", "DIARIZE"])
def test_a_mode_other_than_single_is_refused_before_anything_is_touched(mode):
    subtitles, summarizer = FakeSubtitleGateway(), FakeSummarizer([make_slides(3)])
    sections, frames, renderer, checkpoints = (
        FakeSectionGateway(), FakeFrameExtractor(), FakeRenderer(), FakeCheckpoints())
    usecase = BuildDeckUseCase(subtitles, summarizer, sections, frames, renderer, checkpoints=checkpoints)

    with pytest.raises(DiarizationUnavailable):
        usecase.execute("URL", Settings(output_dir="OUT", min_slides=2, max_slides=5),
                        speaker_mode=mode, speaker_count=None)

    assert subtitles.calls == [] and summarizer.compressed == []
    assert frames.calls == [] and renderer.rendered == [] and checkpoints.stored == {}


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
