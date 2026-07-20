#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重整歌曲檔名：依現有開頭數字排序，重新編上連續流水號（NN-歌名.副檔名）。

用法：
    python rename_songs.py                # 處理腳本所在資料夾，先預覽再確認
    python rename_songs.py "D:\\其他資料夾"  # 指定資料夾
    python rename_songs.py --yes          # 跳過確認直接改名
"""
import os
import re
import sys

# 要處理的副檔名（小寫比對，實際大小寫保留原樣）
EXTS = {".mp3"}

# 開頭「數字＋分隔符」：數字後可接空格、-、.、_ 的任意組合
PREFIX_RE = re.compile(r"^\s*(\d+)\s*[-._]?\s*")


def sort_key(name):
    """依開頭數字排序；沒有數字的排最後，再依檔名。"""
    m = PREFIX_RE.match(name)
    if m:
        return (0, int(m.group(1)), name.lower())
    return (1, 0, name.lower())


def clean_title(stem):
    """移除開頭的舊數字與分隔符，其餘完全保留。"""
    return PREFIX_RE.sub("", stem, count=1)


def build_plan(folder):
    files = [
        f for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
        and os.path.splitext(f)[1].lower() in EXTS
    ]
    files.sort(key=sort_key)

    width = max(2, len(str(len(files))))  # 超過 99 自動變三位數
    plan = []
    for i, old in enumerate(files, start=1):
        stem, ext = os.path.splitext(old)
        title = clean_title(stem)
        new = f"{i:0{width}d}-{title}{ext}"
        plan.append((old, new))
    return plan


def preview(plan):
    changed = 0
    for old, new in plan:
        if old == new:
            print(f"  (不變) {old}")
        else:
            print(f"  {old}\n       → {new}")
            changed += 1
    print(f"\n共 {len(plan)} 個檔案，其中 {changed} 個需要改名。")
    return changed


def apply(folder, plan):
    """兩階段改名，避免與現有檔名衝突而覆蓋。"""
    to_do = [(o, n) for o, n in plan if o != n]
    if not to_do:
        print("沒有需要改名的檔案。")
        return

    # 第一階段：全部改成獨一無二的暫存名
    temps = []
    for idx, (old, new) in enumerate(to_do):
        tmp = f".__rename_tmp_{idx}__{new}"
        os.rename(os.path.join(folder, old), os.path.join(folder, tmp))
        temps.append((tmp, new))

    # 第二階段：暫存名改成正式新名
    for tmp, new in temps:
        os.rename(os.path.join(folder, tmp), os.path.join(folder, new))

    print(f"完成，已改名 {len(to_do)} 個檔案。")


def main():
    argv = [a for a in sys.argv[1:]]
    auto_yes = "--yes" in argv or "-y" in argv
    argv = [a for a in argv if a not in ("--yes", "-y")]

    folder = argv[0] if argv else os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(folder):
        print(f"找不到資料夾：{folder}")
        sys.exit(1)

    print(f"資料夾：{folder}\n")
    plan = build_plan(folder)
    if not plan:
        print("這個資料夾裡沒有找到 mp3 檔案。")
        return

    changed = preview(plan)
    if changed == 0:
        return

    if not auto_yes:
        ans = input("\n確定要執行改名嗎？(y/N) ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消，未做任何更動。")
            return

    apply(folder, plan)


if __name__ == "__main__":
    main()
