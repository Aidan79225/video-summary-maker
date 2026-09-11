"""BuildDeckUseCase 的編排行為。"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Cue, Settings, Slide, Transcript
from slidebox.domain.errors import (
    NoSubtitlesAvailable,
    SubtitleDownloadFailed,
    OperationCancelled,
    SummarizerOutputInvalid,
)
from slidebox.usecases.build_deck import BuildDeckUseCase

from .fakes import (
    FakeAudioGateway,
    FakeFrameExtractor,
    FakeRenderer,
    FakeSectionGateway,
    FakeSubtitleGateway,
    FakeSummarizer,
    FakeTranscriber,
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


# --- 沒有字幕時改用語音辨識 ---


def _build_speech(subs=None, audio=None, transcriber=None, summ=None, rend=None):
    return BuildDeckUseCase(
        subs or FakeSubtitleGateway(fail=True),
        summ or FakeSummarizer([make_slides(3)]),
        FakeSectionGateway(),
        FakeFrameExtractor(),
        rend or FakeRenderer(),
        audio=audio or FakeAudioGateway(),
        transcriber=transcriber or FakeTranscriber(),
    )


def test_missing_subtitles_fall_back_to_speech():
    """攔的 bug：沒有字幕時直接失敗，語音辨識根本沒被用上。"""
    summ = FakeSummarizer([make_slides(3)])
    trans = FakeTranscriber()
    result = _build_speech(summ=summ, transcriber=trans).execute("URL", _settings())
    assert len(result.deck.slides) == 3
    assert trans.calls == [("OUT/_audio/audio.webm", 120.0)]
    assert "よろしくお願いします" in summ.compressed[0]


def test_speech_metadata_names_the_deck_and_its_folder():
    """攔的 bug：語音路徑沒有字幕 gateway 的 metadata，標題與輸出資料夾只能
    取自音訊 gateway；取錯會寫到錯的資料夾、投影片標題也錯。"""
    result = _build_speech().execute("URL", _settings(output_dir="OUT"))
    assert result.deck.video_title == "沒有字幕的影片"
    assert "spk1" in result.html_path


def test_speech_path_status_says_it_was_transcribed():
    """攔的 bug：使用者不知道這份摘要來自語音辨識，以為字幕品質就是這樣。"""
    seen: list[str] = []
    _build_speech().execute("URL", _settings(), lambda f, s: seen.append(s), None)
    assert any("語音辨識產生" in s for s in seen)


def test_speech_transcript_is_not_rolling_deduped():
    """攔的 bug：語音轉錄被當成 YouTube 滾動字幕去重。Whisper 的輸出是獨立的
    句子，套用滾動去重會把正常的重複內容剪掉。

    刻意選重疊 6 個字的兩句：少於 3 個字的重疊會被 _MIN_OVERLAP 門檻保護，
    那樣的測試區辨不出 is_automatic 的真假。
    """
    cues = (Cue(0.0, 4.0, "今天天氣真好我們出去走走"), Cue(4.0, 8.0, "我們出去走走吧"))
    summ = FakeSummarizer([make_slides(3)])
    _build_speech(summ=summ, transcriber=FakeTranscriber(cues=cues)).execute("URL", _settings())
    assert "我們出去走走吧" in summ.compressed[0]


def test_audio_is_removed_after_transcription():
    """攔的 bug：暫存音訊留在輸出資料夾裡越積越多。"""
    audio = FakeAudioGateway()
    _build_speech(audio=audio).execute("URL", _settings())
    assert audio.events[-1] == "cleanup"
    assert set(audio.cleaned) == set(audio.dest_dirs)


def test_leftover_audio_is_cleared_before_downloading():
    """攔的 bug：上一次執行在 finally 之前就中斷（例如轉錄時關掉視窗），留下的
    .part 會被 yt-dlp 續傳，把兩支影片的位元組拼成一個檔。下載前先清空。"""
    audio = FakeAudioGateway()
    _build_speech(audio=audio).execute("URL", _settings())
    assert audio.events[:2] == ["cleanup", "download"]


def test_audio_is_removed_when_cancelled_during_transcription():
    """攔的 bug：cleanup 不在 finally 裡，取消時暫存音訊殘留。"""
    audio = FakeAudioGateway()
    trans = FakeTranscriber(error=OperationCancelled())
    with pytest.raises(OperationCancelled):
        _build_speech(audio=audio, transcriber=trans).execute("URL", _settings())
    assert audio.events[-1] == "cleanup"


def test_no_detected_speech_is_a_clear_error():
    """攔的 bug：空的轉錄一路流進摘要，在很後面才以難懂的方式失敗。
    典型例子是純音樂或動畫，例如 Big Buck Bunny。"""
    with pytest.raises(NoSubtitlesAvailable) as exc:
        _build_speech(transcriber=FakeTranscriber(cues=())).execute("URL", _settings())
    assert "沒有偵測到語音" in str(exc.value)


def test_an_audio_failure_keeps_its_own_reason():
    """攔的 bug：音訊下載失敗時丟出原本的「沒有字幕」，把真正的原因藏起來。"""
    audio = FakeAudioGateway(error=NoSubtitlesAvailable("這部影片沒有字幕，音訊也下載失敗：HTTP 403"))
    with pytest.raises(NoSubtitlesAvailable) as exc:
        _build_speech(audio=audio).execute("URL", _settings())
    assert "403" in str(exc.value)


def test_speech_progress_never_moves_the_bar_backwards():
    """攔的 bug：音訊下載的進度直接進了進度條，接著摘要從 5% 開始，進度條倒退。
    語音路徑只回報文字，進度條顯示忙碌。"""
    seen: list[float | None] = []
    _build_speech().execute("URL", _settings(), lambda f, s: seen.append(f), None)
    fractions = [f for f in seen if f is not None]
    assert fractions == sorted(fractions)


class _Flag:
    def __init__(self):
        self.on = False

    def set(self):
        self.on = True

    def __call__(self):
        return self.on


def test_cancelling_during_the_audio_download_reaches_the_adapter():
    """攔的 bug：use case 傳給音訊 adapter 的不是自己的取消函式（例如寫成
    lambda: False）。那樣下載途中按取消毫無作用，要等整段下載完。

    斷言 adapter 自己看到了取消，而不只是「最後有拋出 OperationCancelled」——
    後者在接線壞掉時照樣成立，因為 use case 下載完後自己的 check() 也會拋。
    """
    flag = _Flag()
    audio = FakeAudioGateway(on_download=flag.set)
    with pytest.raises(OperationCancelled):
        _build_speech(audio=audio).execute("URL", _settings(), None, flag)
    assert audio.saw_cancel
    assert audio.events[-1] == "cleanup"


def test_cancelling_during_transcription_reaches_the_adapter():
    """攔的 bug：同上，但發生在語音辨識——10 分鐘影片要白等 2 分半。"""
    flag = _Flag()
    audio = FakeAudioGateway()
    trans = FakeTranscriber(on_transcribe=flag.set)
    with pytest.raises(OperationCancelled):
        _build_speech(audio=audio, transcriber=trans).execute("URL", _settings(), None, flag)
    assert trans.saw_cancel
    assert audio.events[-1] == "cleanup"



def test_a_transient_subtitle_failure_is_reported_not_transcribed():
    """攔的 bug：字幕存在但暫時下載失敗（HTTP 429）時也走語音備援——有人工中文
    字幕的影片被無聲地降級成較慢、較差的語音辨識，而且「請過幾分鐘再試」的
    提示被吞掉。使用者要的是「沒有字幕」的影片才改用語音。"""
    err = SubtitleDownloadFailed("字幕軌 zh-TW 下載失敗：YouTube 暫時限制了請求頻率（HTTP 429），請過幾分鐘再試。")
    trans = FakeTranscriber()
    uc = _build_speech(subs=FakeSubtitleGateway(error=err), transcriber=trans)
    with pytest.raises(SubtitleDownloadFailed) as exc:
        uc.execute("URL", _settings())
    assert "請過幾分鐘再試" in str(exc.value)
    assert trans.calls == []
