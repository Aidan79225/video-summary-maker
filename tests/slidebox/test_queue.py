"""佇列：純資料與規則，不碰執行緒也不碰 UI。"""
from __future__ import annotations

from slidebox.usecases.queue import DONE, FAILED, PENDING, RUNNING, JobQueue


def test_added_urls_wait_in_the_order_they_were_pasted():
    """使用者是照想看的順序貼的；換順序等於把選擇權拿走。"""
    q = JobQueue()
    q.add("A")
    q.add("B")
    assert [i.url for i in q.items] == ["A", "B"]
    assert q.items[0].status == PENDING


def test_whitespace_is_trimmed_and_blank_urls_are_refused():
    q = JobQueue()
    assert q.add("  A  ") is not None
    assert q.add("   ") is None
    assert [i.url for i in q.items] == ["A"]


def test_the_same_url_is_not_queued_twice_while_it_is_still_waiting():
    """攔的 bug：連按兩下加入，同一支影片跑兩次，白等一倍的時間。"""
    q = JobQueue()
    q.add("A")
    assert q.add("A") is None
    assert len(q.items) == 1


def test_a_finished_url_can_be_queued_again():
    """重跑是正當需求——換了模型或想要詳細版本。"""
    q = JobQueue()
    item = q.add("A")
    q.start_next()
    q.finish(item, "OUT/a.html", "影片 A")
    assert q.add("A") is not None


def test_start_next_takes_the_first_waiting_item_and_marks_it_running():
    q = JobQueue()
    q.add("A")
    q.add("B")
    item = q.start_next()
    assert item.url == "A"
    assert item.status == RUNNING
    assert q.running is item


def test_nothing_starts_while_something_is_already_running():
    """兩個生成同時跑會互搶 ffmpeg 與 Ollama，而且進度只有一條，
    使用者看不出哪個在動。"""
    q = JobQueue()
    q.add("A")
    q.add("B")
    q.start_next()
    assert q.start_next() is None


def test_finishing_records_where_the_result_landed():
    q = JobQueue()
    item = q.add("A")
    q.start_next()
    q.finish(item, "OUT/a.html", "影片 A")
    assert item.status == DONE
    assert item.html_path == "OUT/a.html"
    assert item.label == "影片 A"
    assert q.running is None


def test_a_failure_keeps_the_reason_visible_and_lets_the_queue_go_on():
    """一支影片失敗不該讓後面排隊的全部停下——使用者可能已經去睡了。"""
    q = JobQueue()
    item = q.add("A")
    q.add("B")
    q.start_next()
    q.fail(item, "連不上 Ollama")
    assert item.status == FAILED
    assert "Ollama" in item.message
    assert q.start_next().url == "B"


def test_cancelling_stops_the_queue_instead_of_moving_on():
    """取消的意思是「停下來」，不是「跳過這支繼續下一支」。"""
    q = JobQueue()
    item = q.add("A")
    q.add("B")
    q.start_next()
    q.cancel(item)
    assert q.running is None
    assert q.items[1].status == PENDING


def test_waiting_items_can_be_removed_but_the_running_one_cannot():
    """移除執行中的項目會讓工作執行緒把結果寫進一個已不存在的項目。"""
    q = JobQueue()
    a = q.add("A")
    b = q.add("B")
    q.start_next()
    assert q.remove(b) is True
    assert q.remove(a) is False
    assert [i.url for i in q.items] == ["A"]


def test_clearing_finished_items_leaves_the_queue_itself_alone():
    q = JobQueue()
    a = q.add("A")
    q.add("B")
    q.start_next()
    q.finish(a, "OUT/a.html", "影片 A")
    q.start_next()
    q.clear_finished()
    assert [i.url for i in q.items] == ["B"]
