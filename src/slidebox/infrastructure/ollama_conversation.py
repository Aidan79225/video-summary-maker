"""ConversationSummarizer 的 Ollama 實作：把一段已標好說話者的對談寫成「重點」。

模組分成兩半，跟 ollama_summarizer 一樣：上半是純函式（提示、schema、回應解析），可以離線測試；
下半是 HTTP 呼叫，沿用 ollama_summarizer 的串流實作（取消、錯誤轉譯都在那裡）。

**模型在這裡看不到、也決定不了誰說了什麼。** 它只看到 `[T12] S1：…` 這樣的行和頁數範圍，
回傳「引用第幾段（turn）＋一句沒有主詞的重點」。說話者由程式從被引用的那一段查出來
（usecases/conversation.py 的 validate_points），所以提示裡沒有姓名、職稱、頻道、影片說明，
也沒有「講者」「主持人」這類角色字眼——沒有就不會被模型學去用。

提示詞、schema、下面的取樣設定（temperature、think）改了，要手動升
usecases/conversation.py 的 PROMPT_VERSION，否則存檔裡舊提示產生的摘要會被當成新提示的。
tests/slidebox/test_ollama_conversation.py 釘住這些內容的雜湊，改了沒升版本時會失敗。
"""
from __future__ import annotations

import json
import re
from typing import cast

from ..domain.entities import RawPoint, RawSlide
from ..domain.errors import SummarizerOutputInvalid
from ..domain.ports import CancelCheck, ProgressCallback
from . import ollama_summarizer

_SYSTEM = """你是對談整理助手。使用者會給你一段對談的逐字稿，每一行的格式是
`[T12] S1：內容`：方括號裡的 T12 是這一段話的編號，S1、S2 是聲音的代號，X 代表不能引用的聲音。

請把這段對談整理成數頁投影片，一頁一個話題。每一頁包含：
- title：這個話題的標題，繁體中文，不超過 20 字。只寫話題，不要出現任何稱呼或代號。
- points：這個話題的重點。每個重點包含：
  - turn：整數。你依據的那一段話的編號，只寫數字（例如 12）。只能用使用者指定範圍內的編號，X 的行不能引用。
  - text：用繁體中文、不帶主詞的短句，寫出**那一段話**說了什麼，不超過 60 字。
  - context：整數陣列。只有當那一段話是很短的回答（例如「對」「不是」「沒有」）時，才填它所回應的上文編號
    （在它之前、相隔不超過 3 段）；其他情況一律是空陣列 []。

寫 text 的規則：
1. 一個重點只寫一段話。問題和回答不要合併：問題寫成「詢問…」「追問…」並引用問題那一段；
   回答另外寫成一個重點，引用回答那一段。
2. 不要有主詞，直接從動作或內容開始寫：不用「他」「她」「對方」這類代名詞，不稱呼任何人
   （不寫姓名、身分或職稱，也不寫聲音代號 S1 或編號 T12），不用第一人稱「我」「我們」。
   需要引述原話時放在「」裡，逐字照抄，而且至少 8 個字。
3. 忠於那一段話：數字照抄、肯定與否定不要寫反、問句就寫成詢問；不要加入那一段話沒說的內容，
   也不要把別段話的內容寫進來。複述對方說過的話（例如「您剛才說…對嗎？」）是在確認，
   不能寫成複述的人自己的主張。
4. 誰說的由程式依你引用的編號決定。你不需要猜測說話的人是誰，也不要在 text 裡提到任何人。
5. 很短的肯定或否定回答，text 寫成「同意…」「不同意…」這樣的表態，內容來自 context 指向的上文。
6. 寒暄、語助詞和聽不清楚的片段不用寫成重點。

每一頁至少 2 個重點，重點依對談的順序排列。頁數要在使用者指定的範圍內，各頁涵蓋對談的不同部分。
只輸出 JSON，格式：
{"slides":[{"title":"…","points":[{"turn":12,"text":"…","context":[]}]}]}
"""


def conversation_system_prompt() -> str:
    return _SYSTEM


def conversation_user_prompt(window_text: str, first_turn: int, last_turn: int,
                             min_slides: int, max_slides: int, hint: str = "") -> str:
    """使用者訊息只有三樣東西：頁數範圍與可引用的編號、（重試時）上一次被退回的原因、對談的行。"""
    parts = [
        f"請整理成 {min_slides} 到 {max_slides} 頁投影片。",
        f"只能引用編號 T{first_turn} 到 T{last_turn} 的話。",
    ]
    if hint:
        parts.append(hint)
    parts.append("以下是對談：\n" + window_text)
    return "\n\n".join(parts)


def conversation_schema() -> dict:
    """傳給 Ollama 的 format：文法層級約束。所有欄位都放進 required，小模型會省略選填欄位。"""
    return {
        "type": "object",
        "properties": {
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "turn": {"type": "integer"},
                                    "text": {"type": "string"},
                                    "context": {"type": "array", "items": {"type": "integer"}},
                                },
                                "required": ["turn", "text", "context"],
                            },
                        },
                    },
                    "required": ["title", "points"],
                },
            }
        },
        "required": ["slides"],
    }


# --- 回應解析 ---

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_TURN_REF_RE = re.compile(r"^\s*\[?\s*T?\s*(\d+)\s*\]?\s*$", re.IGNORECASE)
# 回應前後有客套話或 Markdown 圍欄時，最多從這麼多個「{」試著讀出 JSON
_MAX_BRACE_TRIES = 20


def _load_json(payload: str) -> object:
    """模型的回應 → JSON。先照原樣讀；不行就去掉思考殘留，從每個「{」試著讀出一個有 slides 的物件。

    這樣 Markdown 圍欄、前後的客套話（包括裡面有大括號的）都不會讓整個窗白跑一次。
    """
    if not isinstance(payload, str):
        raise SummarizerOutputInvalid("模型回應不是文字")
    if not payload.strip():
        raise SummarizerOutputInvalid("模型沒有回應任何內容")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        error = e
    cleaned = _THINK_RE.sub("", payload)
    decoder = json.JSONDecoder()
    start = cleaned.find("{")
    for _ in range(_MAX_BRACE_TRIES):
        if start < 0:
            break
        try:
            value, _ = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and "slides" in value:
            return value
        start = cleaned.find("{", start + 1)
    raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{error}")


def _turn_ref(value: object) -> object:
    """模型有時寫 "12"、12.0、"T12"：這些明確是同一個編號，轉成整數。其他的原樣交給檢查去退回。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _TURN_REF_RE.match(value)
        if match:
            return int(match.group(1))
    return value


def _context_refs(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    refs = [_turn_ref(item) for item in value]
    return tuple(ref for ref in refs if isinstance(ref, int) and not isinstance(ref, bool))


def parse_conversation_response(payload: str) -> tuple[RawSlide, ...]:
    """模型回應 → RawSlide 序列。

    只有「連 slides 陣列都拿不到」才 raise；個別欄位缺失或格式不對一律降級，是否退回、為什麼退回
    交給 validate_points 決定——退回的判準只有一處，被退回的點也會算進「模型提出的點數」。
    編號不是整數的點，turn 照原值留著（None、"abc"…），validate_points 會以「turn 必須是整數編號」丟掉它。
    """
    data = _load_json(payload)
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise SummarizerOutputInvalid("模型回應裡沒有 slides 陣列")
    slides: list[RawSlide] = []
    for raw in data["slides"]:
        if not isinstance(raw, dict):
            slides.append(RawSlide(title="", points=()))
            continue
        title = raw.get("title")
        points = raw.get("points")
        slides.append(RawSlide(
            title=title if isinstance(title, str) else "",
            points=tuple(_parse_point(p) for p in points) if isinstance(points, list) else (),
        ))
    return tuple(slides)


def _parse_point(raw: object) -> RawPoint:
    if not isinstance(raw, dict):
        return RawPoint(turn=cast(int, None), text="")
    text = raw.get("text")
    return RawPoint(
        turn=cast(int, _turn_ref(raw.get("turn"))),     # 不是整數的原值留給 validate_points 退回
        text=text if isinstance(text, str) else "",
        context=_context_refs(raw.get("context")),
    )


# --- HTTP ---


class OllamaConversationSummarizer:
    """一次一個窗。輸入是幾千字的對談，輸出是幾百字的 JSON，逾時沿用摘要的 10 分鐘。"""

    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 600.0):
        self._host = host.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._timeout = timeout

    def summarize_window(
        self,
        window_text: str,
        first_turn: int,
        last_turn: int,
        min_slides: int,
        max_slides: int,
        hint: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> tuple[RawSlide, ...]:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": conversation_system_prompt()},
                {"role": "user", "content": conversation_user_prompt(
                    window_text, first_turn, last_turn, min_slides, max_slides, hint)},
            ],
            "format": conversation_schema(),
            "stream": True,
            # 關掉思考：qwen3.5 預設先想幾千個 token，偶爾整段都進了 thinking、content 是空的；
            # 這裡的工作是「照著那一段話改寫」，不需要推理，而且準不準由程式檢查把關
            "think": False,
            "options": {
                # 跟其他工作用同一個 num_ctx（呼叫端傳進來的）：Ollama 換 num_ctx 會重新載入模型
                "num_ctx": self._num_ctx,
                "temperature": 0.2,     # 要穩定、貼著原話，不要創意
            },
        }
        reply = ollama_summarizer._stream_chat(
            self._host, body, self._timeout, progress, is_cancelled, "整理對談重點中")
        return parse_conversation_response(reply)
