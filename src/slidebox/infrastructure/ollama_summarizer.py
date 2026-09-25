"""Summarizer 與 ModelCatalog 的 Ollama 實作。

模組刻意分成兩半：本檔上半是純函式（schema 與回應解析），完全可離線
測試；下半是 HTTP 呼叫，無自動化測試。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..domain.entities import Ask, Brief, KeyNumber, Slide
from ..domain.errors import (
    OperationCancelled,
    SummarizerOutputInvalid,
    SummarizerUnavailable,
)
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.brief import slides_digest

# 傳給 Ollama 的 format：文法層級約束，格式錯誤幾乎不可能發生


def slides_schema(detailed: bool) -> dict:
    """詳細模式多一個 detail 欄位，而且放進 required。

    只加進 properties 不放 required 等於沒加：小模型會直接省略選填欄位，
    使用者勾了「詳細內容」卻拿到跟一般模式一樣的成品，還完全看不出原因。
    """
    properties: dict = {
        "title": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "timestamp": {"type": "number"},
    }
    required = ["title", "bullets", "timestamp"]
    if detailed:
        properties["detail"] = {"type": "string"}
        required.append("detail")
    return {
        "type": "object",
        "properties": {
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        },
        "required": ["slides"],
    }


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _to_bullets(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if item is None:
            continue
        text = item if isinstance(item, str) else str(item)
        if text.strip():
            out.append(text)
    return tuple(out)


def parse_summary_response(payload: str) -> tuple[Slide, ...]:
    """把模型回應的 JSON 字串轉成 Slide 序列。

    只有「連 slides 陣列都拿不到」才 raise；個別欄位缺失一律降級處理，
    是否要重試交給 validate_slides 決定——這樣重試的判準只有一處。
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise SummarizerOutputInvalid("模型回應裡沒有 slides 陣列")

    slides: list[Slide] = []
    for i, raw in enumerate(data["slides"], start=1):
        if not isinstance(raw, dict):
            continue
        title = raw.get("title")
        detail = raw.get("detail")
        slides.append(Slide(
            index=i,
            title=title if isinstance(title, str) else "",
            bullets=_to_bullets(raw.get("bullets")),
            timestamp=_to_float(raw.get("timestamp")),
            detail=detail if isinstance(detail, str) else "",
        ))
    return tuple(slides)


def brief_schema() -> dict:
    """質詢卡的 JSON 結構。每個欄位都放進 required：小模型會省略選填欄位。"""
    return {
        "type": "object",
        "properties": {
            "one_liner": {"type": "string"},
            "key_numbers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "unit": {"type": "string"},
                        "label": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["value", "unit", "label", "quote"],
                },
            },
            "asks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string"},
                        "deadline": {"type": "string"},
                        "response": {"type": "string"},
                    },
                    "required": ["request", "deadline", "response"],
                },
            },
        },
        "required": ["one_liner", "key_numbers", "asks"],
    }


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def parse_brief_response(payload: str) -> Brief:
    """模型回應 → Brief。只有「連物件都不是」才 raise；欄位缺失降級成空。

    落地檢查（數字是否真的在逐字稿裡）不在這裡做——那是 usecases.brief
    的責任，這裡只把 JSON 變成實體。
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SummarizerOutputInvalid("模型回應不是物件")
    numbers = tuple(
        KeyNumber(value=_str(n.get("value")), unit=_str(n.get("unit")),
                  label=_str(n.get("label")), quote=_str(n.get("quote")))
        for n in (data.get("key_numbers") or []) if isinstance(n, dict)
    )
    asks = tuple(
        Ask(request=_str(a.get("request")), deadline=_str(a.get("deadline")),
            response=_str(a.get("response")))
        for a in (data.get("asks") or []) if isinstance(a, dict)
    )
    return Brief(one_liner=_str(data.get("one_liner")), key_numbers=numbers, asks=asks)


# --- 提示 ---

_SYSTEM = """你是影片摘要助手。使用者會給你一支影片的字幕，每一行的格式是
`[秒數] 內容`，方括號裡是該段開始的秒數。

所有輸出一律使用繁體中文。影片若是外語，翻譯成繁體中文再寫，不要照抄原文。

請把整支影片切成數個章節，每個章節產出一頁投影片，包含：
- title：該章節的標題，繁體中文，不超過 20 字
- bullets：2 到 4 條重點，每條繁體中文、不超過 40 字
- timestamp：該章節開始的秒數。**必須直接使用字幕行方括號裡出現過的數字**，不要自己計算。
"""

# 詳細模式追加的欄位說明。字數上限刻意壓在 300 字：15 頁 × 300 字加上原本的
# 字幕，總量才不會逼近 num_ctx——超出時 Ollama 會從頭截斷提示，症狀是摘要
# 只涵蓋影片後半段，而且沒有任何錯誤訊息。
_DETAIL_FIELD = """- detail：該章節的完整敘述，**必須用繁體中文書寫**，150 到 300 字。把這段影片
  實際說了什麼完整寫出來，包含舉的例子、提到的數字與得到的結論，讓沒看過影片的
  人只讀這段也能完全理解。不要只是把 bullets 換句話說，也不要加入字幕裡沒有的
  內容。**嚴禁照抄字幕原文**：字幕若是日文、韓文、英文或其他語言，一律翻譯成
  繁體中文後再寫。
"""

_TAIL = '\n章節要平均涵蓋整支影片，不要全部集中在開頭。只輸出 JSON。'


def _system(detailed: bool) -> str:
    return _SYSTEM + (_DETAIL_FIELD if detailed else "") + _TAIL


def _build_user_prompt(compressed: str, duration: float, min_slides: int,
                       max_slides: int, hint: str, detailed: bool = False) -> str:
    parts = [
        f"影片總長度：{int(duration)} 秒。",
        f"請產出 {min_slides} 到 {max_slides} 頁投影片。",
    ]
    if detailed:
        parts.append("每一頁都必須包含 detail 欄位。")
    if hint:
        parts.append(hint)
    parts.append("以下是字幕：\n" + compressed)
    return "\n\n".join(parts)


_BRIEF_SYSTEM = """你是新聞編輯。使用者會給你一段發言（通常是立法委員的質詢）的分段摘要，
請寫出一張「摘要卡」，讓讀者在十秒內知道講者要什麼。一律使用繁體中文，只輸出 JSON：

- one_liner：一句話（40 字以內）講清楚講者指出的問題與提出的要求，不要用「本段」「影片」開頭。
- key_numbers：最重要的關鍵數字，依重要性排序，最多 4 個。每個包含：
  - value：只能是阿拉伯數字（例如「82.4」「1860」「18」），不含單位、不含逗號
  - unit：單位（例如「億元」「架」「%」「件」），沒有就空字串
  - label：這個數字是什麼，10 字以內（例如「三年累計編列」）
  - quote：摘要裡**原封不動**出現這個數字的那一句話。不可改寫、不可自己補數字；
    摘要裡沒有明確數字的事就不要列
- asks：講者提出的要求或行動，最多 4 項，每項包含：
  - request：要求什麼，30 字以內
  - deadline：講者說的期限（例如「一個月內」「本會期結束前」），沒有就空字串
  - response：對方（官員、部會）當場的回應，20 字以內，沒有回應就空字串
"""


def _build_brief_prompt(digest: str, hint: str = "") -> str:
    parts = ["以下是分段摘要："]
    if hint:
        parts.insert(0, hint)
    parts.append(digest)
    return "\n\n".join(parts)


# --- HTTP ---


def _post_json(url: str, body: dict, timeout: float):
    """送出 JSON POST，回傳可逐行迭代的 response 物件。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _stream_chat(host: str, body: dict, timeout: float, progress: ProgressCallback,
                 is_cancelled: CancelCheck | None, label: str) -> str:
    """呼叫 /api/chat 並把串流的回應拼成一個字串。摘要與質詢卡共用。"""
    chunks: list[str] = []
    try:
        with _post_json(f"{host}/api/chat", body, timeout) as resp:
            for line in resp:
                # 生成是整條 pipeline 最久的一步；只在開始前檢查一次
                # 等於不能取消。離開 with 會關掉連線，Ollama 那端也會
                # 跟著停止生成。
                if is_cancelled is not None and is_cancelled():
                    raise OperationCancelled()
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("error"):
                    raise SummarizerUnavailable(str(event["error"]))
                piece = (event.get("message") or {}).get("content", "")
                if piece:
                    chunks.append(piece)
                    # 總長度未知，用不確定進度讓 UI 顯示忙碌狀態
                    progress(None, f"{label}…（{len(''.join(chunks))} 字）")
                if event.get("done"):
                    break
    except OperationCancelled:
        raise
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        if e.code == 404:
            raise SummarizerUnavailable(
                f"找不到模型 {body.get('model')}，請先執行：ollama pull {body.get('model')}"
            ) from e
        raise SummarizerUnavailable(f"Ollama 回應錯誤 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise SummarizerUnavailable(
            f"連不上 Ollama（{host}），請確認它正在執行。原因：{e.reason}"
        ) from e
    except (json.JSONDecodeError, OSError) as e:
        # urlopen 只會把「連線建立階段」的錯誤包成 URLError；進入串流
        # 迭代之後的讀取逾時、連線重置、或格式壞掉的 NDJSON 行都會以
        # 原始例外外拋。長時間生成中途斷線是常態，必須轉譯成同樣可
        # 行動的訊息，而不是讓 traceback 冒到 UI。
        raise SummarizerUnavailable(
            f"與 Ollama（{host}）的連線在生成途中中斷：{e}"
        ) from e
    return "".join(chunks)


class OllamaSummarizer:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 600.0):
        self._host = host.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._timeout = timeout

    def summarize(self, compressed, duration, min_slides, max_slides, hint,
                  progress: ProgressCallback, detailed: bool = False,
                  is_cancelled: CancelCheck | None = None) -> tuple[Slide, ...]:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _system(detailed)},
                {"role": "user", "content": _build_user_prompt(
                    compressed, duration, min_slides, max_slides, hint, detailed)},
            ],
            "format": slides_schema(detailed),
            "stream": True,
            "options": {
                # 必須顯式設定：Ollama 預設 context 很小，不設會讓長字幕被
                # 靜默截斷，症狀是「摘要只涵蓋影片前十分鐘」，極難察覺。
                "num_ctx": self._num_ctx,
                "temperature": 0.3,     # 摘要要穩定，不要創意
            },
        }
        text = _stream_chat(self._host, body, self._timeout, progress, is_cancelled,
                            "產生摘要中")
        return parse_summary_response(text)


class OllamaBriefWriter:
    """從分段摘要寫質詢卡。輸入只有幾千字，所以逾時比摘要短得多。"""

    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 180.0):
        self._host = host.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._timeout = timeout

    def write(self, slides, progress: ProgressCallback,
              is_cancelled: CancelCheck | None = None, hint: str = "") -> Brief:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _BRIEF_SYSTEM},
                {"role": "user", "content": _build_brief_prompt(slides_digest(slides), hint)},
            ],
            "format": brief_schema(),
            "stream": True,
            "options": {"num_ctx": self._num_ctx, "temperature": 0.2},
        }
        text = _stream_chat(self._host, body, self._timeout, progress, is_cancelled,
                            "整理摘要卡中")
        return parse_brief_response(text)


class OllamaModelCatalog:
    def __init__(self, host: str, timeout: float = 5.0):
        self._host = host.rstrip("/")
        self._timeout = timeout

    def list_models(self) -> list[str]:
        """連不上時回傳空陣列——UI 下拉空著即可，不該擋住整個視窗。"""
        try:
            with urllib.request.urlopen(
                f"{self._host}/api/tags", timeout=self._timeout
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 任何失敗都降級成空清單
            return []
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        return sorted(
            m["name"] for m in models
            if isinstance(m, dict) and isinstance(m.get("name"), str)
        )
