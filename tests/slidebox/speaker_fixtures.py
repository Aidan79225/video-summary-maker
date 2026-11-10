"""手寫的三人對談：每個字都有時間，也有「真正是誰說的」。

說話者代號：H＝主持的人、G＝受訪的人、V＝只出現一次的來電者（都是測試用的代稱，
程式裡不會有這些角色）。diarizer 的原始標籤是 SPEAKER_00/01/02，normalize_turns
之後依第一次出現變成 S1（H）、S2（G）、S3（V）。

故意放進去的狀況：快速插話、對／不是這類很短的回答、來賓重複主持人的話（回音）、
稱呼（謝謝邱委員）、數字、同時說話（交疊）、Whisper 同一段裡換人、diarizer 沒蓋到的字、
只出現一次的第三個聲音。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from slidebox.domain.entities import SpeakerTurn, Word

RAW = {"H": "SPEAKER_00", "G": "SPEAKER_01", "V": "SPEAKER_02"}
# normalize_turns 之後的代號（H 先開口、G 第二、V 第三）
SID = {"H": "S1", "G": "S2", "V": "S3"}
TRUE_SPEAKER = {sid: who for who, sid in SID.items()}

PER_CHAR = 0.2
_PUNCT = set("，。？！、；：「」『』（）…—,.?!;:")


def tokenize(text: str) -> list[str]:
    """中文一個字一個 Word、標點黏在前一個字、英數連續的一個 Word（Whisper 的樣子）。"""
    words: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _PUNCT:
            if words:
                words[-1] += ch
            else:
                words.append(ch)
            i += 1
        elif ch.isascii() and ch.isalnum():
            j = i
            while j < len(text) and text[j].isascii() and text[j].isalnum():
                j += 1
            words.append(text[i:j])
            i = j
        else:
            words.append(ch)
            i += 1
    return words


@dataclass
class Say:
    who: str
    text: str
    gap: float = 0.5                 # 接在前一句之後的靜默（秒）
    segment: int | None = None       # Whisper 段落編號；None＝新的一段
    diar_start: float = 0.0          # diarizer 把開始抓晚（正）或抓早（負）幾秒
    diar_end: float = 0.0
    diar: bool = True                # False＝diarizer 完全沒蓋到這句
    also: tuple[tuple[str, float, float], ...] = field(default=())  # 同時開口的人（相對這句開始的起訖）


@dataclass
class Fixture:
    words: tuple[Word, ...]
    truth: tuple[str, ...]           # 每個字真正的說話者代號（S1/S2/S3）
    turns: tuple[SpeakerTurn, ...]   # diarizer 的原始輸出（標籤是 SPEAKER_xx）
    spans: tuple[tuple[str, float, float], ...]  # 每句話 (who, start, end)，給測試找位置


def build(script: list[Say], start_at: float = 5.0) -> Fixture:
    words: list[Word] = []
    truth: list[str] = []
    turns: list[SpeakerTurn] = []
    spans: list[tuple[str, float, float]] = []
    clock = start_at
    segment = -1
    previous_end = clock
    for say in script:
        clock = previous_end + say.gap if words else start_at
        if say.segment is None or say.segment != segment:
            segment = say.segment if say.segment is not None else segment + 1
        begin = clock
        for token in tokenize(say.text):
            length = max(1, sum(1 for ch in token if ch not in _PUNCT))
            words.append(Word(clock, clock + PER_CHAR * length, token, segment))
            truth.append(SID[say.who])
            clock += PER_CHAR * length
        previous_end = clock
        spans.append((say.who, begin, clock))
        if say.diar:
            turns.append(SpeakerTurn(begin + say.diar_start, clock + say.diar_end, RAW[say.who]))
        for who, a, b in say.also:
            turns.append(SpeakerTurn(begin + a, begin + b, RAW[who]))
    return Fixture(tuple(words), tuple(truth), _merge(turns), tuple(spans))


def _merge(turns: list[SpeakerTurn]) -> tuple[SpeakerTurn, ...]:
    """碰在一起（或重疊）的同一個人的區段併成一段；有縫的就留著縫（diarizer 的縫就是這樣來的）。"""
    merged: list[SpeakerTurn] = []
    for turn in turns:
        if merged and merged[-1].speaker == turn.speaker and turn.start <= merged[-1].end:
            merged[-1] = SpeakerTurn(merged[-1].start, max(merged[-1].end, turn.end), turn.speaker)
        else:
            merged.append(turn)
    return tuple(merged)


def dialogue() -> Fixture:
    return build(DIALOGUE)


# 第 3 句（G）中間被 H 的「嗯」打斷，前後都有 0.35 秒的停頓：這種插話換人是自然的邊界。
# 「對。」「不是，」是答案，要各自成為一段；來賓 G6 重複了主持人 H6 的話（回音）。
DIALOGUE = [
    Say("H", "今天我們來談少子化，先請教您怎麼看目前的補助政策？", gap=0, segment=0),
    Say("G", "現金補助有幫助，但不能只靠現金，托育資源更重要。", gap=0.4, segment=1),
    Say("H", "那公共托育現在有什麼不足？", gap=0.3, segment=2),
    Say("G", "名額不夠，很多家長排不到，排了兩年也沒有消息。", gap=0.3, segment=3),
    # 快速插話：G 說到一半，H 在停頓裡「嗯」了一聲，G 接著說
    Say("G", "而且費用從去年開始漲了三成", gap=0.4, segment=4),
    Say("H", "嗯", gap=0.35, segment=5),
    Say("G", "很多家長只好放棄。", gap=0.35, segment=6),
    Say("H", "所以您認為補助應該加倍嗎？", gap=0.3, segment=7),
    Say("G", "不是，是要把名額加倍。", gap=0.15, segment=8),           # 以「不是」開頭的回答
    Say("H", "是不是還要提高保母的薪資？", gap=0.2, segment=9),
    Say("G", "對。", gap=0.12, segment=10),                            # 很短的回答，緊接在問句後
    Say("H", "好，那我再確認一次。您剛才說名額不夠，家長排不到，對嗎？", gap=0.1, segment=11),
    Say("G", "對，名額不夠，家長排不到。", gap=0.2, segment=12),       # 回音：重複主持人剛說的話
    Say("H", "謝謝邱委員，那預算要怎麼編才夠？", gap=0.3, segment=13),  # 稱呼裡有姓名與職稱
    Say("G", "預算大約要三千億，比去年多了兩成。至少要把公共托育的名額加倍。", gap=0.4, segment=14),
    # Whisper 同一段（15）裡換人：G 的最後一句與 H 的回應
    Say("G", "我們社區就是這樣。", gap=0.5, segment=15),
    Say("H", "好的，謝謝您。", gap=0.15, segment=15),
    Say("H", "我們現在接聽一位民眾的電話。", gap=0.4, segment=16),
    Say("V", "我想補充，我們社區的托育名額真的不夠。", gap=0.5, segment=17),
    Say("H", "謝謝您的來電。", gap=0.4, segment=18),
    # 同時說話：H 還沒說完，G 的聲音在最後 0.8 秒就進來了（diarizer 看到兩個人）
    Say("H", "最後一個問題，所以其實—", gap=0.4, segment=19, also=(("G", 1.0, 2.0),)),
    Say("G", "我想說的是，公共托育一定要先做。", gap=0.3, segment=20, diar_start=0.0),
    # diarizer 沒蓋到中間的「的」：兩側都是 G，所以算 G
    Say("G", "還有一件事，保母", gap=0.5, segment=21),
    Say("G", "的", gap=0.0, segment=21, diar=False),
    Say("G", "薪資也要調。", gap=0.0, segment=21),
    # diarizer 沒蓋到「請」：一側是 G、另一側是 H，不推測
    Say("G", "這樣可以嗎。", gap=0.5, segment=22),
    Say("H", "請", gap=0.0, segment=23, diar=False),
    Say("H", "回答我們最後一題。", gap=0.0, segment=23),
]


# --- 摘要檢查用：已經切好的 Turn 與說話者清單 ---


def dialogue_turns():
    """三人對談經過對齊與分段之後的 Turn，以及說話者清單（S1、S2 是主要發言者，S3 是其他聲音）。"""
    from slidebox.usecases.speakers import (
        assign_speakers,
        build_turns,
        normalize_turns,
        summarize_speakers,
    )

    fixture = dialogue()
    turns = build_turns(assign_speakers(fixture.words, normalize_turns(fixture.turns)))
    return turns, summarize_speakers(turns)


def make_speakers(*kinds: str):
    """測試用的說話者清單：依序是 S1、S2…，kind 為 participant 或 minor。"""
    from slidebox.domain.entities import SpeakerInfo

    return tuple(
        SpeakerInfo(id=f"S{n}", label=f"發言者 {chr(64 + n)}" if kind == "participant" else f"其他聲音 {n}",
                    kind=kind, seconds=60.0, turns=5, first_seen=0.0, last_seen=60.0)
        for n, kind in enumerate(kinds, 1))


def make_turns(*lines: tuple[str, str], step: float = 10.0):
    """(說話者代號, 文字) → Turn，每段 step 秒、編號從 0。"""
    from slidebox.domain.entities import Turn

    return tuple(Turn(i, i * step, i * step + step - 1.0, speaker, text)
                 for i, (speaker, text) in enumerate(lines))
