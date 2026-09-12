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
            path = self.params["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "webm")
            # 比照真實 yt-dlp：目標檔已存在就不覆寫、直接回報那個舊檔（「already
            # downloaded」）。少了這個語意，任何測試都攔不到舊音訊被重複使用。
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(info["id"].encode())
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
        path=str(tmp_path / "a" / "aqz-KE-bpKQ.webm"),
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



def test_a_leftover_file_from_another_video_is_never_reused(tmp_path, monkeypatch):
    """攔的 bug：固定檔名 audio.webm。上一次執行在 finally 之前中斷（例如轉錄時
    關掉視窗）留下舊檔，yt-dlp 預設不覆寫、直接回報舊檔——下一支影片就拿上一支
    影片的聲音去轉錄，做出「新影片的標題與截圖、舊影片的內容」。"""
    dest = tmp_path / "a"
    dest.mkdir()
    (dest / "audio.webm").write_bytes(b"OLD-VIDEO")
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl())
    clip = YtDlpAudioGateway().download_audio("URL", str(dest), _quiet, lambda: False)
    with open(clip.path, "rb") as f:
        assert f.read() == b"aqz-KE-bpKQ"


def _capture_format(monkeypatch, tmp_path):
    captured: list[dict] = []

    class Capturing:
        def __init__(self, params):
            captured.append(params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("stop")

    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", Capturing)
    with pytest.raises(NoSubtitlesAvailable):
        YtDlpAudioGateway().download_audio("URL", str(tmp_path), _quiet, lambda: False)
    return captured[0]["format"]


def test_the_audio_format_excludes_hls_streams(tmp_path, monkeypatch):
    """攔的 bug：直播中的影片沒有字幕，會走到語音路徑；不限定協定時選到 HLS
    音訊，yt-dlp 會一直錄到直播結束，使用者只看到忙碌動畫、永遠等不到。
    限定 http 後，只有 HLS 可用時會直接報「格式不可用」而乾淨地失敗。"""
    fmt = _capture_format(monkeypatch, tmp_path)
    assert all("protocol^=http" in alternative for alternative in fmt.split("/"))


def test_any_yt_dlp_failure_becomes_a_domain_error(tmp_path, monkeypatch):
    """攔的 bug：只接 DownloadError。磁碟滿時 yt-dlp 拋的是 UnavailableVideoError，
    以英文原文衝到 UI。"""
    err = yt_dlp.utils.UnavailableVideoError("磁碟空間不足")
    monkeypatch.setattr(ytdlp_audio.yt_dlp, "YoutubeDL", _fake_ydl(error=err))
    with pytest.raises(NoSubtitlesAvailable):
        YtDlpAudioGateway().download_audio("URL", str(tmp_path), _quiet, lambda: False)
