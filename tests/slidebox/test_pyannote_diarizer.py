"""pyannote 說話者區分 adapter：全部用假的 pipeline、假的 torch、假的解碼器，
不安裝 pyannote、不下載模型、不碰 Hugging Face。"""
from __future__ import annotations

import math
import os
import re
import sys
import types
from types import SimpleNamespace

import pytest

from slidebox.domain.entities import DiarizerOutput, SpeakerTurn
from slidebox.domain.errors import DiarizationUnavailable, OperationCancelled
from slidebox.infrastructure import pyannote_diarizer as mod
from slidebox.infrastructure.pyannote_diarizer import PyannoteSpeakerDiarizer

TOKEN = "hf_FAKEtokenFAKEtokenFAKEtoken1234567890"
WAVE = {"waveform": "WAVEFORM", "sample_rate": 16000}


class Annotation:
    def __init__(self, *tracks):
        self._tracks = tracks

    def itertracks(self, yield_label=False):
        assert yield_label is True
        for start, end, label in self._tracks:
            yield SimpleNamespace(start=start, end=end), None, label


class Pipeline:
    """比照 pyannote 4.x：回傳有 exclusive_speaker_diarization 與 speaker_diarization 的結果。"""

    def __init__(self, exclusive=None, full=None, steps=(), on_step=None, error=None):
        self.exclusive = exclusive if exclusive is not None else Annotation(
            (3.0, 5.0, "SPEAKER_01"), (0.0, 2.0, "SPEAKER_00"))
        self.full = full if full is not None else Annotation(
            (0.0, 2.0, "SPEAKER_00"), (1.5, 5.0, "SPEAKER_01"))
        self.steps = steps
        self.on_step = on_step
        self.error = error
        self.calls: list[tuple[object, dict]] = []
        self.moved: list[object] = []
        self.steps_done = 0

    def __call__(self, audio, hook=None, **kwargs):
        self.calls.append((audio, {"hook": hook, **kwargs}))
        for name, total, completed in self.steps:
            if hook is not None:
                hook(name, None, file=audio, total=total, completed=completed)
            self.steps_done += 1
            if self.on_step is not None:
                self.on_step(name)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(exclusive_speaker_diarization=self.exclusive, speaker_diarization=self.full)

    def to(self, device):
        self.moved.append(device)
        return self


class FakeTorch:
    def __init__(self, cuda=True):
        self.cuda = SimpleNamespace(is_available=lambda: cuda)

    @staticmethod
    def device(name):
        return f"device:{name}"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make(pipeline=None, *, token=TOKEN, device="cpu", mode="pyannote", factory=None, torch=None,
         installed=True, loader=None, clock=None, version="4.0.7", model="test-model"):
    pipeline = pipeline or Pipeline()
    built = []

    def default_factory(name, token):
        built.append((name, token))
        return pipeline

    diarizer = PyannoteSpeakerDiarizer(
        model_name=model, token=token, device=device, mode=mode,
        pipeline_factory=factory or default_factory,
        torch_module=torch or FakeTorch(),
        audio_loader=loader or (lambda path, cancelled: dict(WAVE)),
        spec_finder=lambda name: object() if installed else None,
        version_finder=lambda: version,
        clock=clock or Clock())
    diarizer.built = built
    diarizer.pipeline = pipeline
    return diarizer


def run(diarizer, speakers=None, cancelled=lambda: False, progress=None):
    seen = progress if progress is not None else []
    return diarizer.diarize("audio.webm", 10.0, speakers, lambda f, s: seen.append((f, s)), cancelled)


# --- available()：便宜、不載入模型 ---


def test_available_when_the_token_and_the_package_are_there_and_nothing_is_loaded():
    """攔的 bug：available() 載入整個 pipeline。/capabilities 會被 Pi 一直問，
    每問一次就載一次模型（幾百 MB、好幾秒），還可能下載。"""
    diarizer = make()
    assert diarizer.available() == (True, "")
    assert diarizer.built == [] and diarizer.pipeline.moved == []


def test_a_missing_token_is_reported_and_names_the_variable():
    diarizer = make(token="")
    available, reason = diarizer.available()
    assert not available and "HUGGINGFACE_TOKEN" in reason
    assert diarizer.built == []


def test_a_blank_token_counts_as_missing():
    assert make(token="   ").available()[0] is False


def test_a_missing_package_is_reported_with_the_build_hint():
    available, reason = make(installed=False).available()
    assert not available and "pyannote.audio" in reason and "diarization" in reason


def test_a_package_lookup_that_raises_means_not_installed():
    def broken(name):
        raise ModuleNotFoundError("No module named 'pyannote'")

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", spec_finder=broken)
    assert diarizer.available()[0] is False


def test_the_kill_switch_turns_it_off_even_when_everything_else_is_fine():
    """SLIDEBOX_DIARIZER=off：維運一鍵關掉多人模式，Pi 那邊會拒絕，單人（--speakers 1）照跑。"""
    diarizer = make(mode="off")
    available, reason = diarizer.available()
    assert not available and "SLIDEBOX_DIARIZER=off" in reason
    with pytest.raises(DiarizationUnavailable, match="SLIDEBOX_DIARIZER=off"):
        run(diarizer)
    assert diarizer.built == []


@pytest.mark.parametrize("value", ["sherpa", "on", "OFF "])
def test_an_unknown_engine_setting_is_a_configuration_error_not_a_silent_default(value):
    diarizer = make(mode=value)
    available, reason = diarizer.available()
    if value.strip().lower() == "off":
        assert not available and "關閉" in reason          # 是「關掉了」，不是「寫錯了」
    else:
        assert not available and "SLIDEBOX_DIARIZER" in reason


def test_an_unknown_device_setting_is_reported():
    available, reason = make(device="gpu").available()
    assert not available and "SLIDEBOX_DIARIZATION_DEVICE" in reason


def test_the_answer_is_cached_for_a_few_seconds_and_refreshed_afterwards():
    clock = Clock()
    lookups = []

    def finder(name):
        lookups.append(name)
        return object()

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote",
                                       spec_finder=finder, clock=clock)
    diarizer.available()
    diarizer.available()
    assert lookups == ["pyannote.audio"]
    clock.now += 60
    diarizer.available()
    assert lookups == ["pyannote.audio", "pyannote.audio"]


def test_diarize_without_a_token_raises_the_same_readable_reason():
    diarizer = make(token="")
    with pytest.raises(DiarizationUnavailable, match="HUGGINGFACE_TOKEN"):
        run(diarizer)


# --- 身分：給存檔與結果帶著的指紋 ---


def test_engine_version_model_and_fingerprint():
    diarizer = make(version="4.0.7", model="pyannote/speaker-diarization-community-1")
    assert diarizer.engine == "pyannote.audio"
    assert diarizer.version == "4.0.7"
    assert diarizer.model == "pyannote/speaker-diarization-community-1"
    assert re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_fingerprint_changes_with_the_version_and_the_model_and_is_otherwise_stable():
    base = make(version="4.0.7", model="m1").fingerprint
    assert make(version="4.0.7", model="m1").fingerprint == base
    assert make(version="4.1.0", model="m1").fingerprint != base
    assert make(version="4.0.7", model="m2").fingerprint != base


def test_the_fingerprint_does_not_depend_on_the_token_or_the_device():
    base = make().fingerprint
    assert make(token="other-token", device="cuda").fingerprint == base


def test_the_fingerprint_exists_even_when_the_package_is_not_installed():
    diarizer = make(installed=False, version="")
    assert diarizer.version == "" and re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


# --- 載入 ---


def test_the_model_is_loaded_with_the_token_and_only_once():
    diarizer = make()
    run(diarizer)
    run(diarizer)
    assert diarizer.built == [("test-model", TOKEN)]


def test_a_failed_load_is_not_remembered_so_the_next_job_can_try_again():
    """攔的 bug：載入失敗就永遠失敗，要重開服務。網路斷一下、下載到一半，不該把整個服務
    判成永久沒有多人模式。"""
    calls = []
    pipeline = Pipeline()

    def flaky(name, token):
        calls.append(name)
        if len(calls) == 1:
            raise OSError("connection reset")
        return pipeline

    diarizer = make(factory=flaky)
    with pytest.raises(DiarizationUnavailable):
        run(diarizer)
    assert run(diarizer).turns and len(calls) == 2


def test_available_does_not_report_a_past_load_failure():
    """攔的 bug：available() 回報上一次的載入失敗。Pi 因此拒絕送出任何多人工作，
    就永遠沒有下一次載入可以成功——死結。"""

    def broken(name, token):
        raise OSError("connection reset")

    diarizer = make(factory=broken)
    with pytest.raises(DiarizationUnavailable):
        run(diarizer)
    assert diarizer.available() == (True, "")


def test_a_pipeline_that_comes_back_empty_means_the_terms_were_not_accepted():
    diarizer = make(factory=lambda name, token: None)
    with pytest.raises(DiarizationUnavailable) as error:
        run(diarizer)
    # 要是在「載入」這一關就說清楚，而不是拿 None 去呼叫、事後才變成一句含糊的推論失敗
    assert "無法載入" in str(error.value) and "使用條款" in str(error.value) and "test-model" in str(error.value)


def test_telemetry_is_switched_off_before_pyannote_is_imported(monkeypatch):
    """攔的 bug：先 import 才設 PYANNOTE_METRICS_ENABLED，pyannote 在 import 時就已經讀過它了。"""
    seen = []

    class FakeAudioModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "Pipeline":
                seen.append(os.environ.get("PYANNOTE_METRICS_ENABLED"))
                return SimpleNamespace(from_pretrained=lambda model, token: Pipeline())
            raise AttributeError(name)

    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "placeholder")
    monkeypatch.delenv("PYANNOTE_METRICS_ENABLED")
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", FakeAudioModule("pyannote.audio"))
    diarizer = PyannoteSpeakerDiarizer(
        model_name="m", token=TOKEN, device="cpu", mode="pyannote", torch_module=FakeTorch(),
        audio_loader=lambda path, cancelled: dict(WAVE), spec_finder=lambda n: object())
    run(diarizer)
    assert seen == ["0"]


def test_an_explicit_telemetry_choice_is_respected(monkeypatch):
    seen = []

    class FakeAudioModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "Pipeline":
                seen.append(os.environ.get("PYANNOTE_METRICS_ENABLED"))
                return SimpleNamespace(from_pretrained=lambda model, token: Pipeline())
            raise AttributeError(name)

    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", FakeAudioModule("pyannote.audio"))
    diarizer = PyannoteSpeakerDiarizer(
        model_name="m", token=TOKEN, device="cpu", mode="pyannote", torch_module=FakeTorch(),
        audio_loader=lambda path, cancelled: dict(WAVE), spec_finder=lambda n: object())
    run(diarizer)
    assert seen == ["1"]


# --- 裝置 ---


def test_cpu_never_moves_the_pipeline():
    """SLIDEBOX_DIARIZATION_DEVICE=cpu 是逃生口：鎖檔裡的 torch 是 CUDA 13，驅動對不上時，
    連 torch.cuda 都不能碰。"""
    torch = FakeTorch()
    torch.cuda = SimpleNamespace(is_available=lambda: pytest.fail("cpu 不該去問 CUDA"))
    diarizer = make(device="cpu", torch=torch)
    run(diarizer)
    assert diarizer.pipeline.moved == [] and diarizer.device == "cpu"


def test_cuda_moves_the_pipeline_to_the_graphics_card():
    diarizer = make(device="cuda")
    run(diarizer)
    assert diarizer.pipeline.moved == ["device:cuda"] and diarizer.device == "cuda"


def test_auto_uses_the_graphics_card_when_there_is_one():
    diarizer = make(device="auto", torch=FakeTorch(cuda=True))
    run(diarizer)
    assert diarizer.pipeline.moved == ["device:cuda"]


def test_auto_stays_on_the_cpu_without_a_graphics_card():
    diarizer = make(device="auto", torch=FakeTorch(cuda=False))
    run(diarizer)
    assert diarizer.pipeline.moved == [] and diarizer.device == "cpu"


def test_auto_falls_back_to_the_cpu_when_the_graphics_card_cannot_be_used():
    """驅動和 torch 的 CUDA 版本對不上時，auto 不該讓整個多人模式壞掉。"""

    class Picky(Pipeline):
        def to(self, device):
            self.moved.append(device)
            if device == "device:cuda":
                raise RuntimeError("CUDA driver version is insufficient for CUDA runtime version")
            return self

    pipeline = Picky()
    seen = []
    diarizer = make(pipeline, device="auto", torch=FakeTorch(cuda=True))
    run(diarizer, progress=seen)
    assert diarizer.device == "cpu" and pipeline.moved == ["device:cuda", "device:cpu"]
    assert any("CPU" in status for _, status in seen)


def test_an_explicit_cuda_that_fails_names_the_escape_hatch():
    class Picky(Pipeline):
        def to(self, device):
            self.moved.append(device)
            if device == "device:cuda":
                raise RuntimeError("CUDA driver version is insufficient")
            return self

    pipeline = Picky()
    diarizer = make(pipeline, device="cuda")
    with pytest.raises(DiarizationUnavailable) as error:
        run(diarizer)
    assert "SLIDEBOX_DIARIZATION_DEVICE=cpu" in str(error.value)
    # 明確要求顯示卡就不偷偷改用 CPU：操作者以為在用顯示卡，一支兩小時的影片卻跑了十倍的時間
    assert pipeline.moved == ["device:cuda"]


# --- 結果 ---


def test_the_exclusive_turns_are_returned_with_the_raw_labels_and_the_overlap_comes_from_the_full_annotation():
    diarizer = make()
    output = run(diarizer)
    assert isinstance(output, DiarizerOutput)
    assert output.turns == (SpeakerTurn(0.0, 2.0, "SPEAKER_00"), SpeakerTurn(3.0, 5.0, "SPEAKER_01"))
    # 完整的標註裡 SPEAKER_00 到 2.0、SPEAKER_01 從 1.5 開始：同時有兩人的有 0.5 秒
    assert output.overlap_seconds == pytest.approx(0.5)


def test_overlap_counts_distinct_speakers_not_overlapping_pieces_of_the_same_speaker():
    full = Annotation((0.0, 3.0, "A"), (2.0, 4.0, "A"), (10.0, 12.0, "A"), (11.0, 13.0, "B"))
    output = run(make(Pipeline(full=full)))
    assert output.overlap_seconds == pytest.approx(1.0)


def test_turns_that_only_touch_are_not_an_overlap():
    """A 說到 4.0、B 從 4.0 開始：接得很緊，但沒有同時出聲；同一位的連續兩段也一樣。"""
    full = Annotation((0.0, 4.0, "A"), (4.0, 8.0, "B"), (8.0, 9.0, "B"), (9.0, 10.0, "A"))
    assert run(make(Pipeline(full=full))).overlap_seconds == 0.0


def test_without_the_exclusive_annotation_the_full_one_is_used_for_both():
    pipeline = Pipeline()
    result = SimpleNamespace(speaker_diarization=pipeline.full)
    pipeline.__class__ = type("P", (Pipeline,), {"__call__": lambda self, audio, hook=None, **kw: result})
    output = run(make(pipeline))
    assert [t.speaker for t in output.turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert output.overlap_seconds == pytest.approx(0.5)


def test_a_bare_annotation_from_an_older_pipeline_works_too():
    pipeline = Pipeline()
    annotation = Annotation((0.0, 2.0, "A"), (3.0, 5.0, "B"))
    pipeline.__class__ = type("P", (Pipeline,), {"__call__": lambda self, audio, hook=None, **kw: annotation})
    output = run(make(pipeline))
    assert [(t.start, t.end, t.speaker) for t in output.turns] == [(0.0, 2.0, "A"), (3.0, 5.0, "B")]


def test_empty_and_inverted_tracks_are_ignored():
    exclusive = Annotation((0.0, 0.0, "A"), (4.0, 3.0, "A"), (5.0, 6.0, "B"))
    output = run(make(Pipeline(exclusive=exclusive, full=exclusive)))
    assert output.turns == (SpeakerTurn(5.0, 6.0, "B"),)


def test_no_voice_at_all_is_an_error_not_an_empty_result():
    empty = Annotation()
    with pytest.raises(DiarizationUnavailable, match="人聲"):
        run(make(Pipeline(exclusive=empty, full=empty)))


def test_a_fixed_speaker_count_is_passed_on_and_none_means_automatic():
    diarizer = make()
    run(diarizer, speakers=3)
    run(diarizer, speakers=None)
    first, second = diarizer.pipeline.calls
    assert first[1]["num_speakers"] == 3
    assert "num_speakers" not in second[1]


def test_the_pipeline_gets_the_decoded_waveform_not_a_path():
    """解碼自己做（ffmpeg 管線）：pyannote 內建的解碼依賴 torchcodec 與系統 ffmpeg 的版本。"""
    loaded = []
    diarizer = make(loader=lambda path, cancelled: loaded.append(path) or dict(WAVE))
    run(diarizer)
    assert loaded == ["audio.webm"]
    assert diarizer.pipeline.calls[0][0] == WAVE


def test_progress_reports_the_steps_and_ends_with_the_number_of_voices():
    pipeline = Pipeline(steps=[("segmentation", 10, 5), ("embeddings", None, None), ("segmentation", 10, 10)])
    seen = []
    run(make(pipeline), progress=seen)
    statuses = [s for _, s in seen]
    assert any("5/10" in s for s in statuses)
    assert "完成" in statuses[-1] and "2" in statuses[-1] and seen[-1][0] == 1.0


# --- 取消 ---


def test_cancel_before_anything_is_loaded_loads_nothing():
    diarizer = make()
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=lambda: True)
    assert diarizer.built == []


def test_cancel_in_the_middle_aborts_the_pipeline_through_its_hook():
    """攔的 bug：只在呼叫前後檢查取消。pipeline 一跑就是幾分鐘（長影片更久），取消要等到它自己跑完。"""
    state = {"cancel": False}
    pipeline = Pipeline(steps=[("segmentation", 10, 1), ("segmentation", 10, 2), ("segmentation", 10, 3)],
                        on_step=lambda name: state.update(cancel=True))
    with pytest.raises(OperationCancelled):
        run(make(pipeline), cancelled=lambda: state["cancel"])
    assert pipeline.steps_done == 1


def test_cancel_after_the_pipeline_returned_still_wins():
    state = {"cancel": False}
    pipeline = Pipeline(steps=[], on_step=None)
    original = Pipeline.__call__

    def finish_then_cancel(self, audio, hook=None, **kw):
        result = original(self, audio, hook=hook, **kw)
        state["cancel"] = True
        return result

    pipeline.__class__ = type("P", (Pipeline,), {"__call__": finish_then_cancel})
    with pytest.raises(OperationCancelled):
        run(make(pipeline), cancelled=lambda: state["cancel"])


def test_a_cancel_that_surfaces_as_another_exception_is_still_a_cancel():
    """pipeline 內部把 hook 丟出的例外包成別的例外時，使用者按的是取消，不是失敗。"""
    state = {"cancel": False}

    def boom(name):
        state["cancel"] = True
        raise RuntimeError("wrapped")

    pipeline = Pipeline(steps=[("segmentation", 1, 1)], on_step=boom)
    with pytest.raises(OperationCancelled):
        run(make(pipeline), cancelled=lambda: state["cancel"])


def test_cancel_and_failure_of_the_audio_decoder_propagate():
    def cancel_loader(path, cancelled):
        raise OperationCancelled()

    def failing_loader(path, cancelled):
        raise DiarizationUnavailable("音訊解碼失敗：moov atom not found")

    with pytest.raises(OperationCancelled):
        run(make(loader=cancel_loader))
    with pytest.raises(DiarizationUnavailable, match="moov atom"):
        run(make(loader=failing_loader))


# --- 失敗：可讀、不含 token ---


def test_an_inference_failure_is_readable_and_points_at_the_escape_hatch():
    pipeline = Pipeline(error=RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"))
    with pytest.raises(DiarizationUnavailable) as error:
        run(make(pipeline))
    text = str(error.value)
    assert "CUDA out of memory" in text and "SLIDEBOX_DIARIZATION_DEVICE=cpu" in text


def test_the_token_never_appears_in_any_error_message():
    """攔的 bug：Hugging Face 或 pyannote 的錯誤訊息帶著 token（網址、標頭），一路進了工作的 error
    欄位，再被 Pi 存進資料庫、印在畫面上。這個專案的 repo 是公開的，任務紀錄也會被分享。"""

    def leaking_factory(name, token):
        raise RuntimeError(f"401 for https://huggingface.co/{name} with Authorization: Bearer {token}")

    with pytest.raises(DiarizationUnavailable) as load_error:
        run(make(factory=leaking_factory))
    assert TOKEN not in str(load_error.value) and "hf_FAKE" not in str(load_error.value)

    leaking_pipeline = Pipeline(error=RuntimeError(f"failed (token={TOKEN})"))
    with pytest.raises(DiarizationUnavailable) as run_error:
        run(make(leaking_pipeline))
    assert TOKEN not in str(run_error.value) and "hf_FAKE" not in str(run_error.value)

    class Picky(Pipeline):
        def to(self, device):
            raise RuntimeError(f"cuda failed {TOKEN}")

    with pytest.raises(DiarizationUnavailable) as move_error:
        run(make(Picky(), device="cuda"))
    assert TOKEN not in str(move_error.value)


def test_a_configured_token_that_does_not_look_like_hf_is_still_scrubbed():
    """第一層：設定的那把 token 原字串取代。token 不一定是 hf_ 開頭（代理、測試用的假值），
    只靠樣式比對會漏。"""
    odd = "plain-secret-value-0001"

    def leaking_factory(name, token):
        raise RuntimeError(f"denied for {token}")

    with pytest.raises(DiarizationUnavailable) as error:
        run(make(token=odd, factory=leaking_factory))
    assert odd not in str(error.value)


def test_a_different_hf_token_in_an_error_is_scrubbed_too():
    """第二層：錯誤訊息裡出現的不是我們設定的那把（環境裡殘留的舊 token、代理回顯別人的）。"""
    stray = "hf_STRAYtokenSTRAYtoken9876543210"

    def leaking_factory(name, token):
        raise RuntimeError(f"cached credential {stray} was rejected")

    with pytest.raises(DiarizationUnavailable) as error:
        run(make(factory=leaking_factory))
    assert stray not in str(error.value) and "hf_STRAY" not in str(error.value)


def test_the_token_is_not_in_the_reason_or_the_progress_text():
    diarizer = make()
    seen = []
    run(diarizer, progress=seen)
    assert TOKEN not in diarizer.available()[1]
    assert all(TOKEN not in status for _, status in seen)
    assert TOKEN not in repr(diarizer)


# --- 預設的解碼器（ffmpeg 管線）---


class FakeTensor:
    def __init__(self, data, shape=None):
        self.data = data
        self.shape = shape if shape is not None else (len(data),)

    def unsqueeze(self, axis):
        assert axis == 0
        return FakeTensor(self.data, (1, *self.shape))


class TorchWithNumpy:
    """只提供解碼器會用到的 from_numpy。"""

    @staticmethod
    def from_numpy(array):
        return FakeTensor(array)


class FakeProcess:
    def __init__(self, data: bytes, returncode: int = 0, stderr_text: str = "", err_file=None):
        import io

        self.stdout = io.BytesIO(data)
        self.returncode_value = returncode
        self.returncode = None
        self.killed = False
        self._stderr_text = stderr_text
        self._err_file = err_file

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self):
        self.returncode = -9 if self.killed else self.returncode_value
        if self._err_file is not None and self._stderr_text:
            self._err_file.write(self._stderr_text.encode("utf-8"))
        return self.returncode


def popen_returning(data: bytes, returncode: int = 0, stderr_text: str = ""):
    made = []

    def popen(command, stdout=None, stderr=None, **kw):
        process = FakeProcess(data, returncode, stderr_text, err_file=stderr)
        process.command = command
        made.append(process)
        return process

    popen.made = made
    return popen


def test_the_default_decoder_asks_ffmpeg_for_16k_mono_pcm_and_scales_to_floats():
    import numpy as np

    pcm = np.array([0, 16384, -16384, 32767], dtype="<i2").tobytes()
    popen = popen_returning(pcm)
    wave = mod.decode_waveform("in.webm", lambda: False, TorchWithNumpy, popen=popen, ffmpeg_exe="ffmpeg")
    command = popen.made[0].command
    assert command[0] == "ffmpeg" and "in.webm" in command
    assert command[command.index("-ac") + 1] == "1" and command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-f") + 1] == "s16le" and command[-1] == "-"
    assert wave["sample_rate"] == 16000
    assert list(wave["waveform"].data) == pytest.approx([0.0, 0.5, -0.5, 32767 / 32768])
    assert wave["waveform"].shape == (1, 4)


def test_a_failing_ffmpeg_is_a_readable_error():
    popen = popen_returning(b"", returncode=1, stderr_text="in.webm: Invalid data found when processing input")
    with pytest.raises(DiarizationUnavailable, match="Invalid data"):
        mod.decode_waveform("in.webm", lambda: False, TorchWithNumpy, popen=popen, ffmpeg_exe="ffmpeg")


def test_silence_that_decodes_to_nothing_is_an_error():
    popen = popen_returning(b"")
    with pytest.raises(DiarizationUnavailable, match="空"):
        mod.decode_waveform("in.webm", lambda: False, TorchWithNumpy, popen=popen, ffmpeg_exe="ffmpeg")


def test_cancelling_the_decode_kills_ffmpeg():
    import numpy as np

    popen = popen_returning(np.zeros(4 * mod.DECODE_CHUNK_BYTES // 2, dtype="<i2").tobytes())
    with pytest.raises(OperationCancelled):
        mod.decode_waveform("in.webm", lambda: True, TorchWithNumpy, popen=popen, ffmpeg_exe="ffmpeg")
    assert popen.made[0].killed


def test_a_missing_ffmpeg_is_a_readable_error():
    def popen(command, **kw):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(DiarizationUnavailable, match="ffmpeg"):
        mod.decode_waveform("in.webm", lambda: False, TorchWithNumpy, popen=popen, ffmpeg_exe="ffmpeg")


def test_real_ffmpeg_decodes_a_generated_tone(tmp_path):
    """真的跑一次 ffmpeg（合成一秒的 440 Hz 音，不碰網路、不用模型）：指令列要真的能用。"""
    import subprocess

    try:
        from slidebox.infrastructure.ffmpeg import get_ffmpeg_exe
        exe = get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg 不可用")
    source = str(tmp_path / "tone.m4a")
    subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1:sample_rate=44100", "-ac", "2", source], check=True)
    wave = mod.decode_waveform(source, lambda: False, TorchWithNumpy, ffmpeg_exe=exe)
    samples = wave["waveform"].data
    assert 15000 < len(samples) < 17500                      # 1 秒、16 kHz
    assert 0.05 < float(abs(samples).max()) <= 1.0           # 有聲音（lavfi 的 sine 預設只有 1/8 振幅）、沒有爆音
    assert float(abs(samples[4000:12000]).mean()) > 0.02     # 中段是持續的音，不是只有一個尖峰


# --- 補強：對照 mutation 的結果補上的防線（每一個都對應一個曾經沒人守的改動） ---


def _clean_settings_env(monkeypatch):
    for name in (mod.TOKEN_ENV, mod.MODE_ENV, mod.DEVICE_ENV):
        monkeypatch.delenv(name, raising=False)


def _from_environment():
    return PyannoteSpeakerDiarizer(spec_finder=lambda name: object(), clock=Clock())


def test_the_token_comes_from_the_environment_and_surrounding_blanks_are_ignored(monkeypatch):
    """攔的 bug：token 填在環境變數（.env.gpu → compose）卻沒讀進來，/capabilities 永遠說沒設定。"""
    _clean_settings_env(monkeypatch)
    assert _from_environment().available()[0] is False
    monkeypatch.setenv(mod.TOKEN_ENV, "  " + TOKEN + "  ")
    assert _from_environment().available() == (True, "")


def test_blank_settings_mean_the_defaults(monkeypatch):
    """攔的 bug：留空的設定（例如 `SLIDEBOX_DIARIZER=`）被當成「寫錯的設定值」，多人模式永遠不能用。"""
    _clean_settings_env(monkeypatch)
    monkeypatch.setenv(mod.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mod.MODE_ENV, "")
    monkeypatch.setenv(mod.DEVICE_ENV, "  ")
    assert _from_environment().available() == (True, "")


def test_settings_are_case_insensitive(monkeypatch):
    """攔的 bug：SLIDEBOX_DIARIZER=OFF 沒當成關掉、SLIDEBOX_DIARIZATION_DEVICE=CPU 被拒絕。"""
    _clean_settings_env(monkeypatch)
    monkeypatch.setenv(mod.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mod.DEVICE_ENV, "CPU")
    assert _from_environment().available() == (True, "")
    monkeypatch.setenv(mod.MODE_ENV, "OFF")
    available, reason = _from_environment().available()
    assert not available and "關閉" in reason


def test_a_package_lookup_that_raises_a_value_error_also_means_not_installed():
    """find_spec 在 __spec__ 是 None 的時候丟的是 ValueError，不是 ImportError。"""

    def broken(name):
        raise ValueError("pyannote.audio.__spec__ is None")

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", spec_finder=broken)
    available, reason = diarizer.available()
    assert not available and "pyannote.audio" in reason


def test_the_fingerprint_formula_is_pinned():
    """指紋是存檔的鍵（引擎、版本、模型）：公式一改，所有存檔都對不上——要改就是有意的。"""
    assert make(version="4.0.7", model="m1").fingerprint == "bb473b952417aabb"


def test_a_version_lookup_that_raises_means_no_version():
    def broken():
        raise RuntimeError("metadata is corrupt")

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, version_finder=broken, spec_finder=lambda name: object())
    assert diarizer.version == "" and re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_installed_version_is_read_from_the_package_metadata(monkeypatch):
    import importlib.metadata as metadata

    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
    assert mod._installed_version() == "9.9.9"

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    assert mod._installed_version() == ""


def test_a_cancel_that_arrives_while_the_model_loads_stops_before_decoding():
    state = {"cancel": False}
    decoded = []
    pipeline = Pipeline()

    def factory(name, token):
        state["cancel"] = True                      # 載入模型的時候使用者按了取消
        return pipeline

    diarizer = make(pipeline, factory=factory, loader=lambda path, cancelled: decoded.append(path) or dict(WAVE))
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=lambda: state["cancel"])
    assert decoded == [] and pipeline.calls == []


def test_a_cancel_that_arrives_while_decoding_stops_before_the_pipeline_runs():
    state = {"cancel": False}

    def loader(path, cancelled):
        state["cancel"] = True                      # 解碼音訊的時候使用者按了取消
        return dict(WAVE)

    diarizer = make(loader=loader)
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=lambda: state["cancel"])
    assert diarizer.pipeline.calls == []


def test_a_cancel_raised_inside_the_pipeline_is_not_turned_into_a_failure():
    with pytest.raises(OperationCancelled):
        run(make(Pipeline(error=OperationCancelled())))


def test_the_overlap_is_rounded_to_milliseconds():
    full = Annotation((0.0, 2.0, "A"), (1.0001234, 3.0, "B"))
    assert run(make(Pipeline(full=full))).overlap_seconds == 1.0


def test_progress_counts_voices_not_turns():
    exclusive = Annotation((0.0, 1.0, "A"), (1.0, 2.0, "B"), (2.0, 3.0, "A"), (3.0, 4.0, "A"))
    seen = []
    run(make(Pipeline(exclusive=exclusive, full=exclusive)), progress=seen)
    assert "2 個聲音" in seen[-1][1]


def test_our_own_terms_not_accepted_message_is_not_wrapped_as_a_load_crash():
    """攔的 bug：我們自己丟的「請先接受條款」被當成意外的載入失敗再包一層，訊息變成含糊的推論。"""
    with pytest.raises(DiarizationUnavailable) as error:
        run(make(factory=lambda name, token: None))
    assert str(error.value).startswith("無法載入")


def test_the_device_the_model_ended_up_on_is_announced():
    for device in ("cuda", "cpu"):
        seen = []
        run(make(device=device), progress=seen)
        assert any(f"已載入（{device}）" in status for _, status in seen)


def test_an_explicit_cuda_without_a_graphics_card_fails_instead_of_quietly_using_the_cpu():
    class NoCuda(Pipeline):
        def to(self, device):
            self.moved.append(device)
            if device == "device:cuda":
                raise RuntimeError("Torch not compiled with CUDA enabled")
            return self

    pipeline = NoCuda()
    diarizer = make(pipeline, device="cuda", torch=FakeTorch(cuda=False))
    with pytest.raises(DiarizationUnavailable, match="SLIDEBOX_DIARIZATION_DEVICE=cpu"):
        run(diarizer)
    assert pipeline.moved == ["device:cuda"]


def test_the_fallback_to_the_cpu_never_puts_the_token_in_the_progress_text():
    class Leaky(Pipeline):
        def to(self, device):
            self.moved.append(device)
            if device == "device:cuda":
                raise RuntimeError(f"cannot move: token {TOKEN} was rejected")
            return self

    seen = []
    run(make(Leaky(), device="auto", torch=FakeTorch(cuda=True)), progress=seen)
    text = "\n".join(status for _, status in seen)
    assert "CPU" in text and TOKEN not in text and "***" in text


def test_error_text_is_one_short_line():
    """攔的 bug：底層錯誤動輒數百字、含換行，原樣塞進狀態列與工作失敗訊息。"""
    pipeline = Pipeline(error=RuntimeError("first line\n" + "x" * 500))
    with pytest.raises(DiarizationUnavailable) as error:
        run(make(pipeline))
    text = str(error.value)
    assert "\n" not in text and "first line" in text and "x" * 300 not in text and "…" in text


def test_an_exclusive_annotation_without_the_full_one_serves_for_the_overlap_too():
    pipeline = Pipeline()
    result = SimpleNamespace(exclusive_speaker_diarization=pipeline.exclusive)
    pipeline.__class__ = type("P", (Pipeline,), {"__call__": lambda self, audio, hook=None, **kw: result})
    output = run(make(pipeline))
    assert [t.speaker for t in output.turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert output.overlap_seconds == 0.0


def test_infinite_tracks_are_ignored_and_labels_are_always_text():
    exclusive = Annotation((0.0, math.inf, "A"), (math.nan, 3.0, "A"), (5.0, 6.0, 0))
    output = run(make(Pipeline(exclusive=exclusive, full=exclusive)))
    assert output.turns == (SpeakerTurn(5.0, 6.0, "0"),)


def test_an_odd_trailing_byte_of_pcm_is_dropped():
    import numpy as np

    pcm = np.array([0, 16384], dtype="<i2").tobytes() + b"\x01"
    wave = mod.decode_waveform("in.webm", lambda: False, TorchWithNumpy,
                               popen=popen_returning(pcm), ffmpeg_exe="ffmpeg")
    assert wave["waveform"].shape == (1, 2)
