"""臺中市議會片段的音訊與字幕 gateway。

臺中沒有逐字稿可抓（議事錄沒有時間戳），所以字幕 gateway 一律說「沒有」，
讓 use case 走語音辨識；音訊則用 ffmpeg 直接從 HLS 抓成 16 kHz 單聲道 wav——
faster-whisper 內部就是重採樣到這個格式，先做好可以少一次解碼。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence

from ..domain.entities import AudioClip, Transcript
from ..domain.errors import NoSubtitlesAvailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.sources import tccc_clip
from .ffmpeg import get_ffmpeg_exe
from .tccc_api import TcccClient

# 一小時的片段實測音訊串流幾分鐘就抓完；15 分鐘是「網路壞了」的上限，
# 不是正常情況會碰到的值。
_TIMEOUT_SECONDS = 900
_SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2  # pcm_s16le 單聲道


class NoSubtitlesGateway:
    """字幕 gateway 的「一律沒有」實作，讓 BuildDeckUseCase 走語音辨識備援。"""

    def __init__(self, reason: str):
        self._reason = reason

    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        raise NoSubtitlesAvailable(self._reason)


class TcccAudioGateway:
    def __init__(self, client: TcccClient, runner=subprocess.run,
                 ffmpeg_exe: str | None = None, timeout: float = _TIMEOUT_SECONDS):
        self._client = client
        self._runner = runner
        self._exe = ffmpeg_exe or get_ffmpeg_exe()
        self._timeout = timeout

    def download_audio(self, url: str, dest_dir: str, progress: ProgressCallback,
                       is_cancelled: CancelCheck) -> AudioClip:
        ref = tccc_clip(url)
        if ref is None:
            raise NoSubtitlesAvailable(f"這不是臺中市議會的片段網址（{url[:60]}）")
        if is_cancelled():
            raise OperationCancelled()
        record = self._client.record(ref)
        stream = self._client.video_url(ref)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"tccc-{ref.ano}.wav")
        # ffmpeg 抓串流時沒有好用的百分比，只回報文字；長片段這一步要幾分鐘。
        progress(None, f"下載音訊…（{record.title[:40]}）")
        command = [
            self._exe, "-hide_banner", "-loglevel", "error", "-y",
            "-i", stream,
            "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE), "-c:a", "pcm_s16le",
            dest,
        ]
        try:
            result = self._runner(command, capture_output=True, text=True, timeout=self._timeout)
        except subprocess.TimeoutExpired as e:
            raise NoSubtitlesAvailable(f"音訊下載逾時（超過 {self._timeout:.0f} 秒）") from e
        except OSError as e:
            raise NoSubtitlesAvailable(f"無法執行 ffmpeg：{e}") from e
        if result.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
            stderr = (getattr(result, "stderr", "") or "").strip()[:200]
            raise NoSubtitlesAvailable(f"音訊下載失敗：{stderr or 'ffmpeg 失敗'}")
        # wav 是固定位元率，長度直接由檔案大小算，不必再開一次 ffprobe。
        duration = os.path.getsize(dest) / (_SAMPLE_RATE * _BYTES_PER_SAMPLE)
        return AudioClip(path=dest, video_id=f"tccc-{ref.ano}", title=record.title,
                         duration=float(duration))

    def cleanup(self, dest_dir: str) -> None:
        shutil.rmtree(dest_dir, ignore_errors=True)
