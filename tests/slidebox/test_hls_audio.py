"""通用的 HLS 音訊 gateway：假的 resolver 與 ffmpeg runner，不碰網路。"""
from __future__ import annotations

import os
import subprocess

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable, OperationCancelled
from slidebox.infrastructure.hls_audio import HlsAudioGateway, NoSubtitlesGateway

URL = "https://example.gov.tw/clip/1"
M3U8 = "https://cdn.example/playlist.m3u8?a=1&b=2"


class FakeRunner:
    def __init__(self, fail=False, stderr="boom", seconds=2.0, timeout=False, oserror=False,
                 empty=False):
        self.fail, self.stderr, self.seconds = fail, stderr, seconds
        self.timeout, self.oserror, self.empty = timeout, oserror, empty
        self.commands = []
        self.kwargs = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(kwargs)
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        if self.oserror:
            raise FileNotFoundError("ffmpeg 不見了")
        if not self.fail:
            with open(command[-1], "wb") as f:
                f.write(b"" if self.empty else b"\0" * int(16000 * 2 * self.seconds))

        class Result:
            returncode = 1 if self.fail else 0
            stderr = self.stderr if self.fail else ""
        return Result()


class FakeResolver:
    def __init__(self, error=None):
        self.error = error
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return "src-42", "2026-09-16 某某－某會議", M3U8


def _noop(frac, status):
    pass


def _gateway(resolver=None, runner=None, timeout=900):
    return HlsAudioGateway(resolver or FakeResolver(), runner=runner or FakeRunner(),
                           ffmpeg_exe="ffmpeg", timeout=timeout)


def test_downloads_mono_16k_wav_named_after_the_video_id(tmp_path):
    runner = FakeRunner(seconds=3.0)
    clip = _gateway(runner=runner).download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)
    command = runner.commands[0]
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == M3U8
    i = command.index("-vn")
    assert command[i:i + 8] == ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                                str(tmp_path / "a" / "src-42.wav")]
    assert runner.kwargs[0]["timeout"] == 900
    assert clip.path == str(tmp_path / "a" / "src-42.wav") and os.path.exists(clip.path)
    assert clip.video_id == "src-42"
    assert clip.title == "2026-09-16 某某－某會議"
    assert clip.duration == pytest.approx(3.0)       # 由 wav 檔案大小算


def test_progress_names_the_clip(tmp_path):
    seen = []
    _gateway().download_audio(URL, str(tmp_path / "a"), lambda f, s: seen.append((f, s)),
                              lambda: False)
    assert seen == [(None, "下載音訊…（2026-09-16 某某－某會議）")]


def test_ffmpeg_failure_is_reported_with_its_stderr(tmp_path):
    gateway = _gateway(runner=FakeRunner(fail=True, stderr="  403 Forbidden\n"))
    with pytest.raises(NoSubtitlesAvailable, match="音訊下載失敗：403 Forbidden"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_an_empty_output_counts_as_a_failure(tmp_path):
    """ffmpeg 回 0 卻產出空檔是 HLS 會發生的事；留給 Whisper 才發現就太晚了。"""
    gateway = _gateway(runner=FakeRunner(empty=True))
    with pytest.raises(NoSubtitlesAvailable, match="ffmpeg 失敗"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_ffmpeg_timeout_is_reported(tmp_path):
    gateway = _gateway(runner=FakeRunner(timeout=True), timeout=1800)
    with pytest.raises(NoSubtitlesAvailable, match="逾時（超過 1800 秒）"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_a_missing_ffmpeg_is_reported(tmp_path):
    gateway = _gateway(runner=FakeRunner(oserror=True))
    with pytest.raises(NoSubtitlesAvailable, match="無法執行 ffmpeg"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)


def test_a_resolver_failure_passes_through_without_running_ffmpeg(tmp_path):
    runner = FakeRunner()
    gateway = _gateway(resolver=FakeResolver(error=NoSubtitlesAvailable("找不到串流")),
                       runner=runner)
    with pytest.raises(NoSubtitlesAvailable, match="找不到串流"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)
    assert runner.commands == []


def test_cancel_is_checked_before_the_source_is_asked(tmp_path):
    """找串流要打兩次市議會；已經取消了就一次都不該打。"""
    resolver = FakeResolver()
    with pytest.raises(OperationCancelled):
        _gateway(resolver=resolver).download_audio(URL, str(tmp_path / "a"), _noop,
                                                   lambda: True)
    assert resolver.urls == []


def test_cleanup_removes_the_dir_and_tolerates_a_missing_one(tmp_path):
    target = tmp_path / "a"
    target.mkdir()
    (target / "x.wav").write_bytes(b"1")
    gateway = _gateway()
    gateway.cleanup(str(target))
    assert not target.exists()
    gateway.cleanup(str(tmp_path / "nope"))


def test_no_subtitles_gateway_always_declines_with_its_reason():
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會沒有逐字稿"):
        NoSubtitlesGateway("新北市議會沒有逐字稿，改用語音辨識").fetch(URL, ("zh",))


def test_the_taichung_module_still_exports_the_no_subtitles_gateway():
    """搬到 hls_audio 之後，舊的 import 路徑不能壞。"""
    from slidebox.infrastructure import tccc_audio

    assert tccc_audio.NoSubtitlesGateway is NoSubtitlesGateway
    assert issubclass(tccc_audio.TcccAudioGateway, HlsAudioGateway)
