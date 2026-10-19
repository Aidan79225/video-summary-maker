"""VideoSectionGateway 的 yt-dlp 實作：只下載需要的幾秒片段。"""
from __future__ import annotations

import os
import shutil
from collections.abc import Sequence

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.utils import download_range_func

from ..domain.entities import SectionClips
from ..domain.errors import OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from .ffmpeg import get_ffmpeg_dir


class YtDlpSectionGateway:
    """每個時間點跑一次獨立的 yt-dlp 呼叫。

    選擇一點一呼叫而非單次多區間，是為了失敗隔離：單點失敗只讓該頁沒圖，
    符合「部分截圖失敗仍然出片」的策略。代價是重複的 info extraction。
    """

    def __init__(self, clip_seconds: float = 4.0, ffmpeg_dir: str | None = None):
        self._clip_seconds = clip_seconds
        self._ffmpeg_dir = ffmpeg_dir or get_ffmpeg_dir()
        # yt-dlp 的 download_ranges 前置檢查（FFmpegFD.available()）只查 PATH、
        # 完全無視 ffmpeg_location（其原始碼留有 TODO: Fix path for ffmpeg），
        # 所以 PATH 上找不到 ffmpeg 時整個分段下載會直接中止。
        #
        # 但 PATH 只用來過這個布林檢查——真正執行的仍是 ffmpeg_location 指定的
        # 內建版本。因此使用者已經有 ffmpeg 時就不必動環境變數，避免永久遮蔽
        # 他自己的安裝。
        if shutil.which("ffmpeg") is None:
            os.environ["PATH"] = self._ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> SectionClips:
        os.makedirs(dest_dir, exist_ok=True)
        total = max(1, len(timestamps))
        results: list[str | None] = []
        # 每次呼叫重置：本欄位記錄「這一輪」第一個失敗原因。靜默的
        # 「沒有截圖」跟真的有 bug 看起來一模一樣，第一個成因必須傳到使用者眼前。
        self._first_error: str | None = None
        segmented = self._segmented_format(url, max_height)

        for i, start in enumerate(timestamps):
            if is_cancelled():
                raise OperationCancelled()
            progress(i / total, f"下載片段 {i + 1}/{total}")
            if segmented is not None:
                results.append(self._segment(segmented, start, dest_dir, i))
            else:
                results.append(self._one(url, start, max_height, dest_dir, i))

        ok = sum(r is not None for r in results)
        if self._first_error and ok < total:
            progress(1.0, f"片段下載完成（{ok}/{total}）；失敗原因：{self._first_error[:120]}")
        else:
            progress(1.0, f"片段下載完成（{ok}/{total}）")
        return SectionClips(results, failure=self._first_error or "")

    def cleanup(self, dest_dir: str) -> None:
        shutil.rmtree(dest_dir, ignore_errors=True)

    def _segmented_format(self, url: str, max_height: int | None) -> dict | None:
        """直播剛結束、YouTube 還沒轉檔完的影片（post-live）：回傳選到的切段格式；其他影片回 None。

        這種影片只有「每 target_duration 秒一段」的格式（http_dash_segments，每段網址帶 &sq=N），
        protocol^=http 也會選到它。交給 download_ranges 的話，yt-dlp 讓 ffmpeg 在直播串流的網址上
        -ss 跳到那一秒——跳不過去，下載「成功」卻是一個沒有任何畫格的空殼（實測 13 KB），整份
        投影片一張截圖都沒有。每一段本身就是完整的 fMP4、單獨解得開，所以改成直接抓那一段。

        每份投影片多查一次格式（一兩秒）。查不到就照舊一點一呼叫，失敗原因由那邊記下。
        """
        opts = {"format": _video_format(max_height), "noplaylist": True,
                "quiet": True, "no_warnings": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:  # noqa: BLE001 見上
            return None
        fmt = (info.get("requested_formats") or [info])[0]
        if fmt.get("target_duration") and fmt.get("fragments") is not None:
            return fmt
        return None

    def _segment(self, fmt: dict, start: float, dest_dir: str, index: int) -> str | None:
        """抓包含 start 那一秒的那一段（約 target_duration 秒），存成 clipNNN.mp4。

        抽幀取片段第 2 秒，所以截圖落在 start 前後幾秒內；挑代表畫面這樣就夠準。
        時間點比最後一段的起點還晚（字幕的結尾可能超出一點）就用最後一段。
        """
        path = os.path.join(dest_dir, f"clip{index:03d}.mp4")
        try:
            fragments = fmt["fragments"]
            if not len(fragments):
                # yt-dlp 查最新片段編號的那一次請求失敗時，清單是空的（它把錯誤吞掉了）
                raise RuntimeError("YouTube 沒有給這部直播剛結束影片的片段清單，請稍後再試")
            fragment = fragments[min(max(0, int(start // fmt["target_duration"])), len(fragments) - 1)]
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                data = ydl.urlopen(Request(fragment["url"], headers=fmt.get("http_headers") or {})).read()
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:  # noqa: BLE001 單點失敗只讓該頁沒圖
            if self._first_error is None:
                self._first_error = str(e)
            return None
        return path

    def _one(self, url: str, start: float, max_height: int | None,
             dest_dir: str, index: int) -> str | None:
        stem = os.path.join(dest_dir, f"clip{index:03d}")
        opts = {
            "format": _video_format(max_height),
            "download_ranges": download_range_func(
                None, [(start, start + self._clip_seconds)]
            ),
            # 強制對齊關鍵影格要重編碼、很慢；切點落在前一個關鍵影格
            # 對「取代表畫面」毫無影響。
            "force_keyframes_at_cuts": False,
            "outtmpl": stem + ".%(ext)s",
            "ffmpeg_location": self._ffmpeg_dir,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:  # noqa: BLE001 單點失敗只讓該頁沒圖
            if self._first_error is None:
                self._first_error = str(e)
            return None
        for name in sorted(os.listdir(dest_dir)):
            if name.startswith(os.path.basename(stem)):
                return os.path.join(dest_dir, name)
        return None


def _video_format(max_height: int | None) -> str:
    """只要視訊，不要音訊也不要合併——較快且檔案較小。

    protocol^=http 是必要的：同一畫質常同時有 https(DASH) 與 m3u8_native(HLS) 兩種串流，
    而 HLS 不支援任意位置的分段抽取，選到它時 ffmpeg 會產出一個沒有影像的空容器（約 250 bytes），
    下載「成功」但抽幀必然失敗。實測同一支影片：不限定協定得 257 bytes，限定 http 得 473,425 bytes。
    （post-live 影片的 http_dash_segments 也符合 http 開頭，由 _segmented_format 另外處理。）
    """
    if max_height:
        return (f"bestvideo[height<={max_height}][protocol^=http]/"
                f"bestvideo[protocol^=http]/bestvideo[height<={max_height}]/best")
    return "bestvideo[protocol^=http]/bestvideo/best"
