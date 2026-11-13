"""程式碼的指紋：存檔裡的結果是「那一版程式」算出來的，程式變了，舊結果就不能冒充新的。

手動升版本號（PROMPT_VERSION、ALIGN_VERSION）靠人記得：改了驗證規則、辨識參數或提示詞卻忘了升版，
存檔裡舊規則放行的摘要就被當成新規則放行的。這裡改成把原始碼本身雜湊進存檔的指紋：

- 換行方式與開頭的 BOM 先統一再雜湊：同一份程式在 Windows（CRLF）與 Linux 容器（LF）簽出來，
  指紋一樣，不會因為換機器或重建映像就讓存檔全部作廢。
- 讀不到原始碼（只有 .pyc、檔案不見了、解不開）時**不 raise**，也不用固定的字串頂替：
  改用「這個行程專屬的隨機標記」。它對不上任何存檔——包括同樣讀不到的另一個行程留下的——
  所以那一步照樣重做，絕不拿舊結果冒充；工作本身不會因此失敗。

純函式加讀檔，不碰網路、不碰模型。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
from collections.abc import Iterable

# 這個行程專屬：讀不到原始碼的標記要對不上任何存檔，所以不能是固定值
_PROCESS_MARKER = secrets.token_hex(8)
_UNREADABLE = "unreadable"


def normalize_source(text: str) -> str:
    """換行一律當成 \\n、去掉開頭的 BOM。"""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def source_digest(text: str) -> str:
    """一份原始碼的雜湊（16 個十六進位字元），換行方式不同的同一份程式雜湊一樣。"""
    return hashlib.sha256(normalize_source(text).encode("utf-8")).hexdigest()[:16]


def unreadable_marker(what: str) -> str:
    """讀不到 what 的原始碼時，頂替它的雜湊：帶著名字（出了問題找得到原因），
    後面接這個行程專屬的隨機值（對不上任何存檔）。"""
    return f"{_UNREADABLE}:{what}:{_PROCESS_MARKER}"


def read_module_source(name: str) -> str | None:
    """模組的原始碼文字；沒有原始碼（只有 .pyc、找不到）是 None。換行已經由載入器統一成 \\n。"""
    spec = importlib.util.find_spec(name)
    loader = getattr(spec, "loader", None)
    getter = getattr(loader, "get_source", None)
    return getter(name) if callable(getter) else None


def read_file_source(path: str) -> str | None:
    """檔案的文字（UTF-8）；讀不到或不是 UTF-8 就 raise，由 file_digest 轉成標記。"""
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def module_digest(name: str) -> str:
    """模組原始碼的雜湊；讀不到就是 unreadable_marker(name)。**絕不 raise。**"""
    try:
        text = read_module_source(name)
    except Exception:  # noqa: BLE001 讀不到的原因五花八門（找不到、沒權限、解不開…），一律當作讀不到
        text = None
    return source_digest(text) if text is not None else unreadable_marker(name)


def file_digest(path: str) -> str:
    """檔案內容的雜湊；讀不到就是 unreadable_marker(檔名)。**絕不 raise。**"""
    try:
        text = read_file_source(path)
    except Exception:  # noqa: BLE001
        text = None
    return source_digest(text) if text is not None else unreadable_marker(os.path.basename(path))


def code_fingerprint(modules: Iterable[str] = (), files: Iterable[str] = ()) -> str:
    """一組程式碼的指紋（16 個十六進位字元）：每個模組名與每個檔名（只用檔名，不含資料夾，
    搬到別的機器或容器不變）連同它的雜湊。順序不影響結果；任何一份讀不到，結果就對不上任何舊存檔。"""
    entries = [[f"module:{name}", module_digest(name)] for name in modules]
    entries += [[f"file:{os.path.basename(path)}", file_digest(path)] for path in files]
    entries.sort()
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
