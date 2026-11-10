"""pyannote.audio 的說話者區分 adapter（SpeechDiarizer port 的實作）。

這個模組只在 GPU 主機的多人影片工作被叫到時才載入 pyannote 與 torch；Pi 端與
議會的單人工作都不會碰到它。沒有安裝套件、沒有 Hugging Face token 時，available()
用便宜的檢查（不載入模型、不下載任何東西）回答「不行」與原因，/capabilities 因此可以
被一直問。

引擎可以換：換引擎只動這個檔案與環境變數，其他地方只認 SpeechDiarizer port。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable

from ..domain.entities import DiarizerOutput, SpeakerTurn
from ..domain.errors import DiarizationUnavailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback

DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"
ENGINE = "pyannote.audio"
PACKAGE = "pyannote.audio"
TOKEN_ENV = "HUGGINGFACE_TOKEN"
DEVICE_ENV = "SLIDEBOX_DIARIZATION_DEVICE"
MODE_ENV = "SLIDEBOX_DIARIZER"
METRICS_ENV = "PYANNOTE_METRICS_ENABLED"

_DEVICES = ("auto", "cpu", "cuda")
_MODES = ("pyannote", "off")
# available() 的答案放幾秒：/capabilities 會被一直問
AVAILABLE_CACHE_SECONDS = 5.0

SAMPLE_RATE = 16000
DECODE_CHUNK_BYTES = 1 << 20

_ESCAPE_HATCH = f"若是顯示卡記憶體或驅動（CUDA 版本）的問題，可以設 {DEVICE_ENV}=cpu 改用 CPU（較慢）"
# Hugging Face token 的樣子；錯誤訊息一律先把它蓋掉
_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{8,}")
_STEP_NAMES = {
    "segmentation": "偵測人聲",
    "speaker_counting": "估計人數",
    "embeddings": "比對聲紋",
    "discrete_diarization": "整理結果",
}


class PyannoteSpeakerDiarizer:
    """官方 community-1 pipeline，只輸出匿名的 speaker label。

    所有會碰外面的東西（載入 pipeline、torch、解碼音訊、套件查詢、時鐘）都可以注入，
    測試因此不必安裝 pyannote、不必下載模型。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        token: str | None = None,
        device: str | None = None,
        mode: str | None = None,
        pipeline_factory: Callable[..., object] | None = None,
        torch_module: object | None = None,
        audio_loader: Callable[[str, CancelCheck], object] | None = None,
        spec_finder: Callable[[str], object] | None = None,
        version_finder: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._model_name = model_name
        self._token = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
        self._device_setting = (device if device is not None
                                else os.environ.get(DEVICE_ENV, "auto")).strip().lower() or "auto"
        self._mode = (mode if mode is not None
                      else os.environ.get(MODE_ENV, "pyannote")).strip().lower() or "pyannote"
        self._pipeline_factory = pipeline_factory
        self._torch = torch_module
        self._audio_loader = audio_loader
        self._spec_finder = spec_finder or importlib.util.find_spec
        self._version_finder = version_finder or _installed_version
        self._clock = clock
        self._pipeline = None
        self._device_used = ""
        self._cached: tuple[float, tuple[bool, str]] | None = None

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
        """引擎＋版本＋模型：換了其中之一，存檔裡舊的說話者區分結果就不會被拿來冒充。"""
        raw = json.dumps([self.engine, self.version, self.model], ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def device(self) -> str:
        """實際用的裝置（"cuda"／"cpu"）；還沒載入時是空字串。"""
        return self._device_used

    # --- 能力檢查：便宜 ---

    def available(self) -> tuple[bool, str]:
        """token 在、套件裝了、沒被關掉。**不載入模型、不下載、不 import pyannote**。

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
        if not self._token:
            return False, (f"沒有設定 {TOKEN_ENV}；多人模式需要 Hugging Face token，"
                           f"並先在 Hugging Face 接受 {self._model_name} 的使用條款")
        try:
            installed = self._spec_finder(PACKAGE) is not None
        except (ImportError, ValueError):                   # ModuleNotFoundError 是 ImportError 的子類別
            installed = False
        if not installed:
            return False, f"沒有安裝 {PACKAGE}；GPU 映像要用 --group diarization 建置"
        return True, ""

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
        pipeline = self._load(progress)
        if is_cancelled():
            raise OperationCancelled()

        progress(None, "解碼音訊…")
        loader = self._audio_loader or (lambda path, cancelled: decode_waveform(path, cancelled, self._torch))
        audio = loader(audio_path, is_cancelled)
        if is_cancelled():
            raise OperationCancelled()

        progress(None, "區分說話者…")
        cancelled_in_hook = False

        def hook(step_name, step_artifact=None, file=None, total=None, completed=None):
            # pyannote 在每個步驟、每批資料之間呼叫它；從這裡丟例外就能在跑到一半時中止
            nonlocal cancelled_in_hook
            if is_cancelled():
                cancelled_in_hook = True
                raise OperationCancelled()
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
        except OperationCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if cancelled_in_hook or is_cancelled():
                raise OperationCancelled() from e
            raise DiarizationUnavailable(
                f"說話者區分失敗（{self._safe(e)}）；請確認 GPU 主機已接受 {self._model_name} 的模型條款。"
                f"{_ESCAPE_HATCH}"
            ) from e
        if is_cancelled():
            raise OperationCancelled()
        if not turns:
            raise DiarizationUnavailable("說話者區分沒有找到可用的人聲區段")
        voices = len({t.speaker for t in turns})
        progress(1.0, f"說話者區分完成（{voices} 個聲音）")
        return DiarizerOutput(turns=tuple(turns), overlap_seconds=round(overlap, 3))

    # --- 載入 ---

    def _load(self, progress: ProgressCallback):
        if self._pipeline is not None:
            return self._pipeline
        available, reason = self._check()
        if not available:
            raise DiarizationUnavailable(reason)
        progress(None, "載入說話者區分模型…（首次使用需下載）")
        try:
            # 要在 import pyannote 之前：它在 import 的時候就讀這個環境變數
            os.environ.setdefault(METRICS_ENV, "0")
            factory = self._pipeline_factory or _default_factory()
            pipeline = factory(self._model_name, token=self._token)
            if pipeline is None:
                # pyannote 3.x 沒接受條款時不丟例外，只回 None
                raise DiarizationUnavailable(
                    f"無法載入 {self._model_name}：請先在 Hugging Face 接受這個模型的使用條款，"
                    f"並確認 {TOKEN_ENV} 有讀取權限")
            device = self._move(pipeline, progress)
        except DiarizationUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise DiarizationUnavailable(
                f"說話者區分模型載入失敗：{self._safe(e)}。請確認已安裝 {PACKAGE}、設定 {TOKEN_ENV}，"
                f"並接受 {self._model_name} 的使用條款。{_ESCAPE_HATCH}"
            ) from e
        self._pipeline = pipeline
        self._device_used = device
        progress(None, f"說話者區分模型已載入（{device}）")
        return pipeline

    def _move(self, pipeline, progress: ProgressCallback) -> str:
        """把 pipeline 放到顯示卡上（或留在 CPU）；回傳實際用的裝置。

        cpu 是逃生口，連 torch 都不碰（鎖檔裡的 torch 是 CUDA 13，驅動對不上時 import 或
        第一次用顯示卡才會出錯）。auto 試顯示卡、不行就留在 CPU；cuda 是明確要求，失敗照實回報。
        """
        if self._device_setting == "cpu":
            return "cpu"
        torch = self._torch
        if torch is None:
            import torch as torch_module
            torch = torch_module
        if self._device_setting == "auto" and not torch.cuda.is_available():
            return "cpu"
        try:
            pipeline.to(torch.device("cuda"))
        except Exception as e:  # noqa: BLE001
            if self._device_setting != "auto":
                raise
            progress(None, f"顯示卡不能用（{self._safe(e)}），說話者區分改用 CPU（較慢）")
            pipeline.to(torch.device("cpu"))
            return "cpu"
        return "cuda"

    def _safe(self, error: Exception) -> str:
        """給人看的錯誤：蓋掉 token、壓成一行、截短。"""
        text = " ".join(str(error).split())
        if self._token:
            text = text.replace(self._token, "***")
        text = _HF_TOKEN_RE.sub("hf_***", text)
        return text if len(text) <= 240 else text[:237] + "…"


def _installed_version() -> str:
    try:
        return importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _default_factory() -> Callable[..., object]:
    from pyannote.audio import Pipeline   # 延遲 import：載入 torch 要好幾秒，也只有多人工作用得到

    return Pipeline.from_pretrained


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


def _turns(annotation: object) -> list[SpeakerTurn]:
    return [SpeakerTurn(start, end, label)
            for start, end, label in sorted(_tracks(annotation), key=lambda t: (t[0], t[1]))]


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
    is_cancelled: CancelCheck,
    torch_module: object | None = None,
    popen: Callable[..., object] = subprocess.Popen,
    ffmpeg_exe: str | None = None,
) -> dict:
    """用 ffmpeg 把音訊解成 16 kHz 單聲道，包成 pyannote 吃的 {"waveform", "sample_rate"}。

    不交給 pyannote 內建的解碼：4.x 靠 torchcodec 與系統 ffmpeg 的版本配對，配不上就整個
    不能用；吃現成的波形可以完全繞開它。長影片要在這裡讀進記憶體（2 小時約 0.4 GB）。
    ffmpeg 的輸出一塊一塊讀，每塊之間檢查取消。
    """
    import numpy as np

    torch = torch_module
    if torch is None:
        import torch as torch_module_
        torch = torch_module_
    exe = ffmpeg_exe
    if exe is None:
        from .ffmpeg import get_ffmpeg_exe
        exe = get_ffmpeg_exe()
    command = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", audio_path,
               "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    pcm = bytearray()
    # 錯誤訊息寫進暫存檔而不是管線：壞檔案會讓 ffmpeg 吐出大量錯誤，管線塞滿它就卡住，
    # 我們又在等它的標準輸出，兩邊互等
    with tempfile.TemporaryFile() as errors:
        try:
            process = popen(command, stdout=subprocess.PIPE, stderr=errors)
        except OSError as e:
            raise DiarizationUnavailable(f"無法執行 ffmpeg：{e}") from e
        finished = False
        try:
            while True:
                data = process.stdout.read(DECODE_CHUNK_BYTES)
                if not data:
                    break
                pcm.extend(data)
                if is_cancelled():
                    raise OperationCancelled()
            finished = True
        finally:
            if not finished:
                process.kill()      # 取消或讀取出錯：不能留下還在跑的 ffmpeg
            # 正常讀到結尾就讓它自己收尾：這時 poll() 可能還是 None（管線剛關、行程還沒結束），
            # 在那裡 kill 會把一次成功的解碼誤判成失敗
            process.wait()
        errors.seek(0)
        stderr = errors.read().decode("utf-8", "replace")
    if process.returncode != 0:
        raise DiarizationUnavailable(f"音訊解碼失敗：{_first_line(stderr) or f'ffmpeg 結束碼 {process.returncode}'}")
    count = len(pcm) // 2                   # 16 位元：多出來的半個樣本丟掉
    if count == 0:
        raise DiarizationUnavailable("音訊解碼後是空的（沒有任何聲音資料）")
    samples = np.frombuffer(pcm, dtype="<i2", count=count).astype(np.float32) / 32768.0
    return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": SAMPLE_RATE, "uri": "slidebox"}


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""
