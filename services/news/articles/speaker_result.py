"""GPU 回傳的多人說話者結果（契約 v1）：驗證、清理、檢查。

為什麼這裡要這麼嚴：這個功能最重要的是「誰說了什麼」不能安錯人，安錯人比整支影片失敗更糟。GPU 是
另一台機器、另一份程式；它可能比 Pi 舊（沒更新就回舊格式）、可能有 bug、結果可能被截斷。Pi 在動資料庫之前
先確認這份結果真的是契約 v1 的多人結果、內部互相對得起來，否則整個工作失敗、文章不發佈——絕不退回
「講者」一個人的寫法，也不收下一半。

兩層：
  clean_speakers／clean_points／clean_diarization：白名單清理。只留契約裡的鍵、列舉與上限、有限的數字、
    控制字元與雙向覆寫字元一律拿掉（youtube_source.clean_text），不認得的鍵丟掉。形狀不對就丟 JobFailed。
  check_speaker_result：跨欄位的規則（echo 對得上、每個重點的說話者是列出來的主要發言者、條列與完整敘述裡的
    記號跟重點對得上…），通過才回傳清理過的結果給 save_result。

名字從來不在 GPU 的結果裡：name、role、basis、merged_into 是 Pi 自己的欄位，只有操作者（label_video）會改。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import groupby
from typing import NoReturn

from .gpu_client import JobFailed
from .speakers import PLACEHOLDER, SPEAKER_ID, UNDECIDABLE
from .youtube_source import clean_text, video_id_from_ivod_id

MODE = "diarize"

# 上限。GPU 自己有限制，這裡是第二道：結果寫進資料庫、印在終端機、送到網頁之前，不信任任何一個大小。
MAX_SPEAKERS = 12
MAX_SAMPLES = 3
MAX_FLAGS = 8
MAX_WARNINGS = 8
MAX_SLIDES = 200
MAX_POINTS_PER_SLIDE = 100
MAX_BULLETS = 8
LABEL_LENGTH = 40
SAMPLE_LENGTH = 80
POINT_TEXT_LENGTH = 120
EVIDENCE_LENGTH = 120
BULLET_LENGTH = 300
DETAIL_LENGTH = 8000
DESCRIPTION_LENGTH = 120
MAX_SECONDS = 24 * 3600     # 一天；影片上限是 --max-minutes（預設 180 分鐘），遠遠不到
MAX_COUNT = 100_000         # 發言段落編號、段落數、重點數
MAX_REQUESTED = 20          # 與 add_video --speakers 的上限一樣
MAX_FOUND = 100             # 找到的人數、其他聲音的數量（GPU 自己在超過 8 位主要發言者時就會失敗）

KINDS = ("participant", "minor")
STATUSES = ("ok", "warn")
KNOWN_WARNINGS = ("count_mismatch", "high_unassigned", "many_minor", "single_found")

_FLAG = re.compile(r"[a-z][a-z0-9_]{0,31}")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
# 逐字稿的每一行：「mm:ss [[Sn]]：內容」（分鐘可以超過 99）
_TRANSCRIPT_LINE = re.compile(r"\d{1,4}:[0-5]\d \[\[(S\d+)\]\]：")
# 條列的每一項：「[[Sn]]：重點」
_BULLET = re.compile(r"\[\[(S[1-9][0-9]{0,2})\]\]：(.+)", re.S)

NOT_UPDATED = ("GPU 主機很可能還沒更新到支援多人區分的版本；請先更新並重啟 GPU 主機，再用 "
               "add_video --rerun 重做")


def _fail(message: str) -> NoReturn:
    raise JobFailed(f"GPU 的多人區分結果不合契約，為避免把話安到錯的人頭上，文章不發佈：{message}")


def _fail_old(what: str) -> NoReturn:
    raise JobFailed(f"GPU 回傳的不是多人區分的結果（{what}）：{NOT_UPDATED}。文章不發佈")


def _show(value: object) -> str:
    """把結果裡的值放進錯誤訊息：訊息會印在操作者的終端機、存進資料庫，不能原樣帶進 GPU 給的字串。"""
    try:
        return clean_text(repr(value))[:40]
    except ValueError:      # 幾千位的整數：repr 會超過 Python 的轉換上限
        return "（過大的數字）"


def _has_brackets(text: str) -> bool:
    return "[[" in text or "]]" in text


# --- 原子欄位 ---


def _int(value: object, where: str, low: int = 0, high: int = MAX_COUNT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(f"{where} 要是 {low}～{high} 的整數，不是 {_show(value)}")
    return value


def _number(value: object, where: str, low: float = 0.0, high: float = MAX_SECONDS) -> float:
    """有限的數字。NaN、無限大、布林、字串、超出範圍都不收（int 直接比大小，不先轉 float：10**400 會溢位）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where} 要是數字，不是 {_show(value)}")
    if isinstance(value, float) and not math.isfinite(value):
        _fail(f"{where} 不是有限的數字：{_show(value)}")
    if not low <= value <= high:
        _fail(f"{where} 要在 {low:g}～{high:g} 之間，不是 {_show(value)}")
    return float(value)


def _text(value: object, where: str, *, max_length: int, min_length: int = 1) -> str:
    """一行文字：先清理（拿掉控制字元與雙向覆寫），再檢查長度與記號。順序不能反：零寬字元夾在 [[ 之間的字串
    清理之後會變成真的 [[S1]]，先檢查再清理就漏掉了。"""
    if not isinstance(value, str):
        _fail(f"{where} 要是文字，不是 {_show(value)}")
    cleaned = clean_text(value)
    if not min_length <= len(cleaned) <= max_length:
        _fail(f"{where} 清理後要 {min_length}～{max_length} 個字，不是 {len(cleaned)} 個")
    if _has_brackets(cleaned):
        _fail(f"{where} 不能含有 [[ 或 ]]（那是說話者記號）")
    return cleaned


def _description(value: object, max_length: int = DESCRIPTION_LENGTH) -> str:
    """只給人看的說明文字（引擎、版本、模型）：清理並截短，壞了就當空的，不為它讓整個工作失敗。"""
    return clean_text(value)[:max_length] if isinstance(value, str) else ""


# --- 白名單清理 ---


def clean_speakers(raw: object) -> list[dict]:
    """GPU 的 speakers → 只留契約裡的鍵。Pi 自己的 name、role、basis、merged_into 由 save_result 另外補。"""
    if not isinstance(raw, list):
        _fail(f"speakers 要是清單，不是 {_show(raw)}")
    if not 1 <= len(raw) <= MAX_SPEAKERS:
        _fail(f"speakers 要有 1～{MAX_SPEAKERS} 位，不是 {len(raw)} 位")
    speakers: list[dict] = []
    for position, item in enumerate(raw):
        where = f"speakers[{position}]"
        if not isinstance(item, dict):
            _fail(f"{where} 要是物件，不是 {_show(item)}")
        speaker_id = item.get("id")
        if not isinstance(speaker_id, str) or not SPEAKER_ID.fullmatch(speaker_id):
            _fail(f"{where}.id 要是 S1、S2… 的編號，不是 {_show(speaker_id)}")
        if any(speaker_id == s["id"] for s in speakers):
            _fail(f"{where}.id {speaker_id} 重複了")
        kind = item.get("kind")
        if kind not in KINDS:
            _fail(f"{where}.kind 要是 participant 或 minor，不是 {_show(kind)}")
        label = _text(item.get("label"), f"{where}.label", max_length=LABEL_LENGTH)
        if any(label == s["label"] for s in speakers):
            _fail(f"{where}.label「{label}」重複了：兩位說話者會顯示成同一個名字")
        speakers.append({
            "id": speaker_id,
            "label": label,
            "kind": kind,
            "seconds": _number(item.get("seconds"), f"{where}.seconds"),
            "turns": _int(item.get("turns"), f"{where}.turns"),
            "first_seen": _number(item.get("first_seen"), f"{where}.first_seen"),
            "last_seen": _number(item.get("last_seen"), f"{where}.last_seen"),
            "samples": _clean_samples(item.get("samples")),
            "flags": _clean_flags(item.get("flags")),
        })
    return speakers


def _is_seconds(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (math.isfinite(value) if isinstance(value, float) else True) and 0 <= value <= MAX_SECONDS


def _clean_samples(raw: object) -> list[dict]:
    """代表性的話（給 label_video --sheet 讓操作者聽音檔取名用）。壞掉的丟掉，不為它讓工作失敗。"""
    samples: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if len(samples) >= MAX_SAMPLES:
            break
        if not isinstance(item, dict):
            continue
        start, text = item.get("start"), item.get("text")
        if not _is_seconds(start) or not isinstance(text, str):
            continue
        text = clean_text(text)[:SAMPLE_LENGTH]
        if text and not _has_brackets(text):
            samples.append({"start": float(start), "text": text})
    return samples


def _clean_flags(raw: object) -> list[str]:
    flags: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and _FLAG.fullmatch(item) and item not in flags and len(flags) < MAX_FLAGS:
            flags.append(item)
    return flags


def clean_points(raw: object, participants: set[str], where: str = "points") -> list[dict]:
    """一頁的重點 → {speaker、turn、start、text、evidence}。說話者必須是列出來的「主要發言者」。"""
    if not isinstance(raw, list):
        _fail(f"{where} 要是清單，不是 {_show(raw)}")
    if not raw:
        _fail(f"{where} 是空的：每一頁至少要有一個有人說的重點")
    if len(raw) > MAX_POINTS_PER_SLIDE:
        _fail(f"{where} 有 {len(raw)} 個，超過上限 {MAX_POINTS_PER_SLIDE}")
    points: list[dict] = []
    for position, item in enumerate(raw):
        here = f"{where}[{position}]"
        if not isinstance(item, dict):
            _fail(f"{here} 要是物件，不是 {_show(item)}")
        speaker = item.get("speaker")
        if not isinstance(speaker, str) or speaker not in participants:
            _fail(f"{here}.speaker {_show(speaker)} 不是列出來的主要發言者"
                  "（重點只能出自主要發言者；其他聲音與無法判斷的段落不能被引用）")
        evidence = item.get("evidence")
        if not isinstance(evidence, str):
            _fail(f"{here}.evidence 要是文字，不是 {_show(evidence)}")
        evidence = clean_text(evidence)
        if len(evidence) > EVIDENCE_LENGTH:
            _fail(f"{here}.evidence 超過 {EVIDENCE_LENGTH} 個字")
        points.append({
            "speaker": speaker,
            "turn": _int(item.get("turn"), f"{here}.turn"),
            "start": _number(item.get("start"), f"{here}.start"),
            "text": _text(item.get("text"), f"{here}.text", max_length=POINT_TEXT_LENGTH),
            "evidence": evidence,
        })
    return points


def clean_diarization(raw: object) -> dict:
    """GPU 的 diarization → 白名單。說明性的字串壞了就清空；會影響判斷或顯示的欄位（狀態、人數、數字）壞了就失敗。"""
    if not isinstance(raw, dict):
        _fail(f"diarization 要是物件，不是 {_show(raw)}")
    status = raw.get("status")
    if status not in STATUSES:
        _fail(f"diarization.status 要是 ok 或 warn，不是 {_show(status)}")
    requested = raw.get("requested_speakers")
    if requested is not None:
        requested = _int(requested, "diarization.requested_speakers", low=2, high=MAX_REQUESTED)
    warnings, dropped = [], 0
    for item in raw.get("warnings") if isinstance(raw.get("warnings"), list) else []:
        if item in KNOWN_WARNINGS and item not in warnings and len(warnings) < MAX_WARNINGS:
            warnings.append(item)
        else:
            dropped += 1
    if warnings or dropped:
        # 有警告卻說 ok：網站的警示條看狀態，不看警告清單。沒辦法翻成人話的警告（Pi 不認得的新代碼）也不能就此消失
        status = "warn"
    fingerprint = raw.get("fingerprint")
    return {
        "engine": _description(raw.get("engine")),
        "version": _description(raw.get("version")),
        "model": _description(raw.get("model")),
        "fingerprint": fingerprint if isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint) else "",
        "status": status,
        "warnings": warnings,
        "requested_speakers": requested,
        "found_speakers": _int(raw.get("found_speakers"), "diarization.found_speakers", low=1, high=MAX_FOUND),
        "minor_speakers": _int(raw.get("minor_speakers"), "diarization.minor_speakers", high=MAX_FOUND),
        "speech_seconds": _number(raw.get("speech_seconds"), "diarization.speech_seconds"),
        "unassigned_ratio": _number(raw.get("unassigned_ratio"), "diarization.unassigned_ratio", high=1.0),
        "overlap_ratio": _number(raw.get("overlap_ratio"), "diarization.overlap_ratio", high=1.0),
        "proposed_points": _int(raw.get("proposed_points"), "diarization.proposed_points"),
        "kept_points": _int(raw.get("kept_points"), "diarization.kept_points"),
    }


def with_pi_fields(speakers: list[dict]) -> list[dict]:
    """存進 Article.speakers 的樣子：GPU 的欄位加上 Pi 自己的（名字只由操作者設定）。"""
    return [{**s, "name": "", "role": "", "basis": "none", "merged_into": ""} for s in speakers]


# --- 檢查 ---


def requested_speaker_count(article) -> int | None:
    """這支影片要求的人數：None＝自動、>= 2＝指定。送出請求和核對結果的 echo 用同一個答案。"""
    count = article.speaker_count
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 2 else None


@dataclass(frozen=True)
class SlideResult:
    bullets: list[str]
    detail: str
    points: list[dict]


@dataclass(frozen=True)
class SpeakerResult:
    """check_speaker_result 通過的結果：全部是清理過的，save_result 只存這裡面的東西。"""
    transcript_text: str
    speakers: list[dict]
    diarization: dict
    slides: list[SlideResult]


def check_speaker_result(article, payload: object) -> SpeakerResult:
    """多人影片的 GPU 結果能不能發佈。不能就丟 JobFailed（文章不發佈、舊內容不動）；能就回傳清理過的結果。

    在 save_result 動資料庫與檔案之前呼叫。全部成立才通過：
      1. 結果自己說是多人模式（speaker_mode == "diarize"），不是舊版 GPU 的單人或逐句標記格式；
      2. 結果的 video_id（有給的話）是這支影片的；diarization 的狀態是 ok／warn、至少找到一位說話者，而且
         requested_speakers 跟我們送出去的一樣（自動＝None，指定人數＝那個數字）：接回了另一種設定的工作時會對不上；
      3. speakers 是 1～12 位、編號不重複、種類是 participant／minor、至少一位 participant；
      4. 每一頁至少一個重點；每個重點的說話者是列出來的 participant，turn 是整數、start 是有限的非負數、
         text 1～120 字且不含 [[；同一個發言段落（turn）不能被標成兩個人；
      5. 逐字稿每一行是「mm:ss [[Sn]]：」開頭、Sn 是列出來的說話者或 S0；條列的第 i 項記號就是第 i 個重點的
         說話者、完整敘述裡記號的順序就是重點的說話者依序（連續同一人併成一段）；
      6. brief 一律不收（GPU 在多人模式不會產生；產了也丟掉）。
    """
    if not isinstance(payload, dict):
        _fail(f"結果要是物件，不是 {_show(payload)}")
    mode = payload.get("speaker_mode")
    if mode != MODE:
        _fail_old(f"沒有 speaker_mode={MODE}" + (f"，是 {_show(mode)}" if mode is not None else ""))
    raw_speakers = payload.get("speakers")
    if isinstance(raw_speakers, list) and raw_speakers and all(isinstance(x, str) for x in raw_speakers):
        _fail_old("speakers 是一串文字，這是舊版逐句標記的格式")
    if payload.get("diarization") is None:
        _fail_old("沒有 diarization")
    _check_video(article, payload)

    diarization = clean_diarization(payload.get("diarization"))
    expected = requested_speaker_count(article)
    if diarization["requested_speakers"] != expected:
        _fail("requested_speakers 對不上：我們要求的是"
              f"{'自動判斷人數' if expected is None else f'{expected} 位'}，"
              f"結果回的是 {diarization['requested_speakers']}（可能接回了另一種設定的工作）")

    speakers = clean_speakers(raw_speakers)
    participants = {s["id"] for s in speakers if s["kind"] == "participant"}
    if not participants:
        _fail("speakers 裡一位 participant 都沒有")

    transcript = _check_transcript(payload.get("transcript_text"), {s["id"] for s in speakers})

    raw_slides = payload.get("slides")
    if not isinstance(raw_slides, list) or not 1 <= len(raw_slides) <= MAX_SLIDES:
        _fail(f"slides 要有 1～{MAX_SLIDES} 頁，不是 {_show(raw_slides) if not isinstance(raw_slides, list) else len(raw_slides)}")
    turn_speaker: dict[int, str] = {}
    slides: list[SlideResult] = []
    for position, raw in enumerate(raw_slides):
        slides.append(_check_slide(raw, position, participants, turn_speaker))
    return SpeakerResult(transcript, speakers, diarization, slides)


def _check_video(article, payload: dict) -> None:
    """結果自己說的 video_id 要是這支影片的：拿到另一支影片的說話者清單，比任何一個格式錯誤都更危險。"""
    got = payload.get("video_id")
    if got is None:
        return
    expected = video_id_from_ivod_id(getattr(article, "ivod_id", "") or "")
    if not isinstance(got, str) or got != expected:
        _fail(f"結果是另一支影片的（video_id 是 {_show(got)}，這支是 {_show(expected)}）")


def _check_transcript(raw: object, listed: set[str]) -> str:
    if not isinstance(raw, str):
        _fail(f"transcript_text 要是文字，不是 {_show(raw)}")
    lines = [line for line in (clean_text(line) for line in raw.split("\n")) if line]
    if not lines:
        _fail("transcript_text 是空的")
    for number, line in enumerate(lines, start=1):
        head = _TRANSCRIPT_LINE.match(line)
        if head is None:
            _fail(f"逐字稿第 {number} 行不是「mm:ss [[Sn]]：內容」的格式：{_show(line[:20])}")
        if head.group(1) != UNDECIDABLE and head.group(1) not in listed:
            _fail(f"逐字稿第 {number} 行的 {head.group(1)} 不在 speakers 裡（只能是列出來的人或 S0）")
        if _has_brackets(line[head.end():]):
            _fail(f"逐字稿第 {number} 行的內容裡多出了 [[ 或 ]]")
    return "\n".join(lines)


def _check_slide(raw: object, position: int, participants: set[str], turn_speaker: dict[int, str]) -> SlideResult:
    where = f"slides[{position}]"
    if not isinstance(raw, dict):
        _fail(f"{where} 要是物件，不是 {_show(raw)}")
    title = raw.get("title")
    if isinstance(title, str) and _has_brackets(clean_text(title)):
        _fail(f"{where}.title 含有 [[ 或 ]]：小標不會被換成名字，記號會原樣漏到網頁上")
    # 不是每個 GPU 版本都會給 timestamp 的類型，但有給就要是能存進資料庫的數字（NaN 會讓整篇存不進去）
    if raw.get("timestamp") is not None:
        _number(raw.get("timestamp"), f"{where}.timestamp")

    points = clean_points(raw.get("points"), participants, f"{where}.points")
    for index, point in enumerate(points):
        if turn_speaker.setdefault(point["turn"], point["speaker"]) != point["speaker"]:
            _fail(f"{where}.points[{index}]：發言段落 {point['turn']} 被標成 {point['speaker']}，"
                  f"但前面已經把它標成 {turn_speaker[point['turn']]}（同一段話不可能是兩個人說的）")

    bullets = _check_bullets(raw.get("bullets"), points, where)
    detail = _check_detail(raw.get("detail"), points, where)
    return SlideResult(bullets, detail, points)


def _check_bullets(raw: object, points: list[dict], where: str) -> list[str]:
    if not isinstance(raw, list) or len(raw) > MAX_BULLETS:
        _fail(f"{where}.bullets 要是不超過 {MAX_BULLETS} 項的清單")
    if len(raw) > len(points):
        _fail(f"{where}.bullets 有 {len(raw)} 項，比重點（{len(points)} 個）還多")
    bullets: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            _fail(f"{where}.bullets[{index}] 要是文字，不是 {_show(item)}")
        bullet = clean_text(item)
        match = _BULLET.fullmatch(bullet)
        if match is None or len(bullet) > BULLET_LENGTH:
            _fail(f"{where}.bullets[{index}] 要是「[[Sn]]：重點」、不超過 {BULLET_LENGTH} 個字")
        if _has_brackets(match.group(2)):
            _fail(f"{where}.bullets[{index}] 的內容裡多出了 [[ 或 ]]")
        if match.group(1) != points[index]["speaker"]:
            _fail(f"{where}.bullets[{index}] 寫的是 {match.group(1)}，但對應的重點是 "
                  f"{points[index]['speaker']} 說的（條列和重點對不上，不知道哪一邊才對）")
        bullets.append(bullet)
    return bullets


def _check_detail(raw: object, points: list[dict], where: str) -> str:
    if not isinstance(raw, str):
        _fail(f"{where}.detail 要是文字，不是 {_show(raw)}")
    detail = clean_text(raw)
    if not detail or len(detail) > DETAIL_LENGTH:
        _fail(f"{where}.detail 要是 1～{DETAIL_LENGTH} 個字")
    if _has_brackets(PLACEHOLDER.sub("", detail)):
        _fail(f"{where}.detail 裡有格式不對的 [[ 或 ]]")
    runs = [speaker for speaker, _ in groupby(p["speaker"] for p in points)]
    if PLACEHOLDER.findall(detail) != runs:
        _fail(f"{where}.detail 裡的說話者記號順序（{'、'.join(PLACEHOLDER.findall(detail)) or '沒有'}）"
              f"跟重點的說話者順序（{'、'.join(runs)}）對不上")
    return detail


def check_single_result(payload: object) -> None:
    """單人影片（--speakers 1）的結果不該長得像多人結果。

    接回了另一種模式的 GPU 工作、或 GPU 搞錯模式時，結果會帶著 [[S1]] 記號——單人文章沒有說話者清單，
    記號不會被換掉，會原樣漏到網頁上，而且那些話其實是多人的。所以擋下來，不發佈。
    """
    if not isinstance(payload, dict):
        return
    texts = [payload.get("transcript_text")]
    for slide in payload.get("slides") if isinstance(payload.get("slides"), list) else []:
        if isinstance(slide, dict):
            texts.extend([slide.get("detail"), *(slide.get("bullets") if isinstance(slide.get("bullets"), list) else [])])
    if payload.get("speaker_mode") == MODE or any(isinstance(t, str) and PLACEHOLDER.search(t) for t in texts):
        raise JobFailed("這支影片是以單人模式（--speakers 1）送出的，GPU 卻回了多人區分的結果"
                        "（可能接回了另一種模式的工作）；為避免把話安到錯的人頭上，文章不發佈。"
                        "要重做請用 add_video --rerun")
