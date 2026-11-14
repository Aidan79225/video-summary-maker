"""音訊切割器（只處理影片的一段區間）：ffmpeg 的實作。

大部分用假的 runner（不跑 ffmpeg，只寫一個指定秒數的 WAV）；最後一個測試用真的 ffmpeg 切一段
合成的音訊，確認切出來的就是要的那一段。
"""
from __future__ import annotations

import os
import struct
import subprocess
import wave
from types import SimpleNamespace

import pytest

from slidebox.domain.entities import MIN_RANGE_SECONDS, AudioClip
from slidebox.domain.errors import AudioCutFailed
from slidebox.infrastructure.audio_cutter import FfmpegAudioCutter, ffmpeg_cut, wav_duration

RATE = 16000
CLIP = AudioClip(path="in/audio.webm", video_id="abcdefghijk", title="辯論", duration=1800.0)


def write_wav(path: str, seconds: float, value_of_second=lambda k: 0) -> None:
    """16 kHz 單聲道 16 位元的 WAV；第 k 秒的每個取樣都是 value_of_second(k)（拿來認內容是不是對的那一段）。"""
    frames = bytearray()
    for k in range(int(seconds)):
        frames += struct.pack("<h", value_of_second(k)) * RATE
    frames += struct.pack("<h", value_of_second(int(seconds))) * int((seconds % 1) * RATE)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(bytes(frames))


class FakeFfmpeg:
    """比照 subprocess.run：把 -t 指定的秒數（沒指定就是 remaining 秒）寫成 dest 的 WAV。"""

    def __init__(self, returncode: int = 0, stderr: str = "", write: bool = True, remaining: float = 600.0,
                 written: float | None = None):
        self.returncode, self.stderr, self.write, self.remaining, self.written = (
            returncode, stderr, write, remaining, written)
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if self.write and self.returncode == 0:
            seconds = self.written
            if seconds is None:
                seconds = float(command[command.index("-t") + 1]) if "-t" in command else self.remaining
            write_wav(command[-1], seconds)
        return SimpleNamespace(returncode=self.returncode, stderr=self.stderr)


def cutter(runner) -> FfmpegAudioCutter:
    return FfmpegAudioCutter(runner=runner, ffmpeg_exe="ffmpeg")


# --- 切出來的檔 ---


def test_the_cut_clip_is_a_wav_in_the_given_folder_that_knows_where_it_started(tmp_path):
    ffmpeg = FakeFfmpeg()
    clip = cutter(ffmpeg).cut(CLIP, 600.0, 1500.0, str(tmp_path / "audio"))
    assert clip.path == os.path.join(str(tmp_path / "audio"), "range.wav") and os.path.exists(clip.path)
    assert (clip.video_id, clip.title) == (CLIP.video_id, CLIP.title)
    assert clip.offset == 600.0 and clip.duration == 900.0       # 長度是量出來的，不是照參數說的


def test_the_command_cuts_the_range_to_mono_16k_wav(tmp_path):
    ffmpeg = FakeFfmpeg()
    cutter(ffmpeg).cut(CLIP, 600.0, 1500.0, str(tmp_path))
    command = ffmpeg.commands[0]
    assert command[:2] == ["ffmpeg", "-v"]
    assert command[command.index("-ss") + 1] == "600.000" and command[command.index("-t") + 1] == "900.000"
    assert command[command.index("-i") + 1] == "in/audio.webm"
    assert command[command.index("-ac") + 1] == "1" and command[command.index("-ar") + 1] == "16000"
    assert "-vn" in command and command[-1].endswith("range.wav")


def test_an_open_end_cuts_to_the_end_of_the_audio(tmp_path):
    ffmpeg = FakeFfmpeg(remaining=1200.0)
    clip = cutter(ffmpeg).cut(CLIP, 600.0, None, str(tmp_path))
    assert "-t" not in ffmpeg.commands[0]
    assert clip.duration == 1200.0 and clip.offset == 600.0


def test_the_folder_is_created_when_it_is_missing(tmp_path):
    target = tmp_path / "a" / "b"
    cutter(FakeFfmpeg()).cut(CLIP, 0.0, 60.0, str(target))
    assert (target / "range.wav").exists()


# --- 失敗 ---


def test_an_ffmpeg_failure_names_the_reason_without_the_command_or_paths(tmp_path):
    ffmpeg = FakeFfmpeg(returncode=1, stderr="[matroska,webm @ 000001de7e76f5c0] Invalid data found when processing input\n"
                                              "Error opening input file in/audio.webm.\n")
    with pytest.raises(AudioCutFailed, match="切出指定區間的音訊失敗：Invalid data found") as raised:
        cutter(ffmpeg).cut(CLIP, 600.0, 1500.0, str(tmp_path))
    assert "in/audio.webm" not in str(raised.value) and "000001de" not in str(raised.value)


def test_a_missing_output_file_is_a_failure_even_when_ffmpeg_says_it_succeeded(tmp_path):
    with pytest.raises(AudioCutFailed, match="沒有輸出檔"):
        cutter(FakeFfmpeg(write=False)).cut(CLIP, 600.0, 1500.0, str(tmp_path))


def test_audio_that_is_too_short_to_use_is_a_failure_that_points_at_the_start(tmp_path):
    """起點在影片結尾之後時，ffmpeg 常常照樣「成功」，寫出一個幾乎空的檔。長度不明（yt-dlp 沒回報）時，
    use case 沒辦法事先檢查起點，這裡是最後一道。"""
    with pytest.raises(AudioCutFailed, match=rf"只有 1\.0 秒.*{MIN_RANGE_SECONDS:g} 秒.*--start"):
        cutter(FakeFfmpeg(written=1.0)).cut(CLIP, 5000.0, None, str(tmp_path))


def test_audio_that_is_exactly_the_minimum_is_accepted(tmp_path):
    clip = cutter(FakeFfmpeg(written=MIN_RANGE_SECONDS)).cut(CLIP, 0.0, 600.0, str(tmp_path))
    assert clip.duration == MIN_RANGE_SECONDS


def test_an_output_file_that_is_not_a_wav_is_a_failure_not_a_crash(tmp_path):
    def garbage(command, **kwargs):
        with open(command[-1], "wb") as f:
            f.write(b"this is not a wav file at all")
        return SimpleNamespace(returncode=0, stderr="")

    with pytest.raises(AudioCutFailed, match="讀不出"):
        cutter(garbage).cut(CLIP, 600.0, 1500.0, str(tmp_path))


def test_the_duration_helper_reads_the_length_from_the_header(tmp_path):
    path = str(tmp_path / "a.wav")
    write_wav(path, 12.5)
    assert wav_duration(path) == pytest.approx(12.5)


# --- 共用的 ffmpeg_cut（diarize_check 也用它）：沒有切割器的呼叫端照舊 ---


def test_ffmpeg_cut_is_still_the_plain_function_the_check_tool_uses(tmp_path):
    ffmpeg = FakeFfmpeg()
    dest = str(tmp_path / "slice.wav")
    ffmpeg_cut("in.webm", dest, 30.0, 60.0, runner=ffmpeg, ffmpeg_exe="ffmpeg")
    assert os.path.exists(dest) and ffmpeg.commands[0][-1] == dest


# --- 真的 ffmpeg ---


def _real_ffmpeg() -> str | None:
    try:
        from slidebox.infrastructure.ffmpeg import get_ffmpeg_exe
        exe = get_ffmpeg_exe()
        subprocess.run([exe, "-version"], capture_output=True, check=True)
        return exe
    except Exception:  # noqa: BLE001 沒有 ffmpeg 的環境跳過這一個測試，不是失敗
        return None


@pytest.mark.skipif(_real_ffmpeg() is None, reason="這個環境沒有可用的 ffmpeg")
def test_the_real_ffmpeg_cuts_exactly_the_requested_part(tmp_path):
    """攔的 bug：-ss 放錯位置（在 -i 後面會逐格解碼，慢而且在某些格式上不準）、單位弄錯、或切出的是別段。
    用「第 k 秒的取樣值是 1000 × k」的合成音訊，切 [4, 10) 秒：第一個取樣要是 4000、最後一個要是 9000。"""
    source = str(tmp_path / "source.wav")
    write_wav(source, 16.0, value_of_second=lambda k: 1000 * k)
    clip = AudioClip(path=source, video_id="v", title="t", duration=16.0)

    cut = FfmpegAudioCutter().cut(clip, 4.0, 10.0, str(tmp_path / "out"))

    assert cut.offset == 4.0 and cut.duration == pytest.approx(6.0, abs=0.05)
    with wave.open(cut.path, "rb") as f:
        assert (f.getnchannels(), f.getframerate(), f.getsampwidth()) == (1, RATE, 2)
        frames = f.readframes(f.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    assert samples[100] == pytest.approx(4000, abs=1) and samples[-100] == pytest.approx(9000, abs=1)

    tail = FfmpegAudioCutter().cut(clip, 10.0, None, str(tmp_path / "out2"))
    assert tail.duration == pytest.approx(6.0, abs=0.05)
