"""多人模式（speaker_mode="diarize"）用的零件：存檔指紋、逐窗的摘要迴圈、彙整與可靠度檢查、來源註記。

整條流程（下載音訊 → 說話者區分 → 語音辨識 → 對齊 → 摘要 → 出片）在
BuildDeckUseCase._execute_diarized；那裡管 IO 與取消，這裡管「結果可不可信」。

鐵則：**寧可失敗，也不發佈歸屬不可靠的成品。** 模型提出的重點每一個都過
conversation.validate_points（說話者由程式從被引用的那一段查出來）；被丟掉太多、
或有一段一頁都留不下來，就 raise AttributionUnreliable，工作失敗、什麼都不發佈。
門檻（被丟掉的比例上限…）都是暫定值，要用實際影片校正。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..domain.entities import DiarizationReport, Settings, Slide, SpeakerInfo, Turn
from ..domain.errors import AttributionUnreliable, SummarizerOutputInvalid
from ..domain.ports import CancelCheck, ConversationSummarizer, ProgressCallback
from .conversation import (
    PROMPT_VERSION,
    Allocation,
    CheckedSlide,
    ValidationResult,
    format_window,
    render_slide,
    validate_points,
)
from .speakers import (
    ALIGN_VERSION,
    PARTICIPANT,
    WARN_COUNT_MISMATCH,
    WARN_HIGH_UNASSIGNED,
    WARN_MANY_MINOR,
    WARN_SINGLE_FOUND,
)

# 模型提出的重點被程式檢查丟掉超過這個比例（整份，或單一窗），這份摘要就不可信（暫定值）
MAX_DROPPED_RATIO = 0.25
# 每個窗最多問模型幾次：第二次會帶著第一次被退回的原因
ATTEMPTS = 2

SOURCE_NOTE = "由語音辨識產生，說話者由聲音自動區分；標籤可能有誤，未經人工確認"


# --- 存檔指紋 ---


def stage_fingerprint(*parts) -> str:
    """產生某一步時用到的設定與輸入。存檔的指紋對不上就重做那一步，不拿舊設定的結果冒充。"""
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def diarization_fingerprint(diarizer_fingerprint: str, speaker_count: int | None) -> str:
    """說話者區分：引擎、版本、模型（diarizer 自己的指紋）與指定的人數。"""
    return stage_fingerprint("diarize", diarizer_fingerprint, speaker_count)


def words_fingerprint(whisper_model: str, speech_hint: str | None) -> str:
    """語音辨識：模型與提示。ALIGN_VERSION 也算進來——改了 transcribe_words 的參數或對齊之前的
    字的處理，要升版，舊的字才不會被拿來冒充。"""
    return stage_fingerprint("multi", whisper_model, speech_hint or "", ALIGN_VERSION)


def conversation_fingerprint(settings: Settings, chunk_chars: int,
                             plan: Sequence[tuple[int, int, str]]) -> str:
    """摘要：模型、頁數範圍、窗的大小、提示版本，以及餵給模型的每個窗（頁數範圍與文字）。

    plan 是每個窗的 (min_slides, max_slides, 窗的文字)：說話者區分、語音辨識、對齊或分段變了，
    窗的文字就變了，舊的摘要不會被拿來冒充。detailed 不算：多人模式用不到它。
    """
    return stage_fingerprint("multi", settings.model, settings.num_ctx, settings.min_slides,
                             settings.max_slides, chunk_chars, PROMPT_VERSION, list(plan))


# --- 逐窗的摘要 ---


@dataclass(frozen=True)
class WindowOutcome:
    """一個窗的結果。proposed／kept 是模型提出與通過檢查的重點數；skipped 是整個窗沒有任何
    主要發言者可以引用，所以沒有送給模型。"""
    allocation: Allocation
    slides: tuple[CheckedSlide, ...]
    proposed: int
    kept: int
    skipped: bool = False
    trimmed_pages: int = 0


def dropped_ratio(proposed: int, kept: int) -> float:
    return (proposed - kept) / proposed if proposed > 0 else 1.0


def _acceptable(result: ValidationResult) -> bool:
    return dropped_ratio(result.proposed, result.kept) <= MAX_DROPPED_RATIO


def _quality(result: ValidationResult) -> tuple[bool, int, int]:
    """兩次嘗試比較用（越大越好）：先看過不過關，再看留下的重點數，最後看被丟掉的重點越少越好
    （丟掉的是模型亂寫的）。過關的那一次一定贏過沒過關的，這樣「過關就停」與「選哪一次」不會打架。"""
    return _acceptable(result), result.kept, result.kept - result.proposed


def fit_to_range(result: ValidationResult, max_slides: int) -> tuple[ValidationResult, int]:
    """頁數超過這個窗的上限時只留前面幾頁（模型多寫的頁不會送去抓影片片段、截圖）。

    被砍掉的頁不算「被檢查丟掉」：提出與留下的點數都扣掉它們的點，否則多寫幾頁就會被當成歸屬不可靠。
    回傳（調整後的結果, 被砍掉的頁數）。
    """
    if len(result.slides) <= max_slides:
        return result, 0
    cut = result.slides[max_slides:]
    removed = sum(len(s.points) for s in cut)
    return (replace(result, slides=result.slides[:max_slides],
                    proposed=result.proposed - removed, kept=result.kept - removed),
            len(cut))


def retry_hint(problems: Sequence[str]) -> str:
    """第二次問模型時附上的、第一次被程式檢查退回的原因。"""
    lines = "\n".join(f"- {problem}" for problem in problems) or "- 沒有任何一頁通過檢查"
    return "上一次的結果有這些問題，請修正後重新產出（只修正被指出的地方）：\n" + lines


def _has_citable_turn(allocation: Allocation, speakers: Sequence[SpeakerInfo]) -> bool:
    citable = {s.id for s in speakers if s.kind == PARTICIPANT}
    return any(t.speaker in citable for t in allocation.window.turns)


def summarize_windows(
    conversation: ConversationSummarizer,
    allocations: Sequence[Allocation],
    turns: Sequence[Turn],
    speakers: Sequence[SpeakerInfo],
    progress: ProgressCallback,
    check: Callable[[], None],
    cancelled: CancelCheck,
) -> tuple[WindowOutcome, ...]:
    """一個窗一個窗地問模型、檢查它提出的重點。

    每個窗最多問兩次：第一次的結果過了就用；沒過（一頁都沒留下、或被丟掉的重點超過 25%）
    就帶著被退回的原因再問一次。兩次裡過關的算數；都沒過關就選留下來的重點多的（一樣多就選
    被丟掉的少的，還是一樣就用第一次），再交給整份的比例檢查決定。模型的回應格式壞掉
    （SummarizerOutputInvalid）也算一次；兩次都壞才放棄。連不上模型（SummarizerUnavailable）
    不在這裡處理，原樣往上丟。沒有任何主要發言者可以引用的窗不送給模型（沒有東西可以引用）。
    """
    outcomes: list[WindowOutcome] = []
    total = len(allocations)
    for number, allocation in enumerate(allocations, 1):
        window = allocation.window
        where = f"第 {number}/{total} 段"
        if not _has_citable_turn(allocation, speakers):
            progress((number - 1) / total, f"{where}沒有主要發言者可以整理，略過")
            outcomes.append(WindowOutcome(allocation, (), 0, 0, skipped=True))
            continue
        text = format_window(window, speakers)

        def inner(frac: float | None, status: str, number: int = number, where: str = where) -> None:
            progress((number - 1) / total, f"{where}：{status}")

        best: ValidationResult | None = None
        trimmed = 0
        hint = ""
        for attempt in range(1, ATTEMPTS + 1):
            check()
            progress((number - 1) / total,
                     f"整理{where}對談…" if attempt == 1 else f"{where}不合要求，重試一次…")
            try:
                raw = conversation.summarize_window(
                    text, window.first_turn, window.last_turn, allocation.min_slides,
                    allocation.max_slides, hint, inner, cancelled)
            except SummarizerOutputInvalid as e:
                if attempt == ATTEMPTS and best is None:
                    raise SummarizerOutputInvalid(
                        f"{where}的模型輸出重試後仍不符合要求（{e}）。可以試試換一個模型。") from e
                hint = retry_hint([str(e)])
                continue
            result, cut = fit_to_range(validate_points(raw, turns, speakers, window), allocation.max_slides)
            if best is None or _quality(result) > _quality(best):
                best, trimmed = result, cut
            if _acceptable(result):
                break
            hint = retry_hint(result.problems)
        assert best is not None                  # 迴圈裡不是 return 就是 raise 或有結果
        outcomes.append(WindowOutcome(allocation, best.slides, best.proposed, best.kept,
                                      trimmed_pages=trimmed))
    return tuple(outcomes)


def check_reliability(outcomes: Sequence[WindowOutcome]) -> None:
    """有一段一頁都留不下來、或整份被丟掉的重點太多，就 raise AttributionUnreliable（訊息帶數字）。"""
    total = len(outcomes)
    for number, outcome in enumerate(outcomes, 1):
        if outcome.skipped or outcome.slides:
            continue
        window = outcome.allocation.window
        raise AttributionUnreliable(
            f"第 {number}/{total} 段對談（T{window.first_turn}–T{window.last_turn}）沒有任何一頁通過程式檢查"
            f"（模型提出 {outcome.proposed} 個重點、留下 {outcome.kept} 個）；"
            f"寧可不發佈，也不寫錯是誰說的")
    proposed, kept = totals(outcomes)
    if proposed <= 0:
        raise AttributionUnreliable("沒有任何一段對談整理出重點；不發佈")
    if dropped_ratio(proposed, kept) > MAX_DROPPED_RATIO:
        raise AttributionUnreliable(
            f"模型提出的 {proposed} 個重點裡，有 {proposed - kept} 個沒有通過程式檢查"
            f"（{dropped_ratio(proposed, kept):.0%}，上限 {MAX_DROPPED_RATIO:.0%}）；"
            f"說話者的歸屬不可靠，不發佈")


def totals(outcomes: Sequence[WindowOutcome]) -> tuple[int, int]:
    """(模型提出的重點數, 通過檢查留下的重點數)。"""
    return sum(o.proposed for o in outcomes), sum(o.kept for o in outcomes)


def assemble(outcomes: Sequence[WindowOutcome]) -> tuple[Slide, ...]:
    """所有窗的頁依「第一個被引用的 turn」排序、從 1 重新編號，排成投影片。

    每個窗的頁數是各自決定的，窗與窗之間的順序以對談的順序為準，不看模型回傳的順序。
    """
    ordered = sorted((slide for outcome in outcomes for slide in outcome.slides),
                     key=lambda slide: slide.points[0].turn)         # 穩定排序：同一段開頭的維持模型給的順序
    return tuple(render_slide(index, slide.title, slide.points) for index, slide in enumerate(ordered, 1))


# --- 來源註記 ---


def _warning_text(report: DiarizationReport, code: str) -> str:
    if code == WARN_COUNT_MISMATCH:
        return f"指定 {report.requested_speakers} 人，實際找到 {report.found_speakers} 位主要發言者"
    if code == WARN_HIGH_UNASSIGNED:
        return f"約 {report.unassigned_ratio:.0%} 的說話時間無法判斷是誰在說"
    if code == WARN_MANY_MINOR:
        return f"有 {report.minor_speakers} 個只出現一下的聲音，未納入重點"
    if code == WARN_SINGLE_FOUND:
        return "只找到 1 位主要發言者"
    return code


def source_note(report: DiarizationReport) -> str:
    """寫進成品的來源註記：固定的那一句，加上說話者區分的警告（有的話）。"""
    warnings = [_warning_text(report, code) for code in report.warnings]
    return SOURCE_NOTE + ("；" + "；".join(warnings) if warnings else "")
