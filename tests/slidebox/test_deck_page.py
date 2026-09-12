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
from slidebox.usecases.queue import CANCELLED, DONE, PENDING, RUNNING  # noqa: E402


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


def _page(tmp_path, usecase) -> DeckPage:
    settings = Settings(output_dir=str(tmp_path))
    return DeckPage(usecase, Catalog(), settings, save=lambda: None)


def _pump(page, predicate, timeout=5.0):
    """處理 Qt 事件直到條件成立；回傳是否成立。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
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
