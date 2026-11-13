"""把語音辨識的「字」對到說話者，再切成一段一段的 Turn（純邏輯，不碰 IO、不碰模型）。

流程：diarizer 的區段 → normalize_turns（S1..Sn）→ assign_speakers（每個字一個說話者，
說不準的是 S0）→ build_turns（同一人連續說的話為一段）→ summarize_speakers（誰說了多久）
→ diarization_report（這次區分的品質）→ transcript_lines（給人讀的逐字稿）。

這一層的鐵則：**寧可標成 S0（無法判斷），也不把一個字歸給可能不對的人。**
S0 不會被模型引用，也不會出現在說話者清單上，只在逐字稿裡標成「無法判斷」。
時間與長度的門檻（0.3 秒、1.5 秒、45 秒…）都是暫定值，要用實際影片校正。
"""
from __future__ import annotations

import math
import unicodedata
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace

from ..domain.entities import (
    DiarizationReport,
    SpeakerInfo,
    SpeakerSample,
    SpeakerTurn,
    Turn,
    Word,
)
from ..domain.errors import AttributionUnreliable

# 手動的版本號，會進語音辨識（字）的存檔指紋，給「原始碼看不出來」的改動用：
# 例如換了校正用的資料、辨識的前處理改在別的地方。辨識參數與辨識器、正體修正的原始碼，
# 已經由辨識器的 fingerprint 自動算進去（whisper_transcriber.WORD_OPTIONS），不必靠人記得升版；
# 本模組（對齊、分段、說話者清單）的原始碼則自動算進摘要存檔的指紋（diarized_pipeline.RULE_MODULES）。
ALIGN_VERSION = 1

# 無法判斷是誰的代號。模型看到的是固定的 X（不能引用），逐字稿上是 [[S0]]。
UNDECIDED = "S0"

# --- 對齊（暫定值）---
# 換人的地方必須是句子結尾，或前後兩字之間停頓至少這麼久
PAUSE_SECONDS = 0.3
# diarizer 說的換人點若不在自然的邊界上，只看這個範圍內有沒有自然邊界
SNAP_SECONDS = 1.5
# 沒有任何區段蓋住的字，兩側同一人、中間沒有超過這麼久、且空隙裡沒有自然的邊界才算那個人
INHERIT_MAX_GAP_SECONDS = 2.0
# 別位說話者的區段蓋到一個字超過這麼久，就是 diarizer 的換人點切過這個字，這個字說不準。
# 語音辨識與 diarizer 的時間都以幾十毫秒為單位，所以留一點餘裕，差一兩格不算
STRADDLE_TOLERANCE_SECONDS = 0.05
_EPS = 1e-6

# --- 分段（暫定值）---
MAX_TURN_SECONDS = 45.0
# 同一個人的兩個字之間靜默超過這麼久，就不當成同一段話
SILENCE_SPLIT_SECONDS = 4.0
# 純語助詞：這樣的一段整段拿掉。對、不是、沒有、好、可以是答案，不在其中。
FILLER_CHARS = frozenset("嗯啊哦喔")

# --- 說話者清單與品質（暫定值）---
MINOR_SECONDS = 60.0
MINOR_SHARE = 0.1
MINOR_TURNS = 3
SAMPLE_COUNT = 3
SAMPLE_CHARS = 80
MAX_PARTICIPANTS = 8
# 發佈的說話者清單（主要發言者加其他聲音）最多幾個。新聞服務存檔之前也檢查同一個數字
# （services/news/articles/speaker_result.py 的 MAX_SPEAKERS，speakers 要 1 到 12 個），
# 兩邊要一起改；這裡先擋，才不會等整支影片都做完、成品交到新聞服務才被拒絕。
MAX_PUBLISHED_SPEAKERS = 12
UNASSIGNED_WARN_RATIO = 0.25
MANY_MINOR = 4

WARN_COUNT_MISMATCH = "count_mismatch"
WARN_HIGH_UNASSIGNED = "high_unassigned"
WARN_MANY_MINOR = "many_minor"
WARN_SINGLE_FOUND = "single_found"

PARTICIPANT = "participant"
MINOR = "minor"

# 字的暫時狀態，用不可能是說話者標籤的字串，避免跟 diarizer 的原始標籤撞在一起：
# 沒有任何區段蓋住它；或說不準（兩位以上同時蓋住、換人點不在自然的邊界上、時間不可信）。
_UNCOVERED = "\x00uncovered"
_UNSURE = "\x00unsure"

_SENTENCE_END = ("。", "！", "？", "!", "?", "…", ".")
_CLAUSE_END = ("，", "、", "；", "：", ",", ";", ":")
_CLOSERS = "」』”’\"')）]】》〉"


# --- diarizer 區段整理 ---


def _usable_turns(turns: Sequence[SpeakerTurn]) -> list[SpeakerTurn]:
    """丟掉不合理的區段（沒有標籤、時間不是有限數字、結束不晚於開始），開始早於 0 的夾回 0，排序。"""
    usable: list[SpeakerTurn] = []
    for turn in turns:
        if not turn.speaker:
            continue
        if not (math.isfinite(turn.start) and math.isfinite(turn.end)):
            continue
        start = max(0.0, turn.start)
        if turn.end > start:
            usable.append(SpeakerTurn(start, turn.end, turn.speaker))
    usable.sort(key=lambda t: (t.start, t.end))
    return usable


def normalize_turns(turns: Sequence[SpeakerTurn]) -> tuple[SpeakerTurn, ...]:
    """排序、丟掉不合理的區段，並把說話者重新編成 S1..Sn（依第一次出現）。

    交疊的區段原樣保留（對齊時被兩位以上蓋住的字會標成無法判斷）。
    """
    names: dict[str, str] = {}
    out = []
    for turn in _usable_turns(turns):
        label = names.setdefault(turn.speaker, f"S{len(names) + 1}")
        out.append(SpeakerTurn(turn.start, turn.end, label))
    return tuple(out)


# --- 字 → 說話者 ---


def assign_speakers(
    words: Sequence[Word], turns: Sequence[SpeakerTurn],
) -> tuple[tuple[Word, str], ...]:
    """每個字一個說話者；說不準的是 S0。輸出與輸入一對一、同順序、文字不改。

    1. 以字的中點看哪些區段蓋住它：恰好一位 → 候選；兩位以上 → 交疊（S0）；沒有 → 空隙。
    2. 空隙裡的字，只有「前後最近的有人字是同一位、中間沒有超過 2 秒、且空隙裡（含兩端）
       沒有句子結尾或 0.3 秒以上的停頓」才算那一位，其餘是 S0（兩側不同人、另一側是
       交疊／起點／終點、或換人可能藏在空隙裡，都不推測）。
    3. 兩個相鄰的字候選不同＝換人。換人只允許發生在自然的邊界：前一個字是句子結尾，
       或兩字之間停頓 >= 0.3 秒。不在自然邊界上的換人，不把任何字挪給另一個人，
       而是把「diarizer 的換人點到最近的自然邊界（1.5 秒內）」之間的字標成 S0；
       1.5 秒內沒有自然邊界，就只把換人點兩側各一個字標成 S0。
    4. 最後，別位說話者的區段蓋到這個字超過 0.05 秒的字也是 S0：字的中點只能二選一，但 diarizer 的
       換人點若切過這個字，就表示它對這個字本身並不確定（換人點剛好落在自然邊界上也一樣）。

    時間不是有限數字的字一律是 S0。字的順序就是文字的順序；開始時間會被夾成不遞減。
    """
    clean, trustworthy = _sanitize(words)
    spans = [t for t in _usable_turns(turns) if t.speaker != UNDECIDED]
    labels = _midpoint_labels(clean, spans)
    for i, ok in enumerate(trustworthy):
        if not ok:
            labels[i] = _UNSURE
    labels = _fill_gaps(clean, labels)
    labels = _snap_changes(clean, labels)
    labels = _doubt_straddled_words(clean, spans, labels)
    return tuple((w, label if _is_speaker(label) else UNDECIDED)
                 for w, label in zip(clean, labels))


def _is_speaker(label: str) -> bool:
    return label not in (_UNCOVERED, _UNSURE)


def _sanitize(words: Sequence[Word]) -> tuple[list[Word], list[bool]]:
    """開始時間夾成不遞減、結束不早於開始；非有限數字的字時間補成前一個字的結束，並標為不可信。"""
    clean: list[Word] = []
    trustworthy: list[bool] = []
    prev_start = 0.0
    prev_end = 0.0
    for w in words:
        if math.isfinite(w.start) and math.isfinite(w.end):
            start = max(w.start, prev_start)
            end = max(w.end, start)
            ok = True
        else:
            start = end = prev_end
            ok = False
        clean.append(w if (start, end) == (w.start, w.end) else replace(w, start=start, end=end))
        trustworthy.append(ok)
        prev_start, prev_end = start, end
    return clean, trustworthy


def _midpoint_labels(words: Sequence[Word], spans: Sequence[SpeakerTurn]) -> list[str]:
    starts = [t.start for t in spans]
    ends_max: list[float] = []
    running = -math.inf
    for t in spans:
        running = max(running, t.end)
        ends_max.append(running)
    labels: list[str] = []
    for w in words:
        mid = (w.start + w.end) / 2
        hit: set[str] = set()
        k = bisect_right(starts, mid) - 1
        # ends_max[k] 是前 k+1 個區段的最大結束時間：不超過 mid 就不可能再有區段蓋住它
        while k >= 0 and ends_max[k] > mid:
            if spans[k].end > mid:
                hit.add(spans[k].speaker)
            k -= 1
        labels.append(next(iter(hit)) if len(hit) == 1 else (_UNSURE if hit else _UNCOVERED))
    return labels


def _fill_gaps(words: Sequence[Word], labels: list[str]) -> list[str]:
    out = list(labels)
    i = 0
    n = len(out)
    while i < n:
        if out[i] != _UNCOVERED:
            i += 1
            continue
        j = i
        while j < n and out[j] == _UNCOVERED:
            j += 1
        # 空隙是 [i, j)，左右鄰居是 i-1 與 j
        if i > 0 and j < n and _is_speaker(out[i - 1]) and out[i - 1] == out[j]:
            close_enough = words[j].start - words[i - 1].end <= INHERIT_MAX_GAP_SECONDS + _EPS
            # 空隙裡（含兩端）若有句子結尾或停頓，換人就可能藏在那裡：別人的短句（對。好。）
            # 常常正是 diarizer 漏掉的那一小段。這種空隙兩側同一人也不推測。
            can_hide_a_change = any(_is_natural_boundary(words, k) for k in range(i - 1, j))
            if close_enough and not can_hide_a_change:
                for k in range(i, j):
                    out[k] = out[i - 1]
        i = j
    # 沒被補上的空隙一律說不準
    return [_UNSURE if label == _UNCOVERED else label for label in out]


def _doubt_straddled_words(
    words: Sequence[Word], spans: Sequence[SpeakerTurn], labels: list[str],
) -> list[str]:
    """別位說話者的區段蓋到這個字超過 STRADDLE_TOLERANCE_SECONDS 的字，改成說不準。

    審查用有標準答案的模擬對談量過（換人點的誤差 0.2 秒）：整句引用歸錯人的機率少了約三分之二，
    代價是多約 3% 的字變成無法判斷；diarizer 完全正確時一個字也不會多掉。
    """
    starts = [t.start for t in spans]
    ends_max: list[float] = []
    running = -math.inf
    for t in spans:
        running = max(running, t.end)
        ends_max.append(running)
    out = list(labels)
    for i, (w, label) in enumerate(zip(words, labels)):
        if not _is_speaker(label):
            continue
        k = bisect_right(starts, w.end) - 1
        # ends_max[k] 不超過字的開始（加上容許）就不可能再有區段蓋到它超過容許
        while k >= 0 and ends_max[k] > w.start + STRADDLE_TOLERANCE_SECONDS:
            t = spans[k]
            if (t.speaker != label
                    and min(t.end, w.end) - max(t.start, w.start) > STRADDLE_TOLERANCE_SECONDS):
                out[i] = _UNSURE
                break
            k -= 1
    return out


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip().rstrip(_CLOSERS).rstrip()
    return stripped.endswith(_SENTENCE_END)


def _boundary_time(words: Sequence[Word], j: int) -> float:
    return (words[j].end + words[j + 1].start) / 2


def _is_natural_boundary(words: Sequence[Word], j: int) -> bool:
    """第 j 個字與下一個字之間，是不是可以換人的地方。"""
    if _ends_sentence(words[j].text):
        return True
    return words[j + 1].start - words[j].end + _EPS >= PAUSE_SECONDS


def _snap_changes(words: Sequence[Word], labels: list[str]) -> list[str]:
    out = list(labels)
    n = len(labels)
    for i in range(n - 1):
        a, b = labels[i], labels[i + 1]
        if not (_is_speaker(a) and _is_speaker(b)) or a == b:
            continue
        if _is_natural_boundary(words, i):
            continue
        # 換人點 i|i+1 不在自然邊界上。左右兩段的範圍用原始標籤算，處理順序不影響結果。
        left = i
        while left > 0 and labels[left - 1] == a:
            left -= 1
        right = i + 1
        while right + 1 < n and labels[right + 1] == b:
            right += 1
        at = _boundary_time(words, i)
        best: tuple[float, int] | None = None
        # 往前：自然邊界 j 在 [left, i-1]；被標成 a 的 j+1..i 之間的字要標成說不準
        for j in range(i - 1, left - 1, -1):
            distance = abs(_boundary_time(words, j) - at)
            if distance > SNAP_SECONDS + _EPS:
                break
            if _is_natural_boundary(words, j):
                best = (distance, j)
                break
        # 往後：自然邊界 j 在 [i+1, right-1]；被標成 b 的 i+1..j 之間的字要標成說不準
        for j in range(i + 1, right):
            distance = abs(_boundary_time(words, j) - at)
            if distance > SNAP_SECONDS + _EPS:
                break
            if _is_natural_boundary(words, j):
                # 一樣近時取較前面的，結果才固定
                if best is None or distance < best[0] - _EPS:
                    best = (distance, j)
                break
        if best is None:
            zone = range(i, i + 2)
        elif best[1] < i:
            zone = range(best[1] + 1, i + 1)
        else:
            zone = range(i + 1, best[1] + 1)
        for k in zone:
            out[k] = _UNSURE
    return out


# --- 字 → 段 ---


def squash(text: str) -> str:
    """只留文字本身（字母、數字、漢字）：比對重複、語助詞與引文用，忽略空白與標點。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())


class _Piece:
    """同一位說話者、同一個 Whisper 段落裡連續的字。"""

    __slots__ = ("speaker", "segment", "words")

    def __init__(self, speaker: str, segment: int):
        self.speaker = speaker
        self.segment = segment
        self.words: list[Word] = []

    @property
    def text(self) -> str:
        return "".join(w.text for w in self.words)


def build_turns(
    assigned: Sequence[tuple[Word, str]], max_turn_seconds: float = MAX_TURN_SECONDS,
) -> tuple[Turn, ...]:
    """把標好說話者的字切成 Turn：同一人連續說的話一段，編號從 0 連續。

    - 換人就換段；S0 也成段（逐字稿要看得到，只是不能被引用）。
    - 同一個說話者、同一段文字連續重複輸出（Whisper 的重複幻覺）只留第一份。
      比對的鍵是（說話者, 文字），所以主持人說「對。」之後來賓也說「對。」不會被當成重複。
    - 整段只有語助詞（嗯 啊 哦 喔）的段落拿掉，前後同一人的話接起來。
      「對」「不是」「沒有」「好」「可以」是答案，不是語助詞，永遠是自己的一段。
    - 同一人的兩個字之間靜默超過 4 秒就斷開；超過 45 秒的段落在句子結尾切開
      （沒有句號就退而求其次：逗號、停頓，最後才在字與字之間硬切）。
    - 文字原樣：一段的文字是它的字接在一起、去掉頭尾空白，沒有改寫。
    """
    pieces = _drop_repeated(_pieces(assigned))
    pieces = [p for p in pieces if not _only_fillers(p)]

    runs: list[tuple[str, list[Word]]] = []
    for piece in pieces:
        if runs and runs[-1][0] == piece.speaker:
            runs[-1][1].extend(piece.words)
        else:
            runs.append((piece.speaker, list(piece.words)))

    turns: list[Turn] = []
    for speaker, words in runs:
        for stretch in _split_on_silence(words):
            for part in _split_long(stretch, max_turn_seconds):
                text = "".join(w.text for w in part).strip()
                if text:
                    turns.append(Turn(len(turns), part[0].start, part[-1].end, speaker, text))
    return tuple(turns)


def _pieces(assigned: Sequence[tuple[Word, str]]) -> list[_Piece]:
    pieces: list[_Piece] = []
    for word, speaker in assigned:
        if not pieces or pieces[-1].speaker != speaker or pieces[-1].segment != word.segment:
            pieces.append(_Piece(speaker, word.segment))
        pieces[-1].words.append(word)
    return pieces


def _drop_repeated(pieces: Sequence[_Piece]) -> list[_Piece]:
    kept: list[_Piece] = []
    previous: tuple[str, str] | None = None
    for piece in pieces:
        key = (piece.speaker, squash(piece.text))
        if key[1] and key == previous:
            continue
        kept.append(piece)
        previous = key
    return kept


def _only_fillers(piece: _Piece) -> bool:
    content = squash(piece.text)
    return bool(content) and all(ch in FILLER_CHARS for ch in content)


def _split_on_silence(words: Sequence[Word]) -> list[list[Word]]:
    stretches: list[list[Word]] = []
    for w in words:
        if stretches and w.start - stretches[-1][-1].end <= SILENCE_SPLIT_SECONDS + _EPS:
            stretches[-1].append(w)
        else:
            stretches.append([w])
    return stretches


def _split_long(words: list[Word], max_seconds: float) -> list[list[Word]]:
    parts: list[list[Word]] = []
    start = 0
    while start < len(words):
        end = start + 1
        while end < len(words) and words[end].end - words[start].start <= max_seconds:
            end += 1
        if end >= len(words):
            parts.append(words[start:])
            break
        cut = _best_cut(words, start, end)
        parts.append(words[start:cut])
        start = cut
    return parts


def _best_cut(words: Sequence[Word], start: int, end: int) -> int:
    """words[start:end] 放得下、再多一個字就太長：回傳下一段第一個字的位置。

    越晚越好，但自然程度優先：句子結尾 > 逗號 > 停頓 > 硬切。
    """
    tests = (
        lambda c: _ends_sentence(words[c - 1].text),
        lambda c: words[c - 1].text.rstrip().endswith(_CLAUSE_END),
        lambda c: words[c].start - words[c - 1].end + _EPS >= PAUSE_SECONDS,
    )
    for test in tests:
        for c in range(end, start, -1):
            if test(c):
                return c
    return end


# --- 說話者清單 ---


def _letter(index: int) -> str:
    """0 → A、25 → Z、26 → AA…"""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize_speakers(turns: Sequence[Turn]) -> tuple[SpeakerInfo, ...]:
    """每位說話者的秒數、段數、首末時間與最多三段樣本；S0 不列。

    主要發言者（participant）標「發言者 A, B, C…」（依第一次出現），其餘標
    「其他聲音 1, 2…」（minor，不能被引用）。minor 的條件：說話秒數少於
    min(60, 全部說話秒數的 10%)，或不到 3 段。說得最多的人一定是 participant——
    19 秒的單人影片也要有一位發言者。樣本是最長的三段，依時間排列。
    """
    grouped: dict[str, list[Turn]] = {}
    for turn in turns:
        if turn.speaker != UNDECIDED:
            grouped.setdefault(turn.speaker, []).append(turn)
    if not grouped:
        return ()
    seconds = {sid: sum(t.end - t.start for t in ts) for sid, ts in grouped.items()}
    threshold = min(MINOR_SECONDS, MINOR_SHARE * sum(seconds.values()))
    first_seen = {sid: min(t.start for t in ts) for sid, ts in grouped.items()}
    top = max(grouped, key=lambda sid: (seconds[sid], -first_seen[sid]))

    def is_minor(sid: str) -> bool:
        return sid != top and (seconds[sid] < threshold or len(grouped[sid]) < MINOR_TURNS)

    order = sorted(grouped, key=lambda sid: (first_seen[sid], sid))
    labels: dict[str, str] = {}
    participants = minors = 0
    for sid in order:
        if is_minor(sid):
            minors += 1
            labels[sid] = f"其他聲音 {minors}"
        else:
            labels[sid] = f"發言者 {_letter(participants)}"
            participants += 1

    infos = []
    for sid in order:
        ts = grouped[sid]
        longest = sorted(ts, key=lambda t: (-(t.end - t.start), t.start))[:SAMPLE_COUNT]
        samples = tuple(SpeakerSample(round(t.start, 1), _clip(t.text, SAMPLE_CHARS))
                        for t in sorted(longest, key=lambda t: t.start))
        infos.append(SpeakerInfo(
            id=sid, label=labels[sid], kind=MINOR if is_minor(sid) else PARTICIPANT,
            seconds=round(seconds[sid], 1), turns=len(ts),
            first_seen=round(first_seen[sid], 1),
            last_seen=round(max(t.end for t in ts), 1),
            samples=samples,
        ))
    return tuple(infos)


def _too_many_voices(participants: int, minors: int, total: int, requested: int | None) -> str:
    """發佈的清單放不下這麼多聲音：每個數字都寫出來，並說明下一步（不猜、不把聲音併進 S0）。"""
    counts = (f"{participants} 位主要發言者、{minors} 個其他聲音，共 {total} 個聲音，"
              f"超過發佈上限 {MAX_PUBLISHED_SPEAKERS} 個")
    if requested is None:
        return (f"找到 {counts}；聲音區分不可靠，不發佈（通常是同一個人被分成好幾個聲音）。"
                "若確知實際人數，請用 --speakers N 指定人數再試一次")
    return (f"已指定 {requested} 人，仍找到 {counts}；聲音區分不可靠，不發佈。"
            "請確認指定的人數是否正確，或換成較短、雜音較少的片段")


def diarization_report(
    turns: Sequence[Turn],
    speakers: Sequence[SpeakerInfo],
    *,
    engine: str,
    version: str,
    model: str,
    fingerprint: str,
    requested_speakers: int | None,
    overlap_seconds: float = 0.0,
) -> DiarizationReport:
    """這次說話者區分的品質；明顯不可靠就 raise AttributionUnreliable（工作失敗，不發佈）。

    失敗：要求 2 人以上卻只找到 1 位發言者、主要發言者超過 8 位、主要發言者與其他聲音合計
    超過 12 個（發佈的說話者清單放不下）、一位發言者都沒有。
    警告（照常發佈，但網站會標出來）：人數與要求不符、S0 超過 25%、其他聲音 4 個以上、
    自動模式只找到 1 位發言者。found_speakers 是主要發言者（participant）的人數。
    proposed_points／kept_points 由 use case 在模型的重點檢查完之後補上。
    """
    participants = [s for s in speakers if s.kind == PARTICIPANT]
    minors = [s for s in speakers if s.kind == MINOR]
    requested = requested_speakers if requested_speakers and requested_speakers >= 2 else None
    found = len(participants)

    if found == 0:
        raise AttributionUnreliable("沒有找到任何主要發言者，無法可靠地判斷誰說了什麼")
    if found > MAX_PARTICIPANTS:
        raise AttributionUnreliable(
            f"找到 {found} 位主要發言者，超過上限 {MAX_PARTICIPANTS} 位；聲音區分不可靠")
    if len(speakers) > MAX_PUBLISHED_SPEAKERS:
        raise AttributionUnreliable(_too_many_voices(found, len(minors), len(speakers), requested))
    if requested is not None and found == 1:
        raise AttributionUnreliable(
            f"指定 {requested} 人，卻只找到 1 位主要發言者；可能把不同的人合併成一個了")

    speech = sum(t.end - t.start for t in turns)
    undecided = sum(t.end - t.start for t in turns if t.speaker == UNDECIDED)
    unassigned_ratio = undecided / speech if speech > 0 else 0.0
    overlap_ratio = min(1.0, overlap_seconds / speech) if speech > 0 and overlap_seconds > 0 else 0.0

    warnings = []
    if requested is not None and found != requested:
        warnings.append(WARN_COUNT_MISMATCH)
    if unassigned_ratio > UNASSIGNED_WARN_RATIO:
        warnings.append(WARN_HIGH_UNASSIGNED)
    if len(minors) >= MANY_MINOR:
        warnings.append(WARN_MANY_MINOR)
    if requested is None and found == 1:
        warnings.append(WARN_SINGLE_FOUND)

    return DiarizationReport(
        engine=engine, version=version, model=model, fingerprint=fingerprint,
        status="warn" if warnings else "ok", warnings=tuple(warnings),
        requested_speakers=requested_speakers, found_speakers=found,
        minor_speakers=len(minors), speech_seconds=round(speech, 1),
        unassigned_ratio=round(unassigned_ratio, 3), overlap_ratio=round(overlap_ratio, 3),
    )


# --- 給人讀的逐字稿 ---


def clock(seconds: float) -> str:
    """mm:ss，分鐘不封頂：超過一小時是 62:05、125:30，不換成 hh:mm:ss。"""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def transcript_lines(turns: Sequence[Turn]) -> str:
    """每段一行：`mm:ss [[Sn]]：文字`。第 k 行就是編號 k 的 Turn；S0 寫成 [[S0]]。

    [[Sn]] 是代號，名字或「發言者 A」之類的標籤只在顯示時才換上去（見 render_placeholders）。
    """
    return "\n".join(
        f"{clock(t.start)} [[{t.speaker}]]：{' '.join(t.text.splitlines())}" for t in turns)
