from types import SimpleNamespace

import pytest

from slidebox.domain.errors import DiarizationUnavailable
from slidebox.infrastructure.pyannote_diarizer import PyannoteSpeakerDiarizer


class _Annotation:
    def itertracks(self, yield_label=False):
        assert yield_label is True
        yield SimpleNamespace(start=3.0, end=5.0), None, "SPEAKER_B"
        yield SimpleNamespace(start=0.0, end=2.0), None, "SPEAKER_A"


class _Pipeline:
    def __init__(self):
        self.calls = []

    def __call__(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return SimpleNamespace(speaker_diarization=_Annotation())


def test_pyannote_labels_are_anonymous_and_stable_in_chronological_order():
    pipeline = _Pipeline()

    def factory(model, token):
        assert model == "test-model"
        assert token == "hf-test"
        return pipeline

    diarizer = PyannoteSpeakerDiarizer(
        model_name="test-model", token="hf-test", device="cpu", pipeline_factory=factory
    )
    progress = []

    assert diarizer.available() == (True, "")
    turns = diarizer.diarize("audio.wav", 10.0, 2, lambda f, s: progress.append(s), lambda: False)

    assert [(turn.start, turn.end, turn.speaker) for turn in turns] == [
        (0.0, 2.0, "說話者 1"), (3.0, 5.0, "說話者 2")
    ]
    assert pipeline.calls == [("audio.wav", {"num_speakers": 2})]
    assert "完成" in progress[-1]


def test_missing_hugging_face_token_makes_capability_unavailable():
    diarizer = PyannoteSpeakerDiarizer(token="")

    available, reason = diarizer.available()

    assert not available
    assert "HUGGINGFACE_TOKEN" in reason
    with pytest.raises(DiarizationUnavailable, match="HUGGINGFACE_TOKEN"):
        diarizer.diarize("audio.wav", 10.0, None, lambda *_: None, lambda: False)
