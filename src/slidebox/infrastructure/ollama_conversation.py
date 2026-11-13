"""ConversationSummarizer 的 Ollama 實作：把一段已標好說話者的對談寫成「重點」。

模組分成兩半，跟 ollama_summarizer 一樣：上半是純函式（提示、schema、回應解析），可以離線測試；
下半是 HTTP 呼叫，沿用 ollama_summarizer 的串流實作（取消、錯誤轉譯都在那裡）。

**模型在這裡看不到、也決定不了誰說了什麼。** 它只看到 `[T12] S1：…` 這樣的行和頁數範圍，
回傳「先照抄的一小段原文（quote）＋那段原文在第幾輪（turn）＋一句沒有主詞的重點」。
說話者由程式查出來：程式找出真正有這段原文的那一輪（usecases/conversation.py 的 validate_points），
說話者就是那一輪的說話者。turn 欄位在 schema 裡是只能從「可以引用的 turn」選的 enum
（其他聲音與無法判斷的段落在逐字稿裡標成 X，也不在 enum 裡），所以模型連引用 X 都做不到。

提示詞裡沒有頻道、影片說明與真實的姓名，也沒有「說話者」「講者」「主持人」這類角色字眼
（沒有就不會被模型學去用）。唯一的例外是範例與規則裡一個**虛構**的稱謂「王主任」（各出現一次），
用來教模型「逐字稿裡的人名、小名與職稱一個都不要寫進重點」：實測沒有這個例子時，約每十個重點就有一個
把逐字稿裡的稱呼抄進文字裡；有了之後剩下的由 conversation.detect_names 的檢查擋掉。
每條規則在檢查那一側的對應（哪些會被程式擋掉、哪些會改顯示原話）見 README 的〈每個重點怎麼核對〉。

提示詞、schema、下面的取樣設定（temperature、think）改了，要手動升
usecases/conversation.py 的 PROMPT_VERSION，否則存檔裡舊提示產生的摘要會被當成新提示的。
tests/slidebox/test_ollama_conversation.py 釘住這些內容的雜湊，改了沒升版本時會失敗。
這是人記得的那一道；另一道不靠人：fingerprint 把這個模組與 ollama_summarizer 的原始碼雜湊進摘要存檔的
指紋，提示詞、取樣設定或回應解析改了，舊的摘要自己就不會被沿用。
"""
from __future__ import annotations

import http.client
import json
import re
from collections.abc import Sequence
from typing import cast

from ..domain.entities import RawPoint, RawSlide
from ..domain.errors import SummarizerOutputInvalid, SummarizerUnavailable
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.code_fingerprint import code_fingerprint
from . import ollama_summarizer

# 決定「同樣的窗會得到什麼摘要」的程式：提示詞、schema、取樣設定、回應解析（本模組），
# 以及送出請求、讀串流的 ollama_summarizer。它們的原始碼就是這個摘要器的指紋
CODE_MODULES = (__name__, f"{__package__}.ollama_summarizer")

_SYSTEM = """你是對話整理助手。使用者會給你一段多人對話逐字稿的片段，請把它整理成幾頁投影片。

【逐字稿格式】
每一行是一輪發言，格式為：
[T12] S1：發言內容
- 方括號裡的 T 加數字（例如 T12）是這一輪發言的編號。
- S1、S2 是由聲音自動區分出來的聲音代號，不是姓名，也不是身分。
- X 表示聲音無法確定是誰的發言。X 的內容只能當背景閱讀，不可以引用，也不可以寫成重點。
- 逐字稿是語音辨識的結果，可能有錯字、口吃、贅詞與斷句錯誤，請讀懂意思再整理。

【輸出格式】
只輸出一個 JSON 物件，不要其他文字：
{"slides": [{"title": "頁面標題", "points": [{"quote": "原文", "turn": 整數, "text": "重點", "context": []}]}]}
- 每一頁有 title 與 points；points 是這一頁的重點，依時間順序排列，每一頁 3 到 4 個重點。
- 每個重點都要先從逐字稿裡「照抄」一小段原文放進 quote，再填這段原文所在那一輪的編號 turn，最後才寫 text。
- turn 只填數字（T12 就填 12），而且只能填使用者指定範圍內、且不是 X 的編號。
- 誰說的，由程式根據你引用的 turn 自己標上。你完全不必寫，也不要猜。
- title：這一頁的主題，繁體中文，15 字以內，不含姓名、身分或代號。

【quote 的寫法】
1. quote 只抄「一小段」：同一輪發言裡，兩個空格（或逗號）之間的一個子句，大約 8 到 20 個字，最長 25 個字。寧可短，不要長。
2. quote 必須是 turn 那一輪裡連續、逐字的一段：一個字都不可以改、不可以省略、不可以補字，中間不可以跳過任何字或子句。數字照原樣抄。
3. 不可以跨輪：quote 不可以把兩個 [T…] 行的話接在一起，也不可以向上一輪或下一輪借字。
4. 如果那一句有口誤和自我修正（例如「一共設了二十三，不對，二十四個車站」），不可以只抄修正後的字、把前面的字跳過。要嘛連修正一起照抄（「二十三，不對，二十四個車站」），要嘛改選同一輪裡沒有口誤的另一小段。
5. quote 要選最能支持這個重點的那一句，而且要是這一輪的人自己的主張。如果一句話的前半只是複述別人（「你說…」「你剛剛提到…」），quote 要從他自己的主張開始抄，不要抄複述的那一半。
6. 如果那一輪整輪都不到 8 個字，就整輪照抄。
7. 程式會檢查 quote 是不是真的出現在 turn 那一輪；對不上的重點會被丟掉。

【text 的寫法】
1. 一個重點只寫 quote 所在那一輪裡說的事。不要把兩輪以上的內容合在一個重點裡；同一輪說了好幾件事，就分成好幾個重點，各自抄一小段 quote。
2. text 的關鍵詞和數字都要直接取自 quote，不要換成同義詞，也不要加入 quote 裡沒有的內容。只把口吃、贅詞去掉，寫成沒有主詞的短句，35 字以內，例如「主張…」「說明…」「詢問…」「反對…」。
3. 只寫那一輪實際說的話，不補充、不推測、不改變意思。期間、單位和對象要照原話：原話說「每個月」就不能寫成「每日」，說「兩週後」就不能寫成「下週」。
4. 數字、單位與專有名詞，逐字照抄那一輪的寫法：逐字稿寫「1.2萬」就寫「1.2萬」，不要改成「一點二萬」；逐字稿寫「六百份」就寫「六百份」，不要改成「600份」。不要換算、不要加總、不要四捨五入。
5. 逐字稿裡的人名、小名與職稱（例如下面範例裡的「王主任」）只是稱呼，一個都不要寫進 text 或 title；也不要寫「某某說」「某某認為」。
6. 不用代名詞（他、她、它、他們、她們、對方、雙方）。逐字稿裡的「他們」如果指某一群人，就改寫成那群人（例如廠商、住戶）；指不出來就省略。
7. 不用第一人稱（我、我們、本席）。需要保留原話時，把原話逐字放在「」裡，而且整句必須出現在那一輪。
8. 問題和回答是不同人說的，不可以寫在同一個重點裡。問句要寫成「詢問…」「追問…」；回答的內容要從回答的那一輪抄 quote、填那一輪的編號。
9. 如果那一輪只是「對」「嗯」「好」「沒有」「是」這類很短的回答，quote 就抄那一個字，把它所回應的那一輪的編號放進 context，text 寫成回答的立場加上所回應的內容（「同意…」「不同意…」）。沒有實質內容的附和或語助詞，不要寫成重點。
10. 如果那一輪只是複述前面別人剛說過的話（照念對方的數字、確認「你是說…」），那不是這個人的主張，不要寫成重點；要寫就從原本說這件事的那一輪抄 quote、填那一輪的編號。
11. context 只在第 9 點的情況使用，其他情況一律給空陣列 []。

【範例】
逐字稿：
[T0] S1：各位好 今天談社區圖書館延長開放 王主任先請您說明
[T1] S2：謝謝 這季借閱人次是1.2萬 比去年多了三成 晚上七點以後的人 其實只占百分之八
[T2] S1：百分之八喔 那延長到九點划算嗎
[T3] S2：要看人力 延長兩小時 每個月要多編四十萬，不對，是四十二萬 我自己是覺得先試辦三個月
[T4] S1：所以你主張先試辦三個月 對嗎
[T5] S2：對
[T6] X：（聽不清楚）
[T7] S3：我反對 他們每次都說試辦 最後都變成常態 我們家長會要的是週末至少開到五點 問卷收了六百份 有七成贊成
輸出：
{"slides": [{"title": "延長開放的使用情形", "points": [{"quote": "這季借閱人次是1.2萬 比去年多了三成", "turn": 1, "text": "說明借閱人次1.2萬，比去年多三成", "context": []}, {"quote": "晚上七點以後的人 其實只占百分之八", "turn": 1, "text": "指出晚上七點以後的人只占百分之八", "context": []}, {"quote": "那延長到九點划算嗎", "turn": 2, "text": "詢問延長到九點是否划算", "context": []}, {"quote": "每個月要多編四十萬，不對，是四十二萬", "turn": 3, "text": "說明每個月要多編四十二萬", "context": []}]}, {"title": "試辦與週末開放", "points": [{"quote": "我自己是覺得先試辦三個月", "turn": 3, "text": "主張先試辦三個月", "context": []}, {"quote": "對", "turn": 5, "text": "同意先試辦三個月", "context": [4]}, {"quote": "他們每次都說試辦 最後都變成常態", "turn": 7, "text": "反對延長，指出試辦最後都變成常態", "context": []}, {"quote": "問卷收了六百份 有七成贊成", "turn": 7, "text": "指出問卷收了六百份，有七成贊成", "context": []}]}]}
"""


def conversation_system_prompt() -> str:
    return _SYSTEM


def conversation_user_prompt(window_text: str, first_turn: int, last_turn: int,
                             min_slides: int, max_slides: int, hint: str = "") -> str:
    """使用者訊息只有三樣東西：頁數範圍與可填的編號、（重試時）上一次被退回的原因、逐字稿的行。

    範圍的兩行用單一換行接起來，其後各段之間空一行；重試的原因單獨一段，夾在範圍與逐字稿之間。
    訊息以換行結尾，和實驗（attribution lab）實際送出的請求逐字相同：實驗量到的數字都是這個格式的。
    """
    parts = [
        f"這個片段的發言編號是 T{first_turn} 到 T{last_turn}，turn 只能填 {first_turn} 到 {last_turn} 之間的整數。\n"
        f"請整理成 {min_slides} 到 {max_slides} 頁。",
    ]
    if hint:
        parts.append(hint)
    parts.append("以下是逐字稿：\n" + window_text)
    return "\n\n".join(parts) + "\n"


def conversation_schema(citable_turns: Sequence[int]) -> dict:
    """傳給 Ollama 的 format：文法層級約束。所有欄位都放進 required，小模型會省略選填欄位。

    欄位的順序就是模型輸出的順序：quote（先照抄原文）、turn、text、context。
    turn 是只能從 citable_turns 選的整數（enum，由小到大、去掉重複）：其他聲音與無法判斷的段落不在裡面。

    - 沒有任何可以引用的 turn：raise ValueError。空的 enum 是無解的文法，Ollama 會卡住或亂回；
      呼叫端（summarize_windows）不會把這種窗送過來，送來就是程式錯了，不要悄悄送出去。
    - 只有一個：enum 只有一個值，合法。
    - 上千個：照樣全部列進 enum。enum 只是讓取樣更準，不是把關的地方（validate_points 一樣會退回
      範圍外與不能引用的 turn），而一個窗的字數有上限（見 usecases/conversation.py），turn 數也有上限。
    """
    if any(isinstance(t, bool) or not isinstance(t, int) for t in citable_turns):     # 先查型別：True 會和 1 併成同一個
        raise ValueError("citable_turns 必須都是整數的 turn 編號")
    turns = sorted(set(citable_turns))
    if not turns:
        raise ValueError("citable_turns 是空的：這個窗沒有任何可以引用的 turn，不能送給模型")
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
                                    "quote": {"type": "string"},
                                    "turn": {"type": "integer", "enum": turns},
                                    "text": {"type": "string"},
                                    "context": {"type": "array", "items": {"type": "integer"}},
                                },
                                "required": ["quote", "turn", "text", "context"],
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
#
# 模型的回應是不可信的資料。這一層對任何輸入只有兩種結果：RawSlide，或 SummarizerOutputInvalid
# （summarize_windows 會帶著原因重問一次）。不會有第三種：沒接住的 RecursionError、ValueError
# 會讓整個工作以 traceback 當掉、也不重問。錯誤訊息裡也不放模型寫的東西，因為訊息會被抄進重問的提示。

_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
# 「12」「T12」「[T12]」這幾種寫法，前後可以有空白。每兩個 \s* 之間都隔著一個非空白的字元，
# 回溯才不會變成多次方：模型寫在 turn 裡的字串長度沒有上限
_TURN_REF_RE = re.compile(r"^\s*(?:\[\s*)?(?:T\s*)?(\d+)\s*(?:\]\s*)?$", re.IGNORECASE)
# 回應前後有客套話或 Markdown 圍欄時，最多從這麼多個「{」試著讀出 JSON
_MAX_BRACE_TRIES = 20


def _strip_thinking(text: str) -> str:
    """去掉所有 <think>…</think>（不分大小寫、可以跨行）；沒有收尾的 <think> 原樣留著。

    結果和 re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE) 一模一樣，但是線性的：
    那個寫法對每一個沒有收尾的 <think> 都掃到字串尾才放棄，兩萬個就是十幾億步。這裡第一個 <think>
    後面找不到 </think>，後面的 <think> 更找不到，直接收工。
    """
    kept: list[str] = []
    position = 0
    while True:
        opened = _THINK_OPEN_RE.search(text, position)
        closed = _THINK_CLOSE_RE.search(text, opened.end()) if opened else None
        if opened is None or closed is None:
            break
        kept.append(text[position:opened.start()])
        position = closed.end()
    kept.append(text[position:])
    return "".join(kept)


def _decode_at(decoder: json.JSONDecoder, text: str, start: int) -> object:
    """從 text[start] 讀一個 JSON 值；讀不了就回 None：語法錯、巢狀太深、整數位數超過上限都一樣。"""
    try:
        return decoder.raw_decode(text, start)[0]
    except (ValueError, RecursionError):          # JSONDecodeError 是 ValueError 的一種
        return None


def _load_json(payload: str) -> object:
    """模型的回應 → JSON。先照原樣讀；不行就去掉思考殘留，從每個「{」試著讀出一個有 slides 的物件。

    這樣 Markdown 圍欄、前後的客套話（包括裡面有大括號的）都不會讓整個窗白跑一次。
    讀不了的一律是 SummarizerOutputInvalid：json 對巢狀太深丟 RecursionError、對位數超過 4300 的整數
    丟普通的 ValueError（不是 JSONDecodeError），兩種都要接住。
    """
    if not isinstance(payload, str):
        raise SummarizerOutputInvalid("模型回應不是文字")
    if not payload.strip():
        raise SummarizerOutputInvalid("模型沒有回應任何內容")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        reason = str(e)                           # 只有出錯的位置，沒有模型寫的字
    except RecursionError:
        reason = "巢狀太深"
    except ValueError:
        reason = "裡面有位數超過上限的整數"
    cleaned = _strip_thinking(payload)
    decoder = json.JSONDecoder()
    start = cleaned.find("{")
    for _ in range(_MAX_BRACE_TRIES):
        if start < 0:
            break
        value = _decode_at(decoder, cleaned, start)
        if isinstance(value, dict) and "slides" in value:
            return value
        start = cleaned.find("{", start + 1)
    raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{reason}")


def _turn_ref(value: object) -> object:
    """模型有時寫 "12"、12.0、"T12"：這些明確是同一個編號，轉成整數。其他的原樣交給檢查去退回。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _TURN_REF_RE.match(value)
        if match:
            try:
                return int(match.group(1))
            except ValueError:                    # 位數超過 Python 轉換整數的上限：不是任何一輪的編號
                return value                      # 原樣留著，檢查會以「turn 必須是整數編號」退回
    return value


def _text(value: object) -> str:
    """模型給的文字欄位。不是文字、或是存不下的文字（孤立的代理碼位：模型在 JSON 裡寫了 \\ud800 之類，
    寫不成 UTF-8）都當作沒給，後面的檢查會以「空的」退回；不然它會一路走到存檔才以 UnicodeEncodeError 當掉。"""
    if not isinstance(value, str):
        return ""
    if not value.isascii():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return ""
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
    文字欄位不是文字、或是寫不成 UTF-8 的文字，也算格式不對（見 _text）。
    """
    data = _load_json(payload)
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise SummarizerOutputInvalid("模型回應裡沒有 slides 陣列")
    slides: list[RawSlide] = []
    for raw in data["slides"]:
        if not isinstance(raw, dict):
            slides.append(RawSlide(title="", points=()))
            continue
        points = raw.get("points")
        slides.append(RawSlide(
            title=_text(raw.get("title")),
            points=tuple(_parse_point(p) for p in points) if isinstance(points, list) else (),
        ))
    return tuple(slides)


def _parse_point(raw: object) -> RawPoint:
    if not isinstance(raw, dict):
        return RawPoint(turn=cast(int, None), text="")
    return RawPoint(
        turn=cast(int, _turn_ref(raw.get("turn"))),     # 不是整數的原值留給 validate_points 退回
        text=_text(raw.get("text")),
        context=_context_refs(raw.get("context")),
        quote=_text(raw.get("quote")),                  # 缺了或不是文字就是空的：validate_points 以 bad_quote 丟掉
    )


# --- HTTP ---


class _CallerError(Exception):
    """呼叫端的 progress 或 is_cancelled 自己出了錯：包起來穿過 _stream_chat，出來再原樣丟回去。
    那是程式的錯，不是模型的回應壞掉，不能被當成「模型輸出無效」而重問。"""

    def __init__(self, error: Exception):
        super().__init__(type(error).__name__)
        self.error = error


# _stream_chat 與單人模式共用，只接 JSONDecodeError 與 OSError，這裡不改它。串流裡的壞資料會讓它丟出沒人接的
# 例外：不是合法 UTF-8 的一行（UnicodeDecodeError）、巢狀太深（RecursionError）、整數位數超過上限（ValueError）、
# 不是物件的事件或訊息（AttributeError）、不是文字的內容（TypeError）
_BAD_STREAM_DATA = (UnicodeError, RecursionError, ValueError, AttributeError, TypeError)


def _read_reply(host: str, body: dict, timeout: float, progress: ProgressCallback,
                is_cancelled: CancelCheck | None) -> str:
    """呼叫 /api/chat、把串流拼成模型的回應。串流裡的壞資料一律是 SummarizerOutputInvalid
    （訊息只有例外的種類，沒有模型寫的字），summarize_windows 會重問一次。

    不是「模型的回應壞了」的錯不算在內：呼叫端自己的 progress／is_cancelled 出錯（程式的錯）、連線之前的錯
    （網址寫錯之類，設定的問題）都原樣往上丟；連線在串流中被截斷（http.client 的 IncompleteRead 之類，
    _stream_chat 只接了 OSError）和連線被重置一樣，是 SummarizerUnavailable。
    """
    reading = False
    failed: Exception | None = None

    def guarded(call, *args):
        try:
            return call(*args)
        except _BAD_STREAM_DATA as e:
            raise _CallerError(e) from e

    def cancelled() -> bool:
        nonlocal reading
        reading = True        # _stream_chat 每收到一行、讀它之前問一次：問過了，就是已經在讀串流
        return is_cancelled is not None and guarded(is_cancelled)

    try:
        return ollama_summarizer._stream_chat(
            host, body, timeout, lambda frac, status: guarded(progress, frac, status), cancelled,
            "整理對談重點中")
    except _CallerError as e:
        failed = e.error                      # 在 except 外面丟：不帶「During handling of…」的包裝
    except _BAD_STREAM_DATA as e:
        if not reading:
            raise
        raise SummarizerOutputInvalid(f"模型回應的串流資料壞了（{type(e).__name__}）") from e
    except http.client.HTTPException as e:
        raise SummarizerUnavailable(
            f"與 Ollama（{host}）的連線異常中斷，收到的回應不完整：{type(e).__name__}") from e
    raise failed


class OllamaConversationSummarizer:
    """一次一個窗。輸入是幾千字的對談，輸出是幾百字的 JSON，逾時沿用摘要的 10 分鐘。"""

    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 600.0):
        self._host = host.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._timeout = timeout

    @property
    def fingerprint(self) -> str:
        """這一版摘要程式的指紋（CODE_MODULES 的原始碼）。主機與模型不算在內：模型在存檔指紋的別的欄位，
        主機只是連到哪裡。讀不到原始碼時指紋對不上任何舊存檔（見 usecases/code_fingerprint.py）。"""
        return code_fingerprint(CODE_MODULES)

    def summarize_window(
        self,
        window_text: str,
        first_turn: int,
        last_turn: int,
        citable_turns: Sequence[int],
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
            "format": conversation_schema(citable_turns),
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
        return parse_conversation_response(
            _read_reply(self._host, body, self._timeout, progress, is_cancelled))
