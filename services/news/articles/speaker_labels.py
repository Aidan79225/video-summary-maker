"""label_video 的規則：替說話者取名字、設角色、併人、清除。先把每一項都驗證完，才算出新的清單。

這份清單決定網站上「誰說了什麼」顯示成誰，所以寧可嚴格也不猜：
  * 名字、角色只能由操作者給（GPU 的結果從來沒有名字）；
  * 名字不能含 [[ ]]（說話者記號）、冒號（版面是「名字：內容」）、換行；長度有上限；
  * 兩位不同的說話者不能顯示成同一個字（讀者分不出來；真的是同一個人請用 --merge）；
  * 併人只併一層：被併進去的那位不能自己再被併，也不能繞成圈；主要發言者不能併進其他聲音；
  * 任何一項不對，整批都不套用。

純函式，不碰資料庫；寫進資料庫的是指令（label_video）。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .speakers import (
    SPEAKER_ID,
    UNDECIDABLE,
    UNDECIDABLE_TEXT,
    UNKNOWN_TEXT,
    display_of,
    has_structured_speakers,
    resolve,
)
from .youtube_source import clean_text

NAME_LENGTH = 40
ROLE_LENGTH = 20
# 換行不是「清掉就好」的字元：貼上多行文字多半是貼錯了東西，直接拒絕
_LINE_BREAKS = "\n\r\v\f\x85  "
_SEPARATORS = ("=", "＝")
_FORBIDDEN = ("[[", "]]", ":", "：")


class LabelError(ValueError):
    """操作者給的設定不對。訊息就是原因（可能有好幾行）；一項都不套用。"""


@dataclass
class Labels:
    """一次 label_video 要做的事（還沒驗證）。"""
    names: list[tuple[str, str]] = field(default_factory=list)
    roles: list[tuple[str, str]] = field(default_factory=list)
    merges: list[tuple[str, str]] = field(default_factory=list)
    clears: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.names or self.roles or self.merges or self.clears)


def split_pair(raw: str, flag: str) -> tuple[str, str]:
    """「S1=黃國昌」→ ("S1", "黃國昌")。只切第一個等號（名字裡可以有等號）；全形＝也認。"""
    positions = [raw.index(sep) for sep in _SEPARATORS if sep in raw]
    if not positions:
        raise LabelError(f"{flag} 的格式是 S1=內容，不是 {clean_text(raw)[:40]!r}")
    cut = min(positions)
    return raw[:cut].strip(), raw[cut + 1:]


def check_name(value: str, flag: str, limit: int) -> str:
    """名字或角色 → 清理過、可以存的一行字。不行就丟 LabelError。"""
    if any(char in value for char in _LINE_BREAKS):
        raise LabelError(f"{flag} 的內容不能換行")
    cleaned = clean_text(value)
    if not cleaned:
        raise LabelError(f"{flag} 的內容是空的（要清掉設定請用 --clear）")
    for forbidden in _FORBIDDEN:
        if forbidden in cleaned:
            raise LabelError(f"{flag} 的內容不能含有「{forbidden}」：{cleaned[:40]!r}"
                             "（[[ ]] 是說話者記號，冒號會讓「名字：內容」分不出誰說的）")
    if len(cleaned) > limit:
        raise LabelError(f"{flag} 的內容最多 {limit} 個字，這個有 {len(cleaned)} 個")
    if SPEAKER_ID.fullmatch(cleaned) or cleaned in (UNDECIDABLE_TEXT, UNKNOWN_TEXT):
        raise LabelError(f"{flag} 的內容不能是 {cleaned}：那是程式用來表示說話者編號或「無法判斷」的字")
    return cleaned


def apply_labels(speakers: object, labels: Labels) -> list[dict]:
    """驗證全部的設定，通過才回傳新的說話者清單（不動傳進來的）。不通過丟 LabelError（每個問題一行）。"""
    if not has_structured_speakers(speakers):
        raise LabelError("這支影片沒有說話者的資料")
    result = copy.deepcopy(speakers)
    for row in result:
        row.setdefault("name", "")
        row.setdefault("role", "")
        row.setdefault("basis", "none")
        row.setdefault("merged_into", "")
    rows = {row["id"]: row for row in result}
    errors: list[str] = []

    def known(speaker_id: str, flag: str) -> bool:
        if speaker_id == UNDECIDABLE:
            errors.append(f"{flag}：S0 是保留給「無法判斷」的，不是說話者")
        elif not SPEAKER_ID.fullmatch(speaker_id):
            errors.append(f"{flag}：{clean_text(speaker_id)[:20]!r} 不是說話者編號（要像 S1、S2）")
        elif speaker_id not in rows:
            errors.append(f"{flag}：這支影片沒有 {speaker_id}（有：{'、'.join(rows)}）")
        else:
            return True
        return False

    def once(pairs: list[tuple[str, str]], flag: str) -> dict[str, str]:
        seen: dict[str, str] = {}
        for speaker_id, value in pairs:
            if speaker_id in seen:
                errors.append(f"{flag}：{speaker_id} 給了不只一次")
            seen[speaker_id] = value
        return seen

    names, roles = once(labels.names, "--name"), once(labels.roles, "--role")
    merges = once(labels.merges, "--merge")
    clears = list(dict.fromkeys(labels.clears))

    clean_names: dict[str, str] = {}
    clean_roles: dict[str, str] = {}
    for flag, given, limit, out in (("--name", names, NAME_LENGTH, clean_names),
                                    ("--role", roles, ROLE_LENGTH, clean_roles)):
        for speaker_id, value in given.items():
            if not known(speaker_id, flag):
                continue
            try:
                out[speaker_id] = check_name(value, f"{flag} {speaker_id}", limit)
            except LabelError as e:
                errors.append(str(e))
    for speaker_id in clears:
        known(speaker_id, "--clear")
    # --merge A=B：A 併進 B（A 的話之後顯示成 B）
    for merged, into in merges.items():
        if known(merged, "--merge") & known(into, "--merge"):
            if merged == into:
                errors.append(f"--merge {merged}={into}：不能併給自己")
            elif rows[merged]["kind"] == "participant" and rows[into]["kind"] == "minor":
                errors.append(f"--merge {merged}={into}：主要發言者（{merged}）不能併進只講了幾句的其他聲音"
                              f"（{into}）；要併的話方向反過來：--merge {into}={merged}")
    for merged, into in merges.items():
        for flag, given in (("--name", clean_names), ("--role", clean_roles)):
            if merged in given:
                errors.append(f"{flag} {merged}：{merged} 同時被併進 {into}；"
                              "被併掉的編號沒有自己的名字，名字要設在併進去的那位")
    if errors:
        raise LabelError("\n".join(errors))

    # 套用的順序固定：先清除、再設名字與角色、最後併人
    for speaker_id in clears:
        row = rows[speaker_id]
        row.update(name="", role="", basis="none", merged_into="")
    for speaker_id, value in clean_names.items():
        if rows[speaker_id]["merged_into"]:
            errors.append(f"--name {speaker_id}：{speaker_id} 已經併進 {rows[speaker_id]['merged_into']}；"
                          f"名字要設在 {rows[speaker_id]['merged_into']}，或先 --clear {speaker_id} 把它分開")
            continue
        rows[speaker_id].update(name=value, basis="operator")
    for speaker_id, value in clean_roles.items():
        if rows[speaker_id]["merged_into"]:
            errors.append(f"--role {speaker_id}：{speaker_id} 已經併進 {rows[speaker_id]['merged_into']}；"
                          f"角色要設在 {rows[speaker_id]['merged_into']}，或先 --clear {speaker_id} 把它分開")
            continue
        rows[speaker_id].update(role=value, basis="operator")
    for merged, into in merges.items():
        # 被併掉的編號不留自己的名字與角色（留著也看不到，之後 --clear 分開時卻會冒出來）
        rows[merged].update(name="", role="", basis="none", merged_into=into)
    errors.extend(_final_state_errors(result, rows))
    if errors:
        raise LabelError("\n".join(errors))
    return result


def _final_state_errors(result: list[dict], rows: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    for row in result:
        target = row["merged_into"]
        if not target:
            continue
        if target == row["id"] or target not in rows:
            errors.append(f"{row['id']} 併進了 {target or '（空）'}，這不是這支影片的說話者")
        elif rows[target]["merged_into"]:
            errors.append(f"{row['id']} 併進 {target}，但 {target} 自己已經併進 {rows[target]['merged_into']}："
                          f"只能併一層，請直接併進 {resolve(result, target)['id']}")
        elif row["kind"] == "participant" and rows[target]["kind"] == "minor":
            errors.append(f"{row['id']}（主要發言者）併進了其他聲音 {target}")
    if errors:
        return errors
    # 兩位不同的說話者不能顯示成同一個字
    shown: dict[str, str] = {}
    for row in result:
        target = resolve(result, row["id"])
        if target is None or target["id"] != row["id"]:
            continue
        text = display_of(result, row["id"]).casefold()
        if text in shown:
            errors.append(f"{shown[text]} 和 {row['id']} 都會顯示成「{display_of(result, row['id'])}」，"
                          "讀者分不出誰是誰；真的是同一個人請用 --merge，不是的話請換一個名字")
        shown.setdefault(text, row["id"])
    return errors


def changed(before: list[dict], after: list[dict]) -> list[str]:
    """名字、角色、身分依據、併法有變的說話者編號（照清單的順序）。"""
    keys = ("name", "role", "basis", "merged_into")
    old = {row["id"]: tuple(row.get(key, "") for key in keys) for row in before}
    return [row["id"] for row in after if old.get(row["id"]) != tuple(row.get(key, "") for key in keys)]
