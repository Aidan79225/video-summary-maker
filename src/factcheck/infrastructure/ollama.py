"""主張抽取器與文字判讀器的 Ollama 實作。

上半是純函式（schema、提示詞、回應解析），可離線測試；下半是 HTTP，透過
注入的 chat 函式呼叫，測試時換成假的。模型只做兩件事：挑出主張、判讀文字。
它不給分數，也不判斷數字對不對。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from ..domain.entities import ClaimKind, Evidence, ExtractedClaim, Judgement, Speech, Verdict
from ..domain.errors import ModelOutputInvalid, ModelUnavailable

MAX_CLAIMS = 8

Chat = Callable[[str, dict, float], str]

# 傳給 Ollama 的 format：文法層級約束，格式錯誤幾乎不可能發生
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "quote": {"type": "string"},
            "kind": {"type": "string", "enum": [k.value for k in ClaimKind]},
            "statement": {"type": "string"},
            "figures": {"type": "array", "items": {"type": "string"}},
            "law": {"type": "string"},
            "article": {"type": "string"},
            "bill_keywords": {"type": "string"},
            "proposer": {"type": "string"},
        },
        # 全部放進 required：小模型會直接省略選填欄位
        "required": ["quote", "kind", "statement", "figures", "law", "article",
                     "bill_keywords", "proposer"],
    }}},
    "required": ["claims"],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "contradicted", "insufficient"]},
        "evidence_quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "evidence_quote", "reason"],
}

_EXTRACT_SYSTEM = ("你是立法院的事實查核助理。你的工作是從委員的發言逐字稿中，挑出"
                   "「可以用官方法律條文或議案資料驗證」的具體事實陳述。你不判斷對錯。")

_EXTRACT_RULES = f"""規則：
1. 只挑三種可以查證的陳述：
   - law_article：關於現行法律條文的內容，例如罰鍰金額、刑期、期限。必須說得出是哪一部法律的第幾條；委員沒講條號也可以依內容推斷，推斷不出就不要挑。
   - bill_content：關於某個議案或草案的內容，例如某個版本編列多少預算、要把罰則提高到多少。
   - bill_status：關於議案的進度，例如已經三讀、還在委員會審查。
2. 不要挑意見、評價、預測、承諾、口號，也不要挑無法用法律或議案資料驗證的統計數字。
3. quote 必須從逐字稿逐字複製，連錯字、空白都照抄，不要修正、不要改寫、不要加標點。逐字稿由語音辨識產生，錯字很多，照抄即可。
4. figures：quote 裡跟這個主張有關的數字片段，逐字複製（例如「3萬到5萬」「6年2100億」）；沒有數字就給空陣列。同一句話如果同時講了現行規定和提案要改成多少，要拆成兩則主張，各自只放自己的數字。
5. statement：用一句話把主張講清楚，補上主詞（例如「醫療法現行對妨礙醫療業務者的罰鍰為3萬到5萬元」）。
6. law：法律全名（例如「醫療法」），不是 law_article 就給空字串。article：條號（例如「第106條」），不是 law_article 就給空字串。
   bill_keywords：議案名稱裡會出現的關鍵詞，越短越好（例如「無人載具」），可以用空白分隔多個；不是議案就給空字串。
   proposer：提案者（例如「行政院」「國民黨黨團」「民進黨黨團」「台灣民眾黨黨團」或委員姓名）；不知道就給空字串。
7. 最多 {MAX_CLAIMS} 則。沒有可查證的陳述就回傳空陣列。"""

_JUDGE_SYSTEM = "你是事實查核助理。你只根據提供的證據判斷，不使用自己的知識。"

_JUDGE_RULES = """請判斷證據是否支持這個主張：
- supported：證據明確支持主張
- contradicted：證據明確與主張衝突
- insufficient：證據不足以判斷（證據沒提到，或只提到一部分）
evidence_quote：從證據中逐字複製最關鍵的一句作為依據；insufficient 時給空字串。
reason：用一句話說明理由。"""

_VERDICTS = {"supported": Verdict.SUPPORTED, "contradicted": Verdict.CONTRADICTED}


def build_extract_messages(speech: Speech) -> list[dict]:
    user = (f"委員：{speech.speaker}\n日期：{speech.date.isoformat()}\n會議：{speech.meeting}\n\n"
            f"{_EXTRACT_RULES}\n\n逐字稿：\n{speech.transcript}")
    return [{"role": "system", "content": _EXTRACT_SYSTEM}, {"role": "user", "content": user}]


def build_judge_messages(statement: str, evidence: Sequence[Evidence]) -> list[dict]:
    blocks = "\n\n".join(f"[{i}] {e.title}\n{e.excerpt}" for i, e in enumerate(evidence, 1))
    user = f"主張：{statement}\n\n證據：\n{blocks}\n\n{_JUDGE_RULES}"
    return [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": user}]


def _text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _load(payload: str) -> dict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ModelOutputInvalid(f"模型回應不是 JSON：{payload[:120]}") from e
    if not isinstance(data, dict):
        raise ModelOutputInvalid("模型回應不是 JSON 物件")
    return data


def parse_claims(payload: str) -> list[ExtractedClaim]:
    items = _load(payload).get("claims")
    if not isinstance(items, list):
        raise ModelOutputInvalid("模型回應缺少 claims 陣列")
    claims: list[ExtractedClaim] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            kind = ClaimKind(item.get("kind"))
        except ValueError:
            continue
        quote = _text(item.get("quote"), 500)
        if not quote:
            continue
        raw_figures = item.get("figures") if isinstance(item.get("figures"), list) else []
        claims.append(ExtractedClaim(
            quote=quote,
            kind=kind,
            statement=_text(item.get("statement"), 300) or quote,
            figures=tuple(f for f in (_text(x, 60) for x in raw_figures) if f),
            law=_text(item.get("law"), 100),
            article=_text(item.get("article"), 30),
            bill_keywords=_text(item.get("bill_keywords"), 60),
            proposer=_text(item.get("proposer"), 60),
        ))
        if len(claims) >= MAX_CLAIMS:
            break
    return claims


def parse_judgement(payload: str) -> Judgement:
    data = _load(payload)
    return Judgement(
        verdict=_VERDICTS.get(str(data.get("verdict") or ""), Verdict.UNVERIFIABLE),
        evidence_quote=_text(data.get("evidence_quote"), 500),
        reason=_text(data.get("reason"), 300),
    )


# --- HTTP ---


def _chat(host: str, body: dict, timeout: float) -> str:
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise ModelUnavailable(f"Ollama 回應錯誤 {e.code}：{detail}") from e
    except (OSError, ValueError) as e:
        raise ModelUnavailable(f"連不上 Ollama（{host}）：{e}") from e
    if payload.get("error"):
        raise ModelUnavailable(str(payload["error"]))
    return str((payload.get("message") or {}).get("content") or "")


def _body(model: str, num_ctx: int, messages: list[dict], schema: dict) -> dict:
    return {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        # num_ctx 必須顯式設定：Ollama 預設 context 很小，長逐字稿會被靜默截斷。
        # temperature 0：同一段發言每次抽出的主張要一樣，否則重跑會換一批。
        "options": {"num_ctx": num_ctx, "temperature": 0},
    }


class OllamaClaimExtractor:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 600.0,
                 chat: Chat = _chat):
        self._host, self._model, self._num_ctx = host, model, num_ctx
        self._timeout = timeout
        self._chat = chat

    def extract(self, speech: Speech) -> list[ExtractedClaim]:
        body = _body(self._model, self._num_ctx, build_extract_messages(speech), EXTRACT_SCHEMA)
        return parse_claims(self._chat(self._host, body, self._timeout))


class OllamaTextJudge:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 300.0,
                 chat: Chat = _chat):
        self._host, self._model, self._num_ctx = host, model, num_ctx
        self._timeout = timeout
        self._chat = chat

    def judge(self, statement: str, evidence: Sequence[Evidence]) -> Judgement:
        body = _body(self._model, self._num_ctx, build_judge_messages(statement, evidence),
                     JUDGE_SCHEMA)
        return parse_judgement(self._chat(self._host, body, self._timeout))
