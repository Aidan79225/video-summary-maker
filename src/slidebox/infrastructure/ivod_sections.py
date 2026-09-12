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

from ..domain.errors import NoSubtitlesAvailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.sources import ivod_id
from .ffmpeg import get_ffmpeg_exe
from .ivod_api import IvodClient

# 單次 ffmpeg 呼叫的上限。實測切一段 4 秒片段只要 1 秒，所以 30 秒已經很
# 寬鬆；而且這個值必須小於關視窗時等 worker 的 30 秒（deck_page 的
# _SHUTDOWN_WAIT_MS），否則「等不到就硬關」的崩潰會回來。
_TIMEOUT_SECONDS = 30

# 連續失敗幾次就放棄剩下的時間點。整支影片取不到時（例如完整會議的影片
# 主機連不上），每個時間點都要等滿逾時——15 頁就是好幾分鐘的空等。
# 用「連續失敗」而不是比對 ffmpeg 的錯誤字串：字串會變，而且逾時根本不
# 產生 ffmpeg 的錯誤訊息；偶發的單點失敗也不該讓整份摘要零截圖。
_MAX_CONSECUTIVE_FAILURES = 2

DEFAULT_CLIP_SECONDS = 4.0


class IvodSectionGateway:
    def __init__(self, client: IvodClient, clip_seconds: float = DEFAULT_CLIP_SECONDS,
                 runner=subprocess.run, ffmpeg_exe: str | None = None):
        self._client = client
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
        video_id = ivod_id(url)
        if video_id is None:
            # 走到這裡表示路由接錯了。不打 API——少了 id 會變成打清單端點，
            # 一次無謂的請求換一個看不懂的錯。
            progress(1.0, f"這不是 IVOD 的播放網址（{url[:60]}），將產出無截圖的摘要")
            return [None] * len(timestamps)
        try:
            stream = self._client.video_url(video_id)
        except (NoSubtitlesAvailable, OSError) as e:
            # 逐字稿已經拿到了，沒有截圖的摘要依然有用——這正是既有的
            # 「部分截圖失敗仍然出片」策略。完整會議的影片主機連不上時
            # 就會走到這裡。收窄例外型別是刻意的：裸 Exception 會把程式
            # 錯誤也靜默降級成「沒有畫面」，而截圖全缺是最難察覺的失效。
            progress(1.0, f"這段 IVOD 沒有可用的影片（{str(e)[:80]}），將產出無截圖的摘要")
            return [None] * len(timestamps)

        results: list[str | None] = []
        first_error: str | None = None
        consecutive = 0
        for i, start in enumerate(timestamps):
            if is_cancelled():
                raise OperationCancelled()
            if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                # 連續失敗代表整支影片取不到，不是這個時間點的問題
                results.append(None)
                continue
            progress(i / total, f"擷取畫面 {i + 1}/{total}")
            path, error = self._one(stream, start, dest_dir, i)
            results.append(path)
            consecutive = 0 if path is not None else consecutive + 1
            if error and first_error is None:
                first_error = error

        ok = sum(r is not None for r in results)
        if ok == 0 and first_error:
            progress(1.0, f"取不到影片畫面（{first_error[:80]}），將產出無截圖的摘要")
        elif first_error and ok < total:
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
