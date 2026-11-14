"""說話者區分的工作行程（pyannote_worker.py）。

pyannote 與 PyTorch 只在這個行程裡跑（理由見模組說明：主環境的 cuDNN 12 不能和 torch 的 cuDNN 13 放在一起）。
引擎用假的 pipeline、假的 torch、假的解碼器，不安裝 pyannote、不下載模型、不碰 Hugging Face；
入口與「這個檔案是獨立的」這件事，則用真的 Python 行程跑一遍。
"""
from __future__ import annotations

import ast
import io
import json
import math
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from slidebox.infrastructure import pyannote_worker as worker
from slidebox.infrastructure.pyannote_worker import PyannoteEngine, WorkerFailure

TOKEN = "hf_FAKEtokenFAKEtokenFAKEtoken1234567890"
WAVE = {"waveform": "WAVEFORM", "sample_rate": 16000}
WORKER_FILE = Path(worker.__file__)


class Annotation:
    def __init__(self, *tracks):
        self._tracks = tracks

    def itertracks(self, yield_label=False):
        assert yield_label is True
        for start, end, label in self._tracks:
            yield SimpleNamespace(start=start, end=end), None, label


class Pipeline:
    """比照 pyannote 4.x：回傳有 exclusive_speaker_diarization 與 speaker_diarization 的結果。"""

    def __init__(self, exclusive=None, full=None, steps=(), error=None):
        self.exclusive = exclusive if exclusive is not None else Annotation(
            (3.0, 5.0, "SPEAKER_01"), (0.0, 2.0, "SPEAKER_00"))
        self.full = full if full is not None else Annotation(
            (0.0, 2.0, "SPEAKER_00"), (1.5, 5.0, "SPEAKER_01"))
        self.steps = steps
        self.error = error
        self.calls: list[tuple[object, dict]] = []
        self.moved: list[object] = []

    def __call__(self, audio, hook=None, **kwargs):
        self.calls.append((audio, {"hook": hook, **kwargs}))
        for name, total, completed in self.steps:
            if hook is not None:
                hook(name, None, file=audio, total=total, completed=completed)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(exclusive_speaker_diarization=self.exclusive, speaker_diarization=self.full)

    def to(self, device):
        self.moved.append(device)
        return self


class FakeTorch:
    def __init__(self, cuda=True, peak=None):
        def max_memory_allocated():
            if isinstance(peak, Exception):
                raise peak
            return peak

        self.cuda = SimpleNamespace(is_available=lambda: cuda, max_memory_allocated=max_memory_allocated)

    @staticmethod
    def device(name):
        return f"device:{name}"


def make(pipeline=None, *, token=TOKEN, device="cpu", factory=None, torch=None, loader=None, model="test-model"):
    pipeline = pipeline or Pipeline()
    built = []

    def default_factory(name, token):
        built.append((name, token))
        return pipeline

    engine = PyannoteEngine(model, token=token, device=device, ffmpeg_exe="ffmpeg",
                            pipeline_factory=factory or default_factory,
                            torch_module=torch or FakeTorch(),
                            audio_loader=loader or (lambda path: dict(WAVE)))
    engine.built = built
    engine.pipeline = pipeline
    return engine


def run(engine, speakers=None, progress=None):
    seen = progress if progress is not None else []
    return engine.diarize("audio.webm", speakers, lambda f, s: seen.append((f, s)))


def failed(call) -> WorkerFailure:
    with pytest.raises(WorkerFailure) as error:
        call()
    return error.value


# --- 載入 ---


def test_the_model_is_loaded_with_the_token_and_only_once():
    engine = make()
    run(engine)
    run(engine)
    assert engine.built == [("test-model", TOKEN)]


def test_a_failed_load_is_not_remembered_so_the_next_attempt_can_try_again():
    """攔的 bug：載入失敗就永遠失敗，要重開服務。網路斷一下、下載到一半，不該把引擎判成永久壞掉。"""
    calls = []
    pipeline = Pipeline()

    def flaky(name, token):
        calls.append(name)
        if len(calls) == 1:
            raise OSError("connection reset")
        return pipeline

    engine = make(factory=flaky)
    assert failed(lambda: run(engine)).stage == worker.STAGE_LOAD
    assert run(engine).turns and len(calls) == 2


def test_a_pipeline_that_comes_back_empty_means_the_terms_were_not_accepted():
    """pyannote 3.x 沒接受條款時不丟例外，只回 None：要在「載入」這一關就說清楚，
    而不是拿 None 去呼叫、事後才變成一句含糊的推論失敗。"""
    failure = failed(lambda: run(make(factory=lambda name, token: None)))
    assert failure.stage == worker.STAGE_TERMS


def test_the_terms_failure_is_not_remembered_either():
    """操作者去 Hugging Face 接受條款之後，下一次就該成功，不必重開服務。"""
    answers = [None, Pipeline()]
    engine = make(factory=lambda name, token: answers.pop(0))
    assert failed(lambda: run(engine)).stage == worker.STAGE_TERMS
    assert run(engine).turns


def test_a_load_that_raises_is_a_load_failure_with_the_reason():
    def refused(name, token):
        raise OSError("403 Client Error: gated repo")

    failure = failed(lambda: run(make(factory=refused)))
    assert failure.stage == worker.STAGE_LOAD and "403" in failure.detail and not failure.oom


def _fake_pyannote(monkeypatch, seen):
    class FakeAudioModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "Pipeline":
                seen.append(os.environ.get("PYANNOTE_METRICS_ENABLED"))
                return SimpleNamespace(from_pretrained=lambda model, token: Pipeline())
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", FakeAudioModule("pyannote.audio"))


def _default_factory_engine():
    return PyannoteEngine("m", token=TOKEN, device="cpu", torch_module=FakeTorch(),
                          audio_loader=lambda path: dict(WAVE))


def test_telemetry_is_switched_off_before_pyannote_is_imported(monkeypatch):
    """攔的 bug：先 import 才設 PYANNOTE_METRICS_ENABLED，pyannote 在 import 時就已經讀過它了。"""
    seen = []
    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "placeholder")
    monkeypatch.delenv("PYANNOTE_METRICS_ENABLED")           # 讓 monkeypatch 在測試結束時還原原本的狀態
    _fake_pyannote(monkeypatch, seen)
    run(_default_factory_engine())
    assert seen == ["0"]


def test_an_explicit_telemetry_choice_is_respected(monkeypatch):
    seen = []
    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "1")
    _fake_pyannote(monkeypatch, seen)
    run(_default_factory_engine())
    assert seen == ["1"]


# --- 裝置 ---


def test_cpu_never_moves_the_pipeline_and_never_asks_torch_about_cuda():
    """SLIDEBOX_DIARIZATION_DEVICE=cpu 是逃生口：鎖檔裡的 torch 是 CUDA 13，驅動對不上時，
    連 torch.cuda 都不能碰。"""
    torch = FakeTorch()
    torch.cuda = SimpleNamespace(is_available=lambda: pytest.fail("cpu 不該去問 CUDA"),
                                 max_memory_allocated=lambda: pytest.fail("cpu 不該去量顯示卡"))
    engine = make(device="cpu", torch=torch)
    result = run(engine)
    assert engine.pipeline.moved == [] and result.device == "cpu" and result.gpu_peak_bytes is None


def test_cpu_does_not_even_import_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)          # import torch 會失敗
    engine = PyannoteEngine("m", token=TOKEN, device="cpu", pipeline_factory=lambda name, token: Pipeline(),
                            audio_loader=lambda path: dict(WAVE))
    assert run(engine).device == "cpu"


def test_cuda_moves_the_pipeline_to_the_graphics_card():
    engine = make(device="cuda")
    result = run(engine)
    assert engine.pipeline.moved == ["device:cuda"] and result.device == "cuda"


def test_auto_uses_the_graphics_card_when_there_is_one():
    engine = make(device="auto", torch=FakeTorch(cuda=True))
    assert run(engine).device == "cuda" and engine.pipeline.moved == ["device:cuda"]


def test_auto_stays_on_the_cpu_without_a_graphics_card():
    engine = make(device="auto", torch=FakeTorch(cuda=False))
    assert run(engine).device == "cpu" and engine.pipeline.moved == []


class Picky(Pipeline):
    """搬到顯示卡就丟例外。"""

    def __init__(self, message="CUDA driver version is insufficient for CUDA runtime version", **kw):
        super().__init__(**kw)
        self.message = message

    def to(self, device):
        self.moved.append(device)
        if device == "device:cuda":
            raise RuntimeError(self.message)
        return self


def test_auto_falls_back_to_the_cpu_when_the_graphics_card_cannot_be_used():
    """驅動和 torch 的 CUDA 版本對不上時，auto 不該讓整個多人模式壞掉。"""
    pipeline = Picky()
    seen = []
    engine = make(pipeline, device="auto", torch=FakeTorch(cuda=True))
    result = run(engine, progress=seen)
    assert result.device == "cpu" and pipeline.moved == ["device:cuda", "device:cpu"]
    assert any("CPU" in status for _, status in seen)


def test_an_explicit_cuda_that_fails_is_reported_and_never_quietly_uses_the_cpu():
    """明確要求顯示卡就不偷偷改用 CPU：操作者以為在用顯示卡，一支兩小時的影片卻跑了十倍的時間。"""
    pipeline = Picky()
    failure = failed(lambda: run(make(pipeline, device="cuda")))
    assert failure.stage == worker.STAGE_LOAD and "CUDA driver version" in failure.detail
    assert pipeline.moved == ["device:cuda"]


def test_an_explicit_cuda_without_a_graphics_card_fails_instead_of_quietly_using_the_cpu():
    pipeline = Picky("Torch not compiled with CUDA enabled")
    failure = failed(lambda: run(make(pipeline, device="cuda", torch=FakeTorch(cuda=False))))
    assert failure.stage == worker.STAGE_LOAD and pipeline.moved == ["device:cuda"]


def test_the_fallback_to_the_cpu_never_puts_the_token_in_the_progress_text():
    seen = []
    run(make(Picky(f"cannot move: token {TOKEN} was rejected"), device="auto", torch=FakeTorch(cuda=True)),
        progress=seen)
    text = "\n".join(status for _, status in seen)
    assert "CPU" in text and TOKEN not in text and "***" in text


def test_the_device_the_model_ended_up_on_is_announced():
    for device in ("cuda", "cpu"):
        seen = []
        run(make(device=device), progress=seen)
        assert any(f"已載入（{device}）" in status for _, status in seen)


# --- 結果 ---


def test_the_exclusive_turns_are_returned_with_the_raw_labels_and_the_overlap_comes_from_the_full_annotation():
    result = run(make())
    assert result.turns == [(0.0, 2.0, "SPEAKER_00"), (3.0, 5.0, "SPEAKER_01")]
    # 完整的標註裡 SPEAKER_00 到 2.0、SPEAKER_01 從 1.5 開始：同時有兩人的有 0.5 秒
    assert result.overlap_seconds == pytest.approx(0.5)


def test_overlap_counts_distinct_speakers_not_overlapping_pieces_of_the_same_speaker():
    full = Annotation((0.0, 3.0, "A"), (2.0, 4.0, "A"), (10.0, 12.0, "A"), (11.0, 13.0, "B"))
    assert run(make(Pipeline(full=full))).overlap_seconds == pytest.approx(1.0)


def test_turns_that_only_touch_are_not_an_overlap():
    """A 說到 4.0、B 從 4.0 開始：接得很緊，但沒有同時出聲；同一位的連續兩段也一樣。"""
    full = Annotation((0.0, 4.0, "A"), (4.0, 8.0, "B"), (8.0, 9.0, "B"), (9.0, 10.0, "A"))
    assert run(make(Pipeline(full=full))).overlap_seconds == 0.0


def test_without_the_exclusive_annotation_the_full_one_is_used_for_both():
    pipeline = Pipeline()
    result = SimpleNamespace(speaker_diarization=pipeline.full)
    pipeline.__class__ = type("P", (Pipeline,), {"__call__": lambda self, audio, hook=None, **kw: result})
    output = run(make(pipeline))
    assert [label for _, _, label in output.turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert output.overlap_seconds == pytest.approx(0.5)


def test_a_bare_annotation_from_an_older_pipeline_works_too():
    pipeline = Pipeline()
    annotation = Annotation((0.0, 2.0, "A"), (3.0, 5.0, "B"))
    pipeline.__class__ = type("P", (Pipeline,), {"__call__": lambda self, audio, hook=None, **kw: annotation})
    assert run(make(pipeline)).turns == [(0.0, 2.0, "A"), (3.0, 5.0, "B")]


def test_an_exclusive_annotation_without_the_full_one_serves_for_the_overlap_too():
    pipeline = Pipeline()
    result = SimpleNamespace(exclusive_speaker_diarization=pipeline.exclusive)
    pipeline.__class__ = type("P", (Pipeline,), {"__call__": lambda self, audio, hook=None, **kw: result})
    output = run(make(pipeline))
    assert [label for _, _, label in output.turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert output.overlap_seconds == 0.0


def test_empty_and_inverted_tracks_are_ignored():
    exclusive = Annotation((0.0, 0.0, "A"), (4.0, 3.0, "A"), (5.0, 6.0, "B"))
    assert run(make(Pipeline(exclusive=exclusive, full=exclusive))).turns == [(5.0, 6.0, "B")]


def test_infinite_tracks_are_ignored_and_labels_are_always_text():
    exclusive = Annotation((0.0, math.inf, "A"), (math.nan, 3.0, "A"), (5.0, 6.0, 0))
    assert run(make(Pipeline(exclusive=exclusive, full=exclusive))).turns == [(5.0, 6.0, "0")]


def test_the_overlap_is_rounded_to_milliseconds():
    full = Annotation((0.0, 2.0, "A"), (1.0001234, 3.0, "B"))
    assert run(make(Pipeline(full=full))).overlap_seconds == 1.0


def test_no_voice_at_all_is_a_failure_not_an_empty_result():
    empty = Annotation()
    failure = failed(lambda: run(make(Pipeline(exclusive=empty, full=empty))))
    assert failure.stage == worker.STAGE_EMPTY


def test_a_fixed_speaker_count_is_passed_on_and_none_means_automatic():
    engine = make()
    run(engine, speakers=3)
    run(engine, speakers=None)
    first, second = engine.pipeline.calls
    assert first[1]["num_speakers"] == 3
    assert "num_speakers" not in second[1]


def test_the_pipeline_gets_the_decoded_waveform_not_a_path():
    """解碼自己做（ffmpeg 管線）：pyannote 內建的解碼依賴 torchcodec 與系統 ffmpeg 的版本。"""
    loaded = []
    engine = make(loader=lambda path: loaded.append(path) or dict(WAVE))
    run(engine)
    assert loaded == ["audio.webm"]
    assert engine.pipeline.calls[0][0] == WAVE


def test_progress_reports_the_loading_decoding_and_each_pipeline_step():
    pipeline = Pipeline(steps=[("segmentation", 10, 5), ("embeddings", None, None), ("segmentation", 10, 10)])
    seen = []
    run(make(pipeline), progress=seen)
    statuses = [s for _, s in seen]
    assert statuses[0].startswith("載入") and "解碼音訊" in statuses[2]
    assert any("偵測人聲 5/10" in s for s in statuses) and any("比對聲紋" in s and "/" not in s for s in statuses)
    assert all(fraction is None for fraction, _ in seen)      # 區分到哪沒有可信的百分比：不假裝有


# --- 記憶體峰值（給 diarize_check 看的；量不到就是 None）---


def test_the_gpu_peak_is_reported_when_the_run_used_the_graphics_card():
    assert run(make(device="cuda", torch=FakeTorch(peak=3 * 2**30))).gpu_peak_bytes == 3 * 2**30


def test_the_gpu_peak_is_unknown_when_the_measurement_fails():
    assert run(make(device="cuda", torch=FakeTorch(peak=RuntimeError("driver")))).gpu_peak_bytes is None


def test_the_gpu_peak_is_unknown_without_a_graphics_card():
    assert run(make(device="auto", torch=FakeTorch(cuda=False, peak=999))).gpu_peak_bytes is None


def test_the_process_memory_peak_comes_from_the_resource_module(monkeypatch):
    fake = types.SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda who: SimpleNamespace(ru_maxrss=2048))
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setattr(sys, "platform", "linux")
    assert worker._peak_rss_bytes() == 2048 * 1024               # Linux 回報的是 KiB
    monkeypatch.setattr(sys, "platform", "darwin")
    assert worker._peak_rss_bytes() == 2048                      # macOS 回報的是位元組


def test_the_process_memory_peak_is_unknown_on_windows(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)           # import resource 會失敗
    assert worker._peak_rss_bytes() is None


# --- 失敗：分出是哪一步、可讀、不含 token ---


def test_a_failure_while_running_is_a_run_failure_that_remembers_the_device():
    pipeline = Pipeline(error=RuntimeError("cuDNN error: CUDNN_STATUS_EXECUTION_FAILED"))
    failure = failed(lambda: run(make(pipeline, device="cuda")))
    assert failure.stage == worker.STAGE_RUN and failure.device == "cuda" and not failure.oom
    assert "CUDNN_STATUS_EXECUTION_FAILED" in failure.detail
    assert failed(lambda: run(make(Pipeline(error=RuntimeError("x")), device="cpu"))).device == "cpu"


def test_cuda_running_out_of_memory_is_flagged():
    """顯示卡同時給 Ollama、語音辨識與說話者區分用；跑到一半最像的失敗就是記憶體不夠。"""
    pipeline = Pipeline(error=RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"))
    assert failed(lambda: run(make(pipeline, device="cuda"))).oom is True


def test_an_out_of_memory_error_class_is_recognised_even_when_its_text_is_terse():
    class OutOfMemoryError(RuntimeError):          # 比照 torch.cuda.OutOfMemoryError 的名字
        pass

    pipeline = Pipeline(error=OutOfMemoryError("allocation failed"))
    assert failed(lambda: run(make(pipeline, device="cuda"))).oom is True


def test_the_out_of_memory_check_is_not_fooled_by_other_errors():
    assert worker.is_out_of_memory(RuntimeError("cuDNN error")) is False
    assert worker.is_out_of_memory(RuntimeError("OUT OF MEMORY")) is True


def test_the_token_never_appears_in_any_failure_detail():
    """攔的 bug：Hugging Face 或 pyannote 的錯誤訊息帶著 token（網址、標頭），一路進了工作的 error
    欄位，再被 Pi 存進資料庫、印在畫面上。這個專案的 repo 是公開的，任務紀錄也會被分享。"""

    def leaking_factory(name, token):
        raise RuntimeError(f"401 for https://huggingface.co/{name} with Authorization: Bearer {token}")

    load = failed(lambda: run(make(factory=leaking_factory)))
    assert TOKEN not in load.detail and "hf_FAKE" not in load.detail

    leaking_pipeline = Pipeline(error=RuntimeError(f"failed (token={TOKEN})"))
    running = failed(lambda: run(make(leaking_pipeline)))
    assert TOKEN not in running.detail and "hf_FAKE" not in running.detail

    moving = failed(lambda: run(make(Picky(f"cuda failed {TOKEN}"), device="cuda")))
    assert TOKEN not in moving.detail

    decoding = failed(lambda: run(make(loader=lambda path: (_ for _ in ()).throw(RuntimeError(f"bad {TOKEN}")))))
    assert TOKEN not in decoding.detail


def test_a_configured_token_that_does_not_look_like_hf_is_still_scrubbed():
    """第一層：設定的那把 token 原字串取代。token 不一定是 hf_ 開頭（代理、測試用的假值），
    只靠樣式比對會漏。"""
    odd = "plain-secret-value-0001"

    def leaking_factory(name, token):
        raise RuntimeError(f"denied for {token}")

    assert odd not in failed(lambda: run(make(token=odd, factory=leaking_factory))).detail


def test_a_different_hf_token_in_an_error_is_scrubbed_too():
    """第二層：錯誤訊息裡出現的不是我們設定的那把（環境裡殘留的舊 token、代理回顯別人的）。"""
    stray = "hf_STRAYtokenSTRAYtoken9876543210"

    def leaking_factory(name, token):
        raise RuntimeError(f"cached credential {stray} was rejected")

    detail = failed(lambda: run(make(factory=leaking_factory))).detail
    assert stray not in detail and "hf_STRAY" not in detail


def test_the_token_is_not_in_the_progress_text():
    seen = []
    run(make(), progress=seen)
    assert all(TOKEN not in status for _, status in seen)


def test_failure_text_is_one_short_line():
    """攔的 bug：底層錯誤動輒數百字、含換行，原樣塞進狀態列與工作失敗訊息。"""
    pipeline = Pipeline(error=RuntimeError("first line\n" + "x" * 500))
    detail = failed(lambda: run(make(pipeline))).detail
    assert "\n" not in detail and "first line" in detail and "x" * 300 not in detail and detail.endswith("…")


def test_the_scrub_happens_before_the_cut_so_a_token_at_the_boundary_cannot_survive_half_removed():
    pad = worker._DETAIL_CHARS - 3 - 10                    # 截斷的界線正好落在 token 的第 10 個字
    detail = worker.scrub("x" * pad + TOKEN, TOKEN)
    assert "hf_" not in detail and TOKEN[:6] not in detail


def test_the_decoder_failing_is_a_decode_failure():
    def broken(path):
        raise MemoryError("cannot hold the waveform")

    failure = failed(lambda: run(make(loader=broken)))
    assert failure.stage == worker.STAGE_DECODE and "MemoryError" in failure.detail and "解碼" in failure.detail


def test_a_decode_failure_that_is_already_clean_is_passed_through_unchanged():
    clean = WorkerFailure(worker.STAGE_DECODE, "音訊解碼失敗：moov atom not found")

    def loader(path):
        raise clean

    assert failed(lambda: run(make(loader=loader))) is clean


# --- 進度：一行一則，寫到 stderr ---


def test_progress_lines_are_one_json_object_after_the_prefix():
    stream = io.StringIO()
    reporter = worker.Reporter(stream, TOKEN)
    reporter(None, "載入說話者區分模型…")
    reporter(0.5, f"token {TOKEN} 與中文")
    first, second = stream.getvalue().splitlines()
    assert first.startswith(worker.PROGRESS_PREFIX)
    assert json.loads(first[len(worker.PROGRESS_PREFIX):]) == {"fraction": None, "text": "載入說話者區分模型…"}
    payload = json.loads(second[len(worker.PROGRESS_PREFIX):])
    assert payload["fraction"] == 0.5 and TOKEN not in payload["text"] and "與中文" in payload["text"]


def test_a_progress_line_is_plain_ascii_whatever_the_encoding_of_stderr():
    """攔的 bug：stderr 的編碼不是 UTF-8（LANG=C 的容器）時，中文進度行寫不出去、整個行程因此失敗。
    JSON 的跳脫序列讓這一行永遠是 ASCII；父行程解開之後還是原本的中文。"""
    stream = io.StringIO()
    worker.Reporter(stream)(None, "區分說話者…（偵測人聲 5/10）")
    line = stream.getvalue().strip()
    assert line.isascii()
    assert json.loads(line[len(worker.PROGRESS_PREFIX):])["text"] == "區分說話者…（偵測人聲 5/10）"


def test_the_protocol_constants_are_pinned():
    """父行程與工作行程各在一個環境裡，靠這幾個值對話：要改就是有意的（連同兩邊的測試一起改）。"""
    assert worker.PROTOCOL == 1
    assert worker.PROGRESS_PREFIX == "SLIDEBOX_PROGRESS "
    assert (worker.EXIT_OK, worker.EXIT_FAILED) == (0, 3)
    assert (worker.STAGE_TERMS, worker.STAGE_LOAD, worker.STAGE_DECODE, worker.STAGE_RUN, worker.STAGE_EMPTY) == (
        "terms", "load", "decode", "run", "empty")
    assert worker.TOKEN_ENV == "HUGGINGFACE_TOKEN"


def test_a_progress_fraction_that_is_not_a_finite_number_becomes_null():
    stream = io.StringIO()
    reporter = worker.Reporter(stream)
    for bad in (math.nan, math.inf, "half"):
        reporter(bad, "x")
    assert all(json.loads(line[len(worker.PROGRESS_PREFIX):])["fraction"] is None for line in stream.getvalue().splitlines())


def test_progress_is_flushed_line_by_line():
    class Stream(io.StringIO):
        flushes = 0

        def flush(self):
            Stream.flushes += 1
            super().flush()

    stream = Stream()
    worker.Reporter(stream)(None, "a")
    assert Stream.flushes >= 1


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
    def __init__(self, data: bytes, returncode: int = 0, stderr_text: str = "", err_file=None, read_error=None):
        self.stdout = io.BytesIO(data)
        if read_error is not None:
            def read(size=-1):
                raise read_error

            self.stdout.read = read
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


def popen_returning(data: bytes, returncode: int = 0, stderr_text: str = "", read_error=None):
    made = []

    def popen(command, stdout=None, stderr=None, **kw):
        process = FakeProcess(data, returncode, stderr_text, err_file=stderr, read_error=read_error)
        process.command = command
        process.options = kw
        made.append(process)
        return process

    popen.made = made
    return popen


def decode(popen, path="in.webm"):
    return worker.decode_waveform(path, TorchWithNumpy, popen=popen, ffmpeg_exe="ffmpeg")


def test_the_default_decoder_asks_ffmpeg_for_16k_mono_pcm_and_scales_to_floats():
    import numpy as np

    pcm = np.array([0, 16384, -16384, 32767], dtype="<i2").tobytes()
    popen = popen_returning(pcm)
    wave = decode(popen)
    command = popen.made[0].command
    assert command[0] == "ffmpeg" and "in.webm" in command
    assert command[command.index("-ac") + 1] == "1" and command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-f") + 1] == "s16le" and command[-1] == "-"
    assert wave["sample_rate"] == 16000
    assert list(wave["waveform"].data) == pytest.approx([0.0, 0.5, -0.5, 32767 / 32768])
    assert wave["waveform"].shape == (1, 4)


def test_the_decoder_does_not_let_ffmpeg_read_the_terminal():
    popen = popen_returning(b"\x00\x00" * 8)
    decode(popen)
    assert popen.made[0].options.get("stdin") == subprocess.DEVNULL and "-nostdin" in popen.made[0].command


def test_a_successful_decode_lets_ffmpeg_finish_by_itself():
    """攔的 bug：正常讀到結尾還去 kill——pipe 剛關、行程還沒結束的瞬間 poll() 是 None，
    把一次成功的解碼誤判成失敗。"""
    popen = popen_returning(b"\x00\x00" * 8)
    decode(popen)
    assert popen.made[0].killed is False


def test_a_failing_ffmpeg_is_a_readable_decode_failure():
    popen = popen_returning(b"", returncode=1, stderr_text="in.webm: Invalid data found when processing input")
    failure = failed(lambda: decode(popen))
    assert failure.stage == worker.STAGE_DECODE and "Invalid data" in failure.detail and "解碼" in failure.detail


def test_ffmpeg_failing_without_a_message_still_says_the_exit_code():
    failure = failed(lambda: decode(popen_returning(b"", returncode=3)))
    assert "ffmpeg 結束碼 3" in failure.detail


def test_silence_that_decodes_to_nothing_is_an_error():
    failure = failed(lambda: decode(popen_returning(b"")))
    assert failure.stage == worker.STAGE_DECODE and "空" in failure.detail


def test_a_missing_ffmpeg_is_a_readable_error():
    def popen(command, **kw):
        raise FileNotFoundError("ffmpeg")

    failure = failed(lambda: decode(popen))
    assert failure.stage == worker.STAGE_DECODE and "ffmpeg" in failure.detail


def test_a_read_error_does_not_leave_ffmpeg_running():
    popen = popen_returning(b"", read_error=OSError("pipe broke"))
    with pytest.raises(OSError):
        decode(popen)
    assert popen.made[0].killed is True


def test_an_odd_trailing_byte_of_pcm_is_dropped():
    import numpy as np

    pcm = np.array([0, 16384], dtype="<i2").tobytes() + b"\x01"
    assert decode(popen_returning(pcm))["waveform"].shape == (1, 2)


def test_real_ffmpeg_decodes_a_generated_tone(tmp_path):
    """真的跑一次 ffmpeg（合成一秒的 440 Hz 音，不碰網路、不用模型）：指令列要真的能用。"""
    try:
        from slidebox.infrastructure.ffmpeg import get_ffmpeg_exe
        exe = get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg 不可用")
    source = str(tmp_path / "tone.m4a")
    subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1:sample_rate=44100", "-ac", "2", source], check=True)
    wave = worker.decode_waveform(source, TorchWithNumpy, ffmpeg_exe=exe)
    samples = wave["waveform"].data
    assert 15000 < len(samples) < 17500                      # 1 秒、16 kHz
    assert 0.05 < float(abs(samples).max()) <= 1.0           # 有聲音（lavfi 的 sine 預設只有 1/8 振幅）、沒有爆音
    assert float(abs(samples[4000:12000]).mean()) > 0.02     # 中段是持續的音，不是只有一個尖峰


# --- 探測：不 import pyannote、不 import torch ---


def test_the_probe_reports_whether_the_package_is_installed_and_its_version():
    assert worker.probe(find_spec=lambda name: object(), get_version=lambda name: "4.0.7") == {
        "protocol": worker.PROTOCOL, "installed": True, "version": "4.0.7"}


def test_the_probe_asks_about_the_right_package():
    asked = []
    worker.probe(find_spec=lambda name: asked.append(("spec", name)) or object(),
                 get_version=lambda name: asked.append(("version", name)) or "1")
    assert asked == [("spec", "pyannote.audio"), ("version", "pyannote.audio")]


def test_the_probe_says_not_installed_when_there_is_no_package():
    import importlib.metadata as metadata

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    assert worker.probe(find_spec=lambda name: None, get_version=missing) == {
        "protocol": worker.PROTOCOL, "installed": False, "version": ""}


@pytest.mark.parametrize("error", [ModuleNotFoundError("No module named 'pyannote'"),
                                   ValueError("pyannote.audio.__spec__ is None")])
def test_a_package_lookup_that_raises_means_not_installed(error):
    def broken(name):
        raise error

    assert worker.probe(find_spec=broken, get_version=lambda name: "")["installed"] is False


# --- 入口：真的把參數、環境變數、結果檔接起來 ---


class Scripted:
    """取代 PyannoteEngine：記下被怎麼呼叫，照劇本回傳或丟例外。"""

    def __init__(self, result=None, error=None, steps=()):
        self.result, self.error, self.steps = result, error, steps
        self.built: list[dict] = []
        self.diarized: list[tuple] = []

    def __call__(self, model, token="", device="auto", ffmpeg_exe="ffmpeg"):
        self.built.append({"model": model, "token": token, "device": device, "ffmpeg": ffmpeg_exe})
        return self

    def diarize(self, audio_path, speaker_count, progress):
        self.diarized.append((audio_path, speaker_count))
        for text in self.steps:
            progress(None, text)
        if self.error is not None:
            raise self.error
        return self.result


def an_engine_result(**overrides):
    values = dict(turns=[(0.0, 2.0, "SPEAKER_00"), (3.0, 5.0, "SPEAKER_01")], overlap_seconds=0.5,
                  device="cuda", gpu_peak_bytes=123, peak_rss_bytes=456)
    values.update(overrides)
    return worker.EngineResult(**values)


def call_main(tmp_path, scripted, *extra, environ=None, args=None):
    result_file = tmp_path / "result.json"
    argv = args if args is not None else ["run", "--audio", "in.webm", "--model", "m", "--device", "cuda",
                                         "--ffmpeg", "ffmpeg-exe", "--result", str(result_file), *extra]
    stdout, stderr = io.StringIO(), io.StringIO()
    code = worker.main(argv, environ={worker.TOKEN_ENV: TOKEN} if environ is None else environ,
                       stdout=stdout, stderr=stderr, engine_factory=scripted)
    return code, result_file, stdout.getvalue(), stderr.getvalue()


def test_a_successful_run_leaves_the_result_file_and_exits_zero(tmp_path):
    scripted = Scripted(result=an_engine_result())
    code, result_file, stdout, stderr = call_main(tmp_path, scripted, "--speakers", "3")
    assert code == worker.EXIT_OK == 0
    assert json.loads(result_file.read_text(encoding="utf-8")) == {
        "protocol": worker.PROTOCOL, "ok": True, "device": "cuda",
        "turns": [[0.0, 2.0, "SPEAKER_00"], [3.0, 5.0, "SPEAKER_01"]], "overlap_seconds": 0.5,
        "gpu_peak_bytes": 123, "peak_rss_bytes": 456}
    assert scripted.diarized == [("in.webm", 3)]
    assert stdout == ""


def test_the_arguments_reach_the_engine_and_the_token_comes_only_from_the_environment(tmp_path):
    scripted = Scripted(result=an_engine_result())
    call_main(tmp_path, scripted, environ={worker.TOKEN_ENV: f"  {TOKEN}  "})
    assert scripted.built == [{"model": "m", "token": TOKEN, "device": "cuda", "ffmpeg": "ffmpeg-exe"}]
    assert scripted.diarized == [("in.webm", None)]                       # 沒給 --speakers 就是自動判斷人數


def test_there_is_no_command_line_option_for_the_token():
    parser = worker.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--audio", "a", "--model", "m", "--result", "r", "--token", TOKEN])


def test_the_progress_goes_to_stderr_and_nothing_goes_to_stdout(tmp_path):
    scripted = Scripted(result=an_engine_result(), steps=["解碼音訊…", f"連線 {TOKEN}"])
    _, _, stdout, stderr = call_main(tmp_path, scripted)
    lines = stderr.splitlines()
    assert [json.loads(line[len(worker.PROGRESS_PREFIX):])["text"] for line in lines] == ["解碼音訊…", "連線 ***"]
    assert stdout == "" and TOKEN not in stderr


def test_the_result_file_appears_complete_or_not_at_all(tmp_path):
    """先寫暫存檔再換名：父行程看到結果檔的時候，它一定是完整的（行程可能在寫到一半時被殺掉）。"""
    seen = []
    real_replace = os.replace

    def spying_replace(source, destination):
        seen.append((os.path.exists(destination), json.loads(Path(source).read_text(encoding="utf-8"))["ok"]))
        real_replace(source, destination)

    scripted = Scripted(result=an_engine_result())
    original = os.replace
    os.replace = spying_replace
    try:
        _, result_file, _, _ = call_main(tmp_path, scripted)
    finally:
        os.replace = original
    assert seen == [(False, True)]                                        # 換名之前，正式的檔案還不存在
    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]        # 沒有留下暫存檔


def test_a_clean_failure_is_written_to_the_result_file_with_exit_code_3(tmp_path):
    error = WorkerFailure(worker.STAGE_RUN, f"CUDA out of memory (token {TOKEN})", oom=True, device="cuda")
    code, result_file, _, _ = call_main(tmp_path, Scripted(error=error))
    assert code == worker.EXIT_FAILED == 3
    written = json.loads(result_file.read_text(encoding="utf-8"))
    assert written == {"protocol": worker.PROTOCOL, "ok": False, "stage": "run", "oom": True, "device": "cuda",
                       "detail": written["detail"]}
    assert "out of memory" in written["detail"] and TOKEN not in written["detail"]


def test_an_unexpected_error_still_ends_as_a_clean_failure_and_the_log_is_scrubbed(tmp_path):
    """沒料到的錯誤（引擎以外的地方出了狀況）：結果檔照樣是乾淨的失敗，GPU 主機的 log 留一份追蹤，
    追蹤裡不能有 token。"""
    code, result_file, _, stderr = call_main(tmp_path, Scripted(error=KeyError(f"surprise {TOKEN}")))
    written = json.loads(result_file.read_text(encoding="utf-8"))
    assert code == worker.EXIT_FAILED and written["ok"] is False and written["stage"] == worker.STAGE_RUN
    assert "KeyError" in written["detail"] and TOKEN not in written["detail"]
    assert "Traceback" in stderr and "KeyError" in stderr and TOKEN not in stderr


def test_an_engine_that_cannot_even_be_built_is_a_clean_failure_too(tmp_path):
    def factory(*args, **kwargs):
        raise TypeError("bad arguments")

    code, result_file, _, _ = call_main(tmp_path, factory)
    assert code == worker.EXIT_FAILED
    assert "TypeError" in json.loads(result_file.read_text(encoding="utf-8"))["detail"]


def test_a_failure_that_cannot_be_encoded_is_not_written_half_way(tmp_path):
    """結果檔不能是半個檔：NaN 不是合法的 JSON，寫的時候就失敗，而不是留下父行程讀不懂的檔。"""
    scripted = Scripted(result=an_engine_result(overlap_seconds=math.nan))
    code, result_file, _, _ = call_main(tmp_path, scripted)
    written = json.loads(result_file.read_text(encoding="utf-8"))
    assert code == worker.EXIT_FAILED and written["ok"] is False


def test_the_probe_command_prints_one_json_line_and_exits_zero(monkeypatch):
    monkeypatch.setattr(worker, "probe", lambda: {"protocol": 1, "installed": True, "version": "4.0.7"})
    stdout = io.StringIO()
    code = worker.main(["probe"], environ={}, stdout=stdout, stderr=io.StringIO())
    assert code == 0 and json.loads(stdout.getvalue()) == {"protocol": 1, "installed": True, "version": "4.0.7"}


@pytest.mark.parametrize("argv", [[], ["run"], ["run", "--audio", "a"], ["run", "--audio", "a", "--model", "m", "--result", "r",
                                                                           "--device", "tpu"], ["nonsense"]])
def test_bad_arguments_are_refused_with_a_usage_error(argv):
    with pytest.raises(SystemExit) as error:
        worker.main(argv, environ={}, stdout=io.StringIO(), stderr=io.StringIO())
    assert error.value.code == 2


# --- 這個檔案是獨立的：在沒有 slidebox、沒有 fastapi 的環境裡也要能跑 ---


def _imported_modules(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "工作行程不能用相對 import：它是被當成獨立的檔案執行的"
            names.add((node.module or "").split(".")[0])
    return names


def test_the_worker_imports_only_the_standard_library_numpy_torch_and_pyannote():
    """攔的 bug：有人在這個檔案裡 import slidebox 的東西（為了共用一個小函式）——它在第二個環境裡跑，
    那裡沒有 slidebox、fastapi、faster-whisper、imageio-ffmpeg，行程一啟動就 ImportError，
    而主環境的所有測試都還是綠的。"""
    tree = ast.parse(WORKER_FILE.read_text(encoding="utf-8"))
    allowed = set(sys.stdlib_module_names) | {"numpy", "torch", "pyannote"}
    assert _imported_modules(tree) - allowed == set()


def test_the_allowlist_check_itself_catches_a_forbidden_import():
    tree = ast.parse("import json\nfrom slidebox.domain import errors\nimport requests\n")
    assert _imported_modules(tree) - (set(sys.stdlib_module_names) | {"numpy", "torch", "pyannote"}) == {
        "slidebox", "requests"}


def test_the_heavy_libraries_are_not_imported_at_module_level():
    """攔的 bug：模組一載入就 import torch——探測（/capabilities 會一直問）每次都要等好幾秒，
    也會在載入失敗時連最便宜的檢查都失敗。numpy、torch、pyannote 只能在用到的函式裡才 import。"""
    tree = ast.parse(WORKER_FILE.read_text(encoding="utf-8"))
    top_level = {name for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                 for name in _imported_modules(ast.Module(body=[node], type_ignores=[]))}
    assert top_level & {"numpy", "torch", "pyannote"} == set()


def _python(*args, cwd=None, env=None):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def test_the_worker_runs_as_a_standalone_file_without_the_project_on_the_path(tmp_path):
    """真的用另一個目錄、沒有 PYTHONPATH、-I（隔離模式）把它當腳本跑：import 到專案的東西就會當場失敗。"""
    alone = tmp_path / "alone"
    alone.mkdir()
    copy = alone / "pyannote_worker.py"
    copy.write_bytes(WORKER_FILE.read_bytes())
    env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
    helped = _python(str(copy), "--help", cwd=str(alone), env=env)
    assert helped.returncode == 0 and "probe" in helped.stdout and "run" in helped.stdout
    probed = _python(str(copy), "probe", cwd=str(alone), env=env)
    answer = json.loads(probed.stdout.strip().splitlines()[-1])
    assert probed.returncode == 0 and answer["protocol"] == worker.PROTOCOL
    assert isinstance(answer["installed"], bool) and isinstance(answer["version"], str)


def test_the_probe_does_not_import_torch_numpy_or_pyannote(tmp_path):
    """這幾個模組都先封死（import 會失敗）：載入模組與探測都不能碰它們。"""
    code = textwrap.dedent("""
        import importlib.util, sys
        for name in ("torch", "numpy", "pyannote", "pyannote.audio"):
            sys.modules[name] = None
        spec = importlib.util.spec_from_file_location("worker_under_test", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules["worker_under_test"] = module          # dataclass 要從 sys.modules 找到自己的模組
        spec.loader.exec_module(module)
        print(module.probe())
    """)
    result = _python("-c", code, str(WORKER_FILE), cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "'protocol': 1" in result.stdout and "'installed': False" in result.stdout


def test_the_worker_exits_with_a_usage_error_when_run_without_a_command(tmp_path):
    result = _python(str(WORKER_FILE), cwd=str(tmp_path))
    assert result.returncode == 2 and "usage" in result.stderr.lower()
