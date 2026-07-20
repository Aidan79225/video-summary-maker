"""MutagenTagGateway 的真實整合測試（用內建 ffmpeg 合成極短 mp3）。"""
from __future__ import annotations

import subprocess

import pytest

try:
    import imageio_ffmpeg
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # noqa: BLE001
    _FFMPEG = None

from musicbox.infrastructure.tags import MutagenTagGateway

pytestmark = pytest.mark.skipif(_FFMPEG is None, reason="需要 ffmpeg 合成測試用 mp3")


def _make_mp3(path: str) -> None:
    subprocess.run(
        [_FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "0.1", "-q:a", "9", "-y", path],
        check=True, capture_output=True,
    )


def _read_title(path: str):
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3
    audio = MP3(path, ID3=EasyID3)
    if audio.tags and "title" in audio:
        return audio["title"][0]
    return None


def test_write_title_creates_tag_when_absent(tmp_path):
    p = str(tmp_path / "song.mp3")
    _make_mp3(p)
    assert _read_title(p) is None            # 合成檔一開始沒有標題
    MutagenTagGateway().write_title(p, "01-告白氣球")
    assert _read_title(p) == "01-告白氣球"


def test_write_title_overwrites_existing(tmp_path):
    p = str(tmp_path / "song.mp3")
    _make_mp3(p)
    gw = MutagenTagGateway()
    gw.write_title(p, "舊標題")
    gw.write_title(p, "新標題")
    assert _read_title(p) == "新標題"
