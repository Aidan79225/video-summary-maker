"""生成佇列：貼一支影片的同時，前一支還在跑。

純資料與規則，不碰執行緒、不碰 UI——所以整套排隊邏輯都能用單元測試驗證，
UI 只負責把按鈕接上去。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from .sources import ivod_id


class ItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        """是否可以被「清除已完成」掃掉。"""
        return self in _FINISHED


_FINISHED = frozenset({ItemStatus.DONE, ItemStatus.FAILED, ItemStatus.CANCELLED})


# 從各種 YouTube 網址形式取出 11 碼影片 id
_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def video_key(url: str) -> str:
    """同一支影片的不同網址形式要算成同一個東西。

    `youtu.be/X`、`watch?v=X`、`watch?v=X&t=30` 都是同一支影片；兩項都跑
    不只白等一倍時間，第二次還會覆寫第一次的成品資料夾（資料夾名用的是
    影片 id）。IVOD 同理，網址尾端有沒有斜線都是同一段發言。
    認不出 id 的網址就拿整串字串比。
    """
    ivod = ivod_id(url)
    if ivod:
        return f"ivod:{ivod}"
    m = _ID_RE.search(url)
    return m.group(1) if m else url


# eq=False：佇列項目天生是 identity 物件，不是值。用值相等的話，remove()
# 刪掉的會是「第一個長得一樣的」而不是使用者選的那一個——重跑同一支影片
# 就會出現兩列一模一樣的項目。
@dataclass(eq=False)
class QueueItem:
    """佇列裡的一項。label 是影片標題——排隊當下還不知道，完成後才填。"""
    url: str
    status: ItemStatus = ItemStatus.PENDING
    label: str = ""
    message: str = ""
    html_path: str = ""

    @property
    def display(self) -> str:
        return self.label or self.url


@dataclass
class JobQueue:
    items: list[QueueItem] = field(default_factory=list)

    @property
    def running(self) -> QueueItem | None:
        return next((i for i in self.items if i.status == ItemStatus.RUNNING), None)

    @property
    def pending_count(self) -> int:
        return sum(1 for i in self.items if i.status == ItemStatus.PENDING)

    def add(self, url: str) -> QueueItem | None:
        """加入一項；空白或「已經在排隊／正在跑的同一個網址」回傳 None。

        擋重複只擋還沒跑完的：連按兩下加入會白等一倍時間，而已經跑完的
        影片再排一次是正當需求（換了模型、或這次想要詳細版本）。
        """
        url = url.strip()
        if not url:
            return None
        key = video_key(url)
        if any(video_key(i.url) == key and i.status in (ItemStatus.PENDING, ItemStatus.RUNNING)
               for i in self.items):
            return None
        item = QueueItem(url=url)
        self.items.append(item)
        return item

    def start_next(self) -> QueueItem | None:
        """把第一個等待中的項目標成執行中並回傳；沒有可跑的則回傳 None。

        已經有一項在跑時一律回傳 None：兩個生成同時跑會互搶 ffmpeg 與
        Ollama，而 UI 只有一條進度，使用者分不出哪個在動。
        """
        if self.running is not None:
            return None
        item = next((i for i in self.items if i.status == ItemStatus.PENDING), None)
        if item is not None:
            item.status = ItemStatus.RUNNING
        return item

    def finish(self, item: QueueItem, html_path: str, label: str) -> None:
        item.status = ItemStatus.DONE
        item.html_path = html_path
        item.label = label or item.label

    def fail(self, item: QueueItem, message: str) -> None:
        item.status = ItemStatus.FAILED
        item.message = message

    def cancel(self, item: QueueItem) -> None:
        item.status = ItemStatus.CANCELLED

    def retry(self, item: QueueItem) -> bool:
        """把失敗或取消的項目放回等待佇列；回傳是否真的放回去了。

        環境壞掉（例如 Ollama 沒開、模型名稱打錯）時整排都會失敗，沒有
        重試路徑就得一支一支重貼網址。
        """
        if not item.status.is_finished or item not in self.items:
            return False
        item.status = ItemStatus.PENDING
        item.message = ""
        return True

    def remove(self, item: QueueItem) -> bool:
        """移除一項；執行中的不給移除，回傳是否真的移除了。

        執行中的項目一旦從清單消失，工作執行緒結束時就會把結果寫進一個
        再也看不到的項目——使用者會看到生成卡在最後不動。
        """
        if item.status == ItemStatus.RUNNING or item not in self.items:
            return False
        self.items.remove(item)
        return True

    def clear_finished(self) -> None:
        self.items = [i for i in self.items if not i.status.is_finished]
