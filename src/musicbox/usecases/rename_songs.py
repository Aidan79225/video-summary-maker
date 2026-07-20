"""歌曲重新編號的 use case（純邏輯，移植自舊 rename_songs.py）。"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence

from ..domain.entities import (
    RenameItem,
    RenameMode,
    RenameOptions,
    RenamePlan,
)
from ..domain.ports import FileSystemGateway, ProgressCallback

# 開頭「數字＋分隔符」：數字後可接空格、-、.、_ 的任意組合
_PREFIX_RE = re.compile(r"^\s*(\d+)\s*[-._]?\s*")


def _sort_key(name: str):
    """依開頭數字排序；沒有數字的排最後，再依檔名。"""
    m = _PREFIX_RE.match(name)
    if m:
        return (0, int(m.group(1)), name.lower())
    return (1, 0, name.lower())


def _clean_title(stem: str) -> str:
    """移除開頭的舊數字與分隔符，其餘完全保留。"""
    return _PREFIX_RE.sub("", stem, count=1)


# --- 歌名正規化 ---

# 已知雜訊關鍵字。拉丁詞：整個括號內容的每個詞都是雜訊詞才移除。
_LATIN_NOISE = {
    "official", "music", "video", "audio", "lyric", "lyrics",
    "mv", "m/v", "hd", "hq", "4k",
}
# CJK 雜訊詞：括號內容包含即移除。
_CJK_NOISE = ("高畫質", "中文字幕", "歌詞", "動態歌詞")

# 括號群組：() [] 【】 {}（全形括號會先被轉成半形）
_BRACKET_RE = re.compile(r"[(\[【{]([^()\[\]【】{}]*)[)\]】}]")
# Windows 非法檔名字元與控制字元
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# OpenCC 簡轉繁：延遲載入，缺套件時降級。
_opencc_converter = None
_opencc_tried = False
OPENCC_AVAILABLE = False


def _fullwidth_to_halfwidth(s: str) -> str:
    """全形英數與標點轉半形，全形空格轉半形空格。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _is_noise_bracket(inner: str) -> bool:
    t = inner.strip().lower()
    if not t:
        return False
    for kw in _CJK_NOISE:
        if kw in t:
            return True
    tokens = re.findall(r"[a-z0-9/]+", t)
    return bool(tokens) and all(tok in _LATIN_NOISE for tok in tokens)


def _strip_noise_brackets(s: str) -> str:
    return _BRACKET_RE.sub(lambda m: "" if _is_noise_bracket(m.group(1)) else m.group(0), s)


def _to_traditional(s: str) -> str:
    """簡體轉繁體（台灣慣用詞）；OpenCC 不可用時原樣回傳。"""
    global _opencc_converter, _opencc_tried, OPENCC_AVAILABLE
    if not _opencc_tried:
        _opencc_tried = True
        try:
            from opencc import OpenCC
            _opencc_converter = OpenCC("s2twp")
            OPENCC_AVAILABLE = True
        except Exception:  # noqa: BLE001 缺套件或初始化失敗都降級
            _opencc_converter = None
            OPENCC_AVAILABLE = False
    if _opencc_converter is None:
        return s
    try:
        return _opencc_converter.convert(s)
    except Exception:  # noqa: BLE001
        return s


def normalize_title(title: str) -> str:
    """依序：全形轉半形 → 移除已知雜訊括號 → 簡轉繁 → 去非法字元 → 整理空白。"""
    s = _fullwidth_to_halfwidth(title)
    s = _strip_noise_brackets(s)
    s = _to_traditional(s)
    s = _ILLEGAL_RE.sub("", s)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def derive_title(old_name: str, normalize: bool) -> str:
    """由舊檔名推導歌名：去掉開頭舊號碼，視開關套用正規化。"""
    stem = os.path.splitext(old_name)[0]
    title = _clean_title(stem)
    if normalize:
        title = normalize_title(title)
    return title


def build_undo_plan(applied: RenamePlan) -> RenamePlan:
    """把已套用的計畫反轉，用來復原（new_name 改回 old_name）。"""
    reversed_items = tuple(
        RenameItem(old_name=it.new_name, new_name=it.old_name)
        for it in applied.items
        if it.changed
    )
    return RenamePlan(folder=applied.folder, items=reversed_items)


class BuildRenamePlanUseCase:
    """讀取資料夾，依設定算出「舊檔名 → 新檔名」計畫。"""

    def __init__(self, gateway: FileSystemGateway):
        self._gateway = gateway

    def execute(self, folder: str, options: RenameOptions) -> RenamePlan:
        """讀取資料夾、依開頭數字排序後，算出計畫。"""
        files = [
            f for f in self._gateway.list_files(folder)
            if os.path.splitext(f)[1].lower() in options.extensions
        ]
        files.sort(key=_sort_key)
        return self.plan_from_order(folder, files, options)

    def plan_from_order(
        self,
        folder: str,
        ordered_names: Sequence[str],
        options: RenameOptions,
    ) -> RenamePlan:
        """依給定的順序（可能是使用者手動調整過的）算出計畫，純邏輯不做 IO。"""
        width = max(options.padding, len(str(len(ordered_names))))  # 超過位數自動加寬
        items: list[RenameItem] = []
        for i, old in enumerate(ordered_names, start=1):
            stem, ext = os.path.splitext(old)
            title = _clean_title(stem)
            num = self._number_for(stem, i, width, options.mode)
            new = f"{num}{options.separator}{title}{ext}"
            items.append(RenameItem(old_name=old, new_name=new))
        return RenamePlan(folder=folder, items=tuple(items))

    def plan_from_titles(
        self,
        folder: str,
        ordered: Sequence[tuple[str, str]],
        options: RenameOptions,
    ) -> RenamePlan:
        """依給定的 (舊檔名, 歌名) 序列組出計畫；歌名已由呼叫端決定，不再 clean。"""
        width = max(options.padding, len(str(len(ordered))))
        items: list[RenameItem] = []
        for i, (old, title) in enumerate(ordered, start=1):
            stem, ext = os.path.splitext(old)
            num = self._number_for(stem, i, width, options.mode)
            new = f"{num}{options.separator}{title}{ext}"
            items.append(RenameItem(old_name=old, new_name=new))
        return RenamePlan(folder=folder, items=tuple(items))

    @staticmethod
    def _number_for(stem: str, index: int, width: int, mode: RenameMode) -> str:
        if mode == RenameMode.RENUMBER:
            return f"{index:0{width}d}"
        # KEEP：保留原有號碼，只補零統一寬度；沒有號碼的用序號補上
        m = _PREFIX_RE.match(stem)
        if m:
            return m.group(1).zfill(width)
        return f"{index:0{width}d}"


class ApplyRenamePlanUseCase:
    """實際套用改名，採兩階段避免與現有檔名衝突而覆蓋。"""

    def __init__(self, gateway: FileSystemGateway):
        self._gateway = gateway

    def execute(self, plan: RenamePlan, progress: ProgressCallback | None = None) -> int:
        cb: ProgressCallback = progress or (lambda frac, status: None)
        todo = [it for it in plan.items if it.changed]
        if not todo:
            cb(1.0, "沒有需要改名的檔案")
            return 0

        total = 2 * len(todo)
        # 第一階段：全部改成獨一無二的暫存名
        temps: list[tuple[str, str]] = []
        for idx, it in enumerate(todo):
            tmp = f".__rename_tmp_{idx}__{it.new_name}"
            self._gateway.rename(plan.folder, it.old_name, tmp)
            temps.append((tmp, it.new_name))
            cb((idx + 1) / total, f"準備 {it.old_name}")

        # 第二階段：暫存名改成正式新名
        for idx, (tmp, new) in enumerate(temps):
            self._gateway.rename(plan.folder, tmp, new)
            cb((len(todo) + idx + 1) / total, f"改名 {new}")

        cb(1.0, f"完成，已改名 {len(todo)} 個檔案")
        return len(todo)


def duplicate_new_names(plan: RenamePlan) -> set[str]:
    """找出計畫中重複的新檔名（手動編輯後可能撞名）。"""
    seen: set[str] = set()
    dups: set[str] = set()
    for it in plan.items:
        if it.new_name in seen:
            dups.add(it.new_name)
        seen.add(it.new_name)
    return dups


def has_illegal_chars(title: str) -> bool:
    """歌名是否含 Windows 非法檔名字元或控制字元。"""
    return bool(_ILLEGAL_RE.search(title))
