"""pyannote.audio 的說話者區分 adapter（SpeechDiarizer port 的實作）。

pyannote 與 PyTorch 不在 API 的主環境裡跑：它們在**第二個 Python 環境**（Dockerfile 的
/opt/diarize-venv）的獨立行程裡跑（pyannote_worker.py）。原因：語音辨識（ctranslate2）要
CUDA 12＋cuDNN 9，PyTorch 2.15 要 CUDA 13＋cuDNN 9，兩個版本的 cuDNN 輪子都解到
site-packages/nvidia/cudnn/lib、檔名相同；放在同一個環境裡會互相蓋掉，而 LD_LIBRARY_PATH 又把那個
資料夾指給 ctranslate2。分開之後主環境和沒有多人模式時完全一樣（議會與單人工作不受影響），
這一側不 import torch、不 import pyannote。

這個 adapter 只管行程：
- 用第二個環境的 Python（SLIDEBOX_DIARIZATION_PYTHON；沒設就是目前這個）啟動工作行程，
  **不把 LD_LIBRARY_PATH 交給它**（也不給 PYTHONPATH、PYTHONHOME 與 API 的金鑰）；token 只走環境變數
- 一行一行讀它的進度（stderr）、結果是它寫的 JSON 檔；結果檔先驗過每一個欄位才採用——
  誰在哪段時間說話寧可整個失敗也不猜
- 取消與逾時就是把整個行程（POSIX 上是整個行程群組）殺掉並收掉，比行程內的版本強：
  卡在 CUDA 裡的 pipeline 以前根本殺不掉
- 失敗分成「哪一步」，訊息的文字（含給操作者的建議）在這裡組

available() 仍然便宜：不載入模型、不下載、不 import pyannote——它問工作行程的 probe（只讀套件的
中繼資料），答案放幾秒，/capabilities 因此可以被一直問。

引擎可以換：換引擎只動這個檔案、工作行程與環境變數，其他地方只認 SpeechDiarizer port。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable

from ..domain.entities import DiarizerOutput, SpeakerTurn
from ..domain.errors import DiarizationUnavailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from ..usecases.code_fingerprint import code_fingerprint
from . import pyannote_worker as worker

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"
ENGINE = "pyannote.audio"
PACKAGE = worker.PACKAGE
TOKEN_ENV = worker.TOKEN_ENV
METRICS_ENV = worker.METRICS_ENV
DEVICE_ENV = "SLIDEBOX_DIARIZATION_DEVICE"
MODE_ENV = "SLIDEBOX_DIARIZER"
PYTHON_ENV = "SLIDEBOX_DIARIZATION_PYTHON"
TIMEOUT_ENV = "SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS"

_DEVICES = worker.DEVICES
_MODES = ("pyannote", "off")
# available() 的答案放幾秒：/capabilities 會被一直問
AVAILABLE_CACHE_SECONDS = 5.0
# 工作行程跑多久還沒結束就當它卡住了：預設六小時（最長 180 分鐘的影片、在很慢的 CPU 上也跑得完）；
# 0 是不設上限。取消不用等它——Pi 取消工作時行程立刻被殺掉。
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60
PROBE_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.1
KILL_WAIT_SECONDS = 10.0
READER_JOIN_SECONDS = 5.0
STDERR_TAIL_LINES = 20
CRASH_TAIL_LINES = 3

_POSIX = os.name == "posix"
_WORKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyannote_worker.py")
# 不交給工作行程的環境變數。LD_LIBRARY_PATH 是主環境給 ctranslate2 用的 cuDNN 12 的位置——torch 要的是
# cuDNN 13，撿到錯的那份就載不起來或當掉；PYTHONPATH／PYTHONHOME 讓主環境的程式與套件漏進第二個環境；
# API 的金鑰這個行程用不到，也不該讓第三方的程式碼拿到。
_NOT_FOR_THE_WORKER = frozenset({"LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "SLIDEBOX_API_KEY"})

_ESCAPE_HATCH = f"若是顯示卡記憶體或驅動（CUDA 版本）的問題，可以設 {DEVICE_ENV}=cpu 改用 CPU（較慢）"
# 負的結束碼是 POSIX 上被訊號終止；編號用 Linux 的（容器是 Linux），不看目前在哪個平台
_SIGNALS = {1: "SIGHUP", 2: "SIGINT", 4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL",
            11: "SIGSEGV", 15: "SIGTERM"}


class _EnvironmentProblem(RuntimeError):
    """說話者區分用的 Python 環境有問題（找不到、起不來、答非所問）；訊息就是給管理者看的原因。"""


class _Unreadable(ValueError):
    """工作行程留下的結果檔不是它該寫出來的樣子。"""


class _VersionMismatch(_Unreadable):
    """結果檔是另一個版本的協定寫的（API 與工作行程不是同一次建置出來的）。"""


class PyannoteSpeakerDiarizer:
    """官方 community-1 pipeline，只輸出匿名的 speaker label。

    所有會碰外面的東西（Python 直譯器、工作行程的檔案、套件查詢、ffmpeg、啟動行程、時鐘）都可以注入，
    測試因此不必安裝 pyannote、不必下載模型。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        token: str | None = None,
        device: str | None = None,
        mode: str | None = None,
        python: str | None = None,
        worker_path: str | None = None,
        timeout_seconds: float | str | None = None,
        spec_finder: Callable[[str], object] | None = None,
        version_finder: Callable[[], str] | None = None,
        ffmpeg_finder: Callable[[], str] | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._model_name = model_name
        self._token = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
        self._device_setting = (device if device is not None
                                else os.environ.get(DEVICE_ENV, "auto")).strip().lower() or "auto"
        self._mode = (mode if mode is not None
                      else os.environ.get(MODE_ENV, "pyannote")).strip().lower() or "pyannote"
        self._python_setting = (python if python is not None else os.environ.get(PYTHON_ENV, "")).strip()
        self._worker_path = worker_path or _WORKER_FILE
        self._timeout, self._timeout_error = _parse_timeout(
            timeout_seconds if timeout_seconds is not None else os.environ.get(TIMEOUT_ENV, ""))
        self._spec_finder = spec_finder or self._probed_spec
        self._version_finder = version_finder or self._probed_version
        self._ffmpeg_finder = ffmpeg_finder or _default_ffmpeg
        self._popen = popen
        self._clock = clock
        self._device_used = ""
        self._gpu_peak: int | None = None
        self._worker_peak: int | None = None
        self._cached: tuple[float, tuple[bool, str]] | None = None
        self._probed: tuple[float, dict | _EnvironmentProblem] | None = None

    def __repr__(self) -> str:
        # 不印 token：repr 會出現在 log、除錯輸出與例外追蹤裡
        return f"PyannoteSpeakerDiarizer(model={self._model_name!r}, device={self._device_setting!r})"

    # --- 身分 ---

    @property
    def engine(self) -> str:
        return ENGINE

    @property
    def version(self) -> str:
        try:
            return self._version_finder() or ""
        except Exception:  # noqa: BLE001 查不到版本就是沒裝
            return ""

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def fingerprint(self) -> str:
        """引擎＋版本＋模型＋程式：換了其中之一，存檔裡舊的說話者區分結果就不會被拿來冒充。

        程式是這一側（本模組）與實際跑的工作行程腳本（self._worker_path）的原始碼：區段怎麼整理、
        交疊怎麼算改了，套件版本沒變也不能沿用舊結果。讀不到腳本或原始碼時，指紋對不上任何舊存檔
        （見 usecases/code_fingerprint.py），不 raise。與 token、裝置、逾時等設定無關。
        """
        code = code_fingerprint([__name__], files=[self._worker_path])
        raw = json.dumps([self.engine, self.version, self.model, code], ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def device(self) -> str:
        """最近一次成功的區分實際用的裝置（"cuda"／"cpu"，是工作行程回報的）；還沒跑過是空字串。"""
        return self._device_used

    @property
    def gpu_peak_bytes(self) -> int | None:
        """最近一次成功的區分，PyTorch 在顯示卡上的記憶體峰值（只算 PyTorch 的部分）；沒用顯示卡或量不到是 None。"""
        return self._gpu_peak

    @property
    def worker_peak_bytes(self) -> int | None:
        """最近一次成功的區分，工作行程自己的記憶體峰值（含載入的模型）；量不到（Windows）是 None。"""
        return self._worker_peak

    # --- 能力檢查：便宜 ---

    def available(self) -> tuple[bool, str]:
        """token 在、套件裝了、沒被關掉。**不載入模型、不下載、不 import pyannote**
        （要問第二個環境裝了沒，會起一個很小的行程，答案放幾秒）。

        也不回報上一次的載入失敗：Pi 看到「不行」就不會送工作，沒有工作就沒有下一次載入——
        暫時性的失敗（網路斷一下）會變成永久的。載入失敗由工作自己的錯誤訊息回報。
        """
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < AVAILABLE_CACHE_SECONDS:
            return self._cached[1]
        answer = self._check()
        self._cached = (now, answer)
        return answer

    def _check(self) -> tuple[bool, str]:
        if self._mode == "off":
            return False, f"多人說話者區分已用 {MODE_ENV}=off 關閉"
        if self._mode not in _MODES:
            return False, f"{MODE_ENV} 只能是 pyannote 或 off（目前是「{self._mode}」）"
        if self._device_setting not in _DEVICES:
            return False, f"{DEVICE_ENV} 只能是 auto、cpu 或 cuda（目前是「{self._device_setting}」）"
        if self._timeout_error:
            return False, self._timeout_error
        if not self._token:
            return False, (f"沒有設定 {TOKEN_ENV}；多人模式需要 Hugging Face token，"
                           f"並先在 Hugging Face 接受 {self._model_name} 的使用條款")
        try:
            installed = self._spec_finder(PACKAGE) is not None
        except _EnvironmentProblem as e:
            return False, str(e)
        except (ImportError, ValueError):                   # ModuleNotFoundError 是 ImportError 的子類別
            installed = False
        if not installed:
            return False, (f"沒有安裝 {PACKAGE}；GPU 映像要用 --only-group diarization 另外建一個環境，"
                           f"並用 {PYTHON_ENV} 指給 API（目前找的是 {self._python_setting or '目前這個環境'}）")
        return True, ""

    # --- 第二個環境：找得到、起得來、答得出 ---

    def _resolve_python(self) -> str:
        setting = self._python_setting or sys.executable
        if os.path.isabs(setting) or os.sep in setting or "/" in setting:
            found = setting if os.path.isfile(setting) else None
        else:
            found = shutil.which(setting)
        if not found:
            raise _EnvironmentProblem(
                f"找不到說話者區分用的 Python（{PYTHON_ENV}={setting}）；"
                f"GPU 映像要用 --only-group diarization 另外建一個環境")
        return found

    def _probed_spec(self, name: str) -> object | None:
        return object() if self._probe()["installed"] else None

    def _probed_version(self) -> str:
        return self._probe()["version"]                      # 環境壞了就丟例外：version 屬性會把它當成「沒有版本」

    def _probe(self) -> dict:
        """問工作行程的 probe；答案（含失敗）放幾秒。"""
        now = self._clock()
        if self._probed is None or now - self._probed[0] >= AVAILABLE_CACHE_SECONDS:
            try:
                answer: dict | _EnvironmentProblem = self._run_probe()
            except _EnvironmentProblem as e:
                answer = e
            self._probed = (now, answer)
        answer = self._probed[1]
        if isinstance(answer, _EnvironmentProblem):
            raise answer
        return answer

    def _run_probe(self) -> dict:
        python = self._resolve_python()
        command = [python, "-I", "-X", "utf8", self._worker_path, "probe"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=PROBE_TIMEOUT_SECONDS, env=self._worker_env(token=False),
                                    stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise _EnvironmentProblem(
                f"說話者區分的 Python 環境沒有在 {PROBE_TIMEOUT_SECONDS:g} 秒內回應（{PYTHON_ENV}={python}）") from None
        except OSError as e:
            raise _EnvironmentProblem(
                f"無法執行說話者區分用的 Python（{PYTHON_ENV}={python}）：{self._safe(e)}") from e
        if result.returncode != 0:
            raise _EnvironmentProblem(
                f"說話者區分的工作行程無法啟動（{describe_exit(result.returncode)}，{PYTHON_ENV}={python}）"
                + self._tail_suffix(result.stderr.splitlines()))
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        try:
            answer = json.loads(lines[-1]) if lines else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise _EnvironmentProblem(f"說話者區分的工作行程回應了無法理解的內容（{PYTHON_ENV}={python}）")
        if answer.get("protocol") != worker.PROTOCOL:
            raise _EnvironmentProblem(
                f"說話者區分的工作行程與 API 的版本不一致（協定 {answer.get('protocol')!r}，"
                f"預期 {worker.PROTOCOL}）；請重新建置映像")
        return {"installed": answer.get("installed") is True, "version": str(answer.get("version") or "")}

    def _worker_env(self, token: bool) -> dict[str, str]:
        env = {name: value for name, value in os.environ.items() if name.upper() not in _NOT_FOR_THE_WORKER}
        env.pop(TOKEN_ENV, None)
        if token:
            env[TOKEN_ENV] = self._token
        return env

    # --- 區分 ---

    def diarize(
        self,
        audio_path: str,
        duration: float,
        speaker_count: int | None,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> DiarizerOutput:
        if is_cancelled():
            raise OperationCancelled()
        available, reason = self._check()
        if not available:
            raise DiarizationUnavailable(reason)
        try:
            python = self._resolve_python()
        except _EnvironmentProblem as e:
            raise DiarizationUnavailable(str(e)) from e
        try:
            ffmpeg = self._ffmpeg_finder()
        except Exception as e:  # noqa: BLE001
            raise DiarizationUnavailable(f"找不到 ffmpeg（音訊解碼要用）：{self._safe(e)}") from e

        workdir = tempfile.mkdtemp(prefix="slidebox_diarize_")
        try:
            result_path = os.path.join(workdir, "result.json")
            command = [python, "-I", "-X", "utf8", self._worker_path, "run", "--audio", audio_path,
                       "--model", self._model_name, "--device", self._device_setting, "--ffmpeg", ffmpeg,
                       "--result", result_path]
            if speaker_count:
                command += ["--speakers", str(speaker_count)]
            code, tail = self._run_worker(command, progress, is_cancelled)
            if is_cancelled():                                  # 工作行程跑完了，使用者按的取消照樣算
                raise OperationCancelled()
            data = self._read_result(result_path)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if data is None:
            raise DiarizationUnavailable(self._crash_message(code, tail))
        if code not in (worker.EXIT_OK, worker.EXIT_FAILED):
            # CUDA 的行程常在收尾（解構）時才 segfault。結果檔是先寫暫存檔再換名的，檔案在就是完整的
            logger.warning("說話者區分的工作行程結束碼是 %s（%s），但結果檔是完整的，照用", code, describe_exit(code))
        if not data["ok"]:
            raise DiarizationUnavailable(self._failure_message(data))

        output = DiarizerOutput(turns=tuple(SpeakerTurn(*t) for t in data["turns"]),
                                overlap_seconds=data["overlap_seconds"])
        self._device_used = data["device"]
        self._gpu_peak = data["gpu_peak_bytes"]
        self._worker_peak = data["peak_rss_bytes"]
        voices = len({t.speaker for t in output.turns})
        progress(1.0, f"說話者區分完成（{voices} 個聲音）")
        return output

    def _run_worker(self, command: list[str], progress: ProgressCallback,
                    is_cancelled: CancelCheck) -> tuple[int, list[str]]:
        """啟動工作行程、等它結束；回傳 (結束碼, stderr 最後幾行)。取消、逾時、任何例外都把它殺掉並收掉。"""
        options: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=self._worker_env(token=True), text=True, encoding="utf-8", errors="replace",
                             bufsize=1)
        if _POSIX:
            options["start_new_session"] = True              # 自己一個行程群組：要殺的時候連它的子行程一起殺
        deadline = None if self._timeout is None else self._clock() + self._timeout
        try:
            process = self._popen(command, **options)
        except OSError as e:
            raise DiarizationUnavailable(
                f"無法啟動說話者區分的工作行程（{PYTHON_ENV}={command[0]}）：{self._safe(e)}") from e

        events: queue.Queue[tuple[float | None, str]] = queue.Queue()
        tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        reader = threading.Thread(target=_pump, args=(process.stderr, events, tail), daemon=True)
        reader.start()
        try:
            while True:
                self._drain(events, progress)
                if is_cancelled():
                    raise OperationCancelled()               # 行程由下面的 except 殺掉
                if deadline is not None and self._clock() >= deadline:
                    raise DiarizationUnavailable(
                        f"說話者區分超過 {self._timeout:g} 秒仍未完成，已中止"
                        f"（可用 {TIMEOUT_ENV} 調整上限；在 CPU 上跑長影片會比較久）")
                if process.poll() is not None:
                    break
                try:
                    item = events.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                self._forward(item, progress)
        except BaseException:
            terminate(process)                               # 不管為什麼離開（取消、逾時、任何例外）：行程不能留著
            reader.join(timeout=READER_JOIN_SECONDS)         # 管線隨著行程關了，讀它的執行緒也收掉
            raise
        reader.join(timeout=READER_JOIN_SECONDS)
        self._drain(events, progress)                        # 結束前最後幾行進度
        return process.returncode, list(tail)

    def _drain(self, events: queue.Queue, progress: ProgressCallback) -> None:
        while True:
            try:
                item = events.get_nowait()
            except queue.Empty:
                return
            self._forward(item, progress)

    def _forward(self, item: tuple[float | None, str], progress: ProgressCallback) -> None:
        fraction, text = item
        progress(fraction, self._safe(text))                 # 在呼叫者的執行緒上；另一個行程的文字不信任，再遮一次

    # --- 結果 ---

    def _read_result(self, path: str) -> dict | None:
        """結果檔驗過每一個欄位才採用；沒有檔案是 None（行程沒留下結果）。"""
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise DiarizationUnavailable(
                f"說話者區分的工作行程留下了無法理解的結果檔（{type(e).__name__}）") from e
        try:
            return _validate(raw)
        except _VersionMismatch as e:
            raise DiarizationUnavailable(f"說話者區分的工作行程與 API 的版本不一致（{e}）；請重新建置映像") from e
        except _Unreadable as e:
            raise DiarizationUnavailable(f"說話者區分的工作行程回傳了無法理解的結果（{e}）") from e

    def _failure_message(self, failure: dict) -> str:
        """工作行程回報的乾淨失敗：依它失敗在哪一步，說該說的話。"""
        stage, detail, device = failure["stage"], self._safe(failure["detail"]), failure["device"]
        if stage == worker.STAGE_TERMS:
            return (f"無法載入 {self._model_name}：請先在 Hugging Face 接受這個模型的使用條款，"
                    f"並確認 {TOKEN_ENV} 有讀取權限")
        if stage == worker.STAGE_LOAD:
            # 條款（或 token 權限）的建議只屬於載入階段；改用 CPU 的逃生口在已經指定 cpu 的時候沒有意義
            return (f"說話者區分模型載入失敗：{detail}。請確認已安裝 {PACKAGE}、設定 {TOKEN_ENV}，"
                    f"並接受 {self._model_name} 的使用條款" + self._escape_hatch())
        if stage == worker.STAGE_DECODE:
            return detail or "音訊解碼失敗"
        if stage == worker.STAGE_EMPTY:
            return "說話者區分沒有找到可用的人聲區段"
        # 其餘（run 與沒見過的階段）：模型已經載入，不提模型條款；
        # 跑到一半最常見的是顯示卡記憶體不夠（同一張卡同時給 Ollama、語音辨識與說話者區分用）
        on_gpu = device != "cpu"
        if stage == worker.STAGE_RUN and on_gpu and failure["oom"]:
            return (f"說話者區分失敗：顯示卡記憶體不足（Ollama、語音辨識與說話者區分共用同一張卡）：{detail}。"
                    f"稍後再試，或設 {DEVICE_ENV}=cpu 改用 CPU（較慢）")
        return f"說話者區分失敗（{detail or stage}）" + (f"。{_ESCAPE_HATCH}" if on_gpu else "")

    def _crash_message(self, code: int, tail: list[str]) -> str:
        if code == worker.EXIT_OK:
            return "說話者區分的工作行程結束了，卻沒有留下結果" + self._tail_suffix(tail, empty="")
        return (f"說話者區分的工作行程異常結束（{describe_exit(code)}）" + self._tail_suffix(tail)
                + self._escape_hatch())                      # 原生的當掉最常出在驅動／CUDA

    def _tail_suffix(self, lines: list[str], empty: str = "；沒有留下任何訊息") -> str:
        last = [self._safe(line)[:160] for line in lines if line.strip()][-CRASH_TAIL_LINES:]
        return f"：{' | '.join(last)}" if last else empty

    def _escape_hatch(self) -> str:
        return "" if self._device_setting == "cpu" else f"。{_ESCAPE_HATCH}"

    def _safe(self, error: object) -> str:
        """給人看的文字：蓋掉 token、壓成一行、截短。"""
        return worker.scrub(error, self._token)


# --- 工作行程的輸出入 ---


def _default_ffmpeg() -> str:
    from .ffmpeg import get_ffmpeg_exe     # 延遲 import：只有真的要區分的時候才需要

    return get_ffmpeg_exe()


def _parse_timeout(value: object) -> tuple[float | None, str]:
    """(上限秒數或 None＝不設上限, 設定值寫錯時的說明)。空白是預設值，0 是不設上限，寫錯是設定錯誤。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return float(DEFAULT_TIMEOUT_SECONDS), ""
        try:
            seconds = float(text)
        except ValueError:
            seconds = math.nan
    else:
        seconds = float(value)                               # type: ignore[arg-type]
    if not math.isfinite(seconds) or seconds < 0:
        return float(DEFAULT_TIMEOUT_SECONDS), f"{TIMEOUT_ENV} 必須是秒數（0 表示不設上限），目前是「{value}」"
    return (None if seconds == 0 else seconds), ""


def _pump(stream, events: queue.Queue, tail: deque) -> None:
    """讀工作行程的 stderr：進度行放進佇列（由呼叫者的執行緒取走），其餘的留最後幾行給當掉時看。"""
    try:
        for line in stream:
            line = line.rstrip("\r\n")
            if line.startswith(worker.PROGRESS_PREFIX):
                parsed = _parse_progress(line[len(worker.PROGRESS_PREFIX):])
                if parsed is not None:
                    events.put(parsed)
                    continue
            if line.strip():
                tail.append(line[:500])
    except (OSError, ValueError):                            # 行程被殺掉，管線在我們讀的時候關了
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _parse_progress(payload: str) -> tuple[float | None, str] | None:
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    fraction = data.get("fraction")
    if fraction is not None and not (_is_number(fraction) and 0 <= fraction <= 1):
        return None
    return fraction, data["text"]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_size(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _validate(raw: object) -> dict:
    """工作行程的結果檔：每個欄位都驗。壞掉的區段不能被悄悄丟掉剩下的照用——
    那會讓後面的對齊把字歸給錯的人。"""
    if not isinstance(raw, dict):
        raise _Unreadable("不是 JSON 物件")
    if raw.get("protocol") != worker.PROTOCOL:
        raise _VersionMismatch(f"協定 {raw.get('protocol')!r}，預期 {worker.PROTOCOL}")
    ok = raw.get("ok")
    if not isinstance(ok, bool):
        raise _Unreadable("沒有 ok 欄位")
    if not ok:
        stage, detail, oom, device = (raw.get("stage"), raw.get("detail", ""), raw.get("oom", False),
                                      raw.get("device", ""))
        if not (isinstance(stage, str) and stage and isinstance(detail, str) and isinstance(oom, bool)
                and isinstance(device, str)):
            raise _Unreadable("失敗的欄位不對")
        return {"ok": False, "stage": stage, "detail": detail, "oom": oom, "device": device}
    turns = raw.get("turns")
    if not isinstance(turns, list) or not turns:
        raise _Unreadable("沒有任何區段")
    parsed = []
    for turn in turns:
        if not (isinstance(turn, list) and len(turn) == 3):
            raise _Unreadable("區段不是 [起, 訖, 標籤]")
        start, end, label = turn
        if not (_is_number(start) and _is_number(end) and 0 <= start < end):
            raise _Unreadable("區段的時間不對")
        if not (isinstance(label, str) and label):
            raise _Unreadable("區段沒有標籤")
        parsed.append((float(start), float(end), label))
    overlap, device = raw.get("overlap_seconds"), raw.get("device")
    if not (_is_number(overlap) and overlap >= 0):
        raise _Unreadable("重疊秒數不對")
    if device not in ("cpu", "cuda"):
        raise _Unreadable("裝置不對")
    gpu_peak, rss_peak = raw.get("gpu_peak_bytes"), raw.get("peak_rss_bytes")
    if not (_is_size(gpu_peak) and _is_size(rss_peak)):
        raise _Unreadable("記憶體峰值不對")
    parsed.sort(key=lambda t: (t[0], t[1]))
    return {"ok": True, "turns": parsed, "overlap_seconds": float(overlap), "device": device,
            "gpu_peak_bytes": gpu_peak, "peak_rss_bytes": rss_peak}


def describe_exit(code: int) -> str:
    """工作行程怎麼結束的，用人看得懂的話。"""
    if code < 0:
        hint = "；常見的原因是主機記憶體不足，被系統終止" if code == -9 else ""
        return f"被 {_SIGNALS.get(-code, f'訊號 {-code}')} 終止{hint}"
    return f"結束碼 {code}"


def terminate(process, wait_seconds: float | None = None) -> None:
    """把工作行程殺掉並收掉（POSIX 上殺整個行程群組：它的 ffmpeg 與執行緒池的子行程也不留）。"""
    if process.poll() is not None:
        return
    try:
        if _POSIX:
            os.killpg(process.pid, getattr(signal, "SIGKILL", 9))
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):             # 已經不在了
        pass
    wait_seconds = KILL_WAIT_SECONDS if wait_seconds is None else wait_seconds
    try:
        process.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        logger.warning("說話者區分的工作行程在被終止之後 %s 秒內仍未結束", wait_seconds)
