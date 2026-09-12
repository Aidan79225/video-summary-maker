"""佇列在 UI 上的接線：用假的 use case 跑真的 QThread。

這一層的 bug（signal 接錯項目、跑完沒接續、生成中鎖住網址列）純邏輯測試
完全看不到，只能真的把畫面跑起來。
"""
from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from slidebox.domain.entities import Deck, DeckResult, Settings, Slide  # noqa: E402
from slidebox.domain.errors import OperationCancelled  # noqa: E402
from slidebox.presentation.deck_page import DeckPage  # noqa: E402
from slidebox.usecases.queue import (  # noqa: E402
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class GatedUseCase:
    """每次 execute 都停在閘門前，直到測試放行——這樣才能觀察「執行中」。"""

    def __init__(self):
        self.gate = threading.Event()
        self.urls: list[str] = []
        self.fail_urls: set[str] = set()

    def execute(self, url, settings, progress=None, is_cancelled=None):
        self.urls.append(url)
        while not self.gate.wait(0.01):
            if is_cancelled is not None and is_cancelled():
                raise OperationCancelled()
        if url in self.fail_urls:
            raise RuntimeError("連不上 Ollama")
        deck = Deck(url, f"標題 {url}", (Slide(1, "章", ("點",), 0.0),))
        return DeckResult(deck=deck, html_path=f"OUT/{url}.html")


class Catalog:
    def list_models(self):
        return ["qwen3.5:9b"]


_PAGES: list[DeckPage] = []


@pytest.fixture(autouse=True)
def _shutdown_pages():
    """每個測試結束都把還在跑的 worker 收掉。

    留著一個還在跑的 QThread 到測試結束，Python 會在它還在跑時銷毀它，
    Qt 直接中止整個 pytest 行程——症狀是測試「跑到一半就沒了」。
    """
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def _page(tmp_path, usecase) -> DeckPage:
    settings = Settings(output_dir=str(tmp_path))
    page = DeckPage(usecase, Catalog(), settings, save=lambda: None)
    _PAGES.append(page)
    return page


def _pump(page, predicate, timeout=5.0):
    """處理 Qt 事件直到條件成立；回傳是否成立。

    刻意不在迴圈裡 sleep：Python 這邊一睡，worker thread 早就收尾完畢，
    「signal 先到、thread 還沒結束」這個競態就永遠測不出來。改用 Qt 自己
    的 maxtime 阻塞，讓事件盡可能貼著 worker 的收尾送達。
    """
    import time

    from PySide6.QtCore import QEventLoop
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents(QEventLoop.AllEvents, 1)
        if predicate():
            return True
    QApplication.processEvents()
    return predicate()


def _add(page, url):
    page.url_edit.setText(url)
    page._add()


def test_the_url_field_stays_usable_while_a_job_runs(app, tmp_path):
    """痛點本身：等待的時候看到別支影片，要能直接排進去。"""
    uc = GatedUseCase()
    page = _page(tmp_path, uc)
    _add(page, "A")
    assert _pump(page, lambda: page._queue.items[0].status == RUNNING)
    assert page.url_edit.isEnabled()
    assert page.add_btn.isEnabled()
    uc.gate.set()
    _pump(page, lambda: page._queue.items[0].status == DONE)


def test_a_second_url_waits_and_starts_by_itself_when_the_first_finishes(app, tmp_path):
    """攔的 bug：排進去卻沒人接續，使用者回來看到第二項還停在等待。"""
    uc = GatedUseCase()
    page = _page(tmp_path, uc)
    _add(page, "A")
    assert _pump(page, lambda: uc.urls == ["A"])
    _add(page, "B")
    assert page._queue.items[1].status == PENDING
    uc.gate.set()
    assert _pump(page, lambda: uc.urls == ["A", "B"])
    assert _pump(page, lambda: all(i.status == DONE for i in page._queue.items))
    assert page._queue.items[0].html_path == "OUT/A.html"
    assert page._queue.items[1].html_path == "OUT/B.html"


def test_results_land_on_the_item_they_belong_to(app, tmp_path):
    """攔的 bug：signal 送達時用「目前執行中的那一項」記結果，會把 A 的
    成品寫到 B 身上，使用者點開第二項看到第一支影片。"""
    uc = GatedUseCase()
    page = _page(tmp_path, uc)
    _add(page, "A")
    _add(page, "B")
    uc.gate.set()
    assert _pump(page, lambda: all(i.status == DONE for i in page._queue.items))
    assert page._queue.items[0].label == "標題 A"
    assert page._queue.items[1].label == "標題 B"


def test_a_failure_does_not_stop_the_rest_of_the_queue(app, tmp_path):
    uc = GatedUseCase()
    uc.fail_urls = {"A"}
    page = _page(tmp_path, uc)
    _add(page, "A")
    _add(page, "B")
    uc.gate.set()
    assert _pump(page, lambda: page._queue.items[1].status == DONE)
    assert "Ollama" in page._queue.items[0].message


def test_cancelling_stops_the_queue_and_leaves_the_rest_waiting(app, tmp_path):
    """取消是「停下來」。自動接著跑下一支，等於使用者按了取消卻沒停。"""
    uc = GatedUseCase()
    page = _page(tmp_path, uc)
    _add(page, "A")
    _add(page, "B")
    assert _pump(page, lambda: uc.urls == ["A"])
    page._on_action()                      # 執行中 → 取消
    assert _pump(page, lambda: page._queue.items[0].status == CANCELLED)
    assert page._queue.items[1].status == PENDING
    assert uc.urls == ["A"]
    assert page.action_btn.text() == "開始"


def test_settings_are_locked_while_a_job_runs_but_freed_afterwards(app, tmp_path):
    uc = GatedUseCase()
    page = _page(tmp_path, uc)
    _add(page, "A")
    assert _pump(page, lambda: not page.model_combo.isEnabled())
    uc.gate.set()
    assert _pump(page, lambda: page.model_combo.isEnabled())


# --- 審查後補強 ---


def test_the_queue_never_stalls_between_two_items(app, tmp_path):
    """攔的 bug：用 QThread.isRunning() 當守衛有競態——finished signal 送達時
    worker thread 還沒真的結束，_start_next 直接 return，下一項就永遠停在
    等待，而且沒有任何訊息。排十支去睡覺，回來看到第四支之後全停住。"""
    uc = GatedUseCase()
    uc.gate.set()                      # 每一項都立刻完成，把交接的縫隙壓到最小
    page = _page(tmp_path, uc)
    for url in "ABCDE":
        _add(page, url)
    assert _pump(page, lambda: len(uc.urls) == 5, timeout=10.0), (
        f"佇列停住了，只跑了 {uc.urls}"
    )
    assert _pump(page, lambda: all(i.status == DONE for i in page._queue.items))


def test_closing_the_window_stops_the_worker_instead_of_crashing(app, tmp_path):
    """攔的 bug：QThread 還在跑就被銷毀，Qt 直接中止行程（0xC0000409），
    使用者看到「應用程式已停止運作」，而且 finally 的暫存清理全部沒跑。"""
    from slidebox.presentation.main_window import MainWindow

    uc = GatedUseCase()
    page = _page(tmp_path, uc)
    win = MainWindow(page)
    _add(page, "A")
    assert _pump(page, lambda: page._queue.items[0].status == RUNNING)
    win.close()
    assert page._worker is None or not page._worker.isRunning()


def test_consecutive_failures_stop_the_queue(app, tmp_path):
    uc = GatedUseCase()
    uc.gate.set()
    uc.fail_urls = {"A", "B", "C"}
    page = _page(tmp_path, uc)
    for url in "ABC":
        _add(page, url)
    assert _pump(page, lambda: page._queue.items[1].status == FAILED, timeout=10.0)
    # 連續兩次失敗就停下，第三項維持等待，而不是跟著燒掉
    assert _pump(page, lambda: page._queue.items[2].status == PENDING)
    assert page._queue.running is None
    assert "C" not in uc.urls


def test_a_failed_item_can_be_retried_from_the_ui(app, tmp_path):
    uc = GatedUseCase()
    uc.gate.set()
    uc.fail_urls = {"A"}
    page = _page(tmp_path, uc)
    _add(page, "A")
    assert _pump(page, lambda: page._queue.items[0].status == FAILED)
    uc.fail_urls = set()
    page.queue_list.setCurrentRow(0)
    page._retry_selected()
    assert _pump(page, lambda: page._queue.items[0].status == DONE)


def test_clearing_finished_items_keeps_the_selection_on_the_same_item(app, tmp_path):
    """攔的 bug：用索引還原選取，清掉前面的項目後選取會跳到下一項；
    使用者接著按「移除」，移掉的是別支影片。"""
    uc = GatedUseCase()
    uc.gate.set()
    page = _page(tmp_path, uc)
    _add(page, "A")
    assert _pump(page, lambda: page._queue.items[0].status == DONE)
    uc.gate.clear()                            # 之後排的都會停在閘門前
    for url in "BCD":
        _add(page, url)
    assert _pump(page, lambda: page._queue.items[1].status == RUNNING)
    page.queue_list.setCurrentRow(2)           # C
    page._clear_finished()                     # A 被清掉，列號整個往前移
    assert page._selected() is not None
    assert page._selected().url == "C"
