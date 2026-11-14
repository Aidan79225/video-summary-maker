"""GPU 回傳的多人說話者結果（契約 v1）：驗證、清理、檢查。

為什麼這裡要這麼嚴：這個功能最重要的是「誰說了什麼」不能安錯人，安錯人比整支影片失敗更糟。GPU 是
另一台機器、另一份程式；它可能比 Pi 舊（沒更新就回舊格式）、可能有 bug、結果可能被截斷。Pi 在動資料庫之前
先確認這份結果真的是契約 v1 的多人結果、內部互相對得起來，否則整個工作失敗、文章不發佈——絕不退回
「講者」一個人的寫法，也不收下一半。

兩層：
  clean_speakers／clean_points／clean_diarization：白名單清理。只留契約裡的鍵、列舉與上限、有限的數字、
    控制字元與雙向覆寫字元一律拿掉（youtube_source.clean_text），不認得的鍵丟掉。形狀不對就丟 JobFailed。
  check_speaker_result：跨欄位的規則（echo 對得上、每個重點的說話者是列出來的主要發言者、逐字稿／條列／完整敘述
    都跟重點對得上…），通過才回傳清理過的結果給 save_result。

名字從來不在 GPU 的結果裡：name、role、basis、merged_into 是 Pi 自己的欄位，只有操作者（label_video）會改。
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from itertools import groupby
from typing import NoReturn

from .gpu_client import JobFailed
from .speakers import PLACEHOLDER, SPEAKER_ID, UNDECIDABLE
from .video_range import RangeInvalid, VideoRange, range_of
from .video_range import clock as time_text
from .youtube_source import UrlRejected, clean_text, parse_video_url, video_id_from_ivod_id

MODE = "diarize"

# 上限。GPU 自己有限制，這裡是第二道：結果寫進資料庫、印在終端機、送到網頁之前，不信任任何一個大小。
# MAX_SPEAKERS 是名單的長度（主要發言者加其他聲音），跟 GPU（usecases/speakers.py）的 MAX_PUBLISHED_SPEAKERS 是
# 同一個數字，兩邊要一起改（網站的 CONTRACT_MAX_SPEAKERS 也是）。它不是品質門檻：能被引用的人另有 MAX_PARTICIPANTS。
MAX_SPEAKERS = 24
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
MAX_BULLET_POINTS = 4       # 一頁的條列只放前幾個重點（GPU 的 conversation.MAX_BULLETS）
DETAIL_LENGTH = 8000
DESCRIPTION_LENGTH = 120
TITLE_LENGTH = 300          # 與 Slide.title 的 max_length 一樣（GPU 自己把標題限制在 60 字）
URL_LENGTH = 500            # 與 Article.ivod_url 的 max_length 一樣
MAX_SECONDS = 24 * 3600     # 一天；影片上限是 --max-minutes（預設 180 分鐘），遠遠不到
MAX_COUNT = 100_000         # 發言段落編號、段落數、重點數
MAX_REQUESTED = 20          # 與 add_video --speakers 的上限一樣
MAX_FOUND = 100             # 找到的人數、其他聲音的數量（GPU 自己在超過 8 位主要發言者時就會失敗）
# 跟 GPU（usecases/speakers.py）一樣的門檻：Pi 要能自己從名單判斷 GPU 該不該失敗、該不該警告，不照單全收
MAX_PARTICIPANTS = 8        # 主要發言者超過這麼多，GPU 就判定聲音區分不可靠、整支失敗
UNASSIGNED_WARN_RATIO = 0.25    # 說話時間裡無法判斷是誰說的超過這個比例，就警告
MANY_MINOR = 4              # 其他聲音有這麼多個以上，就警告
SECONDS_SLACK = 0.05        # 每個人的秒數各自四捨五入到小數一位，加起來最多差這麼多（每人一次，全部說話時間一次）
TIME_SLACK = 1.0            # 重點的時間跟說話者首末時間比對時留的餘地（GPU 的時間只取到小數一位）
DURATION_SLACK = 3.0        # 處理範圍的終點跟影片頁給的長度比對時留的餘地：頁面的長度是整數秒、音訊實際長度可能差一點

KINDS = ("participant", "minor")
STATUSES = ("ok", "warn")
KNOWN_WARNINGS = ("count_mismatch", "high_unassigned", "many_minor", "single_found")

_FLAG = re.compile(r"[a-z][a-z0-9_]{0,31}")
# GPU 給的稱呼只有兩種形狀（speakers.summarize_speakers）：主要發言者依第一次出現的順序叫「發言者 A」「發言者 B」…
# （A～Z，超過 26 位接 AA），其餘叫「其他聲音 1」「其他聲音 2」…。名字與職稱不從 GPU 來，只由操作者用 label_video 設定
_PARTICIPANT_LABEL = re.compile(r"發言者 [A-Z]{1,3}")
_MINOR_LABEL = re.compile(r"其他聲音 [1-9][0-9]?")
_LABEL_SHAPES = {"participant": (_PARTICIPANT_LABEL, "發言者 A、發言者 B…"),
                 "minor": (_MINOR_LABEL, "其他聲音 1、其他聲音 2…")}
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
# 逐字稿的每一行：「mm:ss [[Sn]]：內容」（分鐘可以超過 99）
_TRANSCRIPT_LINE = re.compile(r"(\d{1,4}):([0-5]\d) \[\[(S\d+)\]\]：")
# 條列的每一項：「[[Sn]]：重點」
_BULLET = re.compile(r"\[\[(S[1-9][0-9]{0,2})\]\]：(.+)", re.S)

NOT_UPDATED = ("GPU 主機很可能還沒更新到支援多人區分的版本；請先更新並重啟 GPU 主機，再用 "
               "add_video --rerun 重做")
RANGE_NOT_UPDATED = ("GPU 主機很可能還沒更新到支援處理範圍（video_range）的版本：舊版會悄悄忽略範圍、處理整支影片；"
                     "請先更新並重啟 GPU 主機，再用 add_video --rerun 重做")
# 結果裡有時間落在處理範圍之外的原因：GPU 剪出一段來聽，量到的時間從那一段的開頭算起，存檔前要把範圍的起點加回去
_FORGOT_START = ("GPU 可能沒有把範圍的起點加回時間，或兩邊的版本沒對上：照這樣發佈，時間連結與截圖會跳到影片的別處，"
                 "文章不發佈")


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


# clean_text 留著的、畫面上看不見的字：ZWJ 與 ZWNJ（表情符號的組合與波斯文需要它們），以及幾個空白一樣的填充字
_INVISIBLE = frozenset("\u200c\u200d\u115f\u1160\u3164\uffa0")


def _fold(text: str) -> str:
    """只給「有沒有記號」的判斷用（不存、不顯示）：全形的［］（NFKC 換成半形），以及兩個方括號中間夾了零寬連接
    字元或組合符號的寫法，畫面上都跟真的 [[ 一樣，折成真的再看。"""
    return "".join(char for char in unicodedata.normalize("NFKC", text)
                   if char not in _INVISIBLE and unicodedata.category(char) not in ("Mn", "Me"))


def _has_brackets(text: str) -> bool:
    """有沒有說話者記號的 [[ 或 ]]，連長得像的也算。清理（clean_text）只拿掉 Cc／Cf／Cs，ZWJ 與 ZWNJ 還在，
    所以「先清理再比對原字串」擋不住中間夾了零寬連接字元的 [[S1]]，也擋不住全形的［［S1］］；單個的全形方括號、
    書名號、引號仍是一般文字。"""
    folded = _fold(text)
    return "[[" in folded or "]]" in folded


def _squash(text: str) -> str:
    """連續的空白收成一個：兩邊只差空白的文字不算「寫得不一樣」。"""
    return " ".join(text.split())


def _clock(seconds: int) -> str:
    """5 → 「00:05」、7530 → 「125:30」（跟 GPU 的 clock 一樣，分鐘不封頂）。"""
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


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


def clean_speakers(raw: object, window: tuple[float, float | None] | None = None) -> list[dict]:
    """GPU 的 speakers → 只留契約裡的鍵。Pi 自己的 name、role、basis、merged_into 由 save_result 另外補。

    window 是處理範圍（見 _check_range）：代表的話的時間落在範圍之外的丟掉，跟其他壞掉的代表的話一樣。
    """
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
        shape, example = _LABEL_SHAPES[kind]
        if not shape.fullmatch(label):
            _fail(f"{where}.label {_show(label)} 不是程式給的中性稱呼（{kind} 只會是「{example}」這種）："
                  "名字與職稱只能由操作者用 label_video 設定，GPU 的結果不能自己帶")
        if any(label == s["label"] for s in speakers):
            _fail(f"{where}.label「{label}」重複了：兩位說話者會顯示成同一個名字")
        first_seen = _number(item.get("first_seen"), f"{where}.first_seen")
        last_seen = _number(item.get("last_seen"), f"{where}.last_seen")
        if first_seen > last_seen:
            _fail(f"{where}.first_seen（{first_seen:g}）晚於 last_seen（{last_seen:g}）：還沒開口就已經說完了")
        speakers.append({
            "id": speaker_id,
            "label": label,
            "kind": kind,
            "seconds": _number(item.get("seconds"), f"{where}.seconds"),
            # 列在名單裡的人至少說過一段話（GPU 的名單就是從發言段落整理出來的）
            "turns": _int(item.get("turns"), f"{where}.turns", low=1),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "samples": _clean_samples(item.get("samples"), window),
            "flags": _clean_flags(item.get("flags")),
        })
    return speakers


def _is_seconds(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (math.isfinite(value) if isinstance(value, float) else True) and 0 <= value <= MAX_SECONDS


def _clean_samples(raw: object, window: tuple[float, float | None] | None = None) -> list[dict]:
    """代表性的話（給 label_video --sheet 讓操作者聽音檔取名用）。壞掉的丟掉，不為它讓工作失敗。

    指定了處理範圍的影片：時間不在範圍裡的也算壞掉的（那一句不可能出自被處理的那一段）。
    """
    samples: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if len(samples) >= MAX_SAMPLES:
            break
        if not isinstance(item, dict):
            continue
        start, text = item.get("start"), item.get("text")
        if not _is_seconds(start) or not isinstance(text, str):
            continue
        if window is not None and not _inside(window, start):
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
    cleaned = {
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
    # 只處理了影片的一段才有的兩個鍵（原影片的秒數）：要一起出現、終點在起點之後；沒有範圍就一個鍵都不多
    start, end = raw.get("range_start"), raw.get("range_end")
    if (start is None) != (end is None):
        _fail("diarization.range_start 與 range_end 要一起出現或一起沒有（只有一個沒辦法知道處理的是哪一段）")
    if start is not None:
        start = _number(start, "diarization.range_start")
        end = _number(end, "diarization.range_end")
        if end <= start:
            _fail(f"diarization.range_end（{end:g}）要比 range_start（{start:g}）晚")
        cleaned["range_start"], cleaned["range_end"] = start, end
    return cleaned


def with_pi_fields(speakers: list[dict]) -> list[dict]:
    """存進 Article.speakers 的樣子：GPU 的欄位加上 Pi 自己的（名字只由操作者設定）。"""
    return [{**s, "name": "", "role": "", "basis": "none", "merged_into": ""} for s in speakers]


# --- 檢查 ---


def requested_speaker_count(article) -> int | None:
    """這支影片要求的人數：None＝自動、>= 2＝指定。送出請求和核對結果的 echo 用同一個答案。"""
    count = article.speaker_count
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 2 else None


@dataclass(frozen=True)
class _Line:
    """逐字稿的一行。第 k 行（從 0 算）就是編號 k 的發言段落（turn）。"""
    seconds: int        # mm:ss 換成秒（GPU 的 mm:ss 是那一段開始時間捨去小數）
    speaker: str        # Sn，或 S0＝無法判斷


@dataclass(frozen=True)
class SlideResult:
    title: str          # 清理過的小標：存進資料庫的是這個，不是 GPU 給的原字串
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
      2. 結果的 video_id 與 source_url 都要有、而且都是這支影片的；diarization 的狀態是 ok／warn、至少找到一位說話者，而且
         requested_speakers 跟我們送出去的一樣（自動＝None，指定人數＝那個數字）：接回了另一種設定的工作時會對不上；
      3. speakers 是 1～24 位、編號不重複、種類是 participant／minor、至少一位 participant；稱呼（label）只能是
         程式給的中性形狀（participant＝「發言者 A」…、minor＝「其他聲音 1」…，形狀要跟種類一致）：人名、
         「主持人」這類的字一律拒絕，名字只由操作者用 label_video 設定；名單與 GPU 的區分報告對得上
         （found_speakers／minor_speakers 就是 participant／minor 的人數、participant 不超過 8 位、指定 2 人以上
         時至少 2 位、kept_points 不超過 proposed_points、每人至少說過一段話且首次出現不晚於最後出現、每人的秒數
         加起來不超過全部說話的秒數），而且程式自己從名單算出 GPU 該給的四種警告，GPU 沒給的補上、狀態改成 warn；
      4. 每一頁至少一個重點；每個重點的說話者是列出來的 participant，turn 是整數、start 是有限的非負數、
         text 1～120 字且不含 [[；同一個發言段落（turn）不能被標成兩個人；重點的時間落在這位說話者出現的時間裡；
         每頁的小標是 1～300 字的文字（清理過才存），小標與所有文字都不含 [[ 或 ]]，長得像的（全形的［［、
         夾了零寬連接字元的）也不行；
      5. 逐字稿每一行是「mm:ss [[Sn]]：」開頭、Sn 是列出來的說話者或 S0；重點引用的發言段落（turn）真的在逐字稿裡，
         而且那一行的說話者與時間就是重點寫的（其他聲音與 S0 的段落因此不可能被引用）；條列正好是前 4 個重點各
         一項「[[Sn]]：重點」，完整敘述正好是連續同一人的重點併成一段「[[Sn]]：甲；乙。」、段與段之間一個空格：
         GPU 就是這樣從重點排出來的，三處寫得不一樣就是壞結果；
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
    window = _check_range(article, diarization)

    speakers = clean_speakers(raw_speakers, window)
    participants = {s["id"] for s in speakers if s["kind"] == "participant"}
    if not participants:
        _fail("speakers 裡一位 participant 都沒有")
    diarization = _reconcile(diarization, speakers, expected)
    heard = {s["id"]: (s["first_seen"], s["last_seen"]) for s in speakers}
    if window is not None:
        _check_speakers_inside(window, speakers)

    transcript_text, transcript = _check_transcript(payload.get("transcript_text"), {s["id"] for s in speakers})
    if window is not None:
        _check_transcript_inside(window, transcript)

    raw_slides = payload.get("slides")
    if not isinstance(raw_slides, list) or not 1 <= len(raw_slides) <= MAX_SLIDES:
        _fail(f"slides 要有 1～{MAX_SLIDES} 頁，不是 {_show(raw_slides) if not isinstance(raw_slides, list) else len(raw_slides)}")
    turn_speaker: dict[int, str] = {}
    slides: list[SlideResult] = []
    for position, raw in enumerate(raw_slides):
        slides.append(_check_slide(raw, position, participants, turn_speaker, transcript, heard, window))
    return SpeakerResult(transcript_text, speakers, diarization, slides)


# --- 處理範圍 ---


def _span(window: tuple[float, float | None]) -> str:
    """範圍的白話：「10:00–25:00」（秒數捨去小數）。"""
    start, end = window
    return VideoRange(int(start), None if end is None else int(end)).describe()


def _inside(window: tuple[float, float | None], seconds: float) -> bool:
    """這個時間在處理的範圍裡嗎？兩端各留 TIME_SLACK 秒的餘地（GPU 的時間只取到小數一位、逐字稿的 mm:ss 捨去小數）。"""
    start, end = window
    return start - TIME_SLACK <= seconds and (end is None or seconds <= end + TIME_SLACK)


def _check_range(article, diarization: dict) -> tuple[float, float | None] | None:
    """結果回報的處理範圍要跟我們要求的對得上。回傳這支影片的結果該落在哪一段（起、終），沒有範圍是 None。

    - 沒有要求範圍、結果卻說只處理了一段：接回了另一種設定的工作、或兩邊的版本沒對上——不信任，失敗。
    - 要求了範圍、結果沒有回報：舊版 GPU 不認得 range_start／range_end，會悄悄處理整支影片，那樣結果裡的人與時間
      都不是操作者要的那一段，失敗。只有一個例外：從 0 開始、有給終點的範圍（--end 30:00），GPU 發現終點已經到了
      影片結尾，就是整支影片、不回報範圍——這時結果仍然不能超過終點。
    - 起點跟要求的差不到 TIME_SLACK 秒；終點不能比要求的晚（GPU 把超過影片結尾的終點夾到結尾，所以可以早）；
      沒有給終點的，回報的終點不能超過影片的長度（影片頁給的，留 DURATION_SLACK 秒餘地）。
    """
    try:
        asked = range_of(article)
    except RangeInvalid as e:
        _fail(f"這支影片記著的處理範圍不合理，沒辦法核對結果：{e}")
    start, end = diarization.get("range_start"), diarization.get("range_end")
    if asked is None:
        if start is not None:
            _fail(f"結果說只處理了影片的 {_span((start, end))}，但這支影片沒有指定處理範圍（可能接回了另一種設定的工作，"
                  "或兩邊的版本沒對上；不知道哪一邊才對，為避免時間與說話者對到錯的地方，文章不發佈）")
        return None
    if start is None:
        if asked.start > 0 or asked.end is None:
            _fail(f"這支影片要求只處理 {asked.describe()}，結果卻沒有回報處理的範圍（diarization.range_start／range_end）："
                  f"{RANGE_NOT_UPDATED}。文章不發佈")
        return 0.0, float(asked.end)
    if abs(start - asked.start) > TIME_SLACK:
        _fail(f"結果回報的範圍起點（{time_text(int(start))}）跟要求的（{time_text(asked.start)}）不一樣"
              "（可能接回了另一個範圍的工作）")
    if asked.end is not None:
        if end > asked.end + TIME_SLACK:
            _fail(f"結果回報的範圍終點（{time_text(int(end))}）比要求的（{time_text(asked.end)}）還晚"
                  "（處理了比要求更多的影片）")
    else:
        length = getattr(article, "duration_seconds", 0) or 0
        if length and end > length + DURATION_SLACK:
            _fail(f"結果回報的範圍終點（{time_text(int(end))}）超過影片的長度（{time_text(length)}）")
    return float(start), float(end)


def _check_speakers_inside(window: tuple[float, float | None], speakers: list[dict]) -> None:
    """每位說話者第一次與最後出現的時間都要在處理的範圍裡。"""
    for speaker in speakers:
        for what, key in (("第一次出現", "first_seen"), ("最後出現", "last_seen")):
            if not _inside(window, speaker[key]):
                _fail(f"{speaker['label']}（{speaker['id']}）{what}的時間 {_clock(int(speaker[key]))} "
                      f"不在處理的範圍 {_span(window)} 裡（{_FORGOT_START}）")


def _check_transcript_inside(window: tuple[float, float | None], transcript: list[_Line]) -> None:
    """逐字稿每一行的時間都要在處理的範圍裡（那是原影片的時間，不是剪出來那一段的）。"""
    for number, line in enumerate(transcript, start=1):
        if not _inside(window, line.seconds):
            _fail(f"逐字稿第 {number} 行的時間 {_clock(line.seconds)} 不在處理的範圍 {_span(window)} 裡（{_FORGOT_START}）")


def _check_video(article, payload: dict) -> None:
    """結果自己說的 video_id 與 source_url 都要有，而且都是這支影片的。

    拿到另一支影片的說話者清單，比任何一個格式錯誤都更危險；說不出自己是哪一支影片的結果，更沒有理由相信它。
    GPU 一定會給：video_id 是它處理的那支影片，source_url 是送給它的網址（我們送的是正規網址）。缺了就是兩邊的
    版本沒對上，說明 GPU 可能還沒更新。source_url 只比影片 id，不比整個字串：多一個時間或清單參數、短網址都還是
    同一支影片。
    """
    expected = video_id_from_ivod_id(getattr(article, "ivod_id", "") or "")
    got = payload.get("video_id")
    if got is None:
        _fail_old("沒有 video_id")
    if not isinstance(got, str) or got != expected:
        _fail(f"結果是另一支影片的（video_id 是 {_show(got)}，這支是 {_show(expected)}）")
    url = payload.get("source_url")
    if url is None:
        _fail_old("沒有 source_url")
    if not isinstance(url, str) or len(url) > URL_LENGTH:
        _fail(f"source_url 要是這支影片的網址（不超過 {URL_LENGTH} 個字），不是 {_show(url)}")
    try:
        seen = parse_video_url(clean_text(url)).video_id
    except UrlRejected:
        _fail(f"source_url 不是 YouTube 單一影片的網址：{_show(url)}")
    if seen != expected:
        _fail(f"結果是另一支影片的（source_url 指到 {_show(seen)}，這支是 {_show(expected)}）")


def _check_transcript(raw: object, listed: set[str]) -> tuple[str, list[_Line]]:
    """逐字稿清理過的文字，以及每一行的（時間、說話者）：重點引用的發言段落要拿它們對照。

    空行不算（清理完什麼都不剩）；GPU 一個發言段落一行，所以第 k 個非空行就是編號 k 的段落。
    """
    if not isinstance(raw, str):
        _fail(f"transcript_text 要是文字，不是 {_show(raw)}")
    lines = [line for line in (clean_text(line) for line in raw.split("\n")) if line]
    if not lines:
        _fail("transcript_text 是空的")
    parsed: list[_Line] = []
    for number, line in enumerate(lines, start=1):
        head = _TRANSCRIPT_LINE.match(line)
        if head is None:
            _fail(f"逐字稿第 {number} 行不是「mm:ss [[Sn]]：內容」的格式：{_show(line[:20])}")
        speaker = head.group(3)
        if speaker != UNDECIDABLE and speaker not in listed:
            _fail(f"逐字稿第 {number} 行的 {speaker} 不在 speakers 裡（只能是列出來的人或 S0）")
        if _has_brackets(line[head.end():]):
            _fail(f"逐字稿第 {number} 行的內容裡多出了 [[ 或 ]]")
        parsed.append(_Line(int(head.group(1)) * 60 + int(head.group(2)), speaker))
    return "\n".join(lines), parsed


def _check_slide(raw: object, position: int, participants: set[str], turn_speaker: dict[int, str],
                 transcript: list[_Line], heard: dict[str, tuple[float, float]],
                 window: tuple[float, float | None] | None = None) -> SlideResult:
    where = f"slides[{position}]"
    if not isinstance(raw, dict):
        _fail(f"{where} 要是物件，不是 {_show(raw)}")
    title = raw.get("title")
    if not isinstance(title, str):
        _fail(f"{where}.title 要是文字，不是 {_show(title)}")
    title = clean_text(title)
    if not 1 <= len(title) <= TITLE_LENGTH:
        _fail(f"{where}.title 清理後要 1～{TITLE_LENGTH} 個字，不是 {len(title)} 個")
    if _has_brackets(title):
        _fail(f"{where}.title 含有 [[ 或 ]]（或長得像的字元）：小標不會被換成名字，記號會原樣漏到網頁上")
    # 不是每個 GPU 版本都會給 timestamp 的類型，但有給就要是能存進資料庫的數字（NaN 會讓整篇存不進去）
    if raw.get("timestamp") is not None:
        stamp = _number(raw.get("timestamp"), f"{where}.timestamp")
        if window is not None and not _inside(window, stamp):
            # 截圖是在這個時間抽的：不在範圍裡的話，整個範圍想排除的內容（例如前後的採訪）會出現在網站上
            _fail(f"{where} 的截圖時間 {_clock(int(stamp))} 不在處理的範圍 {_span(window)} 裡（{_FORGOT_START}）")

    points = clean_points(raw.get("points"), participants, f"{where}.points")
    for index, point in enumerate(points):
        if turn_speaker.setdefault(point["turn"], point["speaker"]) != point["speaker"]:
            _fail(f"{where}.points[{index}]：發言段落 {point['turn']} 被標成 {point['speaker']}，"
                  f"但前面已經把它標成 {turn_speaker[point['turn']]}（同一段話不可能是兩個人說的）")
        _check_cited_line(point, transcript, f"{where}.points[{index}]")
        _check_heard(point, heard, f"{where}.points[{index}]")

    bullets = _check_bullets(raw.get("bullets"), points, where)
    detail = _check_detail(raw.get("detail"), points, where)
    return SlideResult(title, bullets, detail, points)


def _reconcile(diarization: dict, speakers: list[dict], requested: int | None) -> dict:
    """名單（speakers）與 GPU 的區分報告（diarization）要互相對得上；回傳補上程式自己算出的警告之後的報告。

    這些數字會公開（API 的 found_speakers、kind、seconds、turns）也會印給操作者看，而且網站的警示條只看
    status／warnings。GPU 的名單與報告是同一份資料算出來的，所以一定一致；對不上就是 GPU 的 bug、接回舊暫存或
    兩邊版本沒對上，不知道哪一邊才對，兩邊都不信。GPU 判定「不可靠」而失敗的情況（超過 8 位主要發言者、指定
    2 人以上卻只找到 1 位）如果還是被送過來，也一併拒絕。
    """
    participants = sum(1 for s in speakers if s["kind"] == "participant")
    minors = len(speakers) - participants
    if diarization["found_speakers"] != participants:
        _fail(f"diarization.found_speakers 是 {diarization['found_speakers']}，但 speakers 裡有 {participants} 位"
              "主要發言者（報告與名單對不上，不知道哪一邊才對）")
    if diarization["minor_speakers"] != minors:
        _fail(f"diarization.minor_speakers 是 {diarization['minor_speakers']}，但 speakers 裡有 {minors} 個"
              "其他聲音（報告與名單對不上，不知道哪一邊才對）")
    if participants > MAX_PARTICIPANTS:
        _fail(f"主要發言者有 {participants} 位，超過上限 {MAX_PARTICIPANTS} 位（GPU 在這種情況會判定聲音區分"
              "不可靠、不產出結果；結果裡還是有的話，可能是兩邊的版本沒對上）")
    if requested is not None and participants == 1:
        _fail(f"要求 {requested} 位說話者，結果卻只有 1 位主要發言者（可能把不同的人併成一個了；GPU 在這種情況會"
              "判定聲音區分不可靠、不產出結果）")
    if diarization["kept_points"] > diarization["proposed_points"]:
        _fail(f"diarization.kept_points（{diarization['kept_points']}）比 proposed_points"
              f"（{diarization['proposed_points']}）還多：留下的重點不可能比提出的多")
    talked = sum(s["seconds"] for s in speakers)
    if talked > diarization["speech_seconds"] + SECONDS_SLACK * (len(speakers) + 1):
        _fail(f"說話者的秒數加起來（{talked:.1f} 秒）比全部說話的時間 diarization.speech_seconds"
              f"（{diarization['speech_seconds']:g} 秒）還多")

    derived = []
    if requested is not None and participants != requested:
        derived.append("count_mismatch")
    if diarization["unassigned_ratio"] > UNASSIGNED_WARN_RATIO:
        derived.append("high_unassigned")
    if minors >= MANY_MINOR:
        derived.append("many_minor")
    if requested is None and participants == 1:
        derived.append("single_found")
    missing = [warning for warning in derived if warning not in diarization["warnings"]]
    if not missing:
        return diarization
    # GPU 沒警告、但數字本身就是該警告的情況：網站的警示條靠這裡，不能只信 GPU 有沒有說
    return {**diarization, "warnings": [*diarization["warnings"], *missing], "status": "warn"}


def _check_heard(point: dict, heard: dict[str, tuple[float, float]], here: str) -> None:
    """重點的時間要落在這位說話者出現的時間裡（第一次開口到最後一次說完）：他那時根本沒有在說話的話，這個
    重點就不可能是他說的。GPU 的首末時間只取到小數一位，所以留 TIME_SLACK 秒的餘地。"""
    first, last = heard[point["speaker"]]
    if not first - TIME_SLACK <= point["start"] <= last + TIME_SLACK:
        _fail(f"{here} 的時間 {_clock(int(point['start']))} 不在 {point['speaker']} 出現的時間"
              f"（{_clock(int(first))}～{_clock(int(last))}）裡：他那時根本沒有在說話")


def _check_cited_line(point: dict, transcript: list[_Line], here: str) -> None:
    """重點引用的發言段落真的在逐字稿裡，而且那一行的說話者與時間就是重點寫的。

    GPU 的重點是從它引用的段落複製說話者與開始時間的（逐字稿的 mm:ss 是同一個時間捨去小數），所以三者一定
    一致。對不上的話，不知道是重點還是逐字稿錯了，網頁上兩個地方的說話者就會不一樣：兩邊都不信。
    其他聲音（minor）與無法判斷（S0）的段落不能被引用，也在這裡一併擋下：那幾行的說話者不會是重點的 participant。
    """
    turn = point["turn"]
    if turn >= len(transcript):
        _fail(f"{here} 引用的發言段落 {turn} 不在逐字稿裡（逐字稿只有 {len(transcript)} 行）")
    line = transcript[turn]
    if line.speaker != point["speaker"]:
        _fail(f"{here} 寫的是 {point['speaker']} 說的，但它引用的發言段落 {turn}（逐字稿第 {turn + 1} 行）是 "
              f"{line.speaker}（重點和逐字稿對不上，不知道哪一邊才對；其他聲音與無法判斷的段落也不能被引用）")
    if int(point["start"]) != line.seconds:
        _fail(f"{here} 的時間 {_clock(int(point['start']))} 跟它引用的逐字稿第 {turn + 1} 行（{_clock(line.seconds)}）"
              "對不上（連結會跳到別人說話的地方）")


def _check_bullets(raw: object, points: list[dict], where: str) -> list[str]:
    """條列正好是這一頁前 MAX_BULLET_POINTS 個重點各一項「[[Sn]]：重點」（GPU 就是這樣排的）。"""
    if not isinstance(raw, list) or len(raw) > MAX_BULLETS:
        _fail(f"{where}.bullets 要是不超過 {MAX_BULLETS} 項的清單")
    if len(raw) > len(points):
        _fail(f"{where}.bullets 有 {len(raw)} 項，比重點（{len(points)} 個）還多")
    expected = points[:MAX_BULLET_POINTS]
    if len(raw) != len(expected):
        _fail(f"{where}.bullets 要是前 {MAX_BULLET_POINTS} 個重點各一項（這一頁有 {len(points)} 個重點，"
              f"所以是 {len(expected)} 項），不是 {len(raw)} 項")
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
        if match.group(1) != expected[index]["speaker"]:
            _fail(f"{where}.bullets[{index}] 寫的是 {match.group(1)}，但對應的重點是 "
                  f"{expected[index]['speaker']} 說的（條列和重點對不上，不知道哪一邊才對）")
        if _squash(match.group(2)) != expected[index]["text"]:
            _fail(f"{where}.bullets[{index}] 的內容跟對應的重點（{where}.points[{index}]）不一樣"
                  "（條列要原樣是重點的文字，不知道哪一邊才對）")
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
    runs = [(speaker, [p["text"] for p in group]) for speaker, group in groupby(points, key=lambda p: p["speaker"])]
    order = [speaker for speaker, _ in runs]
    if PLACEHOLDER.findall(detail) != order:
        _fail(f"{where}.detail 裡的說話者記號順序（{'、'.join(PLACEHOLDER.findall(detail)) or '沒有'}）"
              f"跟重點的說話者順序（{'、'.join(order)}）對不上")
    # 記號順序對了還不夠：每一段的內容也要正是那位說話者的重點（連續同一人的併成一段，用「；」接起來、以「。」收尾）
    expected = " ".join(f"[[{speaker}]]：{'；'.join(texts)}。" for speaker, texts in runs)
    if _squash(detail) != _squash(expected):
        _fail(f"{where}.detail 的內容不是這一頁的重點（每一段要是「[[Sn]]：重點；重點。」，連續同一位說話者的重點"
              "併成一段；多一句、少一句、換了字都不行，不知道哪一邊才對）")
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
