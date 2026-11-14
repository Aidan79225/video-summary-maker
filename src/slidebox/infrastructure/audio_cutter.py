"""AudioCutter 的 ffmpeg 實作：把下載好的音訊切成影片的一段區間（add_video --start/--end）。

輸出是單聲道、16 kHz、16 位元的 WAV：說話者區分（pyannote）與語音辨識（Whisper）都吃這個，
而且 WAV 的標頭直接寫著長度，不必再問 ffmpeg 一次。`-ss` 放在 `-i` 前面（輸入端定位），
對音訊是取樣等級的準確，也不會把區間之前的音訊整段解碼一遍。

ffmpeg_cut 是不帶任何狀態的函式，診斷工具 slidebox_api.diarize_check 也用它：兩邊切出來的
一定是同一個東西。
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import wave

from ..domain.entities import MIN_RANGE_SECONDS, AudioClip
from ..domain.errors import AudioCutFailed
from .ffmpeg import ffmpeg_error, get_ffmpeg_exe

# 切出來的檔名。放在傳進來的暫存資料夾裡，和下載的音訊一起被 AudioGateway.cleanup 清掉
CUT_FILE_NAME = "range.wav"


def ffmpeg_cut(src: str, dest: str, start: float, length: float | None, runner=subprocess.run,
               ffmpeg_exe: str | None = None) -> None:
    """把 src 從 start 秒起的 length 秒（None 是切到結尾）切成單聲道 16 kHz 的 wav。"""
    span = [] if length is None else ["-t", f"{length:.3f}"]
    result = runner(
        [ffmpeg_exe or get_ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{start:.3f}", *span,
         "-i", src, "-vn", "-ac", "1", "-ar", "16000", dest],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not os.path.exists(dest):
        raise AudioCutFailed(f"切出指定區間的音訊失敗：{ffmpeg_error(result.stderr) or '沒有輸出檔'}")


def wav_duration(path: str) -> float:
    """WAV 檔的長度（秒），從標頭讀。不是有效的 WAV 就是切割失敗。"""
    try:
        with contextlib.closing(wave.open(path, "rb")) as f:
            rate = f.getframerate()
            if rate <= 0:
                raise wave.Error("取樣率不合理")
            return f.getnframes() / rate
    except (wave.Error, EOFError, OSError) as e:
        raise AudioCutFailed("切出來的音訊檔讀不出長度（不是有效的 WAV）") from e


class FfmpegAudioCutter:
    def __init__(self, runner=subprocess.run, ffmpeg_exe: str | None = None):
        self._runner = runner
        self._ffmpeg_exe = ffmpeg_exe

    def cut(self, clip: AudioClip, start: float, end: float | None, dest_dir: str) -> AudioClip:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, CUT_FILE_NAME)
        length = None if end is None else end - start
        ffmpeg_cut(clip.path, dest, start, length, self._runner, self._ffmpeg_exe)
        duration = wav_duration(dest)
        if duration < MIN_RANGE_SECONDS:
            # 起點在影片結尾之後時，ffmpeg 常常照樣「成功」，寫出一個幾乎空的檔：
            # 影片長度不明（yt-dlp 沒回報）的時候，這是唯一能發現的地方
            raise AudioCutFailed(
                f"切出來的音訊只有 {duration:.1f} 秒，不到 {MIN_RANGE_SECONDS:g} 秒；"
                "起點可能已經超過影片結尾，請確認 --start 與 --end")
        return AudioClip(path=dest, video_id=clip.video_id, title=clip.title, duration=duration, offset=start)
