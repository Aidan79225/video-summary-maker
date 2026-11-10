"""多人影片的說話者：[[S1]] 記號換成名字、併掉的編號、給 API 與操作指令用的顯示規則。

GPU 回來的文字用 [[S1]] 這樣的記號標示「誰說的」，名字從來不寫進文字：操作者之後用 label_video 替說話者
取名、把被分成兩個編號的同一個人併起來，都只是改 Article.speakers 這份中繼資料，不必重跑 GPU，也不必改寫
任何一段文字。API 讀取時才把記號換掉——有名字用名字，沒有就是 GPU 給的中性標籤（「發言者 A」）。

這個模組不碰資料庫，也不 import models：models.Article.teaser 要用它。
"""
from __future__ import annotations

import re

UNDECIDABLE = "S0"
UNDECIDABLE_TEXT = "（無法判斷）"
UNKNOWN_TEXT = "（未知發言者）"

# 標題或頻道名稱有這些字，影片多半不是一個人從頭講到尾（add_video --speakers 1 會提醒）
CONVERSATION_HINT = re.compile(r"專訪|訪談|對談|座談|圓桌|來賓|podcast", re.IGNORECASE)

# GPU 給的說話者編號：S1、S2…（S0 是保留的「無法判斷」，從來不會出現在清單裡）
SPEAKER_ID = re.compile(r"S[1-9][0-9]{0,2}")
# 文字裡的記號。\d+ 而不是 SPEAKER_ID：連 [[S0]]、[[S9999]] 這種清單裡沒有的也要認得並換掉，不然會原樣漏到網頁上
PLACEHOLDER = re.compile(r"\[\[(S\d+)\]\]")


def conversation_hint(*texts: object) -> str | None:
    """標題、頻道名稱像是訪談或對談的話，回傳比對到的那個字（印給操作者看原因）；不像就是 None。"""
    for text in texts:
        match = CONVERSATION_HINT.search(text) if isinstance(text, str) else None
        if match:
            return match.group(0)
    return None


def _by_id(speakers: object) -> dict[str, dict]:
    if not isinstance(speakers, list):
        return {}
    return {s["id"]: s for s in speakers if isinstance(s, dict) and isinstance(s.get("id"), str)}


def has_structured_speakers(speakers: object) -> bool:
    """這份清單是多人影片的說話者（每一項是帶 id 的 dict）嗎？

    議會的文章、單人影片是空清單；舊版 GPU 做的是一串字串。這兩種的文字一概不碰（不換記號）。
    """
    return isinstance(speakers, list) and bool(speakers) and all(
        isinstance(s, dict) and isinstance(s.get("id"), str) for s in speakers)


def resolve(speakers: object, speaker_id: str) -> dict | None:
    """這個編號實際代表的說話者：順著 merged_into 走到最後一個（label_video --merge 併進去的那位）。

    merged_into 指向不存在的編號、繞成一圈（A→B→A）、指向自己：這些併法是壞的，不能讓 API 當掉或無限迴圈。
    繞成圈的就當作這位說話者沒有被併（回傳他自己）；指向不存在的編號就停在最後一個找得到的。
    """
    by_id = _by_id(speakers)
    start = by_id.get(speaker_id)
    if start is None:
        return None
    seen = {speaker_id}
    current = start
    while True:
        target = current.get("merged_into")
        if not isinstance(target, str) or not target or target not in by_id:
            return current
        if target in seen:
            return start
        seen.add(target)
        current = by_id[target]


def _bare(value: object) -> str:
    """名字、標籤、角色 → 不含 [[ 與 ]] 的字。label_video 與 GPU 的結果本來就不會有；資料庫被直接改過也不能讓
    名字長得像說話者記號（看起來像沒換掉的殘渣）。"""
    return value.replace("[[", "").replace("]]", "").strip() if isinstance(value, str) else ""


def display_of(speakers: object, speaker_id: str) -> str:
    """版面上這個編號顯示成什麼：名字，沒有名字就是標籤；併掉的跟著併進去的那位。"""
    if speaker_id == UNDECIDABLE:
        return UNDECIDABLE_TEXT
    speaker = resolve(speakers, speaker_id)
    if speaker is None:
        return UNKNOWN_TEXT
    for key in ("name", "label"):
        value = _bare(speaker.get(key))
        if value:
            return value
    return f"發言者 {speaker['id']}"


def render_placeholders(text: object, speakers: object) -> str:
    """把文字裡的 [[Sn]] 換成顯示名稱。

    用 callback 換，絕不把名字當成 re.sub 的樣板字串：名字可能是任何字元（`A.*B`、`$1`、`\\1`、`\\g<0>`），
    當樣板會被解讀成反向參照而弄壞，甚至丟例外。換完之後仍留著的 `[[` 或 `]]`（被截斷的記號、格式不對的記號）
    一律拿掉：沒有換成名字的記號不能漏到網頁上。
    """
    if not isinstance(text, str):
        return ""
    rendered = PLACEHOLDER.sub(lambda match: display_of(speakers, match.group(1)), text)
    return rendered.replace("[[", "").replace("]]", "")


# --- 給 API 的樣子 ---


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def speaker_views(speakers: object) -> list[dict]:
    """Article.speakers → API 的 SpeakerOut。只有結構化的說話者才有；samples 與 merged_into 不公開。

    name、role、basis 取「實際代表的那位」的（被併進去的人跟著併進去的那位走）：版面上顯示的是黃國昌，
    旁邊的徽章就不能還寫著「未確認身分」。
    """
    if not has_structured_speakers(speakers):
        return []
    views = []
    for speaker in speakers:
        effective = resolve(speakers, speaker["id"]) or speaker
        flags = speaker.get("flags")
        views.append({
            "id": speaker["id"],
            "label": _bare(speaker.get("label")),
            "name": _bare(effective.get("name")),
            "role": _bare(effective.get("role")),
            "display": display_of(speakers, speaker["id"]),
            "kind": speaker.get("kind") if speaker.get("kind") in ("participant", "minor") else "participant",
            "seconds": _number(speaker.get("seconds")),
            "turns": int(_number(speaker.get("turns"))),
            "basis": effective.get("basis") if effective.get("basis") in ("none", "operator") else "none",
            "flags": [f for f in flags if isinstance(f, str)] if isinstance(flags, list) else [],
        })
    return views


def point_views(points: object, speakers: object) -> list[dict]:
    """Slide.points → API 的 PointOut（speaker、display、start、text）。evidence 留在資料庫給 label_video --audit 用。"""
    if not isinstance(points, list):
        return []
    return [{"speaker": p["speaker"], "display": display_of(speakers, p["speaker"]),
             "start": _number(p.get("start")), "text": _text(p.get("text"))}
            for p in points if isinstance(p, dict) and isinstance(p.get("speaker"), str)]


def diarization_view(diarization: object) -> dict | None:
    """Article.diarization → API 的 DiarizationOut；沒有區分過（議會、單人）是 None。"""
    if not isinstance(diarization, dict) or diarization.get("status") not in ("ok", "warn"):
        return None
    warnings = diarization.get("warnings")
    requested = diarization.get("requested_speakers")
    return {
        "status": diarization["status"],
        "warnings": [w for w in warnings if isinstance(w, str)] if isinstance(warnings, list) else [],
        "requested_speakers": requested if isinstance(requested, int) and not isinstance(requested, bool) else None,
        "found_speakers": int(_number(diarization.get("found_speakers"))),
    }
