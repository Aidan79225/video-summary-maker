"""說話者對照表：add_video 做完時印，label_video --sheet 也印。

操作者靠這張表決定每個編號是誰：每位說話者附幾句代表的話，每句帶一個 YouTube 的連結直接跳到那一秒，
聽一下就知道是誰。程式從來不替操作者猜名字（只有操作者聽過音檔，名字才有根據）。

只讀資料庫裡的東西，不呼叫 GPU。印出來的字串都再過一次 clean_text：資料庫裡的字串進過清理，但終端機不能
賭這一點。
"""
from __future__ import annotations

import math

from .models import Article
from .speakers import display_of, has_structured_speakers, resolve
from .youtube_source import clean_text, video_id_from_ivod_id

# GPU 的警告代碼 → 給操作者看的白話
WARNING_TEXT = {
    "count_mismatch": "分出來的人數跟你指定的不一樣：指定的人數是「每個講超過約 30 秒的聲音都算」，"
                      "包括連線進來的人；多算一個人比少算一個人好修（併起來就好），併錯的沒辦法事後修",
    "high_unassigned": "有一大段話分不出是誰在講（逐字稿裡標成「無法判斷」），可能是重疊說話、雜音或音質太差",
    "many_minor": "出現了很多只講了幾句話的聲音（標成「其他聲音」）：可能是同一個人被切成好幾個",
    "single_found": "只分出一位說話者：可能這支影片其實只有一個人在講，也可能不同的人被併在一起了",
}


def plain(value: object) -> str:
    """資料庫裡取出來的字，印到終端機之前再清一次；不是字串就是空的。"""
    return clean_text(value) if isinstance(value, str) else ""


def whole_seconds(value: object) -> int:
    """資料庫裡的秒數 → 非負整數秒。不是有限的數字（壞資料）就是 0，不讓一個壞值害整張表印不出來。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return max(0, int(value))


def watch_link(video_id: str, seconds: object, lead: int = 0) -> str:
    """跳到 YouTube 影片的第幾秒。lead 是往前多帶幾秒（聽一句話的開頭要早一點進去）。"""
    return f"https://www.youtube.com/watch?v={video_id}&t={max(0, whole_seconds(seconds) - lead)}s"


def clock(seconds: object) -> str:
    """75.4 → 「01:15」；分鐘可以超過 99（跟逐字稿一樣），不加小時。"""
    whole = whole_seconds(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def video_id_of(article: Article) -> str | None:
    return video_id_from_ivod_id(article.ivod_id)


def kind_text(speaker: dict) -> str:
    return "其他聲音（講得少）" if speaker.get("kind") == "minor" else "主要發言者"


def describe(speakers: list[dict], speaker: dict) -> str:
    """一位說話者現在在版面上顯示成什麼，附上角色與併入的對象。"""
    shown = plain(display_of(speakers, speaker["id"]))
    target = resolve(speakers, speaker["id"])
    if target is not None and target["id"] != speaker["id"]:
        return f"併入 {plain(target['id'])}，顯示成「{shown}」"
    role = plain(speaker.get("role"))
    return f"{shown}（{role}）" if role else shown


def rerun_loss_note(article: Article) -> str | None:
    """重跑會丟掉的操作者設定（名字、角色、併起來的編號），印給操作者看；沒有設定過就是 None。

    重跑之後說話者編號可能全部重新排：把舊名字掛到新編號上等於安錯人，所以重跑一律不帶過去。
    """
    speakers = article.speakers
    if not has_structured_speakers(speakers):
        return None
    named, merged = [], []
    for speaker in speakers:
        name, role = plain(speaker.get("name")), plain(speaker.get("role"))
        if name or role:
            named.append(f"{speaker['id']}={name or '（沒有名字）'}" + (f"（{role}）" if role else ""))
        target = plain(speaker.get("merged_into"))
        if target:
            merged.append(f"{speaker['id']}→{target}")
    if not named and not merged:
        return None
    note = "重跑後說話者編號可能改變，之前設定的名字不會保留：" + ("、".join(named) if named else "（沒有）")
    if merged:
        note += "；併起來的編號也會恢復成各自獨立：" + "、".join(merged)
    return note


def next_step_lines(article: Article) -> list[str]:
    """add_video 做完之後印的下一步。名字只有聽過音檔的人才知道，程式不替你猜。"""
    return [
        "下一步：每個編號是誰，只有聽過音檔的人才確定，程式不會猜。開上面的連結聽一下，再執行：",
        f"  python manage.py label_video {article.slug} --name S1=名字 --name S2=名字",
        "  加職稱：--role S1=主持人　同一個人被分成兩個編號：--merge S3=S1　名字取錯了：--clear S1",
        f"  抽查幾個重點有沒有安對人：python manage.py label_video {article.slug} --audit 5",
        "設定完不必重跑 GPU，網站上馬上會換成名字；沒設定之前，網站上只顯示「發言者 A」這種中性稱呼。",
    ]


def warning_lines(article: Article) -> list[str]:
    diarization = article.diarization if isinstance(article.diarization, dict) else {}
    lines = []
    for code in diarization.get("warnings") or []:
        words = WARNING_TEXT.get(code) if isinstance(code, str) else None
        lines.append(f"  ! {words or f'GPU 回報了一個這一版不認得的警告：{plain(code) or plain(str(code))}'}")
    return lines


def sheet_lines(article: Article) -> list[str]:
    """對照表。只有結構化的說話者才有；沒有就回空清單。"""
    speakers = article.speakers
    if not has_structured_speakers(speakers):
        return []
    video_id = video_id_of(article)
    diarization = article.diarization if isinstance(article.diarization, dict) else {}
    lines = ["說話者對照表（用聲音自動區分，不是程式猜出誰是誰；沒有設定名字的編號，網頁上只顯示中性的標籤）"]
    for speaker in speakers:
        minutes = whole_seconds(speaker.get("seconds")) / 60
        samples = speaker.get("samples") if isinstance(speaker.get("samples"), list) else []
        lines.append(
            f"  {plain(speaker['id'])}　{plain(speaker.get('label'))}　{kind_text(speaker)}　"
            f"{minutes:.1f} 分鐘、{whole_seconds(speaker.get('turns'))} 段　"
            f"{clock(speaker.get('first_seen'))}～{clock(speaker.get('last_seen'))}")
        lines.append(f"      目前顯示：{describe(speakers, speaker)}")
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            link = f" {watch_link(video_id, sample.get('start'))}" if video_id else ""
            lines.append(f"      「{plain(sample.get('text'))}」{clock(sample.get('start'))}{link}")
    if diarization:
        requested = diarization.get("requested_speakers")
        wanted = f"指定 {whole_seconds(requested)} 位" if requested is not None else "自動判斷人數"
        status = "有警告" if diarization.get("status") == "warn" else "正常"
        lines.append(f"區分結果：{status}（{wanted}，找到 {whole_seconds(diarization.get('found_speakers'))} 位主要發言者、"
                     f"{whole_seconds(diarization.get('minor_speakers'))} 個其他聲音）")
        lines.extend(warning_lines(article))
    return lines
