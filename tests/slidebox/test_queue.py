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


# --- 審查後補強 ---


def test_two_items_with_identical_fields_are_still_two_different_items():
    """攔的 bug：QueueItem 若用值相等，remove() 刪掉的是「第一個長得一樣的」，
    不是使用者選的那一個。重跑同一支影片就會出現兩列一模一樣的項目。"""
    q = JobQueue()
    a = q.add("A")
    q.start_next()
    q.finish(a, "OUT/a.html", "影片 A")
    b = q.add("A")
    q.start_next()
    q.finish(b, "OUT/a.html", "影片 A")
    q.remove(b)
    assert q.items == [a]
    assert q.items[0] is a


def test_the_same_video_pasted_in_two_url_forms_is_only_queued_once():
    """youtu.be/X 與 watch?v=X 是同一支影片；兩項都跑不只白等一倍時間，
    第二次還會覆寫第一次的成品資料夾（資料夾名用的是 video id）。"""
    q = JobQueue()
    assert q.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is not None
    assert q.add("https://youtu.be/dQw4w9WgXcQ") is None
    assert q.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30") is None
    assert len(q.items) == 1


def test_urls_without_a_recognisable_id_still_compare_as_plain_text():
    q = JobQueue()
    assert q.add("https://example.com/video") is not None
    assert q.add("https://example.com/video") is None
    assert q.add("https://example.com/other") is not None


def test_a_failed_item_can_be_put_back_in_the_queue():
    """環境壞掉（Ollama 沒開）時整排會失敗；沒有重試路徑就得一支一支重貼。"""
    q = JobQueue()
    item = q.add("A")
    q.start_next()
    q.fail(item, "連不上 Ollama")
    assert q.retry(item) is True
    assert item.status == PENDING
    assert item.message == ""
    assert q.start_next() is item


def test_the_running_item_cannot_be_retried():
    q = JobQueue()
    item = q.add("A")
    q.start_next()
    assert q.retry(item) is False
    assert item.status == RUNNING
