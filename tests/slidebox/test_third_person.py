"""摘要要用第三人稱：逐字稿是講者的第一人稱發言，模型很容易照抄視角，
讀者會以為「我們」是整理的人在說話。"""
from __future__ import annotations

from slidebox.domain.entities import Slide
from slidebox.infrastructure.ollama_summarizer import _system
from slidebox.usecases.chapters import first_person_problems, validate_slides


def _slide(index=1, bullets=("講者指出罰鍰偏低",), detail="講者說明現行罰鍰為三萬到五萬元。" * 3):
    return Slide(index, "標題", tuple(bullets), 0.0, detail=detail)


def test_third_person_text_passes():
    assert first_person_problems(_slide()) == []


def test_first_person_in_detail_is_a_problem():
    problems = first_person_problems(_slide(detail="我們對於滋擾醫院秩序之人，把罰鍰提高到五萬元到二十五萬元。" * 2))
    assert problems and "第 1 頁" in problems[0] and "我們" in problems[0]


def test_first_person_in_bullets_is_a_problem():
    assert first_person_problems(_slide(bullets=("我認為應該提高罰鍰",)))


def test_quoted_speech_may_keep_first_person():
    """引述原話放在引號裡是允許的——那明確是講者在說。"""
    assert first_person_problems(_slide(detail="講者強調：「我們一定會嚴格把關。」並要求部會限期回覆。" * 2)) == []


def test_other_first_person_forms_count_too():
    assert first_person_problems(_slide(detail="本席認為這個預算不合理，我方已經提出修正案。" * 2))


def test_validate_slides_includes_first_person_check():
    slides = [_slide(detail="我們要求國防部一個月內提出清冊，這是我們的底線。" * 2)]
    problems = validate_slides(slides, 1, 5, detailed=True)
    assert any("我們" in p for p in problems)


def test_the_prompt_tells_the_model_to_use_the_third_person():
    prompt = _system(detailed=True)
    assert "講者" in prompt
    assert "第三人稱" in prompt
    assert "我們" in prompt


def test_the_problem_quotes_the_surrounding_text():
    """失敗訊息要看得出是真的第一人稱還是誤判。"""
    problems = first_person_problems(_slide(detail="講者說明之後，我們要求部會限期回覆。" * 2))
    assert "…" in problems[0] and "要求部會限期回覆" in problems[0]
