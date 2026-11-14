"""領域層例外。"""
from __future__ import annotations


class OperationCancelled(Exception):
    """使用者主動取消操作。"""


class NoSubtitlesAvailable(Exception):
    """影片沒有任何可用的字幕軌。"""


class DiarizationUnavailable(Exception):
    """需要區分說話者，但 GPU 上的 diarization 無法使用。"""


class SpeechRecognitionFailed(Exception):
    """多人模式的音訊下載或語音辨識失敗（沒有偵測到語音也算）：工作失敗，什麼都不發佈。

    多人模式不用字幕，所以不借用 NoSubtitlesAvailable——那個例外的訊息一開頭就是
    「這部影片沒有字幕」，對一支其實沒去找過字幕的工作是誤導。
    """


class VideoRangeInvalid(Exception):
    """要求的影片區間不能用（起點超過影片長度、夾完之後太短）：工作失敗，什麼都不發佈。訊息給操作者看。"""


class AudioCutFailed(Exception):
    """切出指定區間的音訊失敗（ffmpeg 出錯、沒有輸出檔、切出來的檔太短）：工作失敗，什麼都不發佈。"""


class AttributionUnreliable(Exception):
    """多人模式沒辦法可靠地說出「誰說了什麼」：工作失敗，什麼都不發佈。

    例如聲音區分的結果不合理（要求 2 人卻只找到 1 人、主要發言者超過 8 人、沒有
    任何發言者），或模型的重點被程式檢查丟掉太多、某一段一頁都留不下來。訊息要帶
    數字，讓操作者看得出是哪一種。寧可失敗也不退回單人的「講者」寫法。
    """


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
