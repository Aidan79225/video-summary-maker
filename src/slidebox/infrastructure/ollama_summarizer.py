"""Summarizer 與 ModelCatalog 的 Ollama 實作。

模組刻意分成兩半：本檔上半是純函式（schema 與回應解析），完全可離線
測試；下半是 HTTP 呼叫，無自動化測試。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..domain.entities import Slide
from ..domain.errors import SummarizerOutputInvalid, SummarizerUnavailable
from ..domain.ports import ProgressCallback

# 傳給 Ollama 的 format：文法層級約束，格式錯誤幾乎不可能發生
SLIDES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "timestamp": {"type": "number"},
                },
                "required": ["title", "bullets", "timestamp"],
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
        slides.append(Slide(
            index=i,
            title=title if isinstance(title, str) else "",
            bullets=_to_bullets(raw.get("bullets")),
            timestamp=_to_float(raw.get("timestamp")),
        ))
    return tuple(slides)


# --- 提示 ---

_SYSTEM = """你是影片摘要助手。使用者會給你一支影片的字幕，每一行的格式是
`[秒數] 內容`，方括號裡是該段開始的秒數。

請把整支影片切成數個章節，每個章節產出一頁投影片，包含：
- title：該章節的標題，繁體中文，不超過 20 字
- bullets：2 到 4 條重點，每條繁體中文、不超過 40 字
- timestamp：該章節開始的秒數。**必須直接使用字幕行方括號裡出現過的數字**，不要自己計算。

章節要平均涵蓋整支影片，不要全部集中在開頭。只輸出 JSON。"""


def _build_user_prompt(compressed: str, duration: float, min_slides: int,
                       max_slides: int, hint: str) -> str:
    parts = [
        f"影片總長度：{int(duration)} 秒。",
        f"請產出 {min_slides} 到 {max_slides} 頁投影片。",
    ]
    if hint:
        parts.append(hint)
    parts.append("以下是字幕：\n" + compressed)
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


class OllamaSummarizer:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 600.0):
        self._host = host.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._timeout = timeout

    def summarize(self, compressed, duration, min_slides, max_slides, hint,
                  progress: ProgressCallback) -> tuple[Slide, ...]:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _build_user_prompt(
                    compressed, duration, min_slides, max_slides, hint)},
            ],
            "format": SLIDES_SCHEMA,
            "stream": True,
            "options": {
                # 必須顯式設定：Ollama 預設 context 很小，不設會讓長字幕被
                # 靜默截斷，症狀是「摘要只涵蓋影片前十分鐘」，極難察覺。
                "num_ctx": self._num_ctx,
                "temperature": 0.3,     # 摘要要穩定，不要創意
            },
        }
        chunks: list[str] = []
        try:
            with _post_json(f"{self._host}/api/chat", body, self._timeout) as resp:
                for line in resp:
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
                        progress(None, f"產生摘要中…（{len(''.join(chunks))} 字）")
                    if event.get("done"):
                        break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code == 404:
                raise SummarizerUnavailable(
                    f"找不到模型 {self._model}，請先執行：ollama pull {self._model}"
                ) from e
            raise SummarizerUnavailable(f"Ollama 回應錯誤 {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise SummarizerUnavailable(
                f"連不上 Ollama（{self._host}），請確認它正在執行。原因：{e.reason}"
            ) from e

        return parse_summary_response("".join(chunks))


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
