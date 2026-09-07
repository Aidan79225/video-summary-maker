"""抽幀的真實整合測試：用 ffmpeg 合成測試影片，抽出 WebP。"""
from __future__ import annotations

import os
import subprocess

import pytest

from slidebox.infrastructure.ffmpeg_frames import FfmpegFrameExtractor

try:
    from slidebox.infrastructure.ffmpeg import get_ffmpeg_exe
    FFMPEG = get_ffmpeg_exe()
except Exception:  # noqa: BLE001
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不可用")


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    """用 lavfi 合成一段 4 秒、640x360 的測試圖樣影片。"""
    path = str(tmp_path_factory.mktemp("clip") / "clip.mp4")
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=4",
         "-pix_fmt", "yuv420p", path],
        check=True,
    )
    return path


def test_writes_a_webp_file(tmp_path, clip):
    dest = str(tmp_path / "out.webp")
    FfmpegFrameExtractor().extract(clip, dest, 320)
    assert os.path.getsize(dest) > 100


def test_output_starts_with_the_webp_signature(tmp_path, clip):
    dest = str(tmp_path / "out.webp")
    FfmpegFrameExtractor().extract(clip, dest, 320)
    with open(dest, "rb") as f:
        header = f.read(12)
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WEBP"


def test_creates_parent_directory(tmp_path, clip):
    """use case 只做路徑運算，建立目錄是 adapter 的責任。"""
    dest = str(tmp_path / "deep" / "nested" / "out.webp")
    FfmpegFrameExtractor().extract(clip, dest, 320)
    assert os.path.exists(dest)


def test_raises_on_a_missing_input(tmp_path):
    with pytest.raises(RuntimeError):
        FfmpegFrameExtractor().extract(
            str(tmp_path / "nope.mp4"), str(tmp_path / "out.webp"), 320
        )


def test_falls_back_to_first_frame_for_a_clip_shorter_than_the_offset(tmp_path):
    """片段比預設 offset 短時仍要抽得出圖，不能空手而回。"""
    short = str(tmp_path / "short.mp4")
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=1",
         "-pix_fmt", "yuv420p", short],
        check=True,
    )
    dest = str(tmp_path / "out.webp")
    FfmpegFrameExtractor().extract(short, dest, 160)
    assert os.path.getsize(dest) > 100
