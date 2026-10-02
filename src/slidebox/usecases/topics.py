"""議題分類的純邏輯：給模型的 schema 與提示詞、以及把模型回應對回領域代碼。

不碰網路、不碰 LLM，所以整套規則能離線測試；呼叫 Ollama 的那一半在
infrastructure/ollama_summarizer.py。

模型只看得到領域的名稱，看不到代碼：名稱選完再由這裡換回代碼。模型只負責
「選一個」，規則（次領域等於主領域就當沒有、清單外的主領域就失敗）都在這裡，
這樣換模型時規則不會跟著變。
"""
from __future__ import annotations

import json
from collections.abc import Sequence

from ..domain.entities import TopicLabel
from ..domain.errors import SummarizerOutputInvalid

# 提示詞的版本，跟模型名稱一起寫進每一筆成品。改了提示詞或 schema 就要升版：
# 新聞服務只採用「評估通過的那個版本」分出來的結果，版本號不變的話，換過
# 提示詞的結果會被當成評估過的，混進網站上的指標。
PROMPT_VERSION = "topic-v1"


def classifier_name(model: str) -> str:
    """成品上的分類器名稱：模型與提示詞任一個變了，就是不同的分類器。"""
    return f"{model}#{PROMPT_VERSION}"


def topic_schema(labels: Sequence[TopicLabel]) -> dict:
    """傳給 Ollama 的 format：主領域只能是清單裡的名稱，次領域是名稱或 null。

    兩個欄位都放進 required：小模型會省略選填欄位，「沒有次領域」要它明講
    null，而不是漏寫之後由我們解讀。
    """
    names = [label.label for label in labels]
    return {
        "type": "object",
        "properties": {
            "primary": {"type": "string", "enum": names},
            # null 直接放進 enum，不寫成 type: ["string", "null"]：llama.cpp 把
            # schema 轉成文法時，type 是陣列就走「型別聯集」那條路、enum 被忽略，
            # 次領域就能是清單外的任意字串。
            "secondary": {"enum": [*names, None]},
        },
        "required": ["primary", "secondary"],
    }


_SYSTEM = """你是議會新聞的分類編輯。使用者會給你一段質詢的摘要（一句話與各段小標），
請把它分到下面固定的政策領域，只輸出 JSON。

可選的領域（名稱：涵蓋範圍）：
{labels}

- primary：依「這段質詢主要在問什麼」選一個主領域，只能填上面清單裡的名稱，
  不可自創。
- secondary：只有第二個領域也占了相當篇幅才填，只是順帶一提的不算，沒有就填 null。
  secondary 不能跟 primary 相同。
"""


def _label_line(label: TopicLabel) -> str:
    return f"- {label.label}：{label.description}" if label.description \
        else f"- {label.label}"


def topic_system_prompt(labels: Sequence[TopicLabel]) -> str:
    """只列名稱與說明，不列代碼：模型看到代碼就可能回代碼，而那會被當成失敗。"""
    return _SYSTEM.format(labels="\n".join(_label_line(label) for label in labels))


def topic_user_prompt(text: str) -> str:
    return "以下是要分類的質詢摘要：\n\n" + text.strip()


def _name(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_topic_response(payload: str,
                         labels: Sequence[TopicLabel]) -> tuple[str, str | None]:
    """模型回應 → (主領域代碼, 次領域代碼或 None)。

    主領域對不回清單就 raise：主領域是整個指標的依據，對不上時挑「最接近的」
    等於替模型做決定。次領域不進任何指標，對不上就當成沒有——為它讓工作
    失敗，這篇會每晚重送、每晚失敗，永遠進不了基礎文章。
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SummarizerOutputInvalid("模型回應不是物件")

    key_of = {label.label.strip(): label.key for label in labels}
    primary = key_of.get(_name(data.get("primary")))
    if primary is None:
        raise SummarizerOutputInvalid(f"主領域「{data.get('primary')}」不在清單裡")
    secondary = key_of.get(_name(data.get("secondary")))
    # 次領域的意思是「另一個」也占了相當篇幅的領域。提示詞要求不能相同，但
    # 模型偶爾照填，那等於它沒有找到第二個領域。
    return primary, (secondary if secondary != primary else None)
