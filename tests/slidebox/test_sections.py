"""片段下載 adapter 的契約性質：不碰網路，以 monkeypatch 取代 yt-dlp。"""
from __future__ import annotations

import pytest

from slidebox.domain.errors import OperationCancelled
from slidebox.infrastructure import ytdlp_sections
from slidebox.infrastructure.ytdlp_sections import YtDlpSectionGateway


def _noop_progress(frac, status):
    pass


def test_cleanup_tolerates_a_missing_directory(tmp_path):
    """BuildDeckUseCase 從 finally 呼叫 cleanup；取消於摘要階段時該目錄根本不存在。

    若這裡拋例外，會從 finally 取代掉正在傳播的 OperationCancelled，
    把使用者的「取消」變成檔案系統錯誤。
    """
    YtDlpSectionGateway(ffmpeg_dir=str(tmp_path)).cleanup(str(tmp_path / "never" / "created"))


def test_returns_one_entry_per_timestamp_even_when_every_download_fails(tmp_path, monkeypatch):
    """回傳長度必須恆等於 timestamps 長度——BuildDeckUseCase 以位置配對
    clips[i] 與 slides[i]，短一格就會把圖配到錯的頁。"""
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    monkeypatch.setattr(gw, "_one", lambda *a, **kw: None)
    out = gw.download_sections(
        "URL", [10.0, 20.0, 30.0], 1080, str(tmp_path), _noop_progress, lambda: False
    )
    assert out == [None, None, None]


def test_returns_one_entry_per_timestamp_on_mixed_success(tmp_path, monkeypatch):
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    seen: list[int] = []

    def fake_one(url, start, max_height, dest_dir, index):
        seen.append(index)
        return None if index == 1 else f"clip{index}.mp4"

    monkeypatch.setattr(gw, "_one", fake_one)
    out = gw.download_sections(
        "URL", [10.0, 20.0, 30.0], 1080, str(tmp_path), _noop_progress, lambda: False
    )
    assert len(out) == 3
    assert out[1] is None
    assert out[0] is not None and out[2] is not None
    assert seen == [0, 1, 2]


def test_cancellation_propagates_and_is_not_swallowed(tmp_path, monkeypatch):
    """每個時間點的 except Exception 不得吞掉 OperationCancelled。"""
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    monkeypatch.setattr(gw, "_one", lambda *a, **kw: "clip.mp4")
    with pytest.raises(OperationCancelled):
        gw.download_sections(
            "URL", [10.0, 20.0], 1080, str(tmp_path), _noop_progress, lambda: True
        )


def test_a_failing_download_does_not_abort_the_batch(tmp_path, monkeypatch):
    """單點失敗只讓該頁沒圖，其餘照做——這是整條 pipeline 的降級策略。"""
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))

    def exploding_download(self, *a, **kw):
        raise RuntimeError("yt-dlp 爆了")

    monkeypatch.setattr(ytdlp_sections.yt_dlp.YoutubeDL, "download", exploding_download)
    out = gw.download_sections(
        "URL", [10.0, 20.0], 1080, str(tmp_path), _noop_progress, lambda: False
    )
    assert out == [None, None]
