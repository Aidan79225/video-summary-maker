"""建置映像時檢查兩個 Python 環境沒有混在一起（Dockerfile 在兩次 uv sync 之後跑它；也被測試拿來
檢查 uv.lock 算出來的兩個環境）。

為什麼要分兩個環境：語音辨識（ctranslate2）要 CUDA 12＋cuDNN 9，鎖檔裡的 PyTorch 要 CUDA 13＋cuDNN 9。
兩個版本的 cuDNN 輪子都解到 site-packages/nvidia/cudnn/lib、檔名相同，放進同一個環境會互相蓋掉，
而 Dockerfile 的 LD_LIBRARY_PATH 又把那個資料夾指給 ctranslate2——最壞的情況是連議會與單人工作的
Whisper 都壞掉。所以：

- 主環境（/app/.venv）：和沒有多人模式時完全一樣，CUDA 的東西只有 -cu12 的那幾個，沒有 torch、沒有 pyannote
- 診斷用的第二環境（/opt/diarize-venv）：只有 diarization 群組，沒有任何 -cu12，也沒有主環境的套件

這個檔案只用標準函式庫（建置時用哪個 Python 跑都行），用法：

    python check_environments.py 主環境的Python 第二環境的Python

結束碼：0 乾淨；1 混在一起了（問題列在標準錯誤）；2 參數不對；3 看不到某個環境裡裝了什麼（檢查不了）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence

# 主環境一定要有的：ctranslate2 載入時要的 CUDA 12 函式庫（LD_LIBRARY_PATH 指的就是它們）
MAIN_REQUIRES = ("ctranslate2", "faster-whisper", "nvidia-cublas-cu12", "nvidia-cudnn-cu12")
# 主環境一定不能有的：PyTorch 與用它的東西
MAIN_FORBIDS = ("torch", "torchaudio", "torchcodec", "triton", "pyannote-audio")
# 第二環境一定要有的
DIARIZE_REQUIRES = ("torch", "pyannote-audio")
# 第二環境一定不能有的：主環境的東西（--only-group 沒有生效，專案本身與它的相依套件被裝進來了）
DIARIZE_FORBIDS = ("ctranslate2", "faster-whisper", "fastapi", "uvicorn", "pyside6", "yt-dlp", "imageio-ffmpeg")
# 主環境的 LD_LIBRARY_PATH 裡一定要找得到的檔案（前綴）
MAIN_LIBRARIES = ("libcudnn.so.9", "libcublas.so.12")

_LISTING = ("import importlib.metadata as m, json; "
            "print(json.dumps(sorted(d.metadata['Name'] for d in m.distributions() if d.metadata['Name'])))")


def normalize(name: str) -> str:
    """PEP 503 的套件名稱：小寫、連續的 - _ . 換成一個 -。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_cuda(name: str) -> bool:
    return name.startswith(("nvidia-", "cuda-"))


def check(main: Iterable[str], diarize: Iterable[str], library_dirs: Sequence[str] = (),
          list_dir: Callable[[str], list[str]] | None = None,
          diarize_prefix: str = "/opt/diarize-venv") -> list[str]:
    """回傳問題清單（空的就是沒問題）。main／diarize 是兩個環境裡裝了哪些套件；
    library_dirs 是主環境的 LD_LIBRARY_PATH 的各個資料夾（要檢查就給 list_dir：資料夾 → 檔名）。"""
    main_set, diarize_set = {normalize(n) for n in main}, {normalize(n) for n in diarize}
    problems = []

    for name in MAIN_REQUIRES:
        if name not in main_set:
            problems.append(f"主環境缺少 {name}（語音辨識的 ctranslate2 要靠它載入 CUDA 12 函式庫）")
    for name in sorted(n for n in main_set if n in MAIN_FORBIDS):
        problems.append(f"主環境裡有 {name}：PyTorch 與它的 CUDA 13 函式庫只能在第二個環境")
    for name in sorted(n for n in main_set if _is_cuda(n) and not n.endswith("-cu12")):
        problems.append(f"主環境裡有 {name}：主環境的 CUDA 套件只能是 -cu12 的"
                        f"（CUDA 13 的 cuDNN 會蓋掉 ctranslate2 要用的那份）")

    for name in DIARIZE_REQUIRES:
        if name not in diarize_set:
            problems.append(f"第二個環境缺少 {name}")
    for name in sorted(n for n in diarize_set if n.endswith("-cu12")):
        problems.append(f"第二個環境裡有 {name}：CUDA 12 的函式庫只能在主環境"
                        f"（第二個環境不給 LD_LIBRARY_PATH，torch 用自己那套 CUDA 13）")
    for name in sorted(n for n in diarize_set if n in DIARIZE_FORBIDS):
        problems.append(f"第二個環境裡有 {name}：那是主環境的套件，"
                        f"uv sync 要用 --only-group diarization（不裝專案本身與它的相依套件）")

    for directory in library_dirs:
        if diarize_prefix and directory.startswith(diarize_prefix):
            problems.append(f"LD_LIBRARY_PATH 指到了第二個環境（{directory}）：它不能交給 torch 的行程")
    if list_dir is not None:
        for prefix in MAIN_LIBRARIES:
            if not any(name.startswith(prefix) for directory in library_dirs for name in list_dir(directory)):
                problems.append(f"LD_LIBRARY_PATH 的資料夾裡找不到 {prefix}*（ctranslate2 載入時會找不到）")
    return problems


class ListingFailed(RuntimeError):
    """問不出某個 Python 裝了什麼：不能當作「沒裝東西」，建置要失敗。"""


def installed(python: str, run=subprocess.run) -> set[str]:
    """那個 Python 的環境裡裝了哪些套件（正規化過的名稱）。

    -I（隔離模式）：不吃 PYTHONPATH、使用者的 site-packages 與目前資料夾，看到的就是那個環境自己。
    """
    result = run([python, "-I", "-c", _LISTING], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise ListingFailed(f"{python} 沒辦法列出它裝了什麼（結束碼 {result.returncode}）：{result.stderr.strip()[-300:]}")
    return {normalize(name) for name in json.loads(result.stdout)}


def _list_dir(directory: str) -> list[str]:
    try:
        return os.listdir(directory)
    except OSError:
        return []


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None, out=None, err=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    out, err = out or sys.stdout, err or sys.stderr
    if len(argv) != 2:
        print("用法：check_environments.py 主環境的Python 第二環境的Python", file=err)
        return 2
    main_python, diarize_python = argv
    dirs = [d for d in environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if d]
    try:
        main_packages, diarize_packages = installed(main_python), installed(diarize_python)
    except (OSError, ValueError, ListingFailed) as e:    # 看不到就沒辦法檢查：寧可讓建置失敗
        print(f"沒辦法檢查兩個 Python 環境：{e}", file=err)
        return 3
    problems = check(main_packages, diarize_packages, dirs, _list_dir)
    if problems:
        print("兩個 Python 環境混在一起了：", file=err)
        for problem in problems:
            print(f"  - {problem}", file=err)
        return 1
    print("兩個 Python 環境分得很乾淨：主環境只有 CUDA 12，第二個環境只有 diarization（CUDA 13）。", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
