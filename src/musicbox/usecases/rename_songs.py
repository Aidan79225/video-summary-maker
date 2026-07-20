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
