"""生成佇列：貼一支影片的同時，前一支還在跑。

純資料與規則，不碰執行緒、不碰 UI——所以整套排隊邏輯都能用單元測試驗證，
UI 只負責把按鈕接上去。
"""
from __future__ import annotations

from dataclasses import dataclass, field

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# 已經結束、可以被「清除完成」掃掉的狀態
_FINISHED = (DONE, FAILED, CANCELLED)


@dataclass
class QueueItem:
    """佇列裡的一項。label 是影片標題——排隊當下還不知道，完成後才填。"""
    url: str
    status: str = PENDING
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
        return next((i for i in self.items if i.status == RUNNING), None)

    @property
    def pending_count(self) -> int:
        return sum(1 for i in self.items if i.status == PENDING)

    def add(self, url: str) -> QueueItem | None:
        """加入一項；空白或「已經在排隊／正在跑的同一個網址」回傳 None。

        擋重複只擋還沒跑完的：連按兩下加入會白等一倍時間，而已經跑完的
        影片再排一次是正當需求（換了模型、或這次想要詳細版本）。
        """
        url = url.strip()
        if not url:
            return None
        if any(i.url == url and i.status in (PENDING, RUNNING) for i in self.items):
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
        item = next((i for i in self.items if i.status == PENDING), None)
        if item is not None:
            item.status = RUNNING
        return item

    def finish(self, item: QueueItem, html_path: str, label: str) -> None:
        item.status = DONE
        item.html_path = html_path
        item.label = label or item.label

    def fail(self, item: QueueItem, message: str) -> None:
        item.status = FAILED
        item.message = message

    def cancel(self, item: QueueItem) -> None:
        item.status = CANCELLED

    def remove(self, item: QueueItem) -> bool:
        """移除一項；執行中的不給移除，回傳是否真的移除了。

        執行中的項目一旦從清單消失，工作執行緒結束時就會把結果寫進一個
        再也看不到的項目——使用者會看到生成卡在最後不動。
        """
        if item.status == RUNNING or item not in self.items:
            return False
        self.items.remove(item)
        return True

    def clear_finished(self) -> None:
        self.items = [i for i in self.items if i.status not in _FINISHED]
