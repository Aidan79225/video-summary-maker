"""領域層例外。"""
from __future__ import annotations


class OperationCancelled(Exception):
    """使用者主動取消操作。"""


class NoSubtitlesAvailable(Exception):
    """影片沒有任何可用的字幕軌。"""


class SubtitleDownloadFailed(NoSubtitlesAvailable):
    """取得逐字稿失敗，而且**改走語音也幫不上忙**：use case 直接回報，不做語音備援。

    是 NoSubtitlesAvailable 的子類別，所以既有的例外處理照常運作；只有語音備援需要分辨它。
    例如立法院 IVOD 還沒產生逐字稿——語音備援走的是 yt-dlp，而 yt-dlp 不認得 IVOD 網址，
    降級只會用一個無關的錯誤訊息蓋掉真正的原因。語音備援本身也失敗時，最後的錯誤也用它。
    """


class SubtitleTrackFailed(NoSubtitlesAvailable):
    """字幕軌存在，但這次用 yt-dlp 下載失敗（例如 HTTP 429 限流）：**改走語音辨識**。

    跟 SubtitleDownloadFailed 刻意分開：一般影片網站的備援用同一個 yt-dlp 抓音訊、用 Whisper
    辨識，通常救得回來，使用者要的是拿到投影片，而不是「請過幾分鐘再試」。

    reason：給畫面用的原因（不含「請過幾分鐘再試」，備援成功時那句話是多餘的）；
    retry_later：是不是限流這種過一陣子就好的問題——備援也失敗時，最後的錯誤要提醒稍後重試。
    """

    def __init__(self, reason: str, retry_later: bool = False):
        super().__init__(reason + ("，請過幾分鐘再試。" if retry_later else ""))
        self.reason = reason
        self.retry_later = retry_later


class SummarizerUnavailable(Exception):
    """摘要服務連不上，或指定的模型不存在。"""


class SummarizerOutputInvalid(Exception):
    """模型輸出重試後仍不符合要求。"""
