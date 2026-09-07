"""提示組裝：純函式，不碰網路。"""
from __future__ import annotations

from slidebox.infrastructure.ollama_summarizer import _build_user_prompt


def test_includes_the_compressed_subtitles():
    prompt = _build_user_prompt("[0] 開場白", 120.0, 2, 3, "")
    assert "[0] 開場白" in prompt


def test_states_the_duration_and_page_range():
    prompt = _build_user_prompt("字幕", 125.7, 8, 15, "")
    assert "125" in prompt
    assert "8" in prompt
    assert "15" in prompt


def test_hint_is_absent_on_the_first_attempt():
    """hint 為空字串時不得在提示裡留下空洞的段落。"""
    prompt = _build_user_prompt("字幕", 120.0, 2, 3, "")
    assert "請修正" not in prompt


def test_hint_reaches_the_prompt_on_a_retry():
    """重試的唯一價值就是把驗證錯誤餵回模型——它必須真的進到提示裡。"""
    hint = "上一次的輸出有這些問題，請修正後重新產出：只產出 1 頁，少於下限 2 頁"
    prompt = _build_user_prompt("字幕", 120.0, 2, 3, hint)
    assert hint in prompt
