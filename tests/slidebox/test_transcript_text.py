"""逐字稿的可讀化：把餵給模型的秒數標記換成 mm:ss。"""
from __future__ import annotations

from slidebox.usecases.chapters import readable_transcript


def test_second_markers_become_minutes_and_seconds():
    """攔的 bug：直接把餵給模型的 [125] 丟給使用者看，要自己心算是幾分幾秒。
    秒數是為了讓模型好抄，人看的是另一回事。"""
    assert readable_transcript("[0] 你好\n[125] 世界") == "00:00 你好\n02:05 世界"


def test_hours_keep_counting_in_minutes():
    """長影片不特別處理成 hh:mm:ss，維持分鐘累加即可，不會誤讀。"""
    assert readable_transcript("[3725] 很久以後") == "62:05 很久以後"


def test_lines_without_a_marker_pass_through():
    assert readable_transcript("沒有標記的一行") == "沒有標記的一行"


def test_empty_input_stays_empty():
    assert readable_transcript("") == ""
