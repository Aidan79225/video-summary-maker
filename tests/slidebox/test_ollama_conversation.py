"""對談摘要器的 Ollama 呼叫：用假的 HTTP 回應，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import hashlib
import io
import json
import urllib.error

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.domain.errors import OperationCancelled, SummarizerOutputInvalid, SummarizerUnavailable
from slidebox.infrastructure import ollama_conversation as mod
from slidebox.infrastructure import ollama_summarizer
from slidebox.usecases import conversation as conv

from .conversation_support import at, run

WINDOW_TEXT = "[T3] S1：今天我們來談少子化。\n[T4] S2：現金補助有幫助。"


class FakeResponse:
    """模擬 urlopen 回傳的 NDJSON 串流。"""

    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        for line in self._lines:
            yield line.encode("utf-8")


def _stream(payload: str) -> list[str]:
    lines = [json.dumps({"message": {"content": c}}) for c in payload]
    lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    return lines


def _reply(*slides) -> str:
    return json.dumps({"slides": list(slides)}, ensure_ascii=False)


GOOD_REPLY = _reply({"title": "補助政策", "points": [
    {"turn": 3, "text": "詢問目前補助政策的看法", "context": []},
    {"turn": 4, "text": "現金補助有幫助", "context": []},
]})


@pytest.fixture
def sent(monkeypatch):
    """記下送給 Ollama 的請求，回一個預設的答案；答案可以在測試裡換。"""
    record = {"reply": GOOD_REPLY}

    def fake_post(url, body, timeout):
        record.update(url=url, body=body, timeout=timeout)
        record["response"] = FakeResponse(_stream(record["reply"]))
        return record["response"]

    monkeypatch.setattr(ollama_summarizer, "_post_json", fake_post)
    return record


def _summarize(hint="", is_cancelled=None, num_ctx=32768, host="http://gpu:11434/", progress=None,
               min_slides=2, max_slides=4):
    summarizer = mod.OllamaConversationSummarizer(host, "qwen3.5:9b", num_ctx)
    return summarizer.summarize_window(
        WINDOW_TEXT, 3, 4, min_slides, max_slides, hint, progress or (lambda frac, status: None), is_cancelled)


# --- 提示：不洩漏任何身分，只有該給的東西 ---


FORBIDDEN_IN_THE_PROMPT = (
    "講者", "發言者", "說話者", "主持人", "來賓", "嘉賓", "受訪者", "被訪者", "與談人", "提問者", "回答者",
    "委員", "部長", "院長", "市長", "議員", "局長", "總統", "教授", "老師", "先生", "小姐", "女士",
    "頻道", "影片標題", "影片說明",       # 「投影片」是提示裡正常的用語，所以不能只擋「影片」
)


def test_the_system_prompt_has_no_role_words_names_or_titles():
    """攔的 bug：提示裡出現「講者」「主持人」之類的字，模型就學著這樣稱呼——而誰說了什麼是程式決定的，
    不是模型。姓名與職稱更不能出現：模型看不到它們，也就不可能猜。"""
    prompt = mod.conversation_system_prompt()
    for word in FORBIDDEN_IN_THE_PROMPT:
        assert word not in prompt, word
    for word in conv._LABEL_WORDS:            # 檢查器禁用的稱呼，提示裡也一個都不能有
        assert word not in prompt, word


def test_the_system_prompt_states_every_rule_the_model_must_follow():
    prompt = mod.conversation_system_prompt()
    for rule in (
        "turn",                       # 引用編號
        "context",                    # 很短的回答要附上文
        "不帶主詞",                    # 沒有主詞的短句
        "「他」「她」「對方」",         # 不用代名詞
        "第一人稱",                    # 不用第一人稱
        "問題和回答不要合併",           # 一個重點一位說話者
        "「詢問…」「追問…」",           # 問句寫成詢問
        "複述對方說過的話",             # 複述不算複述者的主張
        "逐字照抄",                    # 引文要逐字
        "指定範圍內的編號",             # 只能引用範圍內的編號
        "只輸出 JSON",
        "繁體中文",
    ):
        assert rule in prompt, rule


def test_the_user_prompt_has_only_the_range_the_lines_and_the_hint():
    """攔的 bug：使用者訊息夾帶影片標題、頻道、說明或總長度——模型看到姓名與職稱就會把它們寫進重點。"""
    assert mod.conversation_user_prompt(WINDOW_TEXT, 3, 4, 2, 4) == (
        "請整理成 2 到 4 頁投影片。\n\n"
        "只能引用編號 T3 到 T4 的話。\n\n"
        "以下是對談：\n" + WINDOW_TEXT)
    assert mod.conversation_user_prompt(WINDOW_TEXT, 3, 4, 2, 4, "上次被退回的原因：…") == (
        "請整理成 2 到 4 頁投影片。\n\n"
        "只能引用編號 T3 到 T4 的話。\n\n"
        "上次被退回的原因：…\n\n"
        "以下是對談：\n" + WINDOW_TEXT)


def test_the_request_carries_nothing_but_the_prompts(sent):
    _summarize()
    system, user = sent["body"]["messages"]
    assert system == {"role": "system", "content": mod.conversation_system_prompt()}
    assert user == {"role": "user", "content": mod.conversation_user_prompt(WINDOW_TEXT, 3, 4, 2, 4)}
    assert set(sent["body"]) == {"model", "messages", "format", "stream", "think", "options"}


def test_a_retry_hint_reaches_the_user_message_and_only_there(sent):
    _summarize(hint="上一次的問題：第 1 頁第 2 點：重點太短或是空的")
    system, user = sent["body"]["messages"]
    assert "重點太短或是空的" in user["content"]
    assert "重點太短或是空的" not in system["content"]


def test_prompt_schema_and_sampling_are_pinned_to_the_prompt_version(sent):
    """攔的 bug：改了提示、schema 或取樣設定卻沒升 PROMPT_VERSION，存檔裡舊設定產生的摘要被當成新設定的
    （slides 階段的指紋含 PROMPT_VERSION）。這裡釘住「實際送出去的」這幾樣東西的雜湊；
    改得有意的話，先升 usecases/conversation.py 的 PROMPT_VERSION，再把下面兩個值換成新的。"""
    _summarize()
    body = sent["body"]
    digest = hashlib.sha256(json.dumps(
        [body["messages"][0]["content"], body["format"], body["options"]["temperature"], body["think"]],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    assert (conv.PROMPT_VERSION, digest) == (
        1, "416035d30d8329bfe6c18242f604803046332f68f0b129cd39fdbe183ad97106")


# --- 請求本身 ---


def test_the_request_goes_to_the_chat_endpoint_of_the_configured_model(sent):
    _summarize()
    assert sent["url"] == "http://gpu:11434/api/chat"
    assert sent["body"]["model"] == "qwen3.5:9b"
    assert sent["body"]["stream"] is True


def test_the_temperature_is_low_and_thinking_is_off(sent):
    """攔的 bug：qwen3.5 預設先思考，偶爾整段都進了 thinking、content 是空的（回應不是合法的 JSON）。"""
    _summarize()
    assert sent["body"]["think"] is False
    assert sent["body"]["options"]["temperature"] == 0.2


def test_the_context_size_is_the_one_it_was_given(sent):
    """Ollama 換 num_ctx 會重新載入模型；對談摘要跟其他工作排同一個佇列交錯跑。"""
    _summarize(num_ctx=32768)
    assert sent["body"]["options"]["num_ctx"] == 32768
    _summarize(num_ctx=16384)
    assert sent["body"]["options"]["num_ctx"] == 16384


def test_the_format_is_the_conversation_schema(sent):
    _summarize()
    assert sent["body"]["format"] == mod.conversation_schema()


def test_the_timeout_is_ten_minutes_by_default(sent):
    _summarize()
    assert sent["timeout"] == 600.0


def test_progress_is_reported_while_the_answer_streams(sent):
    seen = []
    _summarize(progress=lambda frac, status: seen.append((frac, status)))
    assert seen and all(frac is None and "整理對談重點中" in status for frac, status in seen)


# --- schema ---


def test_the_schema_requires_every_field_and_types_the_turn_as_an_integer():
    schema = mod.conversation_schema()
    slide = schema["properties"]["slides"]["items"]
    point = slide["properties"]["points"]["items"]
    assert schema["required"] == ["slides"]
    assert slide["required"] == ["title", "points"]
    assert point["required"] == ["turn", "text", "context"]
    assert point["properties"]["turn"] == {"type": "integer"}
    assert point["properties"]["context"] == {"type": "array", "items": {"type": "integer"}}


# --- 回應解析 ---


def test_a_well_formed_reply_becomes_raw_slides(sent):
    assert _summarize() == (
        RawSlide("補助政策", (
            RawPoint(3, "詢問目前補助政策的看法", ()),
            RawPoint(4, "現金補助有幫助", ()),
        )),
    )


def test_context_turns_are_carried_to_the_raw_point():
    slides = mod.parse_conversation_response(_reply(
        {"title": "t", "points": [{"turn": 7, "text": "同意補助要加倍", "context": [5, 6]}]}))
    assert slides[0].points[0] == RawPoint(7, "同意補助要加倍", (5, 6))


@pytest.mark.parametrize("wrap", [
    lambda s: "```json\n" + s + "\n```",
    lambda s: "```\n" + s + "\n```",
    lambda s: "以下是整理好的結果：\n" + s + "\n希望有幫助。",
    lambda s: "<think>這段對談在談補助。</think>\n" + s,
    # 思考殘留裡有一份合法的草稿：不去掉的話，讀到的是草稿而不是最後的答案
    lambda s: '<think>草稿：{"slides": []}</think>\n' + s,
    # 前面的客套話裡有大括號：第一個「{」不是答案，要往後找
    lambda s: "格式是 {這樣} 的，結果如下：\n" + s,
    lambda s: '例如 {"turn": 1} 這樣。\n' + s + "\n補充：{完}",
    lambda s: "```json\n" + s + "\n```\n\n補充：{完}",
    lambda s: "  \n" + s + "\n\n",
])
def test_the_json_is_found_inside_fences_prose_and_thinking_leftovers(wrap):
    """攔的 bug：模型偶爾把 JSON 包在 Markdown 圍欄或客套話裡，整個窗因此作廢、白跑一次生成。"""
    assert mod.parse_conversation_response(wrap(GOOD_REPLY)) == mod.parse_conversation_response(GOOD_REPLY)


def test_a_thinking_block_that_spans_several_lines_is_removed_whole():
    """攔的 bug：思考殘留跨好幾行時只去掉同一行內的部分，裡面的草稿 JSON 被當成答案讀走。"""
    wrapped = '<think>\n先想一下。\n草稿：{"slides": []}\n再檢查一次。\n</think>\n' + GOOD_REPLY
    assert mod.parse_conversation_response(wrapped) == mod.parse_conversation_response(GOOD_REPLY)


@pytest.mark.parametrize("payload", [
    "", "   \n", "這不是 JSON", "{\"slides\": [", "[]", "[{\"title\": \"x\"}]", "42", "null",
    "{}", "{\"slides\": {}}", "{\"slides\": \"x\"}", "{\"pages\": []}",
])
def test_a_reply_without_a_slides_array_is_invalid(payload):
    with pytest.raises(SummarizerOutputInvalid):
        mod.parse_conversation_response(payload)


@pytest.mark.parametrize("payload", [None, 42, b'{"slides": []}', ["slides"], {"slides": []}])
def test_a_reply_that_is_not_text_is_invalid(payload):
    """攔的 bug：串流層出了狀況給的不是字串，卻在 .strip() 才以 AttributeError 當掉，而不是「這個窗無效」。"""
    with pytest.raises(SummarizerOutputInvalid, match="不是文字"):
        mod.parse_conversation_response(payload)


def test_an_unparsable_reply_keeps_the_parsers_complaint_for_the_operator():
    """工作失敗訊息要讓人看得出模型回了什麼樣的東西，而不只是「不是合法的 JSON」。"""
    with pytest.raises(SummarizerOutputInvalid, match="Expecting value"):
        mod.parse_conversation_response("抱歉，我無法整理這段對談。")


def test_an_empty_slides_array_is_a_valid_reply_that_the_checks_will_reject():
    """沒有任何一頁不是「格式錯誤」：交給 validate_points 去說「沒有提出任何一頁」，重試的判準只有一處。"""
    assert mod.parse_conversation_response('{"slides": []}') == ()


def test_malformed_pieces_degrade_instead_of_failing_the_whole_reply():
    slides = mod.parse_conversation_response(json.dumps({"slides": [
        "不是物件",
        {"title": 5, "points": "不是陣列"},
        {"title": "好", "points": ["不是物件", {"turn": 3, "text": 9, "context": "x"}, {"text": "沒有 turn"}]},
    ]}, ensure_ascii=False))
    assert slides[0] == RawSlide("", ())
    assert slides[1] == RawSlide("", ())
    points = slides[2].points
    assert len(points) == 3                                  # 每個被提出的點都留著，才算得進「提出的點數」
    assert points[0] == RawPoint(None, "", ())
    assert points[1] == RawPoint(3, "", ())
    assert points[2] == RawPoint(None, "沒有 turn", ())


@pytest.mark.parametrize("given,expected", [
    (12, 12), ("12", 12), (" 12 ", 12), ("T12", 12), ("t12", 12), ("[T12]", 12), (12.0, 12),
])
def test_clear_spellings_of_the_same_turn_number_are_accepted(given, expected):
    slides = mod.parse_conversation_response(_reply({"title": "t", "points": [{"turn": given, "text": "x", "context": []}]}))
    assert slides[0].points[0].turn == expected and type(slides[0].points[0].turn) is int


@pytest.mark.parametrize("given", [
    12.5, True, False, None, "abc", "12a", "T", [12], {"n": 12}, "第12段",
    "-3", "+3", "T-3",                          # 帶正負號的不是「明確的同一個編號」：編號沒有負的
])
def test_anything_else_is_left_as_it_is_for_the_checks_to_reject(given):
    slides = mod.parse_conversation_response(_reply({"title": "t", "points": [{"turn": given, "text": "x", "context": []}]}))
    assert slides[0].points[0].turn == given and type(slides[0].points[0].turn) is type(given)


def test_context_keeps_only_integer_references():
    slides = mod.parse_conversation_response(_reply(
        {"title": "t", "points": [{"turn": 9, "text": "x", "context": ["T3", 4, "x", 5.0, True, None, 6.5]}]}))
    assert slides[0].points[0].context == (3, 4, 5)


def test_a_point_with_a_bad_turn_is_counted_and_thrown_away_by_the_checks():
    """攔的 bug：剖析時悄悄丟掉格式不對的點，「模型提出幾個」就少算了，>25% 被丟掉的檢查因此失靈。"""
    slides = mod.parse_conversation_response(_reply({"title": "補助與托育", "points": [
        {"turn": "第三段", "text": "詢問目前補助政策的看法", "context": []},
        {"turn": None, "text": "現金補助有幫助", "context": []},
    ]}))
    extra, result = run(*slides[0].points)
    assert extra == []
    assert result.proposed == 4 and result.kept == 2          # 兩個一定通過的 + 兩個格式不對的
    assert sum("turn 必須是整數編號" in problem for problem in result.problems) == 2


def test_a_point_written_as_a_turn_string_still_checks_out():
    slides = mod.parse_conversation_response(_reply({"title": "補助與托育", "points": [
        {"turn": f"T{at('今天我們')}", "text": "詢問目前補助政策的看法", "context": []},
    ]}))
    assert slides[0].points[0].turn == at("今天我們")


# --- 失敗要說得清楚 ---


def test_a_reply_that_is_not_json_fails_the_window_with_a_readable_error(sent):
    sent["reply"] = "抱歉，我無法整理這段對談。"
    with pytest.raises(SummarizerOutputInvalid, match="JSON"):
        _summarize()


def test_an_empty_reply_is_a_readable_error(sent):
    sent["reply"] = ""
    with pytest.raises(SummarizerOutputInvalid, match="沒有回應任何內容"):
        _summarize()


def test_cancelling_stops_the_stream_and_closes_the_connection(monkeypatch):
    """攔的 bug：取消只在每次嘗試之前檢查，串流中完全不理。一個窗要生成好幾十秒，
    按下取消後停在「取消中…」，整個佇列卡住。"""
    response = FakeResponse(_stream("字" * 60))
    monkeypatch.setattr(ollama_summarizer, "_post_json", lambda *a, **k: response)
    seen = []
    with pytest.raises(OperationCancelled):
        _summarize(progress=lambda frac, status: seen.append(status), is_cancelled=lambda: len(seen) >= 3)
    assert len(seen) < 10
    assert response.closed


def test_a_missing_model_tells_the_operator_what_to_pull(monkeypatch):
    def fake_post(url, body, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}'))

    monkeypatch.setattr(ollama_summarizer, "_post_json", fake_post)
    with pytest.raises(SummarizerUnavailable, match="ollama pull qwen3.5:9b"):
        _summarize()


def test_an_unreachable_ollama_is_reported_as_unavailable(monkeypatch):
    def fake_post(url, body, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ollama_summarizer, "_post_json", fake_post)
    with pytest.raises(SummarizerUnavailable, match="連不上 Ollama"):
        _summarize()


def test_a_connection_that_drops_mid_generation_is_reported_as_unavailable(monkeypatch):
    class Dropping(FakeResponse):
        def __iter__(self):
            yield json.dumps({"message": {"content": "{"}}).encode("utf-8")
            raise ConnectionResetError("reset by peer")

    monkeypatch.setattr(ollama_summarizer, "_post_json", lambda *a, **k: Dropping([]))
    with pytest.raises(SummarizerUnavailable, match="中斷"):
        _summarize()


def test_an_error_event_from_ollama_is_reported_as_unavailable(monkeypatch):
    lines = [json.dumps({"error": "model requires more system memory"})]
    monkeypatch.setattr(ollama_summarizer, "_post_json", lambda *a, **k: FakeResponse(lines))
    with pytest.raises(SummarizerUnavailable, match="more system memory"):
        _summarize()
