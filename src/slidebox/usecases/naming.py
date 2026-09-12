"""輸出資料夾的命名：純字串處理，不碰檔案系統。"""
from __future__ import annotations

import re

# Windows 不允許出現在檔名的字元
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')
# 控制字元（含換行、tab）換成空格而不是刪除，否則換行兩側的字會黏在一起
_CONTROL_RE = re.compile(r"[\x00-\x1f]")

# 標題部分的長度上限。加上 " [影片id]" 後仍遠低於 Windows 的路徑長度限制，
# 讓使用者還能把輸出資料夾放在較深的位置。
_MAX_TITLE = 80


def deck_folder_name(title: str, video_id: str) -> str:
    """由影片標題與 id 組出資料夾名稱：`標題 [影片id]`。

    標題放前面才看得出是哪支影片；id 留在後面有兩個作用——不同影片就算標題
    相同也不會蓋掉彼此（同名影片是真的存在的），而同一支影片重跑時會寫回
    同一個資料夾，不會每次長出新的。

    標題清乾淨後如果什麼都不剩（整串都是非法字元），就只用 id。
    """
    clean = _ILLEGAL_RE.sub("", _CONTROL_RE.sub(" ", title))
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = clean[:_MAX_TITLE].strip()
    # Windows 的資料夾名不能以點或空白結尾：會被靜默去掉或建立失敗，之後拿
    # 同一個路徑去寫檔就找不到。
    clean = clean.rstrip(" .")
    return f"{clean} [{video_id}]" if clean else video_id
