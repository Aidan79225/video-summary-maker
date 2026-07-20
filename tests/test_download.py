"""下載 use case 的單元測試。"""
from __future__ import annotations

import pytest

from musicbox.domain.entities import DownloadFormat, DownloadRequest
from musicbox.domain.errors import OperationCancelled
from musicbox.usecases.download_video import DownloadVideoUseCase

from .fakes import FakeDownloader


def _request(fmt=DownloadFormat.MP4):
    return DownloadRequest(url="http://x", fmt=fmt, output_dir="/out", max_height=720)


def test_delegates_to_downloader_and_returns_path():
    fake = FakeDownloader()
    uc = DownloadVideoUseCase(fake)
    path = uc.execute(_request())
    assert path == "/out/video.mp4"
    assert len(fake.calls) == 1
    assert fake.calls[0].max_height == 720


def test_forwards_progress_callback():
    fake = FakeDownloader()
    seen: list[tuple] = []
    DownloadVideoUseCase(fake).execute(_request(), progress=lambda f, s: seen.append((f, s)))
    assert seen[0] == (0.0, "開始")
    assert seen[-1] == (1.0, "完成")


def test_cancellation_propagates():
    fake = FakeDownloader(respect_cancel=True)
    uc = DownloadVideoUseCase(fake)
    with pytest.raises(OperationCancelled):
        uc.execute(_request(), is_cancelled=lambda: True)


def test_no_cancel_when_flag_false():
    fake = FakeDownloader(respect_cancel=True)
    path = DownloadVideoUseCase(fake).execute(_request(), is_cancelled=lambda: False)
    assert path == "/out/video.mp4"
