"""影片的處理範圍：只處理一支影片裡的一段（add_video --start/--end）。

純規則，不碰資料庫與網路：指令、預檢、送出、結果核對與測試共用同一份，兩邊對「什麼樣的範圍合理」不會各說各話。

時間一律是**整數秒**，而且永遠是原影片的時間軸（不是剪出來那一段的）：要處理 10:00–58:00 就是
`--start 10:00 --end 58:00`，網站上每個時間連結（&t=）、逐字稿的 mm:ss 與截圖也都是原影片的時間。
不收小數：範圍存成整數、送出去的也是整數，「10.5」被悄悄截成 10 的話，操作者以為的跟處理的會差半秒。

一支影片只有一段範圍；兩端都可以省略——省略 --end 就是一直到影片結尾，省略 --start 就是從頭開始。
兩端都省略（或只給 --start 0）就是整支影片，不算有範圍。設計見
docs/superpowers/specs/2026-11-14-video-range-design.md。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 跟 GPU 的 TimeRange（domain/entities.py 的 MIN_RANGE_SECONDS）一樣的下限：太短的一段，說話者區分沒有東西可以認
MIN_RANGE_SECONDS = 5
# 一天：影片上限是 --max-minutes（預設 180 分鐘），遠遠不到；超過的不是影片的時間
MAX_TIME_SECONDS = 24 * 3600

# 每一段只收阿拉伯數字、最多 6 位：int() 連 "１"（全形）、"٣"、"1_0"、"+3" 都吞，打錯的值會變成別的時間；
# 位數先擋住，幾千位的數字也不會走到轉換
_PIECE = re.compile(r"[0-9]{1,6}")


class RangeInvalid(ValueError):
    """範圍不合理（時間看不懂、終點不在起點之後、太短）。訊息是給操作者看的白話。"""


def _shown(text: str) -> str:
    """把輸入放進錯誤訊息：repr 會把控制字元與雙向覆寫跳脫掉，再截短（訊息會印在終端機）。"""
    return repr(text[:40])


def parse_time(text: str, flag: str) -> int:
    """秒（90）、分:秒（1:30）或時:分:秒（1:02:03）→ 整數秒。看不懂、超過一天的丟 RangeInvalid（訊息帶旗標名稱）。

    跟 GPU 的 diarize_check.parse_time 一樣：第一段不封頂（逐字稿會印 125:30），冒號之後的每一段要小於 60。
    不一樣的地方是不收小數：這裡的時間會存進資料庫、跟 GPU 回報的數字逐一核對，一律是整數秒。
    """
    pieces = text.strip().split(":")
    if not 1 <= len(pieces) <= 3 or not all(_PIECE.fullmatch(piece) for piece in pieces):
        raise RangeInvalid(f"{flag} 要是秒數（90）、分:秒（10:30）或時:分:秒（1:02:03），而且只收整數秒，"
                           f"不是 {_shown(text)}")
    numbers = [int(piece) for piece in pieces]
    if any(number >= 60 for number in numbers[1:]):
        raise RangeInvalid(f"{flag} 的分與秒要小於 60（{_shown(text)}）：90 分鐘要寫 1:30:00 或 90:00")
    total = 0
    for number in numbers:
        total = total * 60 + number
    if total > MAX_TIME_SECONDS:
        raise RangeInvalid(f"{flag} 超過一天（{_shown(text)}），不是影片的時間")
    return total


def clock(seconds: int) -> str:
    """600 → 「10:00」、3723 → 「1:02:03」、0 → 「0:00」：跟逐字稿與時間連結一樣的寫法。"""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


@dataclass(frozen=True)
class VideoRange:
    """要處理的一段：start 是整數秒（0＝從頭），end 是整數秒、或 None＝一直到影片結尾。都是原影片的時間。"""
    start: int
    end: int | None = None

    def describe(self) -> str:
        """給人看的寫法：「10:00–58:00」、「10:00–影片結尾」。"""
        return f"{clock(self.start)}–{clock(self.end) if self.end is not None else '影片結尾'}"


def normalize(start: int | None, end: int | None) -> VideoRange | None:
    """兩端的整數秒 → 範圍。沒有範圍（兩端都省略、或只是 --start 0）回 None；不合理的丟 RangeInvalid。

    只給 --end 就是從 0 開始。終點要比起點晚至少 MIN_RANGE_SECONDS 秒。
    """
    if start is None and end is None:
        return None
    start = start or 0
    if end is None:
        return None if start == 0 else VideoRange(start, None)
    if end <= start:
        raise RangeInvalid(f"--end（{clock(end)}）要比 --start（{clock(start)}）晚")
    if end - start < MIN_RANGE_SECONDS:
        raise RangeInvalid(f"--start 到 --end（{clock(start)}–{clock(end)}）太短：範圍至少要 {MIN_RANGE_SECONDS} 秒")
    return VideoRange(start, end)


def resolve_range(start_text: str | None, end_text: str | None) -> VideoRange | None:
    """--start、--end 的文字（沒給是 None）→ 範圍。沒有範圍是 None。"""
    start = parse_time(start_text, "--start") if start_text is not None else None
    end = parse_time(end_text, "--end") if end_text is not None else None
    return normalize(start, end)


def range_of(article) -> VideoRange | None:
    """文章記著的處理範圍；沒有（議會的文章、整支影片）是 None。

    資料庫裡範圍存在的話 range_start 一定是整數（0 也是）、range_end 是整數或空的；沒有範圍就兩個都是空的。
    只有 range_end 的（不該發生）當作從 0 開始。記著的範圍不合理（沒有人會寫出這樣的資料列）丟 RangeInvalid，
    不猜：猜錯的範圍會送去 GPU，處理到不是操作者要的那一段。
    """
    start = getattr(article, "range_start", None)
    end = getattr(article, "range_end", None)
    if start is None and end is None:
        return None
    return normalize(start, end)


def range_length(duration_seconds: int, video_range: VideoRange | None) -> int:
    """要處理的那一段有多長（秒）；0＝不知道。頁數與 --max-minutes 都以它為準：一小時影片裡的十分鐘不該算成一小時。

    沒有範圍就是整支影片的長度。終點超過影片長度就夾到影片結尾；起點在結尾之後沒有東西可以處理（0）。
    影片長度不明（0）時，只有給了終點才算得出來。
    """
    if video_range is None:
        return max(0, duration_seconds)
    if duration_seconds > 0:
        end = duration_seconds if video_range.end is None else min(video_range.end, duration_seconds)
        return max(0, end - video_range.start)
    return video_range.end - video_range.start if video_range.end is not None else 0


def fit_problem(duration_seconds: int, video_range: VideoRange | None) -> str | None:
    """這個範圍放得進這支影片嗎？放不進去回原因（白話、帶旗標名稱），放得進去或沒辦法檢查回 None。

    起點要在影片之內，夾進影片之後也要有 MIN_RANGE_SECONDS 秒（GPU 不收更短的）。終點超過影片長度不算問題：
    GPU 會夾到影片結尾（預檢會另外提醒）。影片長度不明（0）沒辦法檢查，GPU 下載之後才知道，那時再失敗。
    新影片的預檢與已登記影片的指令共用這一句，兩邊說的不會不一樣。
    """
    if video_range is None or duration_seconds <= 0:
        return None
    if video_range.start >= duration_seconds:
        return (f"--start（{clock(video_range.start)}）已經在影片結尾（{clock(duration_seconds)}）之後，"
                "沒有東西可以處理；請確認 --start")
    length = range_length(duration_seconds, video_range)
    if length < MIN_RANGE_SECONDS:
        return (f"範圍 {video_range.describe()} 夾在影片長度（{clock(duration_seconds)}）之內只剩 {length} 秒，"
                f"太短：範圍至少要 {MIN_RANGE_SECONDS} 秒；請確認 --start 與 --end")
    return None
