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


# --- 詳細模式 ---


def test_detail_is_not_requested_in_normal_mode():
    """一般模式多寫一段等於白等一倍的生成時間。"""
    prompt = _build_user_prompt("字幕", 120.0, 2, 3, "", detailed=False)
    assert "detail" not in prompt


def test_detail_is_requested_with_its_own_field_name_in_detailed_mode():
    """模型只會產出提示裡指名的欄位；欄位名沒寫進提示，schema 也救不回內容。"""
    prompt = _build_user_prompt("字幕", 120.0, 2, 3, "", detailed=True)
    assert "detail" in prompt


def test_normal_schema_has_no_detail_field():
    from slidebox.infrastructure.ollama_summarizer import slides_schema
    props = slides_schema(False)["properties"]["slides"]["items"]["properties"]
    assert "detail" not in props


def test_detailed_schema_requires_detail_so_the_model_cannot_skip_it():
    """攔的 bug：只把 detail 放進 properties 而沒放進 required，小模型會直接
    省略它——結果是勾了詳細卻拿到跟一般模式一樣的成品。"""
    from slidebox.infrastructure.ollama_summarizer import slides_schema
    items = slides_schema(True)["properties"]["slides"]["items"]
    assert "detail" in items["properties"]
    assert "detail" in items["required"]
