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


def test_a_clip_without_frames_leaves_no_empty_image_behind(tmp_path):
    """攔的 bug：片段有影像串流卻沒有任何畫格時，ffmpeg 照樣開出一個 0 bytes 的 .webp，
    兩次都失敗後留在成品資料夾裡，看起來像「截圖壞掉」而不是「這頁沒有截圖」。"""
    frag = str(tmp_path / "frag.mp4")
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=2",
         "-pix_fmt", "yuv420p", "-movflags", "frag_keyframe+empty_moov", frag],
        check=True,
    )
    with open(frag, "rb") as f:
        data = f.read()
    init_only = str(tmp_path / "init.mp4")  # 只有 ftyp＋moov：有串流、沒有畫格
    with open(init_only, "wb") as f:
        f.write(data[:data.find(b"moof") - 4])
    dest = tmp_path / "01.webp"
    with pytest.raises(RuntimeError) as exc:
        FfmpegFrameExtractor().extract(init_only, str(dest), 160)
    assert not dest.exists()
    # 這個原因會出現在完成訊息裡；ffmpeg 有時什麼都不說，不能只剩「抽幀失敗：」
    assert not str(exc.value).rstrip().endswith("：")


def test_an_ffmpeg_that_says_nothing_still_gets_a_reason(tmp_path, monkeypatch):
    """ffmpeg 回傳 0、什麼都沒說、也沒寫出圖（片段有串流卻沒有畫格）：原因要講得出來。"""
    from slidebox.infrastructure import ffmpeg_frames

    class Silent:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(ffmpeg_frames.subprocess, "run", lambda *a, **kw: Silent())
    with pytest.raises(RuntimeError) as exc:
        FfmpegFrameExtractor().extract(str(tmp_path / "clip000.mp4"), str(tmp_path / "01.webp"), 160)
    assert str(exc.value) == "抽幀失敗：片段裡沒有可用的畫面"


def test_a_failing_ffmpeg_without_output_reports_its_exit_code(tmp_path, monkeypatch):
    from slidebox.infrastructure import ffmpeg_frames

    class Crashed:
        returncode = 3
        stderr = ""

    monkeypatch.setattr(ffmpeg_frames.subprocess, "run", lambda *a, **kw: Crashed())
    with pytest.raises(RuntimeError) as exc:
        FfmpegFrameExtractor().extract(str(tmp_path / "clip000.mp4"), str(tmp_path / "01.webp"), 160)
    assert str(exc.value) == "抽幀失敗：ffmpeg 結束碼 3"


def test_ffmpeg_errors_become_one_readable_line():
    """攔的 bug：-loglevel error 下 ffmpeg 印兩三行，原樣放進完成訊息會斷成好幾行、帶記憶體位址，
    第三行還會露出暫存片段的路徑。第一行才是原因。"""
    from slidebox.infrastructure.ffmpeg import ffmpeg_error

    stderr = ("[mov,mp4,m4a,3gp,3g2,mj2 @ 000001de7e76f5c0] moov atom not found\n"
              "[in#0 @ 000001de7e775a00] Error opening input: Invalid data found when processing input\n"
              "Error opening input file C:\\Users\\x\\_clips\\clip000.mp4.\n")
    assert ffmpeg_error(stderr) == "moov atom not found"
    assert ffmpeg_error("\n  \n") == ""
    assert ffmpeg_error(None) == ""
    assert ffmpeg_error("[tcp @ 0000023] Connection to tcp://127.0.0.1:9 failed") == (
        "Connection to tcp://127.0.0.1:9 failed")
