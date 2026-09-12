"""領域層例外。"""
from __future__ import annotations


class OperationCancelled(Exception):
    """使用者主動取消操作。"""


class NoSubtitlesAvailable(Exception):
    """影片沒有任何可用的字幕軌。"""


class SubtitleDownloadFailed(NoSubtitlesAvailable):
    """字幕存在，但這次下載失敗（例如 HTTP 429 限流）。

    是 NoSubtitlesAvailable 的子類別，所以既有的例外處理照常運作；只有
    語音備援需要分辨它——這是暫時性失敗，應告知使用者稍後重試，而不是把
    品質最好的人工字幕無聲地換成較差的語音辨識。

    引申用法：來源明確知道「改走語音也幫不上忙」時也用它。例如立法院
    IVOD 還沒產生逐字稿——語音備援走的是 yt-dlp，而 yt-dlp 不認得 IVOD
    網址，降級只會用一個無關的錯誤訊息蓋掉真正的原因。
    """


class SummarizerUnavailable(Exception):
    """摘要服務連不上，或指定的模型不存在。"""


class SummarizerOutputInvalid(Exception):
    """模型輸出重試後仍不符合要求。"""
