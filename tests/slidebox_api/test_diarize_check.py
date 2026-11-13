"""說話者區分的檢查工具（python -m slidebox_api.diarize_check）：全部用假零件，
不下載、不載入模型、不碰 Hugging Face 或 YouTube。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from slidebox.domain.entities import AudioClip, DiarizationReport, DiarizerOutput, SpeakerTurn, Word
from slidebox.domain.errors import DiarizationUnavailable, NoSubtitlesAvailable, OperationCancelled, SpeechRecognitionFailed
from slidebox_api import diarize_check as check
from tests.slidebox.diarized_support import CLIP, FIXTURE, URL, FakeDiarizer, FakeWordTranscriber

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Audio:
    """比照 AudioGateway：真的在 dest_dir 寫一個檔，才看得出暫存有沒有清乾淨；cleanup 只記錄。"""

    def __init__(self, clip: AudioClip = CLIP, error: Exception | None = None):
        self.clip, self.error = clip, error
        self.urls: list[str] = []
        self.cleaned: list[str] = []

    def download_audio(self, url, dest_dir, progress, is_cancelled):
        self.urls.append(url)
        progress(0.5, "下載音訊…")
        if self.error is not None:
            raise self.error
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, "audio.webm")
        with open(path, "wb") as f:
            f.write(b"fake audio")
        return replace(self.clip, path=path)

    def cleanup(self, dest_dir):
        self.cleaned.append(dest_dir)


class Cuts:
    """切音訊的假零件：記下參數，並真的寫出一個檔。"""

    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple] = []
        self.error = error

    def __call__(self, src, dest, start, length):
        self.calls.append((src, dest, start, length))
        if self.error is not None:
            raise self.error
        with open(dest, "wb") as f:
            f.write(b"fake slice")


class Ticks:
    """每被問一次就走一秒的時鐘：耗時是確定的。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def check_run(args, tmp_path, audio=None, diarizer=None, words=None, cuts=None, peak=3_500_000_000,
              gpu_peak=None, **parts_options):
    """跑一次檢查，回傳 (結束碼, 標準輸出, 標準錯誤, 零件)。tmp_path 當暫存資料夾的位置，結束後應該是空的。"""
    parts = check.Parts(
        audio=audio or Audio(), diarizer=diarizer or FakeDiarizer(), words=words or FakeWordTranscriber(),
        whisper_model="whisper-a", cut_audio=cuts or Cuts(), clock=Ticks(),
        peak_memory=lambda: peak, gpu_peak_memory=lambda: gpu_peak, **parts_options)
    out, err = io.StringIO(), io.StringIO()
    code = check.main(args, parts=parts, out=out, err=err, base_dir=str(tmp_path))
    return code, out.getvalue(), err.getvalue(), parts


def nothing_left(tmp_path) -> bool:
    return os.listdir(tmp_path) == []


# --- 時間與參數 ---


@pytest.mark.parametrize("text,expected", [
    ("0", 0.0), ("90", 90.0), ("90.5", 90.5), ("1:30", 90.0), ("0:05", 5.0), ("10:00", 600.0),
    ("1:02:03", 3723.0), (" 2:00 ", 120.0),
])
def test_times_can_be_seconds_or_clock_style(text, expected):
    assert check.parse_time(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", ":30", "1:", "-5", "1:70", "1:02:99", "inf", "nan", "1e999"])
def test_times_that_make_no_sense_are_refused(text):
    with pytest.raises(ValueError):
        check.parse_time(text)


def test_the_sample_is_a_fixed_input_with_nothing_else_to_say():
    options = check.parse_args(["--sample"])
    assert options == check.Options(url=check.SAMPLE_URL, sample=True)


def test_a_video_with_a_range_and_a_speaker_count():
    options = check.parse_args([URL, "--start", "1:00", "--end", "11:00", "--speakers", "3"])
    assert options == check.Options(url=URL, sample=False, start=60.0, end=660.0, speakers=3)


def test_a_bare_url_checks_the_whole_video_with_an_automatic_speaker_count():
    assert check.parse_args([URL]) == check.Options(url=URL)


@pytest.mark.parametrize("argv", [
    [],                                                  # 什麼都沒給
    ["--sample", URL],                                   # 二選一
    ["--sample", "--start", "5"],                        # 樣本是固定的
    ["--sample", "--end", "5"],
    ["--sample", "--speakers", "2"],
    [URL, "--speakers", "1"],                            # 一個人就是單人模式
    [URL, "--speakers", "0"],
    [URL, "--speakers", "two"],
    [URL, "--speakers", "2.5"],
    [URL, "--start", "-1"],
    [URL, "--start", "abc"],
    [URL, "--end", "0"],
    [URL, "--start", "60", "--end", "60"],               # 空的範圍
    [URL, "--start", "60", "--end", "30"],
    [URL, "--end", "0:00"],
    ["file:///etc/passwd"],                              # 只收 http／https
    ["C:\\Users\\someone\\video.mp4"],
    ["ftp://example.com/a.mp4"],
])
def test_bad_arguments_stop_with_exit_code_2_before_anything_is_built(argv, tmp_path, capsys):
    # parts 給 None：若參數檢查沒擋住，default_parts() 會去建真的零件，這個測試就不會是這個結果
    assert check.main(argv, parts=None, base_dir=str(tmp_path)) == check.EXIT_USAGE
    assert capsys.readouterr().err != ""
    assert nothing_left(tmp_path)


def test_the_end_alone_means_from_the_beginning(tmp_path):
    assert check.parse_args([URL, "--end", "30"]).end == 30.0


# --- 一次完整的檢查 ---


def test_a_whole_video_check_prints_who_said_what_and_how_to_listen_for_it(tmp_path):
    code, out, err, parts = check_run([URL], tmp_path)

    assert code == check.EXIT_OK
    assert "少子化對談（abcdefghijk）" in out
    assert "範圍：整支（120 秒）" in out
    assert "fake-engine 1.0（fake/model）" in out
    assert "語音辨識：whisper-a（語言 zh）" in out
    assert "指定人數：自動判斷" in out
    # 說話者區分器找到的原始聲音
    assert "說話者區分器找到 3 個聲音" in out
    # 逐段：起訖、說話者代號與標籤、文字
    assert "00:05–00:09  S1 發言者 A  今天我們來談少子化，先請教您怎麼看目前的補助政策？" in out
    assert "00:09–00:14  S2 發言者 B  現金補助有幫助，但不能只靠現金，托育資源更重要。" in out
    assert "S3 其他聲音 1" in out and "S0 無法判斷" in out
    # 每位說話者的統計
    assert "S1 發言者 A（主要發言者）" in out and "S3 其他聲音 1（只出現一下的聲音）" in out
    # 說話者清單：每個標籤最長的幾段話，附可以點開來聽的連結
    assert "發言者 A（S1）" in out
    assert "00:05  https://www.youtube.com/watch?v=abcdefghijk&t=5s  今天我們來談少子化" in out
    assert "00:34  https://www.youtube.com/watch?v=abcdefghijk&t=34s" in out
    # 抽查與品質
    assert "--- 抽查（平均分布的 10 個位置）---" in out
    assert "狀態：" in out and "無法判斷是誰說的：" in out and "同時有人說話：" in out
    # 沒有任何東西寫到錯誤輸出之外的地方，也沒有留下暫存
    assert nothing_left(tmp_path)


def test_the_check_reports_time_per_stage_and_the_memory_peak(tmp_path):
    _, out, _, _ = check_run([URL], tmp_path, peak=3_500_000_000, gpu_peak=2 ** 30)

    # 時鐘每被問一次走一秒：每個階段前後各一次，加上階段裡回報進度的那一次
    assert "下載 2.0 秒；說話者區分 2.0 秒；語音辨識 2.0 秒；對齊 1.0 秒；合計 10.0 秒" in out
    assert "本行程記憶體峰值：3.3 GiB（含載入的模型）" in out
    assert "PyTorch 顯示卡記憶體峰值：1.0 GiB" in out and "nvidia-smi" in out


def test_a_platform_that_cannot_measure_memory_says_so_instead_of_printing_zero(tmp_path):
    _, out, _, _ = check_run([URL], tmp_path, peak=None, gpu_peak=None)
    assert "本行程記憶體峰值：這個平台量不到" in out
    assert "PyTorch 顯示卡" not in out


def test_the_audio_goes_through_the_audio_gateway_only_and_the_requested_count_reaches_the_diarizer(tmp_path):
    audio, diarizer, words = Audio(), FakeDiarizer(), FakeWordTranscriber()

    code, out, _, _ = check_run([URL, "--speakers", "3"], tmp_path, audio=audio, diarizer=diarizer, words=words)

    assert code == check.EXIT_OK
    assert audio.urls == [URL]
    assert [(duration, count) for _, duration, count in diarizer.calls] == [(120.0, 3)]
    assert diarizer.calls[0][0] == words.calls[0][0]               # 兩個階段聽的是同一個檔
    assert "指定人數：3" in out
    assert words.hints == [None]                                    # 沒有人名提示：檢查的是聲音，不是辨識


def test_no_language_model_is_involved():
    """零件裡根本沒有摘要器：這個工具只到對齊為止。"""
    assert [f for f in check.Parts.__dataclass_fields__ if "summar" in f or "conversation" in f] == []


# --- 只檢查一段 ---


def test_a_range_is_cut_out_and_every_time_printed_is_the_time_in_the_whole_video(tmp_path):
    cuts, diarizer, words = Cuts(), FakeDiarizer(), FakeWordTranscriber()

    code, out, err, _ = check_run([URL, "--start", "30", "--end", "1:30"], tmp_path, cuts=cuts,
                                  diarizer=diarizer, words=words)

    assert code == check.EXIT_OK
    (src, dest, start, length), = cuts.calls
    assert src.endswith("audio.webm") and os.path.basename(dest) == "slice.wav"
    assert (start, length) == (30.0, 60.0)
    # 說話者區分與語音辨識聽的是切出來的那一段，長度是那一段的長度
    assert diarizer.calls[0][:2] == (dest, 60.0) and words.calls[0] == (dest, 60.0)
    # 檢查範圍內第 5 秒的話，在整支影片裡是第 35 秒
    assert "範圍：00:30–01:30（60 秒）" in out
    assert "00:35–00:39  S1 發言者 A  今天我們來談少子化" in out
    assert "00:35  https://www.youtube.com/watch?v=abcdefghijk&t=35s" in out
    assert "&t=5s" not in out
    assert "[切出] 切出 00:30–01:30" in err
    assert nothing_left(tmp_path)


def test_the_cut_file_exists_while_the_models_listen_and_is_gone_afterwards(tmp_path):
    seen: list[bool] = []
    cuts = Cuts()
    diarizer = FakeDiarizer(on_diarize=lambda: seen.append(os.path.exists(cuts.calls[-1][1])))

    check_run([URL, "--start", "10"], tmp_path, cuts=cuts, diarizer=diarizer)

    assert seen == [True]
    assert not os.path.exists(cuts.calls[0][1]) and nothing_left(tmp_path)


def test_a_start_alone_runs_to_the_end_of_the_video(tmp_path):
    cuts = Cuts()
    _, out, _, _ = check_run([URL, "--start", "20"], tmp_path, cuts=cuts)
    assert cuts.calls[0][2:] == (20.0, 100.0)
    assert "範圍：00:20–02:00（100 秒）" in out


def test_an_end_beyond_the_video_is_pulled_back_to_its_end_with_a_note(tmp_path):
    cuts = Cuts()
    code, out, err, _ = check_run([URL, "--start", "30", "--end", "500"], tmp_path, cuts=cuts)
    assert code == check.EXIT_OK
    assert cuts.calls[0][2:] == (30.0, 90.0)
    assert "超過影片長度，改成影片結尾（02:00）" in err


@pytest.mark.parametrize("start", ["300", "2:00"], ids=["beyond", "exactly-at-the-end"])
def test_a_start_at_or_beyond_the_end_fails_before_the_models_are_loaded(tmp_path, start):
    cuts, diarizer = Cuts(), FakeDiarizer()
    code, out, err, _ = check_run([URL, "--start", start], tmp_path, cuts=cuts, diarizer=diarizer)
    assert code == check.EXIT_FAILED
    assert "已經超過影片長度" in err
    assert cuts.calls == [] and diarizer.calls == []
    assert nothing_left(tmp_path)


def test_an_end_exactly_at_the_end_of_the_video_needs_no_note(tmp_path):
    cuts = Cuts()
    code, _, err, _ = check_run([URL, "--start", "30", "--end", "2:00"], tmp_path, cuts=cuts)
    assert code == check.EXIT_OK and cuts.calls[0][2:] == (30.0, 90.0)
    assert "超過影片長度" not in err


def test_when_the_length_of_the_video_is_unknown_the_cut_runs_to_the_end_of_the_file(tmp_path):
    cuts = Cuts()
    audio = Audio(clip=replace(CLIP, duration=0.0))
    _, out, err, _ = check_run([URL, "--start", "20"], tmp_path, audio=audio, cuts=cuts)
    assert cuts.calls[0][2:] == (20.0, None)
    assert "00:20–結尾" in err


def test_an_unknown_length_without_a_range_is_not_printed_as_zero_seconds(tmp_path):
    _, out, _, _ = check_run([URL], tmp_path, audio=Audio(clip=replace(CLIP, duration=0.0)))
    assert "範圍：整支\n" in out


def test_a_failed_cut_stops_with_its_reason_and_leaves_nothing_behind(tmp_path):
    cuts = Cuts(error=check.AudioCutFailed("切出指定範圍的音訊失敗：Invalid data"))
    diarizer = FakeDiarizer()
    code, _, err, _ = check_run([URL, "--start", "10"], tmp_path, cuts=cuts, diarizer=diarizer)
    assert code == check.EXIT_FAILED and "Invalid data" in err
    assert diarizer.calls == [] and nothing_left(tmp_path)


# --- 跑不起來的時候 ---


def test_an_unusable_diarizer_stops_before_anything_is_downloaded(tmp_path):
    audio = Audio()
    diarizer = FakeDiarizer(usable=False, reason="沒有設定 HUGGINGFACE_TOKEN；多人模式需要 Hugging Face token")

    code, out, err, _ = check_run([URL], tmp_path, audio=audio, diarizer=diarizer)

    assert code == check.EXIT_FAILED
    assert "說話者區分目前無法使用：沒有設定 HUGGINGFACE_TOKEN" in err
    assert audio.urls == [] and diarizer.calls == [] and out == ""
    assert nothing_left(tmp_path)


def test_a_failed_download_says_so_and_cleans_up(tmp_path):
    audio = Audio(error=NoSubtitlesAvailable("這部影片沒有字幕，音訊也下載失敗：Video unavailable"))
    code, out, err, _ = check_run([URL], tmp_path, audio=audio)
    assert code == check.EXIT_FAILED
    assert "下載音訊失敗：" in err and "Video unavailable" in err
    assert audio.cleaned and nothing_left(tmp_path)


@pytest.mark.parametrize("error", [
    DiarizationUnavailable("說話者區分模型載入失敗：連不上 Hugging Face"),
    SpeechRecognitionFailed("語音辨識失敗：CUDA out of memory"),
], ids=lambda e: type(e).__name__)
def test_a_listening_stage_that_fails_stops_with_its_own_reason(tmp_path, error):
    diarizer = FakeDiarizer(error=error) if isinstance(error, DiarizationUnavailable) else FakeDiarizer()
    words = FakeWordTranscriber(error=error) if isinstance(error, SpeechRecognitionFailed) else FakeWordTranscriber()
    audio = Audio()

    code, out, err, _ = check_run([URL], tmp_path, audio=audio, diarizer=diarizer, words=words)

    assert code == check.EXIT_FAILED and str(error) in err and out == ""
    assert audio.cleaned and nothing_left(tmp_path)


def test_speech_recognition_that_hears_nothing_still_shows_what_the_diarizer_found(tmp_path):
    code, out, err, _ = check_run([URL], tmp_path, words=FakeWordTranscriber(words=[]))
    assert code == check.EXIT_FAILED
    assert "說話者區分器找到 3 個聲音" in out
    assert "語音辨識沒有辨識出任何字" in out and "SpeechRecognitionFailed" in out
    assert nothing_left(tmp_path)


def _raise(error):
    def action():
        raise error
    return action


@pytest.mark.parametrize("error", [KeyboardInterrupt(), OperationCancelled()], ids=lambda e: type(e).__name__)
def test_an_interruption_exits_with_130_and_cleans_up(tmp_path, error):
    audio = Audio()
    code, _, err, _ = check_run([URL], tmp_path, audio=audio, diarizer=FakeDiarizer(on_diarize=_raise(error)))
    assert code == check.EXIT_INTERRUPTED and "已中斷" in err
    assert audio.cleaned and nothing_left(tmp_path)


def test_a_bug_in_a_model_adapter_is_not_swallowed_but_the_audio_is_still_removed(tmp_path):
    """沒料到的錯誤要留下完整的 traceback 讓人貼出來；但暫存的音訊一樣要刪掉。"""
    audio = Audio()
    with pytest.raises(RuntimeError, match="boom"):
        check_run([URL], tmp_path, audio=audio, diarizer=FakeDiarizer(on_diarize=_raise(RuntimeError("boom"))))
    assert audio.cleaned and nothing_left(tmp_path)


# --- 品質與結束碼 ---


def test_a_requested_count_the_diarizer_cannot_deliver_is_reported_as_what_production_would_refuse(tmp_path):
    merged = DiarizerOutput((SpeakerTurn(0.0, 80.0, "ONLY"),))

    code, out, _, _ = check_run([URL, "--speakers", "2"], tmp_path, diarizer=FakeDiarizer(output=merged))

    assert code == check.EXIT_NOT_RELIABLE
    assert "正式工作會在這裡失敗（AttributionUnreliable）：指定 2 人，卻只找到 1 位主要發言者" in out
    assert "--- 每位說話者 ---" in out and "S1 發言者 A" in out        # 該看的東西照樣印出來


def test_every_warning_the_pipeline_can_raise_is_explained_in_plain_chinese():
    report = DiarizationReport(
        engine="e", version="1", model="m", fingerprint="f" * 16, status="warn",
        warnings=("count_mismatch", "high_unassigned", "many_minor", "single_found", "something_new"),
        requested_speakers=3, found_speakers=2, minor_speakers=4, speech_seconds=100.0,
        unassigned_ratio=0.3, overlap_ratio=0.05)

    text = "\n".join(check._quality_lines(report, ""))

    assert "找到的主要發言者人數和指定的不同" in text
    assert "超過四分之一的說話時間無法判斷是誰說的" in text
    assert "有四個以上只出現一下的聲音" in text
    assert "自動判斷只找到 1 位主要發言者" in text
    assert "警告：something_new" in text                              # 新的代號不會被吞掉
    assert "30.0%" in text and "5.0%" in text


def test_the_device_the_diarizer_ended_up_on_is_part_of_the_header(tmp_path):
    class OnCuda(FakeDiarizer):
        device = "cuda"

    _, out, _, _ = check_run([URL], tmp_path, diarizer=OnCuda())
    _, plain, _, _ = check_run([URL], tmp_path)
    assert "說話者區分：fake-engine 1.0（fake/model，cuda）" in out
    assert "說話者區分：fake-engine 1.0（fake/model）" in plain


def test_the_overlap_the_diarizer_measured_reaches_the_quality_section(tmp_path):
    overlapped = DiarizerOutput(FIXTURE.turns, overlap_seconds=7.0)
    _, plain, _, _ = check_run([URL], tmp_path)
    _, out, _, _ = check_run([URL], tmp_path, diarizer=FakeDiarizer(output=overlapped))
    assert "同時有人說話：0.0%" in plain
    assert "同時有人說話：0.0%" not in out and "同時有人說話：" in out


def test_a_clean_result_has_no_warning_lines(tmp_path):
    _, out, _, _ = check_run([URL], tmp_path)
    assert "警告：" not in out and "正式工作會在這裡失敗" not in out


# --- --sample ---


def four_voices() -> DiarizerOutput:
    return DiarizerOutput((*FIXTURE.turns, SpeakerTurn(100.0, 105.0, "SPEAKER_09")))


def test_the_sample_run_expects_four_voices_and_says_so_when_it_finds_them(tmp_path):
    audio = Audio(clip=AudioClip(path="x", video_id="0-four-speakers-zh", title="0-four-speakers-zh", duration=55.0))

    code, out, _, _ = check_run(["--sample"], tmp_path, audio=audio, diarizer=FakeDiarizer(output=four_voices()))

    assert code == check.EXIT_OK
    assert audio.urls == [check.SAMPLE_URL]
    assert "說話者區分器找到 4 個聲音" in out
    assert "樣本預期 4 個聲音，說話者區分器找到 4 個：符合預期。" in out


def test_the_sample_run_fails_when_the_count_is_not_what_the_sample_has(tmp_path):
    code, out, _, _ = check_run(["--sample"], tmp_path)                # 預設的假對談只有 3 個聲音
    assert code == check.EXIT_NOT_RELIABLE
    assert "樣本預期 4 個聲音，說話者區分器找到 3 個：與預期不符" in out


def test_links_are_only_made_for_youtube_videos(tmp_path):
    audio = Audio(clip=AudioClip(path="x", video_id="0-four-speakers-zh", title="樣本", duration=55.0))
    _, out, _, _ = check_run(["--sample"], tmp_path, audio=audio)
    assert "youtube.com" not in out and "&t=" not in out
    assert "00:05  今天我們來談少子化" in out                           # 沒有連結時只有時間


@pytest.mark.parametrize("url,linked", [
    ("https://www.youtube.com/watch?v=abcdefghijk", True),
    ("https://youtu.be/abcdefghijk", True),
    ("https://m.youtube.com/watch?v=abcdefghijk", True),
    ("https://example.com/watch?v=abcdefghijk", False),
    ("https://notyoutube.com/watch?v=abcdefghijk", False),
])
def test_only_real_youtube_hosts_get_links(tmp_path, url, linked):
    _, out, _, _ = check_run([url], tmp_path)
    assert ("watch?v=abcdefghijk&t=" in out) is linked


# --- 輸出很長的時候 ---


def alternating(count: int):
    words, turns = [], []
    for i in range(count):
        start = i * 3.0
        words.append(Word(start, start + 2.5, f"第{i}句。", segment=i))
        turns.append(SpeakerTurn(start, start + 2.9, "A" if i % 2 == 0 else "B"))
    return words, DiarizerOutput(tuple(turns))


def test_a_long_video_lists_only_the_first_turns_but_says_how_many_there_are(tmp_path):
    words, output = alternating(check.MAX_TURN_LINES + 100)

    code, out, _, _ = check_run([URL], tmp_path, diarizer=FakeDiarizer(output=output),
                                words=FakeWordTranscriber(words=words))

    assert code == check.EXIT_OK
    section = out.split("--- 逐段", 1)[1].split("--- 每位說話者", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("  ")]
    assert len(lines) == check.MAX_TURN_LINES + 1
    assert lines[-1] == f"  …共 {check.MAX_TURN_LINES + 100} 段，只列前 {check.MAX_TURN_LINES} 段"
    assert "第0句。" in lines[0] and f"第{check.MAX_TURN_LINES - 1}句。" in lines[-2]


@pytest.mark.parametrize("count,wanted,expected", [
    (0, 10, []), (1, 10, [0]), (5, 10, [0, 1, 2, 3, 4]), (10, 10, list(range(10))),
    (11, 10, [0, 1, 2, 3, 4, 6, 7, 8, 9, 10]), (3, 2, [0, 2]),
])
def test_spot_checks_are_spread_evenly_and_include_both_ends(count, wanted, expected):
    assert check.spot_indices(count, wanted) == expected


def test_spot_checks_of_a_long_video_cover_the_whole_range():
    picks = check.spot_indices(1000)
    assert len(picks) == check.SPOT_CHECKS and picks[0] == 0 and picks[-1] == 999
    assert picks == sorted(set(picks))


# --- 進度只寫到錯誤輸出，而且不洗版 ---


def test_progress_lines_are_at_least_two_seconds_apart():
    err = io.StringIO()
    now = [0.0]
    progress = check._Progress(err, lambda: now[0])

    progress(0.1, "下載音訊…")                       # 第一行一定印
    now[0] = 1.0
    progress(0.3, "語音辨識中… 0:10 / 2:00")          # 離上一行不到兩秒：略過
    now[0] = 2.5
    progress(0.4, "語音辨識中… 0:20 / 2:00")          # 夠久了：印
    now[0] = 4.0
    progress(0.5, "語音辨識中… 0:30 / 2:00")          # 離上一行才 1.5 秒：略過

    assert err.getvalue().splitlines() == ["    下載音訊…", "    語音辨識中… 0:20 / 2:00"]


def test_the_same_progress_line_is_never_printed_twice_however_long_it_takes():
    err = io.StringIO()
    now = [0.0]
    progress = check._Progress(err, lambda: now[0])

    progress(0.1, "語音辨識中… 0:10 / 2:00")
    progress(0.2, "語音辨識中… 0:10 / 2:00")
    now[0] = 60.0
    progress(0.3, "語音辨識中… 0:10 / 2:00")

    assert err.getvalue().splitlines() == ["    語音辨識中… 0:10 / 2:00"]


def test_progress_goes_to_the_error_stream_not_the_report(tmp_path):
    _, out, err, _ = check_run([URL], tmp_path)
    assert "[下載] 下載音訊…" in err and "[說話者區分]" in err and "[語音辨識]" in err
    assert "[下載]" not in out and "下載音訊…" not in out


# --- 切音訊的指令 ---


class Runner:
    def __init__(self, returncode=0, stderr="", write: str | None = None):
        self.returncode, self.stderr, self.write = returncode, stderr, write
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.write:
            with open(self.write, "wb") as f:
                f.write(b"wav")
        return type("Result", (), {"returncode": self.returncode, "stderr": self.stderr})()


def test_the_cut_command_seeks_before_reading_and_writes_mono_16k(tmp_path):
    dest = str(tmp_path / "slice.wav")
    runner = Runner(write=dest)

    check.ffmpeg_cut("in.webm", dest, 30.0, 60.0, runner=runner, ffmpeg_exe="ffmpeg")

    command, = runner.commands
    assert command == ["ffmpeg", "-v", "error", "-y", "-ss", "30.000", "-t", "60.000", "-i", "in.webm",
                       "-vn", "-ac", "1", "-ar", "16000", dest]


def test_a_cut_without_a_length_goes_to_the_end_of_the_file(tmp_path):
    dest = str(tmp_path / "slice.wav")
    runner = Runner(write=dest)
    check.ffmpeg_cut("in.webm", dest, 5.0, None, runner=runner, ffmpeg_exe="ffmpeg")
    assert "-t" not in runner.commands[0] and runner.commands[0][runner.commands[0].index("-ss") + 1] == "5.000"


def test_a_failed_cut_reports_the_first_line_of_what_ffmpeg_said(tmp_path):
    runner = Runner(returncode=1, stderr="[matroska,webm @ 0000019] Invalid data found\nError opening input file C:\\x\n")
    with pytest.raises(check.AudioCutFailed, match="Invalid data found") as raised:
        check.ffmpeg_cut("in.webm", str(tmp_path / "slice.wav"), 0.0, 10.0, runner=runner, ffmpeg_exe="ffmpeg")
    assert "C:\\x" not in str(raised.value)


def test_a_cut_that_exited_with_an_error_is_a_failure_even_if_it_left_a_partial_file(tmp_path):
    dest = str(tmp_path / "slice.wav")
    runner = Runner(returncode=1, stderr="Conversion failed!", write=dest)
    with pytest.raises(check.AudioCutFailed, match="Conversion failed"):
        check.ffmpeg_cut("in.webm", dest, 0.0, 10.0, runner=runner, ffmpeg_exe="ffmpeg")


def test_a_cut_that_writes_nothing_is_a_failure_even_if_ffmpeg_said_ok(tmp_path):
    with pytest.raises(check.AudioCutFailed, match="沒有輸出檔"):
        check.ffmpeg_cut("in.webm", str(tmp_path / "slice.wav"), 0.0, 10.0, runner=Runner(), ffmpeg_exe="ffmpeg")


# --- 量測 ---


def test_peak_memory_is_a_positive_number_or_unknown():
    peak = check.peak_memory_bytes()
    assert peak is None or peak > 0


def test_the_gpu_peak_is_only_asked_of_torch_when_something_already_loaded_it(monkeypatch):
    class Cuda:
        calls = 0

        @staticmethod
        def is_available():
            return True

        @classmethod
        def max_memory_allocated(cls):
            cls.calls += 1
            return 123456

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert check.gpu_peak_memory_bytes() is None                      # 沒人載入過 torch：不去 import 它
    assert "torch" not in sys.modules

    monkeypatch.setitem(sys.modules, "torch", type("Torch", (), {"cuda": Cuda}))
    assert check.gpu_peak_memory_bytes() == 123456


def test_a_torch_without_a_gpu_or_a_broken_one_reports_nothing(monkeypatch):
    class NoGpu:
        @staticmethod
        def is_available():
            return False

        @staticmethod
        def max_memory_allocated():
            return 999                                   # 沒有顯示卡時就算問得到數字，也不能報

    class Broken:
        @staticmethod
        def is_available():
            raise RuntimeError("driver")

    monkeypatch.setitem(sys.modules, "torch", type("Torch", (), {"cuda": NoGpu}))
    assert check.gpu_peak_memory_bytes() is None
    monkeypatch.setitem(sys.modules, "torch", type("Torch", (), {"cuda": Broken}))
    assert check.gpu_peak_memory_bytes() is None


# --- 真的零件的組裝與進入點 ---


def test_the_default_parts_are_the_services_own_adapters_and_load_nothing():
    from slidebox.infrastructure.whisper_transcriber import FasterWhisperTranscriber
    from slidebox.infrastructure.pyannote_diarizer import PyannoteSpeakerDiarizer
    from slidebox.infrastructure.ytdlp_audio import YtDlpAudioGateway

    parts = check.default_parts()

    assert isinstance(parts.audio, YtDlpAudioGateway)
    assert isinstance(parts.diarizer, PyannoteSpeakerDiarizer)
    assert isinstance(parts.words, FasterWhisperTranscriber)
    assert parts.whisper_model


def _module(*args):
    # 主控台不是 UTF-8（Windows 的預設）時，進入點要自己改成 UTF-8，說明才印得出來
    env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "src"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, "-m", "slidebox_api.diarize_check", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def test_the_module_can_be_run_with_python_dash_m_and_explains_itself():
    result = _module("--help")
    assert result.returncode == 0
    for option in ("--sample", "--start", "--end", "--speakers"):
        assert option in result.stdout


def test_run_as_a_module_with_bad_arguments_exits_with_2_and_tells_why():
    result = _module()
    assert result.returncode == 2
    assert "--sample" in result.stderr


# --- 外面來的文字（標題、網址、逐字稿、錯誤訊息）不能對終端機下指令 ---

ESCAPES = "\x1b[2K\x1b[1A  ALL OK: 0 problems\x1b]0;pwned\x07"
# 報告裡一個控制字元都不該有；換行是報告自己的版面
FORBIDDEN = [chr(c) for c in (*range(0, 10), *range(11, 32), 127, *range(0x80, 0xA0))]


def assert_inert(text: str) -> None:
    found = [repr(c) for c in FORBIDDEN if c in text]
    assert not found, f"報告裡還有控制字元 {found}"
    assert "\N{RIGHT-TO-LEFT OVERRIDE}" not in text and "pwned" not in text


def test_a_title_with_terminal_escape_sequences_cannot_rewrite_the_report(tmp_path):
    """攔的 bug：影片標題由上傳者決定，原樣印出去。裡面的 ESC／OSC 序列可以清掉前面已經印出的
    報告行、改終端機的標題——而這個工具的用途正是「先聽過再相信」，不能讓它的輸出被別人改寫。"""
    clip = replace(CLIP, title="Interview" + ESCAPES)

    code, out, err, _ = check_run([URL], tmp_path, audio=Audio(clip=clip))

    assert code == check.EXIT_OK
    assert_inert(out + err)
    assert "影片：Interview" in out and "（abcdefghijk）" in out        # 看得出是哪支影片
    assert "[2K" not in out and "[1A" not in out                      # 序列整段拿掉，不是只拿掉 ESC 字元


def test_a_title_cannot_forge_extra_report_lines_with_line_breaks(tmp_path):
    clip = replace(CLIP, title="真的標題\n\n  狀態：FORGED\r\n=== 說話者區分檢查 ===")
    _, out, _, _ = check_run([URL], tmp_path, audio=Audio(clip=clip))
    lines = out.splitlines()
    title_line = next(line for line in lines if line.startswith("影片："))
    assert "真的標題" in title_line and "（abcdefghijk）" in title_line     # 整個標題在同一行
    assert lines.count("=== 說話者區分檢查 ===") == 1                       # 偽造的標頭只會是標題行裡的一段字
    assert not [line for line in lines if line.startswith("  狀態：FORGED")]


def test_bidirectional_overrides_in_a_title_are_removed(tmp_path):
    clip = replace(CLIP, title="abc\N{RIGHT-TO-LEFT OVERRIDE}def\N{LEFT-TO-RIGHT ISOLATE}ghi\N{POP DIRECTIONAL ISOLATE}")
    _, out, _, _ = check_run([URL], tmp_path, audio=Audio(clip=clip))
    assert "abcdefghi" in out and "\N{RIGHT-TO-LEFT OVERRIDE}" not in out and "\N{LEFT-TO-RIGHT ISOLATE}" not in out


def test_an_ordinary_title_is_printed_untouched(tmp_path):
    """對照：中文、全形標點、emoji（含零寬連接符組成的）都照原樣。"""
    title = "少子化對談｜第 1 集 — 「托育」😀 👨\N{ZERO WIDTH JOINER}👩\N{ZERO WIDTH JOINER}👧"
    _, out, _, _ = check_run([URL], tmp_path, audio=Audio(clip=replace(CLIP, title=title)))
    assert f"影片：{title}（abcdefghijk）" in out


def test_c1_control_characters_are_removed_too(tmp_path):
    """8 位元的 CSI（U+009B）與 OSC（U+009D）有些終端機也認得。"""
    clip = replace(CLIP, title="a\x9b31mb\x9d0;pwned\x9cc")
    _, out, _, _ = check_run([URL], tmp_path, audio=Audio(clip=clip))
    assert_inert(out)
    assert "影片：abc（" in out


def test_a_malformed_or_unfinished_sequence_is_removed_as_well(tmp_path):
    clip = replace(CLIP, title="x\x1b[31;1" + "\x1b" + "我y\x1b]0;never-terminated")
    _, out, _, _ = check_run([URL], tmp_path, audio=Audio(clip=clip))
    assert_inert(out)
    assert "影片：x我y（" in out


def test_transcribed_text_with_control_characters_is_cleaned_before_it_is_printed(tmp_path):
    words = list(FIXTURE.words)
    words[0] = replace(words[0], text="\x1b[31m" + words[0].text + "\x07")
    _, out, _, _ = check_run([URL], tmp_path, words=FakeWordTranscriber(words=words))
    assert_inert(out)
    assert "今天我們來談少子化" in out


def test_the_address_typed_on_the_command_line_is_cleaned_too(tmp_path):
    dirty = URL + "&x=\x1b]0;pwned\x07"
    _, out, _, _ = check_run([dirty], tmp_path)
    assert_inert(out)
    assert "來源：" + URL in out


def test_error_text_from_a_download_or_a_model_is_cleaned_before_it_reaches_the_terminal(tmp_path):
    """yt-dlp 的錯誤常常整段貼著頁面上的文字（標題、上傳者的說明）。"""
    audio = Audio(error=NoSubtitlesAvailable("影片無法使用：\x1b]0;pwned\x07Video unavailable\x1b[2J"))
    code, out, err, _ = check_run([URL], tmp_path, audio=audio)
    assert code == check.EXIT_FAILED
    assert_inert(out + err)
    assert "Video unavailable" in err

    code, out, err, _ = check_run([URL], tmp_path, diarizer=FakeDiarizer(error=DiarizationUnavailable("壞了\x1b[2J")))
    assert code == check.EXIT_FAILED and "壞了" in err
    assert_inert(out + err)


def test_progress_lines_are_cleaned_too(tmp_path):
    class Chatty(FakeDiarizer):
        def diarize(self, audio_path, duration, speaker_count, progress, is_cancelled):
            progress(0.1, "區分說話者…\x1b[2J（比對聲紋）\x07")
            return super().diarize(audio_path, duration, speaker_count, progress, is_cancelled)

    _, out, err, _ = check_run([URL], tmp_path, diarizer=Chatty())
    assert_inert(out + err)
    assert "區分說話者…" in err


@pytest.mark.parametrize("raw,expected", [
    ("plain text", "plain text"),
    ("a\x1b[31mred\x1b[0mb", "aredb"),                          # CSI（顏色）
    ("a\x1b[2Kb\x1b[1Ac", "abc"),                               # CSI（清行、游標上移）
    ("a\x1b]0;title\x07b", "ab"),                               # OSC，BEL 結尾
    ("a\x1b]0;title\x1b\x5cb", "ab"),                             # OSC，ST（ESC \）結尾
    ("a\x1bPsome dcs payload\x1b\x5cb", "ab"),                    # DCS
    ("a\x1bcb", "ab"),                                          # ESC c（重設終端機）
    ("a\x1b(Bb", "ab"),                                         # ESC ( B（選字元集）
    ("a\x9b2Jb", "ab"),                                         # 8 位元 CSI
    ("a\x9d0;x\x9cb", "ab"),                                    # 8 位元 OSC
    ("a\x00b\x07c\x08d\x7fe", "abcde"),                         # NUL、BEL、退格、DEL：不留空白
    ("a\tb\nc\r\nd", "a b c d"),                                # Tab 與換行當成空白、壓成一個
    ("a\N{LINE SEPARATOR}b\N{PARAGRAPH SEPARATOR}c", "a b c"),
    ("a\N{RIGHT-TO-LEFT OVERRIDE}b\N{ARABIC LETTER MARK}c", "abc"),
    ("  many    spaces  ", "many spaces"),
    ("少子化對談｜「托育」😀", "少子化對談｜「托育」😀"),
])
def test_plain_text_for_the_terminal(raw, expected):
    assert check.plain(raw) == expected


def test_plain_leaves_non_text_input_printable():
    assert check.plain(ValueError("壞了\x1b[2J")) == "壞了"
    assert check.plain(None) == "None"


def test_the_whole_report_is_swept_one_last_time_even_for_fields_nobody_thought_of(tmp_path):
    """第二層：欄位各自清過，但報告印出前還有最後一道。這裡的引擎名稱沒有人特別處理，
    只靠最後一道擋；拿掉它，這個測試就會失敗。"""
    class Odd(FakeDiarizer):
        engine = "fake\x1b[2Jengine\x07"

    _, out, err, _ = check_run([URL], tmp_path, diarizer=Odd())
    assert_inert(out + err)
    assert "fakeengine 1.0" in out
    lines = out.splitlines()
    assert lines[0] == "=== 說話者區分檢查 ===" and len(lines) > 10          # 換行是報告自己的版面，要留著


def test_an_unfinished_sequence_in_one_field_cannot_swallow_the_rest_of_the_report(tmp_path):
    """攔的 bug：整份報告一次清，沒有結尾的 OSC 序列一路吃到下一個 BEL——後面的報告全部消失，
    而這個工具的輸出正是要拿來核對的。最後一道要一行一行清。"""
    class Odd(FakeDiarizer):
        engine = "fake\x1b]0;never-terminated"

    _, out, _, _ = check_run([URL], tmp_path, diarizer=Odd())
    assert_inert(out)
    assert "語音辨識：whisper-a（語言 zh）" in out and "--- 品質 ---" in out and "--- 耗時與記憶體 ---" in out


def test_the_length_limit_counts_what_the_reader_sees_not_the_control_sequences():
    """攔的 bug：先截短再清，標題或逐字稿裡的顏色碼佔掉字數，看得到的字變少。"""
    shown = check._short("\x1b[31m" * 10 + "字" * 70, limit=60)
    assert shown == "字" * 60 + "…"
    assert check._short("\x1b[31m字\x1b[0m") == "字"
