"""工作失敗時存起來的文字：已知的基礎設施錯誤翻成中文，每一則都遮掉機密與路徑。

job.error 與進度狀態會被 GET /jobs/{id} 回傳、被 Pi 寫進資料庫再印在畫面上；repo 是公開的，
任務紀錄也會被分享。專案自己的例外本來就帶著中文訊息，但其他例外（硬碟滿了、記憶體不夠、
第三方套件的錯誤）原本是「ClassName: English text」原樣轉送：帶著 GPU 主機容器裡的絕對路徑，
萬一例外裡夾帶了 token（標頭、網址）也會一起送出去。

規則：
- 格式維持「例外名稱: 訊息」（README 說 error 以例外名稱開頭），已知的基礎設施錯誤只換訊息
- 每一則（錯誤與進度）先遮掉設定的機密、hf_ 樣式的 token 與輸出資料夾的絕對路徑，再截短——
  順序不能反：先截短，剛好被切成兩半的 token 就不再符合樣式，前半段會原樣留下
- 翻譯掉的英文原文不會丟：由呼叫端寫進 GPU 主機自己的 log（同樣先過濾）
"""
from __future__ import annotations

import errno
import os
import re
from collections.abc import Iterable

MAX_ERROR_CHARS = 800
PATH_PLACEHOLDER = "<輸出資料夾>"
# 比這短的路徑（例如 "/"）不當成「輸出資料夾」取代，否則整則訊息的每個斜線都被換掉
_MIN_PATH_CHARS = 4

# Hugging Face token 的樣子；設定的那把另外用原字串取代，這個是抓「不是我們設定的」那些
_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{8,}")

_DISK_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", None)} - {None}
_NOT_WRITABLE = {errno.EACCES, errno.EPERM, errno.EROFS}


class FailureReporter:
    """把例外變成存進 job.error 的字串，並把任何要存起來的文字過濾一遍。"""

    def __init__(self, secrets: Iterable[str] = (), paths: Iterable[str] = ()):
        # 機密：整串原字串取代（不一定是 hf_ 開頭：代理、測試用的假值）；空白的設定不算
        self._secrets = sorted({s.strip() for s in secrets if s and s.strip()}, key=len, reverse=True)
        self._paths = _path_patterns(paths)

    def scrub(self, text: str) -> str:
        """遮掉設定的機密、hf_ 樣式的 token 與輸出資料夾的絕對路徑。可以重複套用。"""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = _HF_TOKEN_RE.sub("hf_***", text)
        for pattern in self._paths:
            text = pattern.sub(PATH_PLACEHOLDER, text)
        return text

    def original(self, error: BaseException) -> str:
        """沒翻譯過的「例外名稱: 訊息」（已過濾）：給 GPU 主機自己的 log，不給 Pi。"""
        return self.scrub(f"{type(error).__name__}: {error}")

    def describe(self, error: BaseException) -> str:
        """存進 job.error 的文字：已知的基礎設施錯誤用中文，其餘照舊；過濾後才截短。"""
        translated = _translate(error)
        text = f"{type(error).__name__}: {translated}" if translated else f"{type(error).__name__}: {error}"
        return self.scrub(text)[:MAX_ERROR_CHARS]


def _translate(error: BaseException) -> str:
    """已知的基礎設施錯誤的中文說明；不認得的回空字串（原訊息照舊）。"""
    if isinstance(error, MemoryError):
        return "GPU 主機記憶體不足；請稍後再試，或換一支比較短的影片"
    if isinstance(error, OSError) and error.errno is not None:
        name = errno.errorcode.get(error.errno, str(error.errno))
        if error.errno in _DISK_FULL:
            return f"GPU 主機磁碟已滿（{name}）；請清出空間（輸出資料夾與 Docker volume）後再重試"
        if error.errno in _NOT_WRITABLE:
            return f"GPU 主機上的檔案無法寫入或讀取（{name}：權限不足、唯讀或被其他程式佔用）；請檢查輸出資料夾"
    if _is_gpu_out_of_memory(error):
        return "顯示卡記憶體不足（Ollama、語音辨識與說話者區分共用同一張卡）；請稍後再試"
    return ""


def _is_gpu_out_of_memory(error: BaseException) -> bool:
    """torch 的 OutOfMemoryError，或 CUDA 的 out of memory（torch、ctranslate2 都這樣寫）。"""
    if type(error).__name__ == "OutOfMemoryError":
        return True
    text = str(error).lower()
    return "cuda" in text and "out of memory" in text


def _path_patterns(paths: Iterable[str]) -> list[re.Pattern[str]]:
    """每個路徑的各種寫法（原樣、絕對路徑、正反斜線、Windows 例外訊息裡雙寫的反斜線），長的在前。"""
    spellings: set[str] = set()
    for path in paths:
        path = (path or "").strip().rstrip("/\\")
        if len(path) < _MIN_PATH_CHARS or os.path.dirname(path) == path:
            continue
        for variant in (path, os.path.abspath(path)):
            forward = variant.rstrip("/\\").replace("\\", "/")
            for form in (variant.rstrip("/\\"), forward, forward.replace("/", "\\")):
                spellings.add(form)
                spellings.add(form.replace("\\", "\\\\"))          # Windows 的 OSError 訊息把反斜線寫成兩個
    flags = re.IGNORECASE if os.name == "nt" else 0
    return [re.compile(re.escape(s), flags) for s in sorted(spellings, key=len, reverse=True)
            if len(s) >= _MIN_PATH_CHARS]
