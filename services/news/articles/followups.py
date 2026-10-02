"""人物側寫第三步：追問率。期限換算、找追問的候選、送 GPU 判斷、落地檢查、狀態、標註集與評估、上線條件。

原則照 issue #24：模型只判斷「後來那篇有沒有再提同一件事」並附一句引用，引用必須在後來那篇
的逐字稿裡；比率由程式算；**判斷器沒通過人工標註集的門檻，就只給待追蹤清單、不給比率**。

幾件咬在一起的事（做法跟議題分布 topics.py 相同）：

- 要求（sync_followups）：基礎文章摘要卡裡帶期限的每一項 ask 是一筆 FollowUp，期限由程式換算
  成到期日（parse_deadline），換不出來就是 None。
- 判斷（check_followups）：同一個人後來的基礎文章先用字元雙字組篩出前 3 篇，再依重疊高到低送
  GPU 的 `followup` 工作，第一篇判定有追問就停；每一對只判斷一次，記在 FollowUp.checked。
- 落地檢查（grounded）：模型說有追問時，引用去掉空白與標點後至少 6 個字、而且出現在後來那篇
  的逐字稿裡，才算數。
- 狀態（state_for）：依日期與判斷結果決定 pending／watching／followed／not_followed。
- 評估與上線條件（evaluate、passing_evaluation）：每個判斷器只看自己最新的一次評估，
  準確率 ≥ 85% 而且至少 30 對才通過。追問率只算通過的那個判斷器判出來的。
"""
from __future__ import annotations

import calendar
import logging
import random
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .gpu_client import GpuApiClient, GpuApiError, JobFailed
from .members_sync import SPEAKER_SEPARATOR
from .models import (Article, ArticleSource, ArticleStatus, FollowUp, FollowUpEvaluation,
                     FollowUpLabel, Membership, Session, Slide)
from .topics import EvaluationAborted, base_articles

logger = logging.getLogger(__name__)

# 到期之後再看多久：觀察期。期限一過就說「沒追」太嚴，問政常常隔一陣子才回頭問
WATCH_DAYS = 90
# 程式篩完送判斷的候選數。全部送的話，一個常發言的人一項要求就是幾十個 GPU 工作
TOP_CANDIDATES = 3
# 上線門檻（issue #24：30 對、85%——誤判會把有追到底的人標成沒追，所以比議題分類嚴）
FOLLOWUP_MIN_LABELS = 30
FOLLOWUP_MIN_ACCURACY = 0.85
# 引用去掉空白與標點後至少幾個字：太短的「是的」「會」到處都找得到，等於沒有落地
MIN_QUOTE_CHARS = 6

# GPU 端 followup 工作各欄位的上限（字數）。超過的請求會被 422 拒絕，所以送出前先在這裡截
REQUEST_LIMIT = 500
RESPONSE_LIMIT = 500
CARD_LIMIT = 4000
EXCERPT_LIMIT = 1500

# 判斷只要幾秒；照摘要工作預設的 3 秒輪詢，一晚 200 對會多等十分鐘（同 topics）
_POLL_SECONDS = 1.0
# 開頭連續失敗幾個就停（同 topics.classify_topics）
_EARLY_FAILURES = 3
# 一次 UPDATE／DELETE 最多帶幾個 id。SQLite 的綁定參數有上限，舊版是 999
_ID_CHUNK = 500


class FollowUpState(StrEnum):
    """一項要求的狀態。協定的一部分（API 與網站都用這幾個字串）。"""
    PENDING = "pending"            # 待追蹤：還沒到期、也還沒找到追問
    WATCHING = "watching"          # 觀察中：已到期、在觀察期內、還沒找到追問
    FOLLOWED = "followed"          # 已追問
    NOT_FOLLOWED = "not_followed"  # 未追問：觀察期結束、沒有找到


# --- 期限換算 ---

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100}
_NUM = r"(\d+|[零〇一二兩三四五六七八九十百]+)"
# 「內」「之內」「以內」是同一個意思
_WITHIN = r"(?:之|以)?內"
_NUMERALS = "0-9零〇一二兩三四五六七八九十百"


def _to_int(token: str) -> int | None:
    """「30」「三十」「兩」「一百二十」「一百零五」→ 整數。不是一個數（「一兩」「兩三」）或是 0：None。

    「一兩個月」「兩三週」是講者自己也沒定的範圍，猜一個數字等於替他定期限，寧可換不出來。
    """
    if token.isdigit():
        return int(token) or None
    total, digit, last = 0, None, ""
    for ch in token:
        if ch in _CN_DIGITS:
            if digit is not None and last != "零":
                return None
            digit = _CN_DIGITS[ch]
        else:
            if digit is None and total:
                return None
            total += (1 if digit is None else digit) * _CN_UNITS[ch]
            digit = None
        last = ch
    return (total + (digit or 0)) or None


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(day: date, months: int) -> date:
    """同日、月底夾住：1 月 31 日加一個月是 2 月 28（或 29）日，不是 3 月 3 日。"""
    years, index = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class _Spoken:
    """換算期限需要的發言資訊。session_number 是立法院的第幾會期（議會與解析不出來的是 None）。"""
    day: date
    source: str
    session_number: int | None


def _year_written_before(text: str, match: re.Match) -> bool:
    """「明年3月底」「2027年10月1日」：有寫年就不是表上「沒寫年」的那一種，不猜是哪一年。"""
    return "年" in text[:match.start()]


def _calendar_day(match: re.Match, text: str, spoken: _Spoken) -> date | None:
    """X 月 X 日（沒寫年）：今年那一天，已經過了就是明年。"""
    if _year_written_before(text, match):
        return None
    month, day = _to_int(match.group(1)), _to_int(match.group(2))
    if month is None or day is None or not 1 <= month <= 12:
        return None
    for year in (spoken.day.year, spoken.day.year + 1):
        try:
            due = date(year, month, day)
        except ValueError:
            return None
        if due >= spoken.day:
            return due
    return None


def _calendar_month_end(match: re.Match, text: str, spoken: _Spoken) -> date | None:
    """X 月底（沒寫年）：今年那個月的最後一天，已經過了就是明年。"""
    if _year_written_before(text, match):
        return None
    month = _to_int(match.group(1))
    if month is None or not 1 <= month <= 12:
        return None
    due = _month_end(spoken.day.year, month)
    return due if due >= spoken.day else _month_end(spoken.day.year + 1, month)


def _days(n: int) -> Callable[[re.Match, str, _Spoken], date | None]:
    def rule(match: re.Match, text: str, spoken: _Spoken) -> date | None:
        count = _to_int(match.group(1))
        return spoken.day + timedelta(days=count * n) if count else None
    return rule


def _months(n: int) -> Callable[[re.Match, str, _Spoken], date | None]:
    def rule(match: re.Match, text: str, spoken: _Spoken) -> date | None:
        count = _to_int(match.group(1)) if match.groups() else 1
        return add_months(spoken.day, count * n) if count else None
    return rule


def _this_month_end(match: re.Match, text: str, spoken: _Spoken) -> date:
    return _month_end(spoken.day.year, spoken.day.month)


def _next_month_end(match: re.Match, text: str, spoken: _Spoken) -> date:
    following = add_months(spoken.day.replace(day=1), 1)
    return _month_end(following.year, following.month)


def _year_end(match: re.Match, text: str, spoken: _Spoken) -> date:
    return date(spoken.day.year, 12, 31)


def _session_end(match: re.Match, text: str, spoken: _Spoken) -> date | None:
    """立法院的本會期：單數會期 5 月 31 日、雙數會期 12 月 31 日（發言那年）。

    議會的會期起訖沒有結構化來源，不換算。「下會期」不是這一種。算出來比發言還早（延會或
    臨時會裡講的「本會期」）就是換不出來：到期日早於發言，等於還沒開始就過期了。
    """
    if spoken.source != ArticleSource.LY or spoken.session_number is None:
        return None
    if re.search(r"下一?個?會期", text):
        return None
    due = date(spoken.day.year, 5, 31) if spoken.session_number % 2 else date(spoken.day.year, 12, 31)
    return due if due >= spoken.day else None


# 依序試，第一個換得出來的就是答案。有數字的寫法排前面：「12月底」要先被當成「X 月底」，
# 才不會被後面的「月底前」吃掉。表的內容就是方法頁上公開的那張，改這裡要一起改方法頁。
_RULES: tuple[tuple[re.Pattern, Callable[[re.Match, str, _Spoken], date | None]], ...] = (
    (re.compile(rf"([{_NUMERALS}]+)月([{_NUMERALS}]+)[日號]"), _calendar_day),
    (re.compile(rf"([{_NUMERALS}]+)月底"), _calendar_month_end),
    (re.compile(_NUM + r"個?(?:天|日)" + _WITHIN), _days(1)),
    (re.compile(_NUM + r"個?(?:週|周|星期|禮拜)" + _WITHIN), _days(7)),
    (re.compile(_NUM + r"個?月" + _WITHIN), _months(1)),
    (re.compile(r"半年" + _WITHIN), _months(6)),
    (re.compile(_NUM + r"年" + _WITHIN), _months(12)),
    (re.compile(r"下個?月"), _next_month_end),
    (re.compile(rf"(?:本|這個?)月底|(?<![{_NUMERALS}個上下本這])月底"), _this_month_end),
    (re.compile(rf"今年年?底|今年內|(?<![{_NUMERALS}明後去隔前今年])年底"), _year_end),
    (re.compile(r"本會期|這個?會期|會期內|會期結束前"), _session_end),
)


def parse_deadline(text: str, spoken: date, source: str,
                   session_number: int | None = None) -> date | None:
    """期限原文 → 到期日。換不出來（「儘快」「下次」「預算審查前」、講者沒定的範圍）是 None。

    None 不計入追問率、也不進待追蹤清單：替講者猜一個到期日，就等於替他定了期限。
    全形數字與中文數字都讀得懂；空白不影響。
    """
    normalized = re.sub(r"\s+", "", (text or "").translate(_FULLWIDTH_DIGITS))
    if not normalized:
        return None
    context = _Spoken(spoken, str(source), session_number)
    for pattern, rule in _RULES:
        for match in pattern.finditer(normalized):
            due = rule(match, normalized, context)
            if due is not None:
                return due
    return None


_LY_SESSION_NUMBER_RE = re.compile(r"第\s*\d+\s*屆第\s*(\d+)\s*會期")


def ly_session_number(name: str) -> int | None:
    """「第11屆第5會期」（會期名稱或會議名稱）→ 5。解析不出來是 None。"""
    match = _LY_SESSION_NUMBER_RE.search((name or "").translate(_FULLWIDTH_DIGITS))
    return int(match.group(1)) if match else None


def window_end(due: date) -> date:
    """觀察期的最後一天：到期日 + 90 天。"""
    return due + timedelta(days=WATCH_DAYS)


# --- 摘要卡裡的要求 ---


@dataclass(frozen=True)
class Ask:
    # 在摘要卡 asks 裡的位置（從 0 起算）：FollowUp 的鍵是 (文章, 位置)
    index: int
    request: str
    deadline: str
    response: str


def _text(item: dict, key: str) -> str:
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def _all_asks(brief: object) -> Iterator[Ask]:
    """摘要卡的每一項要求（要求本身是空的不算）。卡片的形狀由 ingest.clean_brief 釘死，這裡仍然防禦。"""
    asks = brief.get("asks") if isinstance(brief, dict) else None
    if not isinstance(asks, list):
        return
    for index, item in enumerate(asks):
        if isinstance(item, dict) and _text(item, "request"):
            yield Ask(index, _text(item, "request"), _text(item, "deadline"), _text(item, "response"))


def asks_with_deadline(brief: object) -> list[Ask]:
    """帶期限的要求（`deadline.strip()` 非空）：追問率的對象，跟具體度的「帶期限的要求」同一批。"""
    return [ask for ask in _all_asks(brief) if ask.deadline]


def ask_at(brief: object, index: int) -> Ask | None:
    return next((ask for ask in _all_asks(brief) if ask.index == index), None)


def is_base(article: Article) -> bool:
    """跟 topics.base_articles 同一個定義：已完成、單獨發言、有摘要卡。"""
    return (article.status == ArticleStatus.READY and article.brief is not None
            and SPEAKER_SEPARATOR not in (article.speaker or ""))


# --- 字元雙字組 ---

_CJK_RUN = re.compile(r"[㐀-䶿一-鿿]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")
# 只要含這些字的雙字組都不算：「的預」「了解」這種組合在每一篇都有，重疊了也不代表講同一件事
_STOP_CHARS = frozenset("的了嗎呢吧啊喔呀哦之與及和或而並也")
# 質詢裡到處都是的詞：稱謂、客套、動詞與期限的套話。兩篇都有「部長」「檢討」不代表在講同一件事
_STOP_BIGRAMS = frozenset("""
我們 你們 他們 大家 這個 那個 這些 那些 這樣 那樣 什麼 怎麼 為什 是不 不是 可以 沒有 就是 因為 所以
如果 還是 已經 應該 一個 一下 一些 目前 現在 今天 剛剛 剛才 其實 然後 但是 可是 所謂 這邊 那邊 這裡
部長 院長 市長 局長 署長 主委 處長 次長 委員 議員 主席 政府 市府 縣府 行政 請問 謝謝 報告 說明 提出
要求 希望 相關 問題 部分 進行 處理 辦理 檢討 評估 研議 改善 加強 落實 儘快 盡快 盡速 儘速 立即 馬上
期限 月內 天內 週內 年內 以內 會期 底前 我想 知道 覺得 認為 表示 回應 答覆 書面 資料 提供 給本 本席
""".split())


def bigrams(text: str) -> frozenset[str]:
    """一段文字的字元雙字組（中文連續字之間）與英數字詞（「TPASS」「F16」），停用的除外。

    只在同一段連續的中文字裡取：標點兩邊的字不是一個詞。NFKC 把全形英數字轉成半形，「ＡＩ」
    跟「AI」才會是同一個詞。
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    grams: set[str] = set()
    for run in _CJK_RUN.findall(normalized):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _STOP_BIGRAMS and not _STOP_CHARS.intersection(gram):
                grams.add(gram)
    grams.update(word.lower() for word in _LATIN_WORD.findall(normalized))
    return frozenset(grams)


# --- 送給 GPU 的內容 ---


@dataclass(frozen=True)
class _CardParts:
    one_liner: str
    requests: tuple[str, ...]
    titles: tuple[str, ...]


def _card_parts(brief: object, titles: Iterable[str]) -> _CardParts:
    one_liner = _text(brief, "one_liner") if isinstance(brief, dict) else ""
    return _CardParts(one_liner, tuple(ask.request for ask in _all_asks(brief)),
                      tuple(t for t in (title.strip() for title in titles) if t))


def _slide_titles(article: Article) -> list[str]:
    # 用 .all() 而不是 order_by()：呼叫端 prefetch 過的話不必再查一次（Slide 預設照 index 排）
    return [slide.title for slide in article.slides.all()]


def card_for(article: Article) -> str:
    """後來那篇的摘要卡：一句話＋它自己的要求＋各段小標。超過上限時先捨小標、再捨要求。"""
    parts = _card_parts(article.brief, _slide_titles(article))
    requests, titles = list(parts.requests), list(parts.titles)
    while True:
        lines = [f"一句話：{parts.one_liner}"]
        if requests:
            lines += ["要求：", *(f"- {r}" for r in requests)]
        if titles:
            lines += ["各段小標：", *(f"- {t}" for t in titles)]
        text = "\n".join(lines)
        if len(text) <= CARD_LIMIT or not (requests or titles):
            return text[:CARD_LIMIT]
        (titles or requests).pop()


def excerpt_for(request: str, transcript: str) -> str:
    """逐字稿裡跟要求最相關的一段（≤ 1500 字）：含最多種要求雙字組的視窗。

    模型讀不完一整份逐字稿（議會一段一小時），只給一句話又判斷不了「有沒有講同一件事」。
    視窗用滑動的：要求的雙字組在逐字稿裡出現的位置排好，雙指標找出 1500 字內涵蓋最多種的
    那一段，再把視窗置中在那些位置上，前後的上下文才完整。一個都沒有就給開頭那一段。
    """
    if len(transcript) <= EXCERPT_LIMIT:
        return transcript
    wanted = bigrams(request)
    hits = [(i, transcript[i:i + 2]) for i in range(len(transcript) - 1)
            if transcript[i:i + 2] in wanted]
    if not hits:
        return transcript[:EXCERPT_LIMIT]
    counts: Counter = Counter()
    best, best_span, left = 0, (0, 0), 0
    for position, gram in hits:
        counts[gram] += 1
        # 視窗是 [hits[left], hits[left] + 1500)：雙字組整個要在裡面
        while position + 2 > hits[left][0] + EXCERPT_LIMIT:
            counts[hits[left][1]] -= 1
            if not counts[hits[left][1]]:
                del counts[hits[left][1]]
            left += 1
        if len(counts) > best:
            best, best_span = len(counts), (hits[left][0], position + 2)
    first, last = best_span
    start = max(0, min(first - (EXCERPT_LIMIT - (last - first)) // 2,
                       len(transcript) - EXCERPT_LIMIT))
    return transcript[start:start + EXCERPT_LIMIT]


@dataclass(frozen=True)
class PairInput:
    """送給 GPU 的一對：舊的要求與當時的回應、後來那篇的摘要卡與逐字稿片段。"""
    request: str
    response: str
    card: str
    excerpt: str


def pair_input(ask: Ask, candidate: Article) -> PairInput:
    return PairInput(ask.request[:REQUEST_LIMIT], ask.response[:RESPONSE_LIMIT],
                     card_for(candidate), excerpt_for(ask.request, candidate.transcript_text or ""))


# --- GPU 的成品與落地檢查 ---


class FollowUpField(StrEnum):
    """GPU 回傳的 followup 成品欄位。協定的一部分，Pi 上沒有 slidebox，各留一份。"""
    FOLLOWED_UP = "followed_up"
    QUOTE = "quote"
    CLASSIFIER = "classifier"


@dataclass(frozen=True)
class FollowUpResult:
    followed_up: bool
    # 沒有追問時是空字串
    quote: str
    # 模型＋提示詞版本（「qwen3:14b#followup-v1#1a2b3c4d」）
    classifier: str


def parse_result(result: object) -> FollowUpResult:
    """檢查 GPU 回來的判斷。followed_up 不是真假值、沒有判斷器名稱：當成這一對失敗，不猜。"""
    if not isinstance(result, dict):
        raise JobFailed("追問判斷的格式不符預期")
    followed_up = result.get(FollowUpField.FOLLOWED_UP)
    if not isinstance(followed_up, bool):
        raise JobFailed(f"追問判斷的 followed_up 不是真假值：{str(followed_up)[:50]!r}")
    quote = result.get(FollowUpField.QUOTE) or ""
    if not isinstance(quote, str):
        raise JobFailed("追問判斷的 quote 不是文字")
    classifier = result.get(FollowUpField.CLASSIFIER)
    if not isinstance(classifier, str) or not classifier.strip():
        raise JobFailed("追問判斷沒有判斷器名稱")
    return FollowUpResult(followed_up, quote.strip() if followed_up else "", classifier.strip())


def judge_pair(client: GpuApiClient, pair: PairInput, timeout: float) -> FollowUpResult:
    """送一個 followup 工作、等它跑完。錯誤契約同 GpuApiClient：只丟 GpuApiError 或 JobFailed。"""
    job_id = client.submit_followup(pair.request, pair.response, pair.card, pair.excerpt)
    return parse_result(client.wait(job_id, timeout=timeout, poll_seconds=_POLL_SECONDS))


def _squeeze(text: str) -> str:
    """去掉空白、標點與控制字元（換行、零寬字元）；全形轉半形。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text or "")
                   if unicodedata.category(ch)[0] not in "PZ"
                   and unicodedata.category(ch) not in ("Cc", "Cf"))


def grounded(quote: str, transcript: str) -> bool:
    """引用去掉空白與標點後至少 6 個字，而且出現在逐字稿裡（同樣正規化）。

    模型說「有追問」卻抄不出原文，多半是它自己編的——寧可當成沒有追問。
    """
    squeezed = _squeeze(quote)
    return len(squeezed) >= MIN_QUOTE_CHARS and squeezed in _squeeze(transcript)


# --- 狀態 ---


def state_for(due: date | None, followed_by: int | None, classifier: str,
              checked_at: datetime | None, today: date, judge: str | None) -> FollowUpState | None:
    """一項要求今天的狀態；None 是不給（期限換不出來，或要靠還沒通過的判斷才知道）。

    - 判斷算數的條件：有通過評估的判斷器（judge），而且這一項的判斷都出自它（classifier）；
      一個候選都沒有、從來不必判斷的（classifier 空字串）也算——那是程式篩出來的結果。
    - 判斷不算數時只給 pending：還沒到期是日期決定的；到期之後是不是追了，要靠判斷。
    - 觀察期過了，還要在觀察期結束**之後**確認過所有候選都判斷完（checked_at），才是未追問：
      還沒看完不能說沒有。
    """
    if due is None:
        return None
    trusted = judge is not None and classifier in ("", judge)
    if trusted and followed_by:
        return FollowUpState.FOLLOWED
    if today <= due:
        return FollowUpState.PENDING
    if not trusted:
        return None
    end = window_end(due)
    if today <= end or checked_at is None or timezone.localdate(checked_at) <= end:
        return FollowUpState.WATCHING
    return FollowUpState.NOT_FOLLOWED


def state_of(followup: FollowUp, today: date, judge: str | None) -> FollowUpState | None:
    return state_for(followup.due_date, followup.followed_by_id, followup.classifier,
                     followup.checked_at, today, judge)


# --- 候選 ---


@dataclass(frozen=True)
class Doc:
    """候選文章在記憶體裡的樣子：篩選只要日期、講者與雙字組，不必讀逐字稿。"""
    id: int
    source: str
    day: date
    speaker: str
    person_id: int | None
    grams: frozenset[str]


class CandidateIndex:
    """所有可能當候選的基礎文章，一次載入、在記憶體裡篩。

    Pi 的資料庫是 SD 卡上的 SQLite：每一項要求各查一次，一晚就是幾千次查詢。逐字稿不載入
    （一篇幾萬字），只有真的要送判斷的那幾篇才讀。
    """

    def __init__(self, docs: Iterable[Doc], person_of_membership: dict[int, int]):
        self._person_of = person_of_membership
        self._by_person: dict[tuple[str, int], list[Doc]] = defaultdict(list)
        self._by_speaker: dict[tuple[str, str], list[Doc]] = defaultdict(list)
        for doc in docs:
            if doc.person_id is not None:
                self._by_person[(doc.source, doc.person_id)].append(doc)
            self._by_speaker[(doc.source, doc.speaker)].append(doc)

    @classmethod
    def load(cls) -> CandidateIndex:
        # 沒有逐字稿就無從落地檢查：送了也只會是「沒有追問」，白白占掉一個判斷
        articles = base_articles().exclude(transcript_text="")
        titles: dict[int, list[str]] = defaultdict(list)
        for article_id, title in (Slide.objects.filter(article__in=articles)
                                  .order_by("article_id", "index").values_list("article_id", "title")):
            titles[article_id].append(title)
        docs = []
        for article_id, source, day, speaker, person_id, brief in articles.values_list(
                "id", "source", "date", "speaker", "membership__person_id", "brief").iterator():
            parts = _card_parts(brief, titles.get(article_id, ()))
            docs.append(Doc(article_id, source, day, speaker.strip(), person_id,
                            bigrams(" ".join([parts.one_liner, *parts.requests, *parts.titles]))))
        return cls(docs, dict(Membership.objects.values_list("id", "person_id")))

    def candidates(self, followup: FollowUp) -> list[Doc]:
        """一項要求的候選：前 3 篇，依雙字組重疊由高到低（同分取早的）。

        同一個人（任期對到同一個 Person；對不到任期的就看講者寫法相同）、同一個來源、
        日期在 (發言日, 到期日 + 90 天]、而且至少有 1 個雙字組重疊。發言之後、期限之前
        再提也算追問。期限換不出來的沒有觀察期，也就沒有候選。
        """
        source = followup.article
        if followup.due_date is None:
            return []
        pool = {doc.id: doc for doc in self._by_speaker.get((source.source, source.speaker.strip()), ())}
        person = self._person_of.get(source.membership_id)
        if person is not None:
            pool.update((doc.id, doc) for doc in self._by_person.get((source.source, person), ()))
        end = window_end(followup.due_date)
        wanted = bigrams(followup.request)
        scored = [(len(wanted & doc.grams), doc) for doc in pool.values()
                  if source.date < doc.day <= end]
        scored = sorted(((score, doc) for score, doc in scored if score >= 1),
                        key=lambda item: (-item[0], item[1].day, item[1].id))
        return [doc for _, doc in scored[:TOP_CANDIDATES]]


# --- 建立與更新要求 ---


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    removed: int = 0
    # 期限換不出來的（全部，不只這次新增的）：頁面另外列「期限寫法無法換算」的數量
    unparsed: int = 0

    def __str__(self) -> str:
        return (f"要求新增 {self.created}、更新 {self.updated}、移除 {self.removed}"
                f"（期限無法換算 {self.unparsed}）")


def _clear_judgments(followup: FollowUp) -> None:
    followup.checked = []
    followup.followed_by = None
    followup.quote = ""
    followup.classifier = ""
    followup.checked_at = None


_FOLLOWUP_FIELDS = ["request", "deadline_text", "due_date", "checked", "followed_by", "quote",
                    "classifier", "checked_at"]
_DEADLINE_TEXT_LIMIT = 300


def sync_followups() -> SyncReport:
    """替基礎文章帶期限的每一項要求建立／更新 FollowUp，不打模型。

    - 期限每次都重新換算：會期晚一點才掛上、或換算規則改了，到期日跟著更新。到期日變了，
      「觀察期結束後確認過」就不算數了（checked_at 清掉），下一輪重新確認。
    - 要求的文字變了（admin 改了摘要卡）：先前的判斷是對舊文字做的，全部清掉重判。
    - 文章已經不是基礎文章、或那一項不見了、期限被清空：刪掉。
    """
    report = SyncReport()
    existing = {(fu.article_id, fu.ask_index): fu for fu in FollowUp.objects.all()}
    wanted: dict[tuple[int, int], tuple[Ask, date | None]] = {}
    for article_id, source, day, brief, session_name, meeting in (
            base_articles().values_list("id", "source", "date", "brief", "session__name", "meeting")
            .iterator()):
        number = ly_session_number(session_name or meeting) if source == ArticleSource.LY else None
        for ask in asks_with_deadline(brief):
            wanted[(article_id, ask.index)] = (ask, parse_deadline(ask.deadline, day, source, number))

    created, changed = [], []
    for (article_id, index), (ask, due) in wanted.items():
        deadline = ask.deadline[:_DEADLINE_TEXT_LIMIT]
        report.unparsed += due is None
        fu = existing.get((article_id, index))
        if fu is None:
            created.append(FollowUp(article_id=article_id, ask_index=index, request=ask.request,
                                    deadline_text=deadline, due_date=due))
            continue
        dirty = False
        if fu.request != ask.request:
            _clear_judgments(fu)
            fu.request, dirty = ask.request, True
        if (fu.deadline_text, fu.due_date) != (deadline, due):
            fu.deadline_text, fu.due_date, fu.checked_at, dirty = deadline, due, None, True
        if dirty:
            changed.append(fu)
    gone = [fu.id for key, fu in existing.items() if key not in wanted]
    with transaction.atomic():
        FollowUp.objects.bulk_create(created)
        FollowUp.objects.bulk_update(changed, _FOLLOWUP_FIELDS)
        for start in range(0, len(gone), _ID_CHUNK):
            FollowUp.objects.filter(id__in=gone[start:start + _ID_CHUNK]).delete()
    report.created, report.updated, report.removed = len(created), len(changed), len(gone)
    return report


def reset_untrusted(judge: str | None) -> int:
    """把不是通過的判斷器判的要求清掉重來（check_followups --recheck；換模型或提示詞之後用）。

    那些判斷本來就不算進追問率，清掉不會少掉任何數字。沒有通過的判斷器時什麼都不動：
    沒有「舊版本」可言。
    """
    if judge is None:
        return 0
    stale = list(FollowUp.objects.exclude(classifier__in=["", judge]))
    for followup in stale:
        _clear_judgments(followup)
    FollowUp.objects.bulk_update(stale, _FOLLOWUP_FIELDS)
    return len(stale)


# --- 每晚判斷 ---


@dataclass
class CheckReport:
    sync: SyncReport = field(default_factory=SyncReport)
    judged: int = 0
    # 這一輪新找到的追問
    found: int = 0
    failed: int = 0
    # 模型說有追問、引用卻對不上逐字稿：當成沒有追問
    ungrounded: int = 0
    # 送出之後內容變了（同時有人重產），結果不存、下一輪重判
    stale: int = 0
    # --recheck 清掉重來的要求數
    reset: int = 0
    # 還有候選沒判斷的要求：積壓要講出來，否則一晚 200 個的上限會無聲地一直排著
    remaining: int = 0
    stopped: bool = False
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        text = (f"{self.sync}\n判斷完成 {self.judged}（找到追問 {self.found}）、失敗 {self.failed}、"
                f"還有候選沒判斷的要求 {self.remaining}")
        if self.ungrounded:
            text += f"、引用對不上逐字稿當成沒有追問 {self.ungrounded}"
        if self.stale:
            text += f"、內容剛被改過不存 {self.stale}"
        if self.reset:
            text += f"、換判斷器清掉重判 {self.reset} 項"
        return text + (f"（{self.stop_reason or 'GPU 不可用'}，這一輪提前結束）" if self.stopped else "")


def _open_followups():
    """還沒找到追問、期限換得出來的要求，到期早的先判：觀察期快結束的先有答案。"""
    return (FollowUp.objects.filter(due_date__isnull=False, followed_by__isnull=True)
            .select_related("article").defer("article__transcript_text", "article__source_note")
            .order_by("due_date", "id"))


class _Round:
    """一輪 check_followups：預算、報告、失敗處理都在這裡，判斷一對的細節分成小函式。"""

    def __init__(self, client: GpuApiClient, limit: int, timeout: float, now: datetime):
        self.client = client
        self.budget = max(0, limit)
        self.timeout = timeout
        self.now = now
        self.today = timezone.localdate(now)
        self.report = CheckReport()

    def check(self, followup: FollowUp, candidates: list[Doc]) -> None:
        """依重疊高到低判還沒判過的候選，第一篇有追問就停；最後記下是不是全部判完了。"""
        checked = set(followup.checked or [])
        for doc in candidates:
            if doc.id in checked:
                continue
            if self.report.stopped or self.budget <= 0:
                break
            self.budget -= 1
            fresh = self._judge(followup, doc)
            if fresh is None:
                continue
            followup = fresh
            if followup.followed_by_id:
                break
        self._settle(followup, candidates)

    def _judge(self, followup: FollowUp, doc: Doc) -> FollowUp | None:
        """判一對、存起來，回傳存好的 FollowUp；失敗或內容變了回 None。"""
        try:
            ask = ask_at(followup.article.brief, followup.ask_index)
            candidate = Article.objects.prefetch_related("slides").get(pk=doc.id)
            if ask is None:
                raise JobFailed("摘要卡裡已經沒有這一項要求")
            pair = pair_input(ask, candidate)
            result = judge_pair(self.client, pair, self.timeout)
            fresh = _fresh_pair(followup.pk, doc.id, pair)
            if fresh is None:
                self.report.stale += 1
                return None
            fresh_followup, fresh_candidate = fresh
            followed = result.followed_up and grounded(result.quote, fresh_candidate.transcript_text)
            if result.followed_up and not followed:
                self.report.ungrounded += 1
                logger.info("要求 %s 對文章 %s：引用對不上逐字稿，當成沒有追問", followup.pk, doc.id)
            # 存檔也放在 try 裡：SD 卡上的 SQLite 偶爾會鎖住，一對存不進去不該讓這一輪剩下的
            # 都不判（同 topics.classify_topics）
            _record(fresh_followup, doc.id, result, followed)
        except GpuApiError as e:
            self._failure(followup, doc, e)
            self.report.stopped = True
            logger.warning("GPU 不可用，追問判斷這一輪提前結束")
            return None
        except JobFailed as e:
            self._failure(followup, doc, e)
            if self.report.judged == 0 and self.report.failed >= _EARLY_FAILURES:
                # 開頭連續幾個都被拒絕，多半是 GPU 端還沒更新（舊版不認得 followup 工作，每個都回
                # 422）：照樣送完整批只是同一個錯誤重複兩百次
                self.report.stopped = True
                self.report.stop_reason = f"開頭 {self.report.failed} 個都被 GPU 拒絕，GPU 端可能還沒更新"
                logger.warning("追問判斷：%s", self.report.stop_reason)
            return None
        except Exception as e:  # noqa: BLE001
            logger.exception("要求 %s 對文章 %s 判斷時發生預期外的錯誤", followup.pk, doc.id)
            self._failure(followup, doc, e)
            return None
        self.report.judged += 1
        self.report.found += followed
        return fresh_followup

    def _failure(self, followup: FollowUp, doc: Doc, error: Exception) -> None:
        self.report.failed += 1
        self.report.errors.append(f"要求 {followup.pk} 對文章 {doc.id}: {error}")
        logger.warning("要求 %s 對文章 %s 判斷失敗：%s", followup.pk, doc.id, error)

    def _settle(self, followup: FollowUp, candidates: list[Doc]) -> None:
        """記下這一項是不是所有候選都判完了（checked_at），未追問要靠它。

        只在需要時寫：還沒判完而之前記過 → 清掉；判完了而還沒記過、或上次是觀察期結束前記的
        → 記現在。每晚把每一項都寫一次會白白磨 SD 卡。
        """
        checked = set(followup.checked or [])
        complete = bool(followup.followed_by_id) or all(doc.id in checked for doc in candidates)
        if not complete:
            self.report.remaining += 1
            if followup.checked_at is not None:
                FollowUp.objects.filter(pk=followup.pk).update(checked_at=None)
            return
        stale_mark = (followup.checked_at is None or (
            self.today > window_end(followup.due_date) >= timezone.localdate(followup.checked_at)))
        if stale_mark:
            FollowUp.objects.filter(pk=followup.pk).update(checked_at=self.now)


def _fresh_pair(followup_id: int, candidate_id: int,
                pair: PairInput) -> tuple[FollowUp, Article] | None:
    """等 GPU 的這段時間裡，兩篇文章都可能被重產（save_result 會清掉這一項或它的判斷）。

    存之前重讀一次：要求、回應、摘要卡、逐字稿片段有任何一個不一樣就不存，否則舊內容的判斷會
    掛在新內容上、而且之後不會再判（同 topics.classify_topics）。
    """
    followup = FollowUp.objects.select_related("article").filter(pk=followup_id).first()
    candidate = Article.objects.prefetch_related("slides").filter(pk=candidate_id).first()
    if followup is None or candidate is None or not is_base(candidate):
        return None
    ask = ask_at(followup.article.brief, followup.ask_index)
    if ask is None or pair_input(ask, candidate) != pair:
        return None
    return followup, candidate


def _record(followup: FollowUp, candidate_id: int, result: FollowUpResult, followed: bool) -> None:
    """記下一對的判斷。換了判斷器就整項重來：一項要求的判斷永遠出自同一個判斷器。

    否則一項要求的三個候選可能是兩個版本判的，「只算通過的判斷器判出來的」就說不清楚。
    """
    if followup.classifier and followup.classifier != result.classifier:
        _clear_judgments(followup)
    followup.classifier = result.classifier
    if candidate_id not in followup.checked:
        followup.checked = [*followup.checked, candidate_id]
    if followed:
        followup.followed_by_id = candidate_id
        followup.quote = result.quote
    followup.save(update_fields=["checked", "followed_by", "quote", "classifier", "checked_at"])


def check_followups(client: GpuApiClient, limit: int, recheck: bool = False,
                    timeout: float | None = None, now: datetime | None = None) -> CheckReport:
    """替基礎文章建立／更新要求（期限換算），再判斷新的候選對，最多 limit 個判斷工作。

    失敗處理同議題分類：失敗只記 log、下一輪再判；GPU 連不上整輪停；開頭連續 3 個被拒也停。
    判不完的要求照樣記下「還沒判完」（checked_at 清掉），不會被當成未追問。

    recheck=True：先把不是通過的判斷器判的要求清掉重來（換模型或提示詞、新版本評估通過之後用）。
    """
    timeout = timeout or settings.GPU_JOB_TIMEOUT_SECONDS
    round_ = _Round(client, limit, timeout, now or timezone.now())
    round_.report.sync = sync_followups()
    if recheck:
        round_.report.reset = reset_untrusted(passing_judge())
    index = CandidateIndex.load()
    for followup in list(_open_followups()):
        round_.check(followup, index.candidates(followup))
    return round_.report


# --- 重產時的清理 ---


def forget_article(article: Article) -> None:
    """文章重產（ingest.save_result）：它的內容變了，跟它有關的判斷都要重來。

    - 它當來源的要求刪掉：要求的文字與位置都可能換了，下一輪 sync_followups 重建。
    - 它當候選的那些要求：從 checked 與 followed_by 拿掉，下一輪用新內容重判。只有發言日在它
      之前、觀察期涵蓋它的要求才可能判過它，先用日期篩，不必每篇都掃全部。
    """
    FollowUp.objects.filter(article=article).delete()
    day = article.date if isinstance(article.date, date) else date.fromisoformat(str(article.date))
    touched = []
    for followup in FollowUp.objects.filter(
            article__source=article.source, article__date__lt=day,
            due_date__gte=day - timedelta(days=WATCH_DAYS)).only(
            "id", "checked", "followed_by", "quote", "checked_at"):
        if article.pk not in (followup.checked or []) and followup.followed_by_id != article.pk:
            continue
        followup.checked = [i for i in followup.checked if i != article.pk]
        if followup.followed_by_id == article.pk:
            followup.followed_by, followup.quote = None, ""
        followup.checked_at = None
        touched.append(followup)
    FollowUp.objects.bulk_update(touched, ["checked", "followed_by", "quote", "checked_at"])


# --- 標註集 ---


@dataclass
class SampleReport:
    added: int = 0
    total: int = 0
    # 抽得到的要求數（有通過程式篩選的候選、還沒抽過的）
    available: int = 0

    def __str__(self) -> str:
        return f"新抽 {self.added} 對，標註集共 {self.total} 對（還有 {self.available - self.added} 項要求可抽）"


def sample_labels(pairs: int = FOLLOWUP_MIN_LABELS, seed: int = 0) -> SampleReport:
    """從「通過程式篩選的候選對」抽，建立空白的 FollowUpLabel（之後在 admin 標）。

    一半取那項要求重疊最高的那一篇、一半在它的候選裡隨機取：只取最高的，抽到的幾乎都是真的
    追問，評估量不到模型會不會把「同領域的別件事」誤判成追問。
    已經抽過的要求不重抽，只補到 pairs 對；固定種子、要求照 id 排，可以重現。
    """
    sync_followups()
    report = SampleReport(total=FollowUpLabel.objects.count())
    need = pairs - report.total
    drawn = set(FollowUpLabel.objects.values_list("article_id", "ask_index"))
    index = CandidateIndex.load()
    pool = []
    for followup in FollowUp.objects.filter(due_date__isnull=False).select_related("article").defer(
            "article__transcript_text", "article__source_note").order_by("id"):
        if (followup.article_id, followup.ask_index) in drawn:
            continue
        candidates = index.candidates(followup)
        if candidates:
            pool.append((followup, candidates))
    report.available = len(pool)
    if need <= 0:
        return report
    rng = random.Random(f"followup:{seed}")
    picked = rng.sample(pool, min(need, len(pool)))
    top_half = (len(picked) + 1) // 2
    labels = [FollowUpLabel(article_id=followup.article_id, ask_index=followup.ask_index,
                            request=followup.request,
                            candidate_id=(candidates[0] if i < top_half else rng.choice(candidates)).id)
              for i, (followup, candidates) in enumerate(picked)]
    FollowUpLabel.objects.bulk_create(labels)
    report.added = len(labels)
    report.total += len(labels)
    return report


# --- 評估 ---


@dataclass(frozen=True)
class _Outcome:
    label: FollowUpLabel
    # 模型的答案（含落地檢查）；None 是判斷失敗
    model: bool | None
    quote: str = ""
    ungrounded: bool = False
    error: str = ""


@dataclass
class EvaluationReport:
    evaluation: FollowUpEvaluation | None = None
    # 已標註、但已經對不上的（文章不是基礎文章了、或摘要重產之後那一項要求的文字變了）
    skipped: int = 0

    def __str__(self) -> str:
        lines = []
        ev = self.evaluation
        if ev is not None:
            verdict = "通過" if ev.passed else "未通過"
            if not ev.passed and ev.labeled < FOLLOWUP_MIN_LABELS:
                verdict += f"（標註不足 {FOLLOWUP_MIN_LABELS} 對）"
            elif not ev.passed:
                verdict += f"（準確率未達 {FOLLOWUP_MIN_ACCURACY:.0%}）"
            lines.append(f"判斷器 {ev.classifier}：已標註 {ev.labeled} 對、判對 {ev.correct} 對，"
                         f"準確率 {ev.accuracy * 100:.1f}%：{verdict}")
            for m in ev.mistakes:
                lines.append(f"  判錯：{m['article']} 第 {m['ask_index'] + 1} 項 → {m['candidate']}"
                             f"（{m['speaker']}）：人工「{_answer(m['human'])}」、模型「{_model_answer(m)}」")
        if self.skipped:
            lines.append(f"略過 {self.skipped} 對已標註、但已經對不上的（文章不是基礎文章了，"
                         "或摘要重產之後那一項要求變了）")
        return "\n".join(lines)


def _answer(followed: bool) -> str:
    return "有追問" if followed else "沒有追問"


def _model_answer(mistake: dict) -> str:
    if mistake.get("model") is None:
        return f"判斷失敗：{mistake.get('error') or '未知'}"
    if mistake.get("ungrounded"):
        return "沒有追問（說有追問，但引用對不上逐字稿）"
    return _answer(mistake["model"])


def _usable(label: FollowUpLabel) -> Ask | None:
    """標註還對得上現在的文章嗎：兩篇都是基礎文章、那一項要求的文字跟抽樣時一樣、後來那篇有逐字稿。"""
    if not (is_base(label.article) and is_base(label.candidate)
            and (label.candidate.transcript_text or "").strip()):
        return None
    ask = ask_at(label.article.brief, label.ask_index)
    return ask if ask is not None and ask.request == label.request else None


def evaluate(client: GpuApiClient, timeout: float | None = None,
             now: datetime | None = None) -> EvaluationReport:
    """把已標註的對用現在的判斷器重判一次（含落地檢查），存一筆評估。

    準確率 = 判對的對數 ÷ 已標註對數；判斷失敗的算錯（分母照算）。通過 = 已標註至少 30 對、
    準確率至少 85%。一次評估裡 GPU 回來的判斷器必須都一樣，不一樣就整次中止、什麼都不存；
    GPU 連不上（GpuApiError）也直接冒出去、什麼都不存（同 topics.evaluate）。
    """
    timeout = timeout or settings.GPU_JOB_TIMEOUT_SECONDS
    labelled = (FollowUpLabel.objects.filter(followed__isnull=False)
                .select_related("article", "candidate").prefetch_related("candidate__slides")
                .order_by("id"))
    report = EvaluationReport()
    usable = []
    for label in labelled:
        ask = _usable(label)
        if ask is None:
            report.skipped += 1
        else:
            usable.append((label, ask))
    if not usable:
        raise EvaluationAborted("還沒有任何對得上的已標註對：先跑 sample_followup_labels，再到 admin 標註")

    outcomes: list[_Outcome] = []
    classifiers: set[str] = set()
    for label, ask in usable:
        try:
            result = judge_pair(client, pair_input(ask, label.candidate), timeout)
        except JobFailed as e:
            logger.warning("評估：標註 %s 判斷失敗：%s", label.pk, e)
            outcomes.append(_Outcome(label, None, error=str(e)[:200]))
            continue
        classifiers.add(result.classifier)
        if len(classifiers) > 1:
            raise EvaluationAborted(
                f"這次評估裡 GPU 回來的判斷器不一樣（{'、'.join(sorted(classifiers))}），"
                "中途換了模型或提示詞；整次不算，請再跑一次")
        ok = result.followed_up and grounded(result.quote, label.candidate.transcript_text)
        outcomes.append(_Outcome(label, ok, result.quote, result.followed_up and not ok))
    if not classifiers:
        first = next((o.error for o in outcomes if o.error), "")
        raise EvaluationAborted(f"沒有任何一對判斷成功，無法評估。第一個錯誤：{first}"
                                "（GPU 端還沒更新的話，會是 422）")
    report.evaluation = _save_evaluation(classifiers.pop(), outcomes, now or timezone.now())
    return report


def _save_evaluation(classifier: str, outcomes: list[_Outcome], ran_at: datetime) -> FollowUpEvaluation:
    mistakes = []
    for o in outcomes:
        if o.model == o.label.followed:
            continue
        mistake = {"label": o.label.pk, "article": o.label.article.slug, "ask_index": o.label.ask_index,
                   "candidate": o.label.candidate.slug, "speaker": o.label.article.speaker,
                   "request": o.label.request, "human": o.label.followed, "model": o.model}
        if o.quote:
            mistake["quote"] = o.quote
        if o.ungrounded:
            mistake["ungrounded"] = True
        if o.error:
            mistake["error"] = o.error
        mistakes.append(mistake)
    labeled = len(outcomes)
    correct = labeled - len(mistakes)
    accuracy = correct / labeled
    return FollowUpEvaluation.objects.create(
        classifier=classifier, labeled=labeled, correct=correct, accuracy=accuracy,
        passed=labeled >= FOLLOWUP_MIN_LABELS and accuracy >= FOLLOWUP_MIN_ACCURACY,
        mistakes=mistakes, ran_at=ran_at)


# --- 上線條件 ---


def passing_evaluation() -> FollowUpEvaluation | None:
    """上線的那筆評估；None 是沒有通過的判斷器（只給待追蹤清單、不給比率）。

    規則同 topics.passing_evaluations：每個判斷器只看**它自己最新的一次**評估；最新那次有通過
    的判斷器裡，取評估得最晚的。試新模型沒通過，不會讓驗過的舊版本下架；同一個判斷器在更多
    標註上重評沒通過，它就下架。
    """
    newest: dict[str, FollowUpEvaluation] = {}
    for evaluation in FollowUpEvaluation.objects.order_by("classifier", "-ran_at", "-id"):
        newest.setdefault(evaluation.classifier, evaluation)
    live = None
    for evaluation in newest.values():
        if evaluation.passed and (live is None or (evaluation.ran_at, evaluation.id)
                                  > (live.ran_at, live.id)):
            live = evaluation
    return live


def passing_judge() -> str | None:
    evaluation = passing_evaluation()
    return evaluation.classifier if evaluation else None


# --- 側寫與 API ---


def _session_followups(session: Session):
    """這個會期的要求：來源文章掛在這個會期、而且（現在）還是基礎文章。"""
    return (FollowUp.objects.filter(article__session=session, article__status=ArticleStatus.READY,
                                    article__brief__isnull=False)
            .exclude(article__speaker__contains=SPEAKER_SEPARATOR))


def session_states(session: Session, judge: str, today: date) -> Iterator[tuple[str, date, FollowUpState]]:
    """(講者, 發言日, 狀態)：只給算進追問率的已追問與未追問。profiles 用它算 followup_rate。"""
    rows = _session_followups(session).values_list(
        "article__speaker", "article__date", "due_date", "followed_by_id", "classifier", "checked_at")
    for speaker, day, due, followed_by, classifier, checked_at in rows.iterator():
        state = state_for(due, followed_by, classifier, checked_at, today, judge)
        if state in (FollowUpState.FOLLOWED, FollowUpState.NOT_FOLLOWED):
            yield speaker.strip(), day, state


def _article_ref(article: Article) -> dict:
    return {"slug": article.slug, "title": article.title, "date": article.date}


def profile_asks(name: str, session: Session, judge: str | None, today: date) -> tuple[list[dict], int]:
    """側寫上的清單：(要求, 期限無法換算的數量)。

    不靠模型的部分永遠給：要求、期限原文、到期日、來源文章。判斷器沒通過時只給待追蹤
    （state_for 對要靠判斷的狀態回 None）。依到期日排序。講者用跟證據網址同一個名字，
    清單上的篇章點進去才對得上。
    """
    mine = _session_followups(session).filter(article__speaker=name)
    unparsed = mine.filter(due_date__isnull=True).count()
    rows = (mine.filter(due_date__isnull=False).select_related("article", "followed_by")
            .defer("article__transcript_text", "followed_by__transcript_text")
            .order_by("due_date", "article__date", "ask_index"))
    asks = []
    for followup in rows:
        state = state_of(followup, today, judge)
        if state is None:
            continue
        followed = state == FollowUpState.FOLLOWED
        ref = followup.followed_by if followed else None
        asks.append({
            "article": _article_ref(followup.article),
            "request": followup.request,
            "deadline": followup.deadline_text,
            "due_date": followup.due_date,
            "state": state.value,
            # 追問的那篇得有頁面（已完成）才給連結
            "followed_by": _article_ref(ref) if ref and ref.status == ArticleStatus.READY else None,
            "quote": followup.quote if followed else "",
        })
    return asks, unparsed
