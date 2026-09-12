"""IVOD 片段擷取：用假的 ffmpeg runner，不碰網路也不碰 ffmpeg。"""
from __future__ import annotations

import os
import subprocess

import pytest

from slidebox.domain.errors import OperationCancelled, SubtitleDownloadFailed
from slidebox.infrastructure.ivod_sections import IvodSectionGateway

URL = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"
M3U8 = "https://cdn.example/playlist.m3u8"


class FakeClient:
    def __init__(self, url=M3U8, error=None):
        self._url = url
        self._error = error
        self.calls = 0

    def video_url(self, video_id):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._url


class FakeRunner:
    """假的 subprocess.run。fail_at 裡的索引會失敗。"""

    def __init__(self, fail_at=(), stderr="boom", make_file=True):
        self.fail_at = set(fail_at)
        self.stderr = stderr
        self.make_file = make_file
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        index = len(self.commands)
        self.commands.append(list(command))
        failed = index in self.fail_at
        if not failed and self.make_file:
            with open(command[-1], "wb") as f:
                f.write(b"x" * 1024)

        class Result:
            returncode = 1 if failed else 0
            stderr = self.stderr if failed else ""
        return Result()


def _gateway(client=None, runner=None):
    return IvodSectionGateway(client or FakeClient(), runner=runner or FakeRunner(),
                              ffmpeg_exe="ffmpeg")


def _noop(frac, status):
    pass


def test_each_timestamp_becomes_one_clip(tmp_path):
    runner = FakeRunner()
    paths = _gateway(runner=runner).download_sections(
        URL, [0.0, 30.0], None, str(tmp_path), _noop, lambda: False)
    assert len(paths) == 2
    assert all(p is not None and os.path.exists(p) for p in paths)
    assert len(runner.commands) == 2


def test_ffmpeg_seeks_before_opening_the_stream(tmp_path):
    """攔的 bug：-ss 放在 -i 後面會讓 ffmpeg 從頭解碼到那個時間點——
    8 小時的會議等於整段下載一遍。"""
    runner = FakeRunner()
    _gateway(runner=runner).download_sections(
        URL, [123.0], None, str(tmp_path), _noop, lambda: False)
    command = runner.commands[0]
    assert command[command.index("-ss") + 1] == "123.0"
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-i") + 1] == M3U8


def test_one_failed_timestamp_only_costs_that_page(tmp_path):
    runner = FakeRunner(fail_at=(0,), stderr="segment missing")
    paths = _gateway(runner=runner).download_sections(
        URL, [0.0, 30.0], None, str(tmp_path), _noop, lambda: False)
    assert paths[0] is None
    assert paths[1] is not None


def test_an_unreachable_stream_gives_up_after_two_failures(tmp_path):
    """完整會議的影片主機實測連不上，每次嘗試要等滿逾時。15 頁就是好幾
    分鐘的空等——整支影片取不到時，後面每個時間點都注定一樣的結果。"""
    runner = FakeRunner(fail_at=range(20), stderr="Error opening input: Connection timed out")
    statuses = []
    paths = IvodSectionGateway(FakeClient(), runner=runner, ffmpeg_exe="ffmpeg"
                               ).download_sections(
        URL, [0.0, 30.0, 60.0, 90.0], None, str(tmp_path),
        lambda f, s: statuses.append(s), lambda: False)
    assert paths == [None, None, None, None]
    assert len(runner.commands) == 2
    assert any("影片" in s for s in statuses)


def test_timeouts_also_count_towards_giving_up(tmp_path):
    """攔的 bug：用比對 ffmpeg 錯誤字串來判斷「整支取不到」，逾時完全不
    符合任何字串——15 頁 × 逾時＝好幾分鐘空等，期間按取消也沒有反應。"""
    class TimingOutRunner:
        def __init__(self):
            self.commands = []

        def __call__(self, command, **kwargs):
            self.commands.append(list(command))
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))

    runner = TimingOutRunner()
    paths = IvodSectionGateway(FakeClient(), runner=runner, ffmpeg_exe="ffmpeg"
                               ).download_sections(
        URL, [0.0, 30.0, 60.0, 90.0], None, str(tmp_path), _noop, lambda: False)
    assert paths == [None, None, None, None]
    assert len(runner.commands) == 2


def test_a_single_hiccup_does_not_cost_the_whole_deck(tmp_path):
    """攔的 bug：把「Connection reset by peer」這種瞬斷當成整支影片取不到，
    第一頁不巧失敗就讓後面 14 頁一次都不試，成品零截圖。"""
    runner = FakeRunner(fail_at=(0, 2), stderr="Connection reset by peer")
    paths = _gateway(runner=runner).download_sections(
        URL, [0.0, 30.0, 60.0, 90.0], None, str(tmp_path), _noop, lambda: False)
    assert [p is not None for p in paths] == [False, True, False, True]


def test_the_clip_is_copied_not_re_encoded(tmp_path):
    """攔的 bug：少了 -c copy 會整段重新編碼，每頁從 1 秒變數十秒。"""
    runner = FakeRunner()
    _gateway(runner=runner).download_sections(
        URL, [0.0], None, str(tmp_path), _noop, lambda: False)
    command = runner.commands[0]
    assert "copy" in command
    assert "-an" in command
    assert command[command.index("-t") + 1] == "4.0"


def test_a_stream_url_that_cannot_be_resolved_degrades_to_a_deck_with_no_images(tmp_path):
    """攔的 bug：在這裡 raise 會讓整份摘要失敗。逐字稿都拿到了，沒有截圖
    也還是有用的成品——既有策略就是「部分截圖失敗仍然出片」。"""
    client = FakeClient(error=SubtitleDownloadFailed("沒有提供影片網址"))
    paths = _gateway(client=client).download_sections(
        URL, [0.0, 30.0], None, str(tmp_path), _noop, lambda: False)
    assert paths == [None, None]


def test_the_stream_url_is_resolved_once_for_the_whole_deck(tmp_path):
    client = FakeClient()
    _gateway(client=client).download_sections(
        URL, [0.0, 30.0, 60.0], None, str(tmp_path), _noop, lambda: False)
    assert client.calls == 1


def test_cancelling_stops_before_the_next_clip(tmp_path):
    runner = FakeRunner()
    with pytest.raises(OperationCancelled):
        _gateway(runner=runner).download_sections(
            URL, [0.0, 30.0], None, str(tmp_path), _noop, lambda: True)
    assert runner.commands == []


def test_an_empty_output_counts_as_failure(tmp_path):
    """ffmpeg 回傳 0 但產出空檔案是真實發生過的情況（HLS 的空容器），
    抽幀階段才發現就太晚了。"""
    runner = FakeRunner(make_file=False)
    paths = _gateway(runner=runner).download_sections(
        URL, [0.0], None, str(tmp_path), _noop, lambda: False)
    assert paths == [None]


def test_cleanup_tolerates_a_directory_that_was_never_created(tmp_path):
    _gateway().cleanup(str(tmp_path / "nope"))
