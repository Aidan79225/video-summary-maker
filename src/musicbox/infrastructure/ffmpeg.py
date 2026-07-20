"""ffmpeg 路徑處理：把 imageio-ffmpeg 的執行檔複製成 yt-dlp 認得的名稱。"""
from __future__ import annotations

import os
import shutil

import imageio_ffmpeg


def _project_root() -> str:
    # 此檔位於 <root>/src/musicbox/infrastructure/ffmpeg.py，往上四層即專案根目錄
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


def get_ffmpeg_dir() -> str:
    """回傳可供 yt-dlp 使用的 ffmpeg 資料夾。

    imageio-ffmpeg 的執行檔名稱是 ffmpeg-win-x86_64-vX.X.exe，yt-dlp 只認得
    名為 ffmpeg 的檔案，所以複製一份成標準名稱放到專案的 .bin 資料夾。
    """
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    bin_dir = os.path.join(_project_root(), ".bin")
    os.makedirs(bin_dir, exist_ok=True)
    target = os.path.join(bin_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(exe):
        shutil.copy2(exe, target)
    return bin_dir
