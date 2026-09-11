"""SpeechTranscriber 的 faster-whisper 實作：沒有字幕時從語音產生字幕。

只用 CPU。本機沒有 CUDA DLL（cublas64_12.dll），而 ctranslate2 對 RTX 5060 Ti
（Blackwell）的支援也不確定；Zen 5 CPU 上 large-v3-turbo 實測日韓 3.9～4.9 倍
即時，已經夠用。
"""
from __future__ import annotations

from collections.abc import Callable

from ..domain.entities import Cue
from ..domain.errors import NoSubtitlesAvailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback


def _default_model_factory(name: str):
    # 延遲 import：faster-whisper 會連帶載入 ctranslate2、onnxruntime、av，
    # 放在模組頂端會拖慢整個 app 的啟動，而多數影片根本用不到語音辨識。
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="cpu", compute_type="int8")


def _brief(error: Exception) -> str:
    """Hugging Face、PyAV 的錯誤動輒數百字英文，截短後再放進狀態列。"""
    text = " ".join(str(error).split())
    return text if len(text) <= 160 else text[:157] + "…"


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


class FasterWhisperTranscriber:
    def __init__(
        self,
        model_name: str,
        model_factory: Callable[[str], object] = _default_model_factory,
    ):
        self._model_name = model_name
        self._model_factory = model_factory
        # 模型載入要約 40 秒，載一次後留著給之後的生成重複使用
        self._model = None

    def transcribe(
        self,
        audio_path: str,
        duration: float,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> tuple[tuple[Cue, ...], str]:
        model = self._load(progress)
        if is_cancelled():
            raise OperationCancelled()

        total = f" / {_mmss(duration)}" if duration > 0 else ""
        cues: list[Cue] = []
        # faster-whisper 在回傳 generator 之前會一次解碼整個檔、跑完 VAD、偵測
        # 語言——長影片要幾十秒且無法中斷。先更新狀態列，否則會一直停在上一步
        # 的「下載音訊…」。
        progress(None, "準備語音辨識…（解碼音訊、偵測人聲）")
        try:
            # vad_filter 跳過靜音與純音樂段：Whisper 在沒有人聲的地方會幻覺出
            # 「Thanks for watching」之類的句子，而那些句子會被寫進投影片。
            # 語言交給 Whisper 自動偵測，不拿 yt-dlp 的 language 當提示——
            # 上傳者標錯時，強制指定會產出整份錯誤語言的轉錄。
            segments, info = model.transcribe(audio_path, vad_filter=True)
            # segments 是惰性 generator，辨識在迭代時才真正發生，所以取消要
            # 在每段之間檢查，錯誤也可能在迭代中途才冒出來。
            for seg in segments:
                if is_cancelled():
                    raise OperationCancelled()
                text = seg.text.strip()
                # 略過空白段，以及緊接著的重複句：Whisper large 家族偶爾會卡在
                # 同一句話上重複輸出（幻覺迴圈），而語音結果不走滾動去重，這些
                # 重複會原封不動變成投影片內容。隔開出現的相同句子照常保留。
                if text and not (cues and cues[-1].text == text):
                    cues.append(Cue(start=float(seg.start), end=float(seg.end), text=text))
                # 段落時間戳可能略超過影片長度（turbo 以 4 秒對齊），夾住以免顯示
                # 「0:20 / 0:19」這種看起來像算錯的進度。
                done = min(seg.end, duration) if duration > 0 else seg.end
                progress(None, f"語音辨識中… {_mmss(done)}{total}")
        except OperationCancelled:
            raise
        except Exception as e:  # noqa: BLE001 底層錯誤一律轉成可讀訊息
            raise NoSubtitlesAvailable(f"這部影片沒有字幕，語音辨識也失敗：{_brief(e)}") from e
        return tuple(cues), info.language

    def _load(self, progress: ProgressCallback):
        if self._model is not None:
            return self._model
        progress(None, f"載入語音辨識模型 {self._model_name}…（首次使用需下載）")
        try:
            self._model = self._model_factory(self._model_name)
        except ImportError as e:
            # 不只「沒安裝」會走到這裡，ctranslate2 的 DLL 載入失敗也是 ImportError，
            # 所以帶上原因，不要一律叫使用者 uv sync。
            raise NoSubtitlesAvailable(
                f"這部影片沒有字幕；語音辨識套件 faster-whisper 無法載入（{_brief(e)}），"
                "若未安裝請執行 uv sync"
            ) from e
        except Exception as e:  # noqa: BLE001 例如首次使用時無法下載模型
            raise NoSubtitlesAvailable(
                f"這部影片沒有字幕，語音辨識模型 {self._model_name} 也無法載入：{_brief(e)}"
            ) from e
        return self._model
