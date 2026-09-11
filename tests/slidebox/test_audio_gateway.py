"""音訊下載 adapter：只替換 yt-dlp 的網路層。"""
from __future__ import annotations

import os

import pytest
import yt_dlp.utils

from slidebox.domain.entities import AudioClip
from slidebox.domain.errors import NoSubtitlesAvailable, OperationCancelled
from slidebox.infrastructure import ytdlp_audio
from slidebox.infrastructure.ytdlp_audio import YtDlpAudioGateway

INFO = {"id": "aqz-KE-bpKQ", "title": "Big Buck Bunny", "duration": 635}


def _fake_ydl(info=INFO, *, write=True, error=None, hook_events=()):
    """extract_info(download=True) 時先觸發進度 hook、再寫出檔案，並比照真實
    yt-dlp 在 requested_downloads 裡回報實際檔案路徑。"""

    class FakeYDL:
        def __init__(self, params):
            self.params = params

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            for event in hook_events:
                for hook in self.params.get("progress_hooks", []):
                    hook(event)
            if error is not None:
                raise error
            if not write:
                return {**info, "requested_downloads": []}
            folder = os.path.dirname(self.params["outtmpl"])
            path = os.path.join(folder, "audio.webm")
            with open(path, "wb") as f:
                f.write(b"fake-opus")
            return {**info, "requested_downloads": [{"filepath": path}]}

    return FakeYDL


def _quiet(frac, status):
    pass


def test_returns_the_downloaded_file_with_the_video_metadata(tmp_path, monkeypatch):
    """攔的 bug：metadata 取錯欄位。語音路徑拿不到字幕 gateway 的 metadata，
    影片 id 決定輸出資料夾、標題會寫進投影片，都只能從這裡來。"""
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl())
    clip = YtDlpAudioGateway().download_audio("URL", str(tmp_path / "a"), _quiet, lambda: False)
    assert clip == AudioClip(
        path=str(tmp_path / "a" / "audio.webm"),
        video_id="aqz-KE-bpKQ",
        title="Big Buck Bunny",
        duration=635.0,
    )
    assert os.path.exists(clip.path)


def test_cleanup_tolerates_a_missing_directory(tmp_path):
    """攔的 bug：cleanup 在 finally 裡拋例外，把正在傳播的 OperationCancelled 換掉。"""
    YtDlpAudioGateway().cleanup(str(tmp_path / "never" / "created"))


def test_a_download_error_becomes_a_domain_error(tmp_path, monkeypatch):
    """攔的 bug：yt-dlp 的英文錯誤原封不動冒到 UI。"""
    err = yt_dlp.utils.DownloadError("ERROR: HTTP Error 403: Forbidden")
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl(error=err))
    with pytest.raises(NoSubtitlesAvailable) as exc:
        YtDlpAudioGateway().download_audio("URL", str(tmp_path), _quiet, lambda: False)
    assert "403" in str(exc.value)


def test_cancellation_during_the_download(tmp_path, monkeypatch):
    """攔的 bug：音訊下載中按取消沒反應，要等整段下載完。"""
    downloading = {"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100}
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl(hook_events=[downloading]))
    with pytest.raises(OperationCancelled):
        YtDlpAudioGateway().download_audio("URL", str(tmp_path), _quiet, lambda: True)


def test_a_download_that_produces_no_file_is_an_error(tmp_path, monkeypatch):
    """攔的 bug：下載「成功」卻沒有檔案時回傳不存在的路徑，錯誤延後到語音辨識才炸。"""
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl(write=False))
    with pytest.raises(NoSubtitlesAvailable):
        YtDlpAudioGateway().download_audio("URL", str(tmp_path), _quiet, lambda: False)


def test_download_progress_is_forwarded(tmp_path, monkeypatch):
    """攔的 bug：下載音訊期間沒有任何進度。"""
    downloading = {"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100}
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl(hook_events=[downloading]))
    seen: list[tuple[float | None, str]] = []
    YtDlpAudioGateway().download_audio(
        "URL", str(tmp_path), lambda f, s: seen.append((f, s)), lambda: False
    )
    assert (0.25, ) == tuple(f for f, _ in seen if f is not None)[:1]
