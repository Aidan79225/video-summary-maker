"""對談摘要器的 Ollama 呼叫：用假的 HTTP 回應，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error

import pytest

from slidebox.domain.entities import RawPoint, RawSlide, Turn
from slidebox.domain.errors import OperationCancelled, SummarizerOutputInvalid, SummarizerUnavailable
from slidebox.infrastructure import ollama_conversation as mod
from slidebox.infrastructure import ollama_summarizer
from slidebox.usecases import conversation as conv
from slidebox.usecases.names import detect_names
from slidebox.usecases.quotes import locate_quote

from .conversation_support import SPEAKERS, TURNS, WHOLE, at, run
from .speaker_fixtures import make_speakers

WINDOW_TEXT = "[T3] S1：今天我們來談少子化。\n[T4] S2：現金補助有幫助。"
CITABLE = (3, 4)


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
    {"quote": "今天我們來談少子化", "turn": 3, "text": "詢問目前補助政策的看法", "context": []},
    {"quote": "現金補助有幫助", "turn": 4, "text": "現金補助有幫助", "context": []},
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
               min_slides=2, max_slides=4, citable=CITABLE):
    summarizer = mod.OllamaConversationSummarizer(host, "qwen3.5:9b", num_ctx)
    return summarizer.summarize_window(
        WINDOW_TEXT, 3, 4, citable, min_slides, max_slides, hint, progress or (lambda frac, status: None),
        is_cancelled)


# --- 提示：不洩漏任何身分，只有該給的東西 ---


FORBIDDEN_IN_THE_PROMPT = (
    "講者", "發言者", "說話者", "主持人", "來賓", "嘉賓", "受訪者", "被訪者", "與談人", "提問者", "回答者",
    "委員", "部長", "院長", "市長", "議員", "局長", "總統", "教授", "老師", "先生", "小姐", "女士", "主任",
    "頻道", "影片標題", "影片說明",       # 「投影片」是提示裡正常的用語，所以不能只擋「影片」
)
# 提示裡唯一的稱謂：一個虛構的「王主任」，用來教模型「逐字稿裡的稱呼一個都不要寫」。規則裡一次、範例裡一次，不多不少
ALLOWED_NAME = "王主任"


def test_the_system_prompt_has_no_role_words_names_or_titles_except_the_one_made_up_example():
    """攔的 bug：提示裡出現「講者」「主持人」之類的字，模型就學著這樣稱呼——而誰說了什麼是程式決定的，
    不是模型。姓名與職稱更不能出現：模型看不到它們，也就不可能猜。唯一的例外是虛構的「王主任」，
    而且只能出現兩次（規則一次、範例一次）；除去這兩處，檢查器禁用的稱呼與找得出來的稱謂一個都不能有。"""
    prompt = mod.conversation_system_prompt()
    assert prompt.count(ALLOWED_NAME) == 2
    rest = prompt.replace(ALLOWED_NAME, "")
    for word in FORBIDDEN_IN_THE_PROMPT:
        assert word not in rest, word
    for word in conv._LABEL_WORDS:            # 檢查器禁用的稱呼，提示裡也一個都不能有
        assert word not in rest, word
    assert detect_names(rest) == frozenset()
    assert detect_names(prompt) == {ALLOWED_NAME}


def test_the_system_prompt_states_every_rule_the_model_must_follow():
    prompt = mod.conversation_system_prompt()
    for rule in (
        "turn",                       # 引用編號
        "context",                    # 很短的回答要附上文
        "先從逐字稿裡「照抄」一小段原文放進 quote",       # 先抄原文，再填編號，最後才寫 text
        "連續、逐字的一段",              # quote 要連續、逐字
        "不可以跨輪",                   # quote 不可以接兩輪
        "整輪都不到 8 個字，就整輪照抄",   # 很短的一輪整輪抄
        "誰說的，由程式根據你引用的 turn 自己標上",  # 說話者是程式查的
        "不是 X 的編號",                # 只能引用範圍內、不是 X 的編號
        "一個重點只寫 quote 所在那一輪",  # 一個重點一輪
        "沒有主詞的短句",                # 不帶主詞
        "不用代名詞",                   # 不用代名詞
        "不用第一人稱",                  # 不用第一人稱
        "問題和回答是不同人說的",          # 一個重點一位說話者
        "「詢問…」「追問…」",            # 問句寫成詢問
        "複述前面別人剛說過的話",          # 複述不算複述者的主張
        "只輸出一個 JSON 物件",
        "繁體中文",
    ):
        assert rule in prompt, rule


def test_the_user_prompt_has_only_the_range_the_lines_and_the_hint():
    """攔的 bug：使用者訊息夾帶影片標題、頻道、說明或總長度——模型看到姓名與職稱就會把它們寫進重點。"""
    assert mod.conversation_user_prompt(WINDOW_TEXT, 3, 4, 2, 4) == (
        "這個片段的發言編號是 T3 到 T4，turn 只能填 3 到 4 之間的整數。\n"
        "請整理成 2 到 4 頁。\n\n"
        "以下是逐字稿：\n" + WINDOW_TEXT + "\n")                  # 以換行結尾：和實驗送出的請求逐字相同
    assert mod.conversation_user_prompt(WINDOW_TEXT, 3, 4, 2, 4, "上次被退回的原因：…") == (
        "這個片段的發言編號是 T3 到 T4，turn 只能填 3 到 4 之間的整數。\n"
        "請整理成 2 到 4 頁。\n\n"
        "上次被退回的原因：…\n\n"
        "以下是逐字稿：\n" + WINDOW_TEXT + "\n")


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
        2, "413f7b23c2554506af0cb482cb880166eaffcb91c43261e01c1dfe0b12dcf394")


def _prompt_example():
    """提示裡的範例：逐字稿（照 format_window 的樣子）與範例的輸出。"""
    example = mod.conversation_system_prompt().split("【範例】\n", 1)[1]
    transcript, output = example.split("輸出：\n", 1)
    turns = []
    for line in transcript.split("逐字稿：\n", 1)[1].splitlines():
        match = re.match(r"\[T(\d+)\] (S\d+|X)：(.*)$", line)
        index = int(match.group(1))
        turns.append(Turn(index, index * 10.0, index * 10.0 + 9.0, "S0" if match.group(2) == "X" else match.group(2),
                          match.group(3)))
    return tuple(turns), mod.parse_conversation_response(output.strip())


def test_every_quote_in_the_prompt_example_is_found_by_the_quote_check_except_the_one_character_answer():
    """攔的 bug：提示的範例和檢查脫節——範例教模型這樣抄，檢查卻不收。唯一的例外是單字的「對」：
    太短，一定核對不過（提示照做過實驗的原樣保留，所以這一點一定會被丟掉；丟掉是安全的方向）。"""
    turns, slides = _prompt_example()
    by_index = {t.index: t for t in turns}
    citable = conv.citable_ids(make_speakers("participant", "participant", "participant"))
    points = [p for s in slides for p in s.points]

    results = [(p, locate_quote(p.quote, by_index[p.turn], turns, citable)) for p in points]

    assert len(points) == 8
    assert [p.quote for p, found in results if found.turn is None] == ["對"]
    assert all(found.turn.index == p.turn for p, found in results if found.turn is not None)
    assert "太短" in next(found.problem for _, found in results if found.turn is None)


def test_the_example_in_the_prompt_follows_the_text_rules_it_teaches():
    turns, slides = _prompt_example()
    names = detect_names("\n".join(t.text for t in turns))

    assert names == {ALLOWED_NAME}                      # 範例的逐字稿裡有稱謂，所以範例的重點更要示範不寫它
    for point in (p for s in slides for p in s.points):
        assert conv._text_problem(point.text) is None, point.text
        assert conv._name_problem(point.text, names) is None, point.text
    assert all(conv._title_problem(s.title) is None for s in slides)


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


def test_the_format_is_the_conversation_schema_for_the_citable_turns_of_the_window(sent):
    _summarize()
    assert sent["body"]["format"] == mod.conversation_schema(CITABLE)
    _summarize(citable=(4, 9, 3))
    assert sent["body"]["format"]["properties"]["slides"]["items"]["properties"]["points"]["items"][
        "properties"]["turn"]["enum"] == [3, 4, 9]


def test_the_timeout_is_ten_minutes_by_default(sent):
    _summarize()
    assert sent["timeout"] == 600.0


def test_progress_is_reported_while_the_answer_streams(sent):
    seen = []
    _summarize(progress=lambda frac, status: seen.append((frac, status)))
    assert seen and all(frac is None and "整理對談重點中" in status for frac, status in seen)


# --- schema ---


def _point_schema(citable):
    schema = mod.conversation_schema(citable)
    slide = schema["properties"]["slides"]["items"]
    return schema, slide, slide["properties"]["points"]["items"]


def test_the_schema_requires_every_field_and_limits_the_turn_to_the_citable_ones():
    schema, slide, point = _point_schema((4, 3))
    assert schema["required"] == ["slides"]
    assert slide["required"] == ["title", "points"]
    assert point["required"] == ["quote", "turn", "text", "context"]
    assert point["properties"]["quote"] == {"type": "string"}
    assert point["properties"]["turn"] == {"type": "integer", "enum": [3, 4]}
    assert point["properties"]["context"] == {"type": "array", "items": {"type": "integer"}}


def test_the_schema_lists_the_fields_in_the_order_the_model_writes_them():
    """欄位的順序就是輸出的順序：先照抄原文（quote），再填編號，最後才寫重點。"""
    _, _, point = _point_schema((3,))
    assert list(point["properties"]) == ["quote", "turn", "text", "context"]


def test_the_turn_enum_is_sorted_and_has_no_repeats():
    _, _, point = _point_schema([9, 3, 3, 5, 9])
    assert point["properties"]["turn"]["enum"] == [3, 5, 9]


def test_a_window_with_exactly_one_citable_turn_has_a_one_value_enum():
    _, _, point = _point_schema((7,))
    assert point["properties"]["turn"]["enum"] == [7]


def test_thousands_of_citable_turns_all_go_into_the_enum():
    turns = list(range(10_000, 13_000))[::-1]
    _, _, point = _point_schema(turns)
    assert point["properties"]["turn"]["enum"] == sorted(turns) and len(turns) == 3000


@pytest.mark.parametrize("bad", [[], (), [1, True], [1, "2"], [1, 2.0], [None]])
def test_turns_that_cannot_be_a_grammar_are_a_programming_error(bad):
    """沒有任何可以引用的 turn：空的 enum 是無解的文法，Ollama 會卡住或亂回——送來就是程式錯了。"""
    with pytest.raises(ValueError):
        mod.conversation_schema(bad)


def test_a_window_with_no_citable_turn_is_never_sent(sent):
    with pytest.raises(ValueError):
        _summarize(citable=())
    assert "url" not in sent and "body" not in sent


# --- 回應解析 ---


def test_a_well_formed_reply_becomes_raw_slides(sent):
    assert _summarize() == (
        RawSlide("補助政策", (
            RawPoint(3, "詢問目前補助政策的看法", (), "今天我們來談少子化"),
            RawPoint(4, "現金補助有幫助", (), "現金補助有幫助"),
        )),
    )


def test_context_turns_are_carried_to_the_raw_point():
    slides = mod.parse_conversation_response(_reply(
        {"title": "t", "points": [{"quote": "沒錯", "turn": 7, "text": "同意補助要加倍", "context": [5, 6]}]}))
    assert slides[0].points[0] == RawPoint(7, "同意補助要加倍", (5, 6), "沒錯")


def test_the_quote_is_carried_to_the_raw_point_untouched():
    quote = " 名額不夠，  很多家長排不到 "
    slides = mod.parse_conversation_response(_reply(
        {"title": "t", "points": [{"quote": quote, "turn": 7, "text": "x", "context": []}]}))
    assert slides[0].points[0].quote == quote                # 整理空白、標點是核對的事，不是剖析的事


@pytest.mark.parametrize("given", [None, 5, ["名額不夠"], {"q": 1}, True])
def test_a_missing_or_non_text_quote_becomes_an_empty_one_that_the_checks_reject(given):
    point = {"turn": 0, "text": "詢問目前補助政策的看法", "context": []}
    if given is not None:
        point["quote"] = given
    slides = mod.parse_conversation_response(_reply({"title": "補助與托育", "points": [point]}))
    assert slides[0].points[0].quote == ""

    result = conv.validate_points(slides, TURNS, SPEAKERS, WHOLE)

    assert (result.proposed, result.kept, result.unverifiable) == (1, 0, 1)      # 算進提出的點數、算在查不出來
    assert "quote 是空的" in result.problems[0]


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
    assert points[0] == RawPoint(None, "", (), "")
    assert points[1] == RawPoint(3, "", (), "")
    assert points[2] == RawPoint(None, "沒有 turn", (), "")


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
