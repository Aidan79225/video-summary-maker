"""投影片驗證與時間戳夾取。"""
from __future__ import annotations

from slidebox.domain.entities import Slide
from slidebox.usecases.chapters import clamp_timestamps, validate_slides


def _slide(index: int, ts: float = 10.0, title: str = "標題") -> Slide:
    return Slide(index=index, title=title, bullets=("重點",), timestamp=ts)


def test_no_problems_for_a_good_deck():
    slides = tuple(_slide(i) for i in range(1, 9))
    assert validate_slides(slides, 8, 15) == []


def test_reports_too_few_slides():
    problems = validate_slides((_slide(1),), 8, 15)
    assert len(problems) == 1
    assert "8" in problems[0]


def test_reports_too_many_slides():
    slides = tuple(_slide(i) for i in range(1, 20))
    problems = validate_slides(slides, 8, 15)
    assert len(problems) == 1
    assert "15" in problems[0]


def test_reports_blank_title():
    slides = tuple(_slide(i) for i in range(1, 8)) + (_slide(8, title="   "),)
    problems = validate_slides(slides, 8, 15)
    assert any("8" in p for p in problems)


def test_reports_empty_bullets():
    slides = tuple(_slide(i) for i in range(1, 8)) + (
        Slide(index=8, title="標題", bullets=(), timestamp=1.0),
    )
    assert validate_slides(slides, 8, 15) != []


def test_clamp_pulls_back_timestamp_past_duration():
    """9B 模型常給超過影片長度的時間戳，夾回去就好，不值得重跑。"""
    out = clamp_timestamps((_slide(1, ts=9999.0),), duration=600.0)
    assert out[0].timestamp == 599.0


def test_clamp_pulls_up_negative_timestamp():
    assert clamp_timestamps((_slide(1, ts=-5.0),), duration=600.0)[0].timestamp == 0.0


def test_clamp_leaves_valid_timestamp_untouched():
    original = (_slide(1, ts=42.0),)
    assert clamp_timestamps(original, duration=600.0)[0] is original[0]


def test_clamp_tolerates_unknown_duration():
    """yt-dlp 偶爾給不出 duration（回 0），此時只夾負值。"""
    out = clamp_timestamps((_slide(1, ts=9999.0),), duration=0.0)
    assert out[0].timestamp == 9999.0


def test_clamp_preserves_other_fields():
    out = clamp_timestamps((Slide(3, "T", ("a", "b"), 9999.0, "x.webp"),), 600.0)
    assert out[0].index == 3
    assert out[0].bullets == ("a", "b")
    assert out[0].image_path == "x.webp"
