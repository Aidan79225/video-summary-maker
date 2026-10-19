"""ffmpeg 路徑處理：把 imageio-ffmpeg 的執行檔複製成標準名稱。

自 musicbox 複製而來。slidebox 是獨立 app，寧可重複這幾十行也不建立
跨 package 相依。
"""
from __future__ import annotations

import os
import re
import shutil

import imageio_ffmpeg


def _project_root() -> str:
    # 此檔位於 <root>/src/slidebox/infrastructure/ffmpeg.py，往上四層即專案根目錄
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


def get_ffmpeg_exe() -> str:
    """回傳標準名稱的 ffmpeg 執行檔路徑。

    imageio-ffmpeg 的執行檔名是 ffmpeg-win-x86_64-vX.X.exe，yt-dlp 只認得
    名為 ffmpeg 的檔案，所以複製一份成標準名稱放到專案的 .bin 資料夾。
    """
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    bin_dir = os.path.join(_project_root(), ".bin")
    os.makedirs(bin_dir, exist_ok=True)
    target = os.path.join(bin_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(exe):
        shutil.copy2(exe, target)
    return target


def get_ffmpeg_dir() -> str:
    """回傳可供 yt-dlp 使用的 ffmpeg 資料夾。"""
    return os.path.dirname(get_ffmpeg_exe())


# ffmpeg 每行開頭的「[mov,mp4,m4a,3gp,3g2,mj2 @ 000001de7e76f5c0] 」：哪個元件、哪個記憶體位址
_COMPONENT_PREFIX = re.compile(r"^\[[^\]]*@ [0-9a-fA-Fx]+\]\s*")


def ffmpeg_error(stderr: str | None) -> str:
    """ffmpeg 的錯誤給畫面用：第一行有內容的那一行，去掉元件與記憶體位址的前綴。

    -loglevel error 下 ffmpeg 通常印兩三行：第一行才是原因（moov atom not found、連不上），
    後面的「Error opening input file <路徑>」只是重述，還會露出暫存片段的路徑。
    """
    for line in (stderr or "").splitlines():
        line = _COMPONENT_PREFIX.sub("", line.strip())
        if line:
            return line[:200]
    return ""
