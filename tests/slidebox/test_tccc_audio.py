"""臺中片段的音訊：假的 ffmpeg runner，不碰網路。"""
from __future__ import annotations

import os
import subprocess

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable, OperationCancelled
from slidebox.infrastructure.tccc_api import TcccRecord
from slidebox.infrastructure.tccc_audio import NoSubtitlesGateway, TcccAudioGateway

URL = "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833"
M3U8 = "https://cdn.example/playlist.m3u8?iMda_seq=1"
RECORD = TcccRecord(ano="14833", cno="85", speaker="楊啓邦", meeting="市政總質詢",
                    date="2026-09-24", duration_seconds=3000, player_url="https://p")


class FakeClient:
    def __init__(self, error=None):
        self.error = error

    def record(self, ref):
        if self.error:
            raise self.error
        return RECORD

    def video_url(self, ref):
        if self.error:
            raise self.error
        return M3U8


class FakeRunner:
    def __init__(self, fail=False, stderr="boom", seconds=2.0, timeout=False):
        self.fail, self.stderr, self.seconds, self.timeout = fail, stderr, seconds, timeout
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        if not self.fail:
            with open(command[-1], "wb") as f:
                f.write(b"\0" * int(16000 * 2 * self.seconds))  # 16 kHz、16-bit、單聲道

        class Result:
            returncode = 1 if self.fail else 0
            stderr = self.stderr if self.fail else ""
        return Result()


def _noop(frac, status):
    pass


def _gateway(client=None, runner=None):
    return TcccAudioGateway(client or FakeClient(), runner=runner or FakeRunner(),
                            ffmpeg_exe="ffmpeg")


def test_downloads_mono_16k_wav_from_the_stream(tmp_path):
    runner = FakeRunner(seconds=2.0)
    clip = _gateway(runner=runner).download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)
    command = runner.commands[0]
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == M3U8
    i = command.index("-vn")
    assert command[i:i + 5] == ["-vn", "-ac", "1", "-ar", "16000"]
    assert clip.path.endswith("tccc-14833.wav") and os.path.exists(clip.path)
    assert clip.video_id == "tccc-14833"
    assert clip.title == RECORD.title
    assert clip.duration == pytest.approx(2.0)


def test_ffmpeg_failure_is_reported_as_no_subtitles(tmp_path):
    gateway = _gateway(runner=FakeRunner(fail=True, stderr="403 Forbidden"))
    with pytest.raises(NoSubtitlesAvailable, match="403 Forbidden"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_ffmpeg_timeout_is_reported(tmp_path):
    gateway = _gateway(runner=FakeRunner(timeout=True))
    with pytest.raises(NoSubtitlesAvailable, match="逾時"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_a_non_taichung_url_is_rejected_without_touching_the_network(tmp_path):
    gateway = _gateway(client=FakeClient(error=AssertionError("不該打到")))
    with pytest.raises(NoSubtitlesAvailable):
        gateway.download_audio("https://youtu.be/x", str(tmp_path / "a"), _noop, lambda: False)


def test_cancel_before_download_raises(tmp_path):
    with pytest.raises(OperationCancelled):
        _gateway().download_audio(URL, str(tmp_path / "a"), _noop, lambda: True)


def test_cleanup_tolerates_a_missing_dir(tmp_path):
    _gateway().cleanup(str(tmp_path / "nope"))


def test_no_subtitles_gateway_always_declines():
    with pytest.raises(NoSubtitlesAvailable, match="語音辨識"):
        NoSubtitlesGateway("臺中市議會沒有逐字稿，改用語音辨識").fetch(URL, ("zh",))
