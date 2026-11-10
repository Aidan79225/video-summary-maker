"""pyannote.audio 的匿名說話者區分 adapter。

這個模組只在 GPU 主機的多人影片工作被叫到時載入 pyannote。Pi 端不會匯入它，
而沒有安裝套件或 Hugging Face token 時，能力端點會回可讀的拒絕原因。
"""
from __future__ import annotations

import os
from collections.abc import Callable

from ..domain.entities import SpeakerTurn
from ..domain.errors import DiarizationUnavailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback

DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"
TOKEN_ENV = "HUGGINGFACE_TOKEN"
DEVICE_ENV = "SLIDEBOX_DIARIZATION_DEVICE"


def _short(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text if len(text) <= 240 else text[:237] + "…"


class PyannoteSpeakerDiarizer:
    """使用官方 community-1 pipeline，只輸出匿名 speaker label。"""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        token: str | None = None,
        device: str | None = None,
        pipeline_factory: Callable[..., object] | None = None,
        torch_module: object | None = None,
    ):
        self._model_name = model_name
        self._token = token if token is not None else os.environ.get(TOKEN_ENV, "")
        self._device_name = (device or os.environ.get(DEVICE_ENV, "auto")).strip().lower()
        self._pipeline_factory = pipeline_factory
        self._torch = torch_module
        self._pipeline = None
        self._failure: str | None = None

    def available(self) -> tuple[bool, str]:
        if not self._token:
            return False, f"沒有設定 {TOKEN_ENV}；多人模式需要 Hugging Face token"
        try:
            self._load()
        except DiarizationUnavailable as e:
            return False, str(e)
        return True, ""

    def diarize(
        self,
        audio_path: str,
        duration: float,
        speaker_count: int | None,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> tuple[SpeakerTurn, ...]:
        if is_cancelled():
            raise OperationCancelled()
        pipeline = self._load()
        progress(None, "區分說話者…")
        kwargs = {"num_speakers": speaker_count} if speaker_count else {}
        try:
            output = pipeline(audio_path, **kwargs)
            if is_cancelled():
                raise OperationCancelled()
            annotation = getattr(output, "exclusive_speaker_diarization", None)
            if annotation is None:
                annotation = getattr(output, "speaker_diarization", output)
            raw_turns = []
            turns = []
            labels: dict[str, str] = {}
            for turn, _, raw_label in annotation.itertracks(yield_label=True):
                start, end = float(turn.start), float(turn.end)
                if end <= start:
                    continue
                raw_turns.append((start, end, str(raw_label)))
            for start, end, raw in sorted(raw_turns, key=lambda item: (item[0], item[1])):
                label = labels.setdefault(raw, f"說話者 {len(labels) + 1}")
                turns.append(SpeakerTurn(start=start, end=end, speaker=label))
        except OperationCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            raise DiarizationUnavailable(
                f"說話者區分失敗（{_short(e)}）；請確認 GPU 主機已接受 {self._model_name} 的模型條款"
            ) from e
        if not turns:
            raise DiarizationUnavailable("說話者區分沒有找到可用的人聲區段")
        turns.sort(key=lambda item: (item.start, item.end))
        progress(1.0, f"說話者區分完成（{len(labels)} 人）")
        return tuple(turns)

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        if self._failure is not None:
            raise DiarizationUnavailable(self._failure)
        if not self._token:
            self._failure = f"沒有設定 {TOKEN_ENV}；多人模式需要 Hugging Face token"
            raise DiarizationUnavailable(self._failure)
        try:
            torch = self._torch
            factory = self._pipeline_factory
            if factory is None:
                from pyannote.audio import Pipeline
                import torch as torch_module
                factory = Pipeline.from_pretrained
                torch = torch or torch_module
            pipeline = factory(self._model_name, token=self._token)
            if self._device_name != "cpu":
                if torch is None:
                    import torch as torch_module
                    torch = torch_module
                device_name = self._device_name
                if device_name == "auto":
                    device_name = "cuda" if torch.cuda.is_available() else "cpu"
                pipeline.to(torch.device(device_name))
            self._pipeline = pipeline
            return pipeline
        except Exception as e:  # noqa: BLE001
            self._failure = (
                f"GPU 沒有可用的說話者區分模型：{_short(e)}。"
                f"請安裝 pyannote.audio、設定 {TOKEN_ENV}，並接受 {self._model_name} 的條款"
            )
            raise DiarizationUnavailable(self._failure) from e
