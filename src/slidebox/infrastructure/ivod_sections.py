"""VideoSectionGateway 的 IVOD 實作：用 ffmpeg 從 HLS 串流切出幾秒片段。

IVOD 只提供 m3u8，而 yt-dlp 的分段下載不支援 HLS（會產出沒有影像的空
容器）。ffmpeg 自己反而可以：快速 seek 到時間點、複製幾秒的封包就停。
實測切 4 秒片段約 1 秒。

輸出形狀與 YtDlpSectionGateway 完全相同（一個時間點一個本地檔），所以
抽幀那一步與 use case 都不需要知道來源是誰。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence

from ..domain.errors import OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.sources import ivod_id
from .ffmpeg import get_ffmpeg_exe
from .ivod_api import IvodClient

# 單次 ffmpeg 呼叫的上限。連不上的主機實測要等 20 秒才逾時，設上限才不會
# 讓一份摘要卡在網路層。
_TIMEOUT_SECONDS = 90

# 這些字樣代表「輸入根本開不起來」，不是這個時間點的問題
_INPUT_ERRORS = ("Error opening input", "Server returned", "Connection", "No such file")


class IvodSectionGateway:
    def __init__(self, client: IvodClient | None = None, clip_seconds: float = 4.0,
                 runner=subprocess.run, ffmpeg_exe: str | None = None):
        self._client = client or IvodClient()
        self._clip_seconds = clip_seconds
        self._runner = runner
        self._exe = ffmpeg_exe or get_ffmpeg_exe()

    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> list[str | None]:
        """max_height 用不到：IVOD 的 API 只給一個串流網址，沒得挑畫質。"""
        os.makedirs(dest_dir, exist_ok=True)
        total = max(1, len(timestamps))
        try:
            stream = self._client.video_url(ivod_id(url) or "")
        except Exception as e:  # noqa: BLE001 拿不到影片仍要出片
            # 逐字稿已經拿到了，沒有截圖的摘要依然有用——這正是既有的
            # 「部分截圖失敗仍然出片」策略。完整會議的影片主機連不上時
            # 就會走到這裡。
            progress(1.0, f"這段 IVOD 沒有可用的影片（{str(e)[:80]}），將產出無截圖的摘要")
            return [None] * len(timestamps)

        results: list[str | None] = []
        first_error: str | None = None
        for i, start in enumerate(timestamps):
            if is_cancelled():
                raise OperationCancelled()
            if first_error is not None and _looks_like_input_failure(first_error):
                # 輸入開不起來時，後面每個時間點都注定一樣的結果。15 頁
                # × 20 秒逾時＝五分鐘的空等。
                results.append(None)
                continue
            progress(i / total, f"擷取畫面 {i + 1}/{total}")
            path, error = self._one(stream, start, dest_dir, i)
            results.append(path)
            if error and first_error is None:
                first_error = error

        ok = sum(r is not None for r in results)
        if first_error and ok < total:
            if _looks_like_input_failure(first_error):
                progress(1.0, f"取不到影片畫面（{first_error[:80]}），將產出無截圖的摘要")
            else:
                progress(1.0, f"畫面擷取完成（{ok}/{total}）；失敗原因：{first_error[:120]}")
        else:
            progress(1.0, f"畫面擷取完成（{ok}/{total}）")
        return results

    def cleanup(self, dest_dir: str) -> None:
        shutil.rmtree(dest_dir, ignore_errors=True)

    def _one(self, stream: str, start: float, dest_dir: str,
             index: int) -> tuple[str | None, str]:
        dest = os.path.join(dest_dir, f"clip{index:03d}.mp4")
        command = [
            self._exe, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(start),          # 放在 -i 前面才是快速 seek
            "-i", stream,
            "-t", str(self._clip_seconds),
            "-c", "copy",               # 不重編碼，只複製封包
            "-an",                      # 不要音訊，等一下只需要一格畫面
            dest,
        ]
        try:
            result = self._runner(command, capture_output=True, text=True,
                                  timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return None, f"擷取逾時（超過 {_TIMEOUT_SECONDS} 秒）"
        except Exception as e:  # noqa: BLE001 單點失敗只讓該頁沒圖
            return None, str(e)[:200]
        # ffmpeg 回傳 0 卻產出空檔案是真的會發生（HLS 的空容器），
        # 留到抽幀階段才發現就太晚了。
        if result.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest, ""
        return None, (getattr(result, "stderr", "") or "").strip()[:200] or "ffmpeg 失敗"


def _looks_like_input_failure(error: str) -> bool:
    return any(marker in error for marker in _INPUT_ERRORS)
