"""追問判斷的純邏輯：給模型的 schema 與提示詞、以及解析模型的回應。

不碰網路、不碰 LLM，所以整套規則能離線測試；呼叫 Ollama 的那一半在
infrastructure/ollama_summarizer.py。

模型只回答一件事：後來那篇有沒有再提先前那項要求的同一件事，有的話抄一句
引用。哪些要求算、哪篇是候選、逐字稿取哪一段、比率怎麼算，都是新聞服務的
程式決定；引用是不是真的在逐字稿裡，也由新聞服務檢查（它手上有整份逐字稿）。
"""
from __future__ import annotations

import hashlib
import json

from ..domain.entities import FollowUpPair
from ..domain.errors import SummarizerOutputInvalid

# 提示詞的版本，跟模型名稱一起寫進每一筆成品。新聞服務只採用「評估通過的那個
# 判斷器」判出來的結果，版本號不變的話，改過的判斷器判出來的結果會被當成評估
# 過的。提示的文字與 schema 由下面的指紋自動涵蓋；指紋看不到的改動——解析規則
# （例如沒有追問時引用怎麼處理）、溫度、思考開關——一定要升這個版。
PROMPT_VERSION = "followup-v1"

# 同 topics：只用來分辨同一個模型、同一版提示詞有沒有被改過，八個字夠了。
_FINGERPRINT_LENGTH = 8

# 沒有內容的段落明講「沒有」，不留白：空白的段落模型會當成格式錯了，或自己腦補。
_NO_RESPONSE = "（當場沒有回應）"
_NO_EXCERPT = "（沒有逐字稿）"


def followup_schema() -> dict:
    """傳給 Ollama 的 format。

    兩個欄位都放進 required：小模型會省略選填欄位，「沒有引用」要它明講空字串。
    判斷排在引用前面：模型照 properties 的順序生成，先決定有沒有追問，再去
    逐字稿找證據——反過來的話，它會先抄一句、再為了那句硬說有追問。
    """
    return {
        "type": "object",
        "properties": {
            "followed_up": {"type": "boolean"},
            "quote": {"type": "string"},
        },
        "required": ["followed_up", "quote"],
    }


_SYSTEM = """你是議會新聞的查核編輯。使用者會給你一位民意代表先前在質詢中提出的一項要求
（與官員當時的回應），以及同一個人後來另一次發言的摘要卡和逐字稿片段。
請判斷後來這次發言有沒有再追問先前那件事，只輸出 JSON。

- followed_up：後來的發言再次提到**同一件具體的事**——同一個要求、同一個案子、
  同一筆預算或同一個計畫——才填 true，例如追問進度、質疑沒有做到、要求再說明。
  只是同一個領域的另一件事不算（例如先前要求補助長照人力，這次問的是長照據點
  怎麼設），填 false。逐字稿片段裡找不到能證明的那句話，也填 false。
- quote：followed_up 為 true 時，從「逐字稿片段」裡原封不動抄一句證明他再提這件事
  的話，不可改寫、不可摘要、不可從摘要卡或先前的要求抄；followed_up 為 false 時
  填空字串。
"""

_USER = """【先前的要求】
{request}

【官員當時的回應】
{response}

【後來這次發言的摘要卡】
{card}

【後來這次發言的逐字稿片段】
{excerpt}"""


def followup_system_prompt() -> str:
    return _SYSTEM


def followup_user_prompt(pair: FollowUpPair) -> str:
    """舊的要求與新的發言分段標明：分不出來的話，模型會從舊的要求裡抄引用。"""
    return _USER.format(
        request=pair.request.strip(),
        response=pair.response.strip() or _NO_RESPONSE,
        card=pair.card.strip(),
        excerpt=pair.excerpt.strip() or _NO_EXCERPT,
    )


def prompt_fingerprint() -> str:
    """模型實際看到的提示（系統提示、使用者訊息的範本、schema）的指紋。

    改了提示詞卻忘了升 PROMPT_VERSION 是最容易發生的事；指紋直接從提示算，
    任何一個字變了名稱就跟著變，不靠人記得。使用者訊息用空的一對來算：要的是
    範本，不是這一對的內容——內容算進去的話每一對都是不同的判斷器，評估通過
    的名稱永遠對不上每晚判出來的結果。
    """
    material = json.dumps(
        {
            "system": followup_system_prompt(),
            "user": followup_user_prompt(FollowUpPair("", "", "", "")),
            "schema": followup_schema(),
        },
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


def classifier_name(model: str) -> str:
    """成品上的判斷器名稱：「模型#提示詞版本#提示指紋」。

    三段任一個變了就是不同的判斷器，新聞服務要重新評估通過才會採用它判的結果。
    版本字串以 followup- 開頭，跟議題分類器的名稱永遠不會撞在一起。
    """
    return f"{model}#{PROMPT_VERSION}#{prompt_fingerprint()}"


def parse_followup_response(payload: str) -> tuple[bool, str]:
    """模型回應 → (有沒有追問, 引用)。

    判斷不是布林值就 raise：判斷就是這一個欄位，把 "true"、1 解讀成有追問等於
    替模型做決定。schema 已經把它鎖成布林值，會走到這裡表示格式整個壞了。

    沒有追問時引用一律清空：模型偶爾照抄一句，留著的話「沒有追問」旁邊會掛著
    一句看起來像證據的話。有追問卻沒有引用則照實回傳、不改判：新聞服務的落地
    檢查會把它記成 ungrounded，那是評估需要看到的訊號。
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SummarizerOutputInvalid("模型回應不是物件")

    followed_up = data.get("followed_up")
    if not isinstance(followed_up, bool):
        raise SummarizerOutputInvalid(f"followed_up「{followed_up}」不是布林值")
    if not followed_up:
        return False, ""
    quote = data.get("quote")
    return True, quote.strip() if isinstance(quote, str) else ""
