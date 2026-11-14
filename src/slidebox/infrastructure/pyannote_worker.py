"""說話者區分的工作行程：pyannote.audio 與 PyTorch 只在這裡跑，不在 API 的主環境裡。

為什麼是另一個行程、另一個 Python 環境：語音辨識（ctranslate2）要 CUDA 12＋cuDNN 9，鎖檔裡的
PyTorch 2.15 要 CUDA 13＋cuDNN 9。兩個版本的 cuDNN 輪子都把 libcudnn*.so.9 解到
site-packages/nvidia/cudnn/lib，檔名相同、內容不同；放在同一個環境裡，後解開的蓋掉先解開的，
而 LD_LIBRARY_PATH 又把這個資料夾指給 ctranslate2——最壞的情況是連**既有的**議會與單人工作的
Whisper 都壞掉。所以主環境和 master 完全一樣（沒有 torch），pyannote 與 torch 放在第二個環境
（Dockerfile 的 /opt/diarize-venv），由 PyannoteSpeakerDiarizer 用那個環境的 Python 啟動這個檔案，
而且**不把 LD_LIBRARY_PATH 交給它**（否則 CUDA 13 的 torch 會撿到主環境 CUDA 12 的 cuDNN）。

這個檔案是獨立的：頂層與函式裡只 import 標準函式庫、numpy、torch、pyannote.audio——它在
diarization 環境裡執行，那裡沒有 slidebox、fastapi、faster-whisper、imageio-ffmpeg
（tests/slidebox/test_pyannote_worker.py 用語法樹擋著，import 到別的東西就失敗）。
不要在這裡 import slidebox 的任何東西。

用法（由 pyannote_diarizer.PyannoteSpeakerDiarizer 啟動，不是給人直接叫的）：

    python -I -X utf8 pyannote_worker.py probe
    python -I -X utf8 pyannote_worker.py run --audio PATH --model NAME --device auto|cpu|cuda \\
                                              --ffmpeg EXE --result PATH [--speakers N]

-I（隔離模式）：不吃 PYTHONPATH、使用者的 site-packages，腳本所在的資料夾也不加進搜尋路徑——這個行程
看到的就是它那個環境自己有的東西。

- token 只從環境變數 HUGGINGFACE_TOKEN 讀：命令列會出現在行程清單裡，不能放。
- 進度寫到 stderr，一行一則：`SLIDEBOX_PROGRESS {"fraction": null, "text": "…"}`；其他輸出（函式庫的
  警告、例外追蹤）原樣留在 stderr，父行程只在當掉時拿最後幾行當原因。
- 結果寫成 JSON 檔（先寫暫存檔再換名，檔案出現就是完整的）：成功是 {"ok": true, "turns": [[起, 訖, 標籤]…],
  "overlap_seconds": …, "device": …}；乾淨的失敗是 {"ok": false, "stage": …, "detail": …}，結束碼 3。
  其他的（沒有結果檔、結束碼不是 0 也不是 3）都是當掉，由父行程當成失敗。
- 取消靠父行程把這個行程殺掉；這裡不用檢查取消旗標。
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

PROTOCOL = 1
PROGRESS_PREFIX = "SLIDEBOX_PROGRESS "
EXIT_OK = 0
EXIT_FAILED = 3                       # 乾淨的失敗：結果檔裡有原因

PACKAGE = "pyannote.audio"
TOKEN_ENV = "HUGGINGFACE_TOKEN"
METRICS_ENV = "PYANNOTE_METRICS_ENABLED"
SAMPLE_RATE = 16000
DECODE_CHUNK_BYTES = 1 << 20
DEVICES = ("auto", "cpu", "cuda")

# 失敗發生在哪一步；父行程依它決定要給操作者什麼建議（訊息文字由父行程組）
STAGE_TERMS = "terms"                 # 模型沒載入：沒接受條款或 token 沒有權限（pyannote 3.x 回 None）
STAGE_LOAD = "load"                   # 載入模型、搬到裝置時丟了例外
STAGE_DECODE = "decode"               # 音訊解碼失敗（detail 就是給人看的訊息）
STAGE_RUN = "run"                     # 模型已載入，跑到一半的失敗
STAGE_EMPTY = "empty"                 # 跑完了，但沒有任何人聲區段

# Hugging Face token 的樣子；錯誤訊息一律先把它蓋掉（父行程還會再蓋一次）
_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{8,}")
_DETAIL_CHARS = 240


class WorkerFailure(Exception):
    """乾淨的失敗：寫進結果檔，結束碼 3。"""

    def __init__(self, stage: str, detail: str = "", *, oom: bool = False, device: str = ""):
        super().__init__(detail)
        self.stage, self.detail, self.oom, self.device = stage, detail, oom, device


@dataclass
class EngineResult:
    turns: list[tuple[float, float, str]]          # 互斥的區段，依開始時間排序，標籤是 pyannote 的原始標籤
    overlap_seconds: float
    device: str
    gpu_peak_bytes: int | None = None
    peak_rss_bytes: int | None = None


def redact(text: object, token: str = "") -> str:
    """蓋掉設定的那把 token（原字串）與任何 hf_ 樣子的字串；不動其他的（換行、長度都留著）。"""
    out = str(text)
    if token:
        out = out.replace(token, "***")
    return _HF_TOKEN_RE.sub("hf_***", out)


def scrub(text: object, token: str = "") -> str:
    """給人看的錯誤：蓋掉 token、壓成一行、截短（先蓋再截：剛好被切成兩半的 token 就不再認得了）。"""
    out = redact(" ".join(str(text).split()), token)
    return out if len(out) <= _DETAIL_CHARS else out[:_DETAIL_CHARS - 3] + "…"


def is_out_of_memory(error: BaseException) -> bool:
    """torch.cuda.OutOfMemoryError（類別名稱）或訊息裡的 "out of memory"（torch、ctranslate2 都這樣寫）。"""
    return type(error).__name__ == "OutOfMemoryError" or "out of memory" in str(error).lower()


_STEP_NAMES = {
    "segmentation": "偵測人聲",
    "speaker_counting": "估計人數",
    "embeddings": "比對聲紋",
    "discrete_diarization": "整理結果",
}


class Reporter:
    """進度：一行一則寫到 stderr，立刻送出（父行程在另一端一行一行讀）。

    JSON 用預設的 ASCII 跳脫（中文會變成跳脫序列）：這一行不管 stderr 是什麼編碼都寫得出去，
    父行程解開之後拿到的還是原本的中文。
    """

    def __init__(self, stream, token: str = ""):
        self._stream = stream
        self._token = token

    def __call__(self, fraction: float | None, text: str) -> None:
        if fraction is not None and not (isinstance(fraction, (int, float)) and math.isfinite(fraction)):
            fraction = None
        line = json.dumps({"fraction": fraction, "text": scrub(text, self._token)})
        print(PROGRESS_PREFIX + line, file=self._stream, flush=True)


# --- 引擎：載入、搬到裝置、解碼、跑 pipeline（所有會碰外面的東西都可以注入，測試不必安裝 pyannote）---


class PyannoteEngine:
    def __init__(
        self,
        model_name: str,
        token: str = "",
        device: str = "auto",
        ffmpeg_exe: str = "ffmpeg",
        pipeline_factory: Callable[..., object] | None = None,
        torch_module: object | None = None,
        audio_loader: Callable[[str], object] | None = None,
    ):
        self._model_name = model_name
        self._token = token
        self._device_setting = device
        self._ffmpeg_exe = ffmpeg_exe
        self._pipeline_factory = pipeline_factory
        self._torch = torch_module
        self._audio_loader = audio_loader
        self._pipeline = None
        self.device = ""

    def diarize(self, audio_path: str, speaker_count: int | None,
                progress: Callable[[float | None, str], None]) -> EngineResult:
        pipeline = self._load(progress)

        progress(None, "解碼音訊…")
        loader = self._audio_loader or (lambda path: decode_waveform(
            path, self._torch, ffmpeg_exe=self._ffmpeg_exe))
        try:
            audio = loader(audio_path)
        except WorkerFailure:
            raise
        except Exception as e:  # noqa: BLE001 例如主機記憶體不夠放下整段波形
            raise WorkerFailure(STAGE_DECODE,
                                f"音訊解碼失敗：{type(e).__name__}: {scrub(e, self._token)}") from e

        progress(None, "區分說話者…")

        def hook(step_name, step_artifact=None, file=None, total=None, completed=None):
            # pyannote 在每個步驟、每批資料之間呼叫它
            label = _STEP_NAMES.get(str(step_name), str(step_name))
            if total and completed is not None:
                progress(None, f"區分說話者…（{label} {completed}/{total}）")
            else:
                progress(None, f"區分說話者…（{label}）")

        kwargs = {"num_speakers": speaker_count} if speaker_count else {}
        try:
            result = pipeline(audio, hook=hook, **kwargs)
            exclusive, full = _annotations(result)
            turns = _turns(exclusive)
            overlap = _overlap_seconds(full)
        except Exception as e:  # noqa: BLE001 跑到一半的失敗：模型已經載入，條款一定接受過
            raise WorkerFailure(STAGE_RUN, scrub(e, self._token), oom=is_out_of_memory(e),
                                device=self.device) from e
        if not turns:
            raise WorkerFailure(STAGE_EMPTY, "", device=self.device)
        # 只有真的用了顯示卡才去量：cpu 模式連 torch.cuda 都不碰（見 _move）
        gpu_peak = _gpu_peak_bytes(self._torch) if self.device == "cuda" else None
        return EngineResult(turns=turns, overlap_seconds=round(overlap, 3), device=self.device,
                            gpu_peak_bytes=gpu_peak, peak_rss_bytes=_peak_rss_bytes())

    # --- 載入 ---

    def _load(self, progress):
        if self._pipeline is not None:
            return self._pipeline
        progress(None, "載入說話者區分模型…（首次使用需下載）")
        try:
            # 要在 import pyannote 之前：它在 import 的時候就讀這個環境變數
            os.environ.setdefault(METRICS_ENV, "0")
            factory = self._pipeline_factory or _default_factory()
            pipeline = factory(self._model_name, token=self._token)
            if pipeline is None:
                # pyannote 3.x 沒接受條款時不丟例外，只回 None
                raise WorkerFailure(STAGE_TERMS, "")
            device = self._move(pipeline, progress)
        except WorkerFailure:
            raise
        except Exception as e:  # noqa: BLE001
            raise WorkerFailure(STAGE_LOAD, scrub(e, self._token)) from e
        self._pipeline = pipeline
        self.device = device
        progress(None, f"說話者區分模型已載入（{device}）")
        return pipeline

    def _move(self, pipeline, progress) -> str:
        """把 pipeline 放到顯示卡上（或留在 CPU）；回傳實際用的裝置。

        cpu 是逃生口，連 torch.cuda 都不碰（鎖檔裡的 torch 是 CUDA 13，驅動對不上時 import 或
        第一次用顯示卡才會出錯）。auto 試顯示卡、不行就留在 CPU；cuda 是明確要求，失敗照實回報。
        """
        if self._device_setting == "cpu":
            return "cpu"
        torch = self._torch
        if torch is None:
            import torch as torch_module
            torch = self._torch = torch_module
        if self._device_setting == "auto" and not torch.cuda.is_available():
            return "cpu"
        try:
            pipeline.to(torch.device("cuda"))
        except Exception as e:  # noqa: BLE001
            if self._device_setting != "auto":
                raise
            progress(None, f"顯示卡不能用（{scrub(e, self._token)}），說話者區分改用 CPU（較慢）")
            pipeline.to(torch.device("cpu"))
            return "cpu"
        return "cuda"


def _default_factory() -> Callable[..., object]:
    from pyannote.audio import Pipeline   # 延遲 import：載入 torch 要好幾秒

    return Pipeline.from_pretrained


def _gpu_peak_bytes(torch: object | None) -> int | None:
    """PyTorch 在顯示卡上的記憶體峰值；沒用顯示卡或量不到就是 None。"""
    try:
        if torch is not None and torch.cuda.is_available():
            return int(torch.cuda.max_memory_allocated())
    except Exception:  # noqa: BLE001 量不到就不報，不要讓區分因此失敗
        pass
    return None


def _peak_rss_bytes() -> int | None:
    """這個行程的記憶體峰值（含載入的模型）；Windows 沒有 resource，量不到就是 None。"""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


# --- pipeline 的輸出 ---


def _annotations(result: object) -> tuple[object, object]:
    """(互斥的標註, 完整的標註)。pyannote 4.x 兩個都有；3.x 直接回傳一個標註；缺一個就用另一個。"""
    exclusive = getattr(result, "exclusive_speaker_diarization", None)
    full = getattr(result, "speaker_diarization", None)
    if exclusive is None and full is None:
        return result, result
    return (exclusive if exclusive is not None else full), (full if full is not None else exclusive)


def _tracks(annotation: object) -> list[tuple[float, float, str]]:
    out = []
    for segment, _, label in annotation.itertracks(yield_label=True):
        start, end = float(segment.start), float(segment.end)
        if math.isfinite(start) and math.isfinite(end) and end > start:
            out.append((start, end, str(label)))
    return out


def _turns(annotation: object) -> list[tuple[float, float, str]]:
    return sorted(_tracks(annotation), key=lambda t: (t[0], t[1]))


def _overlap_seconds(annotation: object) -> float:
    """同一時間有兩位以上（不同的）說話者的總秒數；同一位說話者自己重疊的片段不算。"""
    events: list[tuple[float, int, str]] = []
    for start, end, label in _tracks(annotation):
        events.append((start, 1, label))
        events.append((end, -1, label))
    events.sort(key=lambda e: (e[0], e[1]))         # 同一刻先結束再開始：碰在一起不算交疊
    active: Counter[str] = Counter()
    total = 0.0
    previous = 0.0
    for at, change, label in events:
        if len(active) >= 2:
            total += at - previous
        active[label] += change
        if active[label] <= 0:
            del active[label]
        previous = at
    return total


# --- 音訊解碼 ---


def decode_waveform(
    audio_path: str,
    torch_module: object | None = None,
    popen: Callable[..., object] = subprocess.Popen,
    ffmpeg_exe: str = "ffmpeg",
) -> dict:
    """用 ffmpeg 把音訊解成 16 kHz 單聲道，包成 pyannote 吃的 {"waveform", "sample_rate"}。

    不交給 pyannote 內建的解碼：4.x 靠 torchcodec 與系統 ffmpeg 的版本配對，配不上就整個
    不能用；吃現成的波形可以完全繞開它。長影片要在這裡讀進記憶體（2 小時約 0.4 GB）。
    取消靠父行程把整個工作行程殺掉；ffmpeg 的輸出管線隨之關閉，它自己會結束。
    """
    import numpy as np

    torch = torch_module
    if torch is None:
        import torch as torch_module_
        torch = torch_module_
    command = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", audio_path,
               "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    pcm = bytearray()
    # 錯誤訊息寫進暫存檔而不是管線：壞檔案會讓 ffmpeg 吐出大量錯誤，管線塞滿它就卡住，
    # 我們又在等它的標準輸出，兩邊互等
    with tempfile.TemporaryFile() as errors:
        try:
            process = popen(command, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL)
        except OSError as e:
            raise WorkerFailure(STAGE_DECODE, f"無法執行 ffmpeg：{e}") from e
        finished = False
        try:
            while True:
                data = process.stdout.read(DECODE_CHUNK_BYTES)
                if not data:
                    break
                pcm.extend(data)
            finished = True
        finally:
            if not finished:
                process.kill()      # 讀取出錯：不能留下還在跑的 ffmpeg
            # 正常讀到結尾就讓它自己收尾：這時 poll() 可能還是 None（管線剛關、行程還沒結束），
            # 在那裡 kill 會把一次成功的解碼誤判成失敗
            process.wait()
        errors.seek(0)
        stderr = errors.read().decode("utf-8", "replace")
    if process.returncode != 0:
        raise WorkerFailure(STAGE_DECODE,
                            f"音訊解碼失敗：{_first_line(stderr) or f'ffmpeg 結束碼 {process.returncode}'}")
    count = len(pcm) // 2                   # 16 位元：多出來的半個樣本丟掉
    if count == 0:
        raise WorkerFailure(STAGE_DECODE, "音訊解碼後是空的（沒有任何聲音資料）")
    samples = np.frombuffer(pcm, dtype="<i2", count=count).astype(np.float32) / 32768.0
    return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": SAMPLE_RATE, "uri": "slidebox"}


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


# --- 能力探測 ---


def probe(find_spec: Callable[[str], object] = importlib.util.find_spec,
          get_version: Callable[[str], str] = importlib.metadata.version) -> dict:
    """pyannote.audio 裝了沒、什麼版本。**不 import pyannote、不 import torch**：
    find_spec 只會 import 上層的 pyannote（命名空間套件，沒有程式），版本讀的是套件的中繼資料。"""
    try:
        installed = find_spec(PACKAGE) is not None
    except (ImportError, ValueError):                       # ModuleNotFoundError 是 ImportError 的子類別
        installed = False
    try:
        version = get_version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        version = ""
    return {"protocol": PROTOCOL, "installed": installed, "version": version}


# --- 進入點 ---


def _write_result(path: str, payload: dict) -> None:
    """先寫暫存檔再換名：結果檔一出現就是完整的。"""
    payload = {"protocol": PROTOCOL, **payload}
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, allow_nan=False)
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyannote_worker.py", description="說話者區分的工作行程（由 API 啟動）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="回報 pyannote.audio 裝了沒、什麼版本（JSON，不 import torch）")
    run = commands.add_parser("run", help="區分一個音訊檔的說話者")
    run.add_argument("--audio", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--device", choices=DEVICES, default="auto")
    run.add_argument("--ffmpeg", default="ffmpeg")
    run.add_argument("--result", required=True)
    run.add_argument("--speakers", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None, environ: dict | None = None, stdout=None, stderr=None,
         engine_factory: Callable[..., PyannoteEngine] = PyannoteEngine) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if args.command == "probe":
        print(json.dumps(probe()), file=stdout, flush=True)
        return EXIT_OK

    token = str(environ.get(TOKEN_ENV, "")).strip()
    reporter = Reporter(stderr, token)
    try:
        engine = engine_factory(args.model, token=token, device=args.device, ffmpeg_exe=args.ffmpeg)
        result = engine.diarize(args.audio, args.speakers, reporter)
        _write_result(args.result, {
            "ok": True, "device": result.device, "turns": [list(t) for t in result.turns],
            "overlap_seconds": result.overlap_seconds, "gpu_peak_bytes": result.gpu_peak_bytes,
            "peak_rss_bytes": result.peak_rss_bytes})
        return EXIT_OK
    except WorkerFailure as e:
        failure = e
    except Exception as e:  # noqa: BLE001 沒料到的錯誤：追蹤留給 GPU 主機的 log（遮掉機密），結果檔仍是乾淨的失敗
        print(redact(traceback.format_exc(), token), file=stderr, flush=True)
        failure = WorkerFailure(STAGE_RUN, f"{type(e).__name__}: {e}")
    _write_result(args.result, {"ok": False, "stage": failure.stage, "detail": scrub(failure.detail, token),
                                "oom": failure.oom, "device": failure.device})
    return EXIT_FAILED


def _use_utf8() -> None:
    """父行程用 -X utf8 啟動這個檔案；萬一沒有，stderr 的中文進度也不能因為編碼而失敗。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _use_utf8()
    sys.exit(main())
