"""FrameExtractor 的 ffmpeg 實作：從片段抽一格，縮放後存成 WebP。"""
from __future__ import annotations

import os
import subprocess

from .ffmpeg import get_ffmpeg_exe


class FfmpegFrameExtractor:
    """預設抽片段第 2 秒那一格。

    片段長 4 秒，取中間可避開切點附近可能的轉場或不完整影格。片段比
    offset 短時 ffmpeg 會抽不到任何影格，此時退回抽第一格。
    """

    def __init__(self, offset_seconds: float = 2.0):
        self._offset = offset_seconds
        self._exe = get_ffmpeg_exe()

    def extract(self, section_path: str, dest_path: str, width: int) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        last_error = ""
        for offset in (self._offset, 0.0):
            ok, last_error = self._run(section_path, dest_path, width, offset)
            if ok:
                return
        raise RuntimeError(f"抽幀失敗（{os.path.basename(section_path)}）：{last_error}")

    def _run(self, src: str, dest: str, width: int, offset: float) -> tuple[bool, str]:
        result = subprocess.run(
            [
                self._exe, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{offset}",          # 放在 -i 前面是快速 seek
                "-i", src,
                "-frames:v", "1",
                "-vf", f"scale={width}:-2",  # -2 讓高度取偶數並維持比例
                "-c:v", "libwebp", "-quality", "80",
                dest,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True, ""
        return False, (result.stderr or "").strip()[:200]
