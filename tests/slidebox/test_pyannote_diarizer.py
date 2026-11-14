"""pyannote 說話者區分 adapter：父行程這一半。

pyannote 與 PyTorch 在另一個 Python 環境的工作行程裡跑（pyannote_worker.py，引擎本身的測試在
test_pyannote_worker.py）。這裡測的是 adapter 怎麼管那個行程：起得來、進度轉得回來、取消與逾時
真的把它殺掉、當掉與亂回答都是看得懂的失敗、token 與函式庫路徑不會跑到不該去的地方。

用真的子行程（跑一支很小的假工作行程腳本）而不是替身：行程管理的錯（沒收的僵屍行程、死結的管線、
殺不掉的子行程）只有真的行程才看得到。不安裝 pyannote、不下載模型、不碰 Hugging Face。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from slidebox.domain.entities import DiarizerOutput, SpeakerTurn
from slidebox.domain.errors import DiarizationUnavailable, OperationCancelled
from slidebox.infrastructure import pyannote_diarizer as mod
from slidebox.infrastructure import pyannote_worker as worker
from slidebox.infrastructure.pyannote_diarizer import PyannoteSpeakerDiarizer

TOKEN = "hf_FAKEtokenFAKEtokenFAKEtoken1234567890"
REAL_WORKER = Path(worker.__file__)

# 假的工作行程：和真的一樣用 `python -I -X utf8 腳本 run|probe …` 啟動，結果寫在 --result 指的檔案裡
FAKE_PREAMBLE = textwrap.dedent('''
    import json, os, sys, time

    def arg(name):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None

    def progress(text, fraction=None):
        print("SLIDEBOX_PROGRESS " + json.dumps({"fraction": fraction, "text": text}), file=sys.stderr, flush=True)

    def result(**payload):
        path = arg("--result")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"protocol": 1, **payload}, f)
        os.replace(path + ".tmp", path)

    TURNS = [[0.0, 2.0, "SPEAKER_00"], [3.0, 5.0, "SPEAKER_01"]]

    def succeed(**overrides):
        result(**{"ok": True, "device": "cuda", "turns": TURNS, "overlap_seconds": 0.5,
                  "gpu_peak_bytes": 1234, "peak_rss_bytes": 5678, **overrides})

    def fail(stage, detail="", oom=False, device="cuda"):
        result(ok=False, stage=stage, detail=detail, oom=oom, device=device)
        sys.exit(3)

    def dump(**extra):
        with open(os.environ["TEST_DUMP"], "w", encoding="utf-8") as f:
            json.dump({"argv": sys.argv, "env": dict(os.environ), "flags": sys.flags.isolated, **extra}, f)

    if sys.argv[1] == "probe":
        print(os.environ.get("TEST_PROBE_OUTPUT") or json.dumps({"protocol": 1, "installed": True, "version": "4.0.7"}))
        if os.environ.get("TEST_PROBE_FLAGS"):
            with open(os.environ["TEST_PROBE_FLAGS"], "w", encoding="utf-8") as f:
                json.dump({"argv": sys.argv, "isolated": sys.flags.isolated, "utf8": sys.flags.utf8_mode}, f)
        if os.environ.get("TEST_PROBE_COUNT"):
            with open(os.environ["TEST_PROBE_COUNT"], "a") as f:
                f.write("x\\n")
        if os.environ.get("TEST_PROBE_DUMP"):
            with open(os.environ["TEST_PROBE_DUMP"], "w", encoding="utf-8") as f:
                json.dump(dict(os.environ), f)
        time.sleep(float(os.environ.get("TEST_PROBE_SLEEP") or 0))
        sys.exit(int(os.environ.get("TEST_PROBE_EXIT") or 0))
''')


def worker_script(tmp_path: Path, body: str = "succeed()", name: str = "fake_worker.py") -> str:
    path = tmp_path / name
    path.write_text(FAKE_PREAMBLE + textwrap.dedent(body).replace("@TOKEN@", TOKEN) + "\n", encoding="utf-8")
    return str(path)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Spy:
    """包住 subprocess.Popen：記下啟動了哪些行程、用什麼參數，測試結束後才查得到它們死了沒有。"""

    def __init__(self):
        self.made: list[subprocess.Popen] = []
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        process = subprocess.Popen(command, **kwargs)
        self.made.append(process)
        self.calls.append((list(command), kwargs))
        return process


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """測試不能被這台機器自己的設定影響，也不能把結果檔丟進別人的暫存資料夾。"""
    for name in (mod.TOKEN_ENV, mod.MODE_ENV, mod.DEVICE_ENV, mod.PYTHON_ENV, mod.TIMEOUT_ENV,
                 "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "SLIDEBOX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    return scratch


def make(tmp_path, body="succeed()", *, token=TOKEN, device="cpu", mode="pyannote", timeout=None, clock=None,
         installed=True, version="4.0.7", model="test-model", python=None, ffmpeg=None, popen=None):
    script = worker_script(tmp_path, body)
    spy = Spy()
    diarizer = PyannoteSpeakerDiarizer(
        model_name=model, token=token, device=device, mode=mode, python=python or sys.executable,
        worker_path=script, timeout_seconds=timeout,
        spec_finder=lambda name: object() if installed else None,
        version_finder=lambda: version,
        ffmpeg_finder=ffmpeg or (lambda: "ffmpeg-for-test"),
        popen=popen or spy, clock=clock or Clock())
    diarizer.spy = spy
    return diarizer


def run(diarizer, speakers=None, cancelled=lambda: False, progress=None):
    seen = progress if progress is not None else []
    return diarizer.diarize("audio.webm", 10.0, speakers, lambda f, s: seen.append((f, s)), cancelled)


def failure_of(diarizer, **kwargs) -> str:
    with pytest.raises(DiarizationUnavailable) as error:
        run(diarizer, **kwargs)
    return str(error.value)


def assert_finished(spy: Spy) -> None:
    """起過的每個子行程都已經結束、也被收掉了（沒有留下還在跑的，也沒有沒收的僵屍行程），連到它的管線也關了。"""
    assert spy.made, "沒有啟動任何行程"
    for process in spy.made:
        assert process.poll() is not None, "有行程還在跑"
        assert process.stderr.closed, "連到行程的管線沒關"


def already_finished(spy: Spy):
    """popen 的替身：行程在還給呼叫者之前就結束了（它寫的東西還留在管線裡），
    等待的迴圈第一次去看，看到的就是「跑完了」。"""
    def popen(command, **kwargs):
        process = spy(command, **kwargs)
        process.wait()
        return process
    return popen


# --- available()：便宜、不載入模型、不啟動工作行程 ---


def test_available_when_the_token_and_the_package_are_there_and_nothing_is_started(tmp_path):
    """攔的 bug：available() 載入整個 pipeline。/capabilities 會被 Pi 一直問，
    每問一次就載一次模型（幾百 MB、好幾秒），還可能下載。"""
    diarizer = make(tmp_path)
    assert diarizer.available() == (True, "")
    assert diarizer.spy.made == []


def test_a_missing_token_is_reported_and_names_the_variable(tmp_path):
    diarizer = make(tmp_path, token="")
    available, reason = diarizer.available()
    assert not available and "HUGGINGFACE_TOKEN" in reason
    assert diarizer.spy.made == []


def test_a_blank_token_counts_as_missing(tmp_path):
    assert make(tmp_path, token="   ").available()[0] is False


def test_a_missing_package_is_reported_with_the_build_hint(tmp_path):
    available, reason = make(tmp_path, installed=False).available()
    assert not available and "pyannote.audio" in reason and "diarization" in reason


@pytest.mark.parametrize("error", [ModuleNotFoundError("No module named 'pyannote'"),
                                   ValueError("pyannote.audio.__spec__ is None")])
def test_a_package_lookup_that_raises_means_not_installed(error):
    def broken(name):
        raise error

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", spec_finder=broken)
    available, reason = diarizer.available()
    assert not available and "pyannote.audio" in reason


def test_the_kill_switch_turns_it_off_even_when_everything_else_is_fine(tmp_path):
    """SLIDEBOX_DIARIZER=off：維運一鍵關掉多人模式，Pi 那邊會拒絕，單人（--speakers 1）照跑。"""
    diarizer = make(tmp_path, mode="off")
    available, reason = diarizer.available()
    assert not available and "SLIDEBOX_DIARIZER=off" in reason
    assert "SLIDEBOX_DIARIZER=off" in failure_of(diarizer)
    assert diarizer.spy.made == []


@pytest.mark.parametrize("value", ["sherpa", "on", "OFF "])
def test_an_unknown_engine_setting_is_a_configuration_error_not_a_silent_default(tmp_path, value):
    available, reason = make(tmp_path, mode=value).available()
    if value.strip().lower() == "off":
        assert not available and "關閉" in reason          # 是「關掉了」，不是「寫錯了」
    else:
        assert not available and "SLIDEBOX_DIARIZER" in reason


def test_an_unknown_device_setting_is_reported(tmp_path):
    available, reason = make(tmp_path, device="gpu").available()
    assert not available and "SLIDEBOX_DIARIZATION_DEVICE" in reason


def test_the_answer_is_cached_for_a_few_seconds_and_refreshed_afterwards():
    clock = Clock()
    lookups = []

    def finder(name):
        lookups.append(name)
        return object()

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", spec_finder=finder, clock=clock)
    diarizer.available()
    diarizer.available()
    assert lookups == ["pyannote.audio"]
    clock.now += 60
    diarizer.available()
    assert lookups == ["pyannote.audio", "pyannote.audio"]


def test_diarize_without_a_token_raises_the_same_readable_reason(tmp_path):
    diarizer = make(tmp_path, token="")
    assert "HUGGINGFACE_TOKEN" in failure_of(diarizer)
    assert diarizer.spy.made == []


@pytest.mark.parametrize("value, ok", [("", True), ("  ", True), ("0", True), ("3600", True), ("90.5", True),
                                       ("abc", False), ("-5", False), ("inf", False), ("nan", False)])
def test_the_timeout_setting_is_validated_like_the_other_settings(tmp_path, monkeypatch, value, ok):
    """攔的 bug：寫錯的逾時設定被當成預設值悄悄吞掉——操作者以為調了，其實沒有。"""
    monkeypatch.setenv(mod.TIMEOUT_ENV, value)
    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", spec_finder=lambda name: object())
    available, reason = diarizer.available()
    assert available is ok
    if not ok:
        assert "SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS" in reason and value in reason


# --- 身分：給存檔與結果帶著的指紋 ---


def test_engine_version_model_and_fingerprint(tmp_path):
    diarizer = make(tmp_path, version="4.0.7", model="pyannote/speaker-diarization-community-1")
    assert diarizer.engine == "pyannote.audio"
    assert diarizer.version == "4.0.7"
    assert diarizer.model == "pyannote/speaker-diarization-community-1"
    assert re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_fingerprint_changes_with_the_version_and_the_model_and_is_otherwise_stable(tmp_path):
    base = make(tmp_path, version="4.0.7", model="m1").fingerprint
    assert make(tmp_path, version="4.0.7", model="m1").fingerprint == base
    assert make(tmp_path, version="4.1.0", model="m1").fingerprint != base
    assert make(tmp_path, version="4.0.7", model="m2").fingerprint != base


def test_the_fingerprint_does_not_depend_on_the_token_or_the_device(tmp_path):
    base = make(tmp_path).fingerprint
    assert make(tmp_path, token="other-token", device="cuda").fingerprint == base


def test_the_fingerprint_exists_even_when_the_package_is_not_installed(tmp_path):
    diarizer = make(tmp_path, installed=False, version="")
    assert diarizer.version == "" and re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_fingerprint_follows_the_worker_script_that_actually_runs(tmp_path):
    """攔的 bug：工作行程的腳本改了（區段怎麼整理、交疊怎麼算），指紋卻沒變——舊腳本區分出來的結果
    被當成新腳本的。同一個版本、同一個模型、同一份腳本，算出同一個指紋（與版本是從哪個環境量到的無關）；
    腳本一個字不同，指紋就不同。只驗性質，不釘雜湊值。"""
    base = make(tmp_path, version="4.0.7", model="m1").fingerprint
    assert make(tmp_path, version="4.0.7", model="m1").fingerprint == base
    assert make(tmp_path, "succeed(overlap_seconds=1.5)", version="4.0.7", model="m1").fingerprint != base


def test_the_worker_script_is_fingerprinted_by_its_text_and_not_by_its_line_endings(tmp_path):
    """Windows 簽出的 CRLF 與容器裡的 LF 是同一份腳本：換機器、重建映像，存檔不能因此全部作廢。"""
    (tmp_path / "lf").mkdir()
    (tmp_path / "crlf").mkdir()
    text = b"print('worker')\nprint('again')\n"
    (tmp_path / "lf" / "pyannote_worker.py").write_bytes(text)
    (tmp_path / "crlf" / "pyannote_worker.py").write_bytes(text.replace(b"\n", b"\r\n"))

    def fingerprint_of(folder: str) -> str:
        return PyannoteSpeakerDiarizer(
            token=TOKEN, version_finder=lambda: "4.0.7", spec_finder=lambda n: object(), model_name="m",
            worker_path=str(tmp_path / folder / "pyannote_worker.py")).fingerprint

    assert fingerprint_of("lf") == fingerprint_of("crlf")


def test_the_fingerprint_follows_the_adapters_own_source(tmp_path, monkeypatch):
    from slidebox.usecases import code_fingerprint as cf

    diarizer = make(tmp_path)
    base = diarizer.fingerprint
    real = cf.read_module_source
    monkeypatch.setattr(cf, "read_module_source",
                        lambda name: real(name) + "\n# 改過\n" if name == mod.__name__ else real(name))
    assert diarizer.fingerprint != base


def test_the_real_worker_file_is_part_of_the_default_fingerprint(tmp_path):
    """沒有注入腳本路徑時，指紋讀的就是真的 pyannote_worker.py（不是別的檔案）。"""
    default = PyannoteSpeakerDiarizer(token=TOKEN, version_finder=lambda: "4.0.7", spec_finder=lambda n: object())
    copy = tmp_path / "pyannote_worker.py"
    copy.write_bytes(REAL_WORKER.read_bytes())
    same = PyannoteSpeakerDiarizer(token=TOKEN, version_finder=lambda: "4.0.7", spec_finder=lambda n: object(),
                                   worker_path=str(copy))
    assert default.fingerprint == same.fingerprint
    copy.write_bytes(copy.read_bytes() + b"\n# edited\n")
    assert default.fingerprint != same.fingerprint


def test_an_unreadable_worker_script_still_gives_a_fingerprint_that_matches_nothing(tmp_path):
    """腳本不見了：指紋照樣算得出來（不 raise），而且對不上任何舊存檔。"""
    present = make(tmp_path)
    base = present.fingerprint
    missing = PyannoteSpeakerDiarizer(token=TOKEN, version_finder=lambda: "4.0.7", spec_finder=lambda n: object(),
                                      worker_path=str(tmp_path / "no_such_worker.py"), model_name="test-model")
    assert re.fullmatch(r"[0-9a-f]{16}", missing.fingerprint) and missing.fingerprint != base
    assert missing.fingerprint == missing.fingerprint


def test_a_version_lookup_that_raises_means_no_version():
    def broken():
        raise RuntimeError("metadata is corrupt")

    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, version_finder=broken, spec_finder=lambda name: object())
    assert diarizer.version == "" and re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_token_is_not_in_the_repr(tmp_path):
    assert TOKEN not in repr(make(tmp_path)) and "test-model" in repr(make(tmp_path))


# --- 設定來自環境變數 ---


def _from_environment():
    return PyannoteSpeakerDiarizer(spec_finder=lambda name: object(), clock=Clock())


def test_the_token_comes_from_the_environment_and_surrounding_blanks_are_ignored(monkeypatch):
    """攔的 bug：token 填在環境變數（.env.gpu → compose）卻沒讀進來，/capabilities 永遠說沒設定。"""
    assert _from_environment().available()[0] is False
    monkeypatch.setenv(mod.TOKEN_ENV, "  " + TOKEN + "  ")
    assert _from_environment().available() == (True, "")


def test_blank_settings_mean_the_defaults(monkeypatch):
    """攔的 bug：留空的設定（例如 `SLIDEBOX_DIARIZER=`）被當成「寫錯的設定值」，多人模式永遠不能用。"""
    monkeypatch.setenv(mod.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mod.MODE_ENV, "")
    monkeypatch.setenv(mod.DEVICE_ENV, "  ")
    monkeypatch.setenv(mod.TIMEOUT_ENV, "")
    monkeypatch.setenv(mod.PYTHON_ENV, "")
    assert _from_environment().available() == (True, "")


def test_settings_are_case_insensitive(monkeypatch):
    """攔的 bug：SLIDEBOX_DIARIZER=OFF 沒當成關掉、SLIDEBOX_DIARIZATION_DEVICE=CPU 被拒絕。"""
    monkeypatch.setenv(mod.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mod.DEVICE_ENV, "CPU")
    assert _from_environment().available() == (True, "")
    monkeypatch.setenv(mod.MODE_ENV, "OFF")
    available, reason = _from_environment().available()
    assert not available and "關閉" in reason


# --- 說話者區分用的 Python：找得到、起得來、答得出 ---


def _probe_diarizer(tmp_path, monkeypatch, *, python=None, **probe_env):
    for name, value in probe_env.items():
        monkeypatch.setenv(name, str(value))
    return PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", python=python or sys.executable,
                                   worker_path=worker_script(tmp_path), clock=Clock())


def test_the_package_check_asks_the_diarization_environment_not_this_process(tmp_path, monkeypatch):
    """攔的 bug：用 find_spec 在 API 自己的環境裡找 pyannote——兩個環境分開之後，主環境永遠沒有它，
    /capabilities 永遠說「沒有安裝」，多人模式明明裝好了卻永遠不能用。"""
    diarizer = _probe_diarizer(tmp_path, monkeypatch)
    assert diarizer.available() == (True, "")
    assert diarizer.version == "4.0.7"
    assert re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_probe_can_say_the_package_is_not_installed(tmp_path, monkeypatch):
    diarizer = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_OUTPUT=json.dumps(
        {"protocol": 1, "installed": False, "version": ""}))
    available, reason = diarizer.available()
    assert not available and "pyannote.audio" in reason and "diarization" in reason
    assert diarizer.version == ""


def test_a_python_that_does_not_exist_is_reported_with_the_variable_that_names_it(tmp_path, monkeypatch):
    missing = str(tmp_path / "no-such-venv" / "bin" / "python")
    diarizer = _probe_diarizer(tmp_path, monkeypatch, python=missing)
    available, reason = diarizer.available()
    assert not available and "SLIDEBOX_DIARIZATION_PYTHON" in reason and missing in reason
    assert diarizer.version == ""                       # 量不到版本不是例外，是空字串


def test_a_bare_command_name_is_looked_up_on_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: sys.executable if name == "python-for-diarization" else None)
    assert _probe_diarizer(tmp_path, monkeypatch, python="python-for-diarization").available() == (True, "")
    available, reason = _probe_diarizer(tmp_path, monkeypatch, python="no-such-command").available()
    assert not available and "no-such-command" in reason


def test_the_python_comes_from_the_environment_variable_and_defaults_to_the_current_one(tmp_path, monkeypatch):
    seen = []

    class Recording(Spy):
        def __call__(self, command, **kwargs):
            seen.append(command[0])
            return super().__call__(command, **kwargs)

    script = worker_script(tmp_path)

    def build():
        return PyannoteSpeakerDiarizer(model_name="m", token=TOKEN, device="cpu", mode="pyannote", worker_path=script,
                                       spec_finder=lambda n: object(), version_finder=lambda: "1",
                                       ffmpeg_finder=lambda: "ffmpeg", popen=Recording(), clock=Clock())

    run(build())
    here = os.path.dirname(sys.executable)
    alias = os.path.join(here, os.pardir, os.path.basename(here), os.path.basename(sys.executable))   # 同一個檔案，不同的寫法
    monkeypatch.setenv(mod.PYTHON_ENV, alias)
    run(build())
    assert seen == [sys.executable, alias]
    monkeypatch.setenv(mod.PYTHON_ENV, str(tmp_path / "nope"))
    assert "SLIDEBOX_DIARIZATION_PYTHON" in failure_of(build())


def test_a_worker_that_cannot_start_is_reported_with_what_it_said(tmp_path, monkeypatch):
    diarizer = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_EXIT=2)
    available, reason = diarizer.available()
    assert not available and "結束碼 2" in reason


def test_a_worker_script_that_is_missing_is_reported(tmp_path):
    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, python=sys.executable, worker_path=str(tmp_path / "gone.py"),
                                       clock=Clock())
    available, reason = diarizer.available()
    assert not available and "工作行程" in reason


def test_a_probe_that_hangs_is_given_up_on(tmp_path, monkeypatch):
    """攔的 bug：探測沒有上限——/capabilities 每被問一次就卡住一個請求。"""
    monkeypatch.setattr(mod, "PROBE_TIMEOUT_SECONDS", 0.5)
    diarizer = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_SLEEP=30)
    available, reason = diarizer.available()
    assert not available and "沒有在" in reason and "回應" in reason


@pytest.mark.parametrize("output", ["not json at all", "[1, 2]", json.dumps({"protocol": 99, "installed": True}),
                                    json.dumps({"installed": True})])
def test_a_probe_answer_that_makes_no_sense_is_a_readable_failure(tmp_path, monkeypatch, output):
    available, reason = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_OUTPUT=output).available()
    assert not available and ("無法理解" in reason or "版本不一致" in reason)


def test_the_probe_answer_is_cached_for_a_few_seconds(tmp_path, monkeypatch):
    counter = tmp_path / "probes.txt"
    clock = Clock()
    monkeypatch.setenv("TEST_PROBE_COUNT", str(counter))
    diarizer = PyannoteSpeakerDiarizer(token=TOKEN, device="cpu", mode="pyannote", python=sys.executable,
                                       worker_path=worker_script(tmp_path), clock=clock)
    diarizer.available()
    diarizer.version
    diarizer.fingerprint
    assert counter.read_text().count("x") == 1                      # 三次詢問、一次啟動
    clock.now += 60
    diarizer.available()
    assert counter.read_text().count("x") == 2


def test_the_probe_gets_neither_the_token_nor_the_library_path(tmp_path, monkeypatch):
    dump = tmp_path / "probe_env.json"
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib")
    monkeypatch.setenv("PYTHONPATH", "/app/src")
    diarizer = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_DUMP=dump)
    monkeypatch.setenv(mod.TOKEN_ENV, TOKEN)
    assert diarizer.available() == (True, "")
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert mod.TOKEN_ENV not in seen and "LD_LIBRARY_PATH" not in seen and "PYTHONPATH" not in seen


def test_the_probe_is_started_isolated_and_in_utf8_mode_like_the_worker(tmp_path, monkeypatch):
    """攔的 bug：probe 沒用 -I，主環境的 PYTHONPATH（/app/src）會被第二個環境的 Python 吃進去——
    它回答的就不是「第二個環境自己有什麼」。"""
    flags = tmp_path / "probe_flags.json"
    diarizer = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_FLAGS=flags)
    assert diarizer.available() == (True, "")
    seen = json.loads(flags.read_text(encoding="utf-8"))
    assert seen["argv"][1:] == ["probe"] and seen["isolated"] == 1 and seen["utf8"] == 1


def test_a_python_that_exists_but_cannot_be_run_is_reported_not_raised(tmp_path, monkeypatch):
    """檔案在，但不是能執行的東西（權限、格式不對）：subprocess 丟 OSError——不能讓 /capabilities 因此 500。"""
    not_a_program = tmp_path / "python.txt"
    not_a_program.write_text("this is not an executable", encoding="utf-8")
    diarizer = _probe_diarizer(tmp_path, monkeypatch, python=str(not_a_program))
    available, reason = diarizer.available()
    assert not available and "SLIDEBOX_DIARIZATION_PYTHON" in reason and str(not_a_program) in reason
    assert diarizer.version == "" and re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)


def test_the_version_and_fingerprint_survive_an_environment_that_cannot_answer(tmp_path, monkeypatch):
    """攔的 bug：第二個環境壞了的時候，讀版本的人（報告、存檔指紋）跟著丟例外，連「不能用」都說不出來。"""
    diarizer = _probe_diarizer(tmp_path, monkeypatch, TEST_PROBE_EXIT=2)
    assert diarizer.version == ""
    assert re.fullmatch(r"[0-9a-f]{16}", diarizer.fingerprint)
    assert diarizer.available()[0] is False


# --- 區分：成功 ---


def test_a_run_returns_the_turns_the_overlap_and_what_the_worker_measured(tmp_path):
    diarizer = make(tmp_path)
    output = run(diarizer)
    assert isinstance(output, DiarizerOutput)
    assert output.turns == (SpeakerTurn(0.0, 2.0, "SPEAKER_00"), SpeakerTurn(3.0, 5.0, "SPEAKER_01"))
    assert output.overlap_seconds == 0.5
    assert diarizer.device == "cuda"                   # 實際用的裝置來自工作行程，不是設定值
    assert diarizer.gpu_peak_bytes == 1234 and diarizer.worker_peak_bytes == 5678
    assert_finished(diarizer.spy)


def test_nothing_is_known_before_the_first_run(tmp_path):
    diarizer = make(tmp_path)
    assert diarizer.device == "" and diarizer.gpu_peak_bytes is None and diarizer.worker_peak_bytes is None


def test_peaks_the_worker_could_not_measure_stay_unknown(tmp_path):
    diarizer = make(tmp_path, 'succeed(gpu_peak_bytes=None, peak_rss_bytes=None, device="cpu")')
    run(diarizer)
    assert diarizer.device == "cpu" and diarizer.gpu_peak_bytes is None and diarizer.worker_peak_bytes is None


def test_the_turns_come_back_sorted_by_start_time(tmp_path):
    diarizer = make(tmp_path, 'succeed(turns=[[3.0, 5.0, "B"], [0.0, 2.0, "A"], [0.0, 1.0, "C"]])')
    assert [t.speaker for t in run(diarizer).turns] == ["C", "A", "B"]


def test_each_run_starts_a_fresh_worker_and_the_next_run_can_succeed_after_a_failure(tmp_path):
    marker = tmp_path / "second"
    body = f'''
        import pathlib
        marker = pathlib.Path({str(marker)!r})
        if marker.exists():
            succeed()
            sys.exit(0)
        marker.write_text("x")
        fail("load", "connection reset")
    '''
    diarizer = make(tmp_path, body)
    assert "connection reset" in failure_of(diarizer)
    assert run(diarizer).turns                          # 失敗不會被記住，下一次重新來過
    assert len(diarizer.spy.made) == 2 and diarizer.available() == (True, "")
    assert_finished(diarizer.spy)


def test_progress_is_forwarded_in_order_and_ends_with_the_number_of_voices(tmp_path):
    body = '''
        progress("載入說話者區分模型…")
        progress("區分說話者…（偵測人聲 5/10）")
        succeed()
    '''
    seen = []
    run(make(tmp_path, body), progress=seen)
    assert [status for _, status in seen[:2]] == ["載入說話者區分模型…", "區分說話者…（偵測人聲 5/10）"]
    assert seen[-1] == (1.0, "說話者區分完成（2 個聲音）")


def test_progress_counts_voices_not_turns(tmp_path):
    seen = []
    run(make(tmp_path, 'succeed(turns=[[0,1,"A"],[1,2,"B"],[2,3,"A"],[3,4,"A"]])'), progress=seen)
    assert "2 個聲音" in seen[-1][1]


def test_progress_callbacks_happen_on_the_callers_thread(tmp_path):
    """攔的 bug：讀 stderr 的執行緒直接呼叫 progress——工作的狀態從別的執行緒被改，
    取消旗標與進度的檢查會和主執行緒搶。"""
    threads = []
    caller = threading.get_ident()
    body = 'progress("a")\nprogress("b")\nsucceed()'
    run(make(tmp_path, body), progress=_Recorder(threads))
    assert threads and set(threads) == {caller}


class _Recorder(list):
    """append 時記下是哪個執行緒在呼叫。"""

    def __init__(self, threads):
        super().__init__()
        self._threads = threads

    def append(self, item):
        self._threads.append(threading.get_ident())
        super().append(item)


def test_progress_lines_that_are_garbled_are_ignored_not_fatal(tmp_path):
    body = '''
        print("SLIDEBOX_PROGRESS {broken", file=sys.stderr, flush=True)
        print("SLIDEBOX_PROGRESS " + json.dumps({"fraction": "lots", "text": "x"}), file=sys.stderr, flush=True)
        print("SLIDEBOX_PROGRESS " + json.dumps([1]), file=sys.stderr, flush=True)
        print("SLIDEBOX_PROGRESS " + json.dumps({"fraction": None, "text": 5}), file=sys.stderr, flush=True)
        print("SLIDEBOX_PROGRESS " + json.dumps({"fraction": None}), file=sys.stderr, flush=True)
        print("some library warning", file=sys.stderr, flush=True)
        progress("真的進度")
        succeed()
    '''
    seen = []
    run(make(tmp_path, body), progress=seen)
    assert [status for _, status in seen] == ["真的進度", "說話者區分完成（2 個聲音）"]


def test_the_progress_text_never_carries_the_token_even_if_the_worker_forgot_to_scrub(tmp_path):
    seen = []
    run(make(tmp_path, 'progress("連線 @TOKEN@ 失敗")\nsucceed()'), progress=seen)
    assert all(TOKEN not in status for _, status in seen) and any("***" in status for _, status in seen)


def test_a_flood_of_progress_lines_does_not_block_the_worker(tmp_path):
    """攔的 bug：沒有人讀 stderr 的管線塞滿之後，工作行程卡在寫入，永遠不結束——死結。"""
    body = '''
        for i in range(5000):
            print("noise " + "x" * 200, file=sys.stderr, flush=True)
            if i % 50 == 0:
                progress(f"step {i}")
        succeed()
    '''
    seen = []
    assert run(make(tmp_path, body), progress=seen).turns
    assert len(seen) >= 100


def test_progress_lines_written_right_before_the_worker_exits_are_still_delivered(tmp_path):
    """攔的 bug：迴圈一看到行程結束就離開，讀 stderr 的執行緒還沒送完的最後幾行進度被丟掉。
    行程在還給呼叫者之前就結束了，它寫的進度全都還留在管線裡。"""
    spy = Spy()
    body = 'for i in range(5):\n    progress(f"step {i}")\nsucceed()'
    seen = []
    run(make(tmp_path, body, popen=already_finished(spy)), progress=seen)
    assert [text for _, text in seen if text.startswith("step")] == [f"step {i}" for i in range(5)]
    assert_finished(spy)


def test_the_stderr_reader_keeps_the_last_ordinary_lines_capped_and_closes_the_pipe():
    """進度行只進佇列；其他的留最後幾行給當掉時看（每行有長度上限：一個沒有換行的巨大輸出不能撐爆記憶體）；
    結束時把管線關掉。"""
    import io
    import queue
    from collections import deque

    progress_line = 'SLIDEBOX_PROGRESS {"fraction": 0.5, "text": "half"}'
    stream = io.StringIO("\n".join([*(f"warning {i}" for i in range(30)), progress_line, "", "x" * 5000, ""]))
    events, tail = queue.Queue(), deque(maxlen=mod.STDERR_TAIL_LINES)

    mod._pump(stream, events, tail)

    assert events.get_nowait() == (0.5, "half") and events.empty()
    assert len(tail) == mod.STDERR_TAIL_LINES                                # 只留最後幾行
    assert list(tail)[-2:] == ["warning 29", "x" * 500]                      # 進度行不在裡面、空行不留、超長的行截短
    assert stream.closed


def test_a_crash_message_quotes_what_the_worker_said_not_its_progress_lines(tmp_path):
    body = ('for i in range(6):\n    progress(f"step {i}")\n'
            'print("Segmentation fault (core dumped)", file=sys.stderr, flush=True)\nos._exit(139)')
    message = failure_of(make(tmp_path, body))
    assert "Segmentation fault" in message and "step" not in message and "SLIDEBOX_PROGRESS" not in message


# --- 區分：工作行程拿到什麼 ---


def test_the_worker_is_started_isolated_with_the_arguments_it_needs_and_no_token_on_the_command_line(
        tmp_path, monkeypatch):
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    diarizer = make(tmp_path, 'dump()\nsucceed()', device="auto", model="org/the-model")
    run(diarizer, speakers=3)
    seen = json.loads(dump.read_text(encoding="utf-8"))
    command, _ = diarizer.spy.calls[0]
    assert command[:3] == [sys.executable, "-I", "-X"] and command[3] == "utf8"       # 隔離模式、UTF-8
    assert command[4].endswith("fake_worker.py")                                   # 然後才是子命令與選項
    argv = seen["argv"][1:]
    assert argv[0] == "run"
    options = dict(zip(argv[1::2], argv[2::2]))
    assert options["--audio"] == "audio.webm" and options["--model"] == "org/the-model"
    assert options["--device"] == "auto" and options["--ffmpeg"] == "ffmpeg-for-test"
    assert options["--speakers"] == "3" and options["--result"].endswith("result.json")
    assert seen["flags"] == 1                                                      # sys.flags.isolated
    assert all(TOKEN not in part for part in command)                              # token 不在命令列（ps 看得到）


def test_no_speaker_count_means_no_option_at_all(tmp_path, monkeypatch):
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    run(make(tmp_path, 'dump()\nsucceed()'), speakers=None)
    assert "--speakers" not in json.loads(dump.read_text(encoding="utf-8"))["argv"]


def test_the_token_goes_to_the_worker_only_through_its_environment(tmp_path, monkeypatch):
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    run(make(tmp_path, 'dump()\nsucceed()'))
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert seen["env"][mod.TOKEN_ENV] == TOKEN
    assert TOKEN not in " ".join(seen["argv"])


def test_the_worker_never_sees_the_main_environments_library_path(tmp_path, monkeypatch):
    """這個 commit 存在的理由：LD_LIBRARY_PATH 指向主環境的 cuDNN 12（給 ctranslate2 用），
    工作行程裡的 torch 要的是 cuDNN 13，撿到錯的那份就載不起來或當掉。PYTHONPATH／PYTHONHOME 同理：
    主環境的程式與套件不能漏進第二個環境。"""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib")
    monkeypatch.setenv("PYTHONPATH", "/app/src")
    monkeypatch.setenv("PYTHONHOME", "/app/.venv")
    run(make(tmp_path, 'dump()\nsucceed()'))
    seen = json.loads(dump.read_text(encoding="utf-8"))["env"]
    assert not [name for name in seen if name.upper() in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")]


def test_the_worker_does_not_get_the_services_api_key_but_keeps_the_ordinary_settings(tmp_path, monkeypatch):
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    monkeypatch.setenv("SLIDEBOX_API_KEY", "the-api-key-0001")
    monkeypatch.setenv("HF_HOME", "/data/hf")
    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "0")
    run(make(tmp_path, 'dump()\nsucceed()'))
    seen = json.loads(dump.read_text(encoding="utf-8"))["env"]
    assert "SLIDEBOX_API_KEY" not in seen
    assert seen["HF_HOME"] == "/data/hf" and seen["PYANNOTE_METRICS_ENABLED"] == "0" and seen.get("PATH")


def test_a_library_path_spelled_in_another_case_is_dropped_too(tmp_path, monkeypatch):
    """Windows 的環境變數名稱不分大小寫。"""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    environ = dict(os.environ, Ld_Library_Path="/x", pythonpath="/y")
    monkeypatch.setattr(os, "environ", environ)
    run(make(tmp_path, 'dump()\nsucceed()'))
    seen = json.loads(dump.read_text(encoding="utf-8"))["env"]
    assert not [name for name in seen if name.upper() in ("LD_LIBRARY_PATH", "PYTHONPATH")]


def test_the_result_folder_is_removed_after_every_kind_of_ending(tmp_path, isolated_environment):
    for body, outcome in (("succeed()", None), ("fail('run', 'x')", DiarizationUnavailable),
                          ("sys.exit(9)", DiarizationUnavailable)):
        diarizer = make(tmp_path, body)
        if outcome is None:
            run(diarizer)
        else:
            with pytest.raises(outcome):
                run(diarizer)
        assert list(isolated_environment.iterdir()) == [], f"{body} 之後留下了暫存檔"


def test_the_worker_is_started_without_a_console_to_read_from(tmp_path):
    diarizer = make(tmp_path)
    run(diarizer)
    _, kwargs = diarizer.spy.calls[0]
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stdout"] == subprocess.DEVNULL


def test_ffmpeg_is_resolved_by_this_side_and_handed_over_by_path(tmp_path, monkeypatch):
    """imageio-ffmpeg 在主環境裡，第二個環境沒有：路徑由這一側找好、用 --ffmpeg 交給工作行程。"""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("TEST_DUMP", str(dump))
    run(make(tmp_path, 'dump()\nsucceed()', ffmpeg=lambda: "/usr/bin/ffmpeg"))
    argv = json.loads(dump.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--ffmpeg") + 1] == "/usr/bin/ffmpeg"


def test_a_missing_ffmpeg_is_a_readable_failure_before_anything_is_started(tmp_path):
    def broken():
        raise RuntimeError(f"no ffmpeg anywhere (token {TOKEN})")

    diarizer = make(tmp_path, ffmpeg=broken)
    message = failure_of(diarizer)
    assert "ffmpeg" in message and TOKEN not in message and diarizer.spy.made == []


# --- 區分：失敗的訊息（照階段、照在哪個裝置上說不同的話）---


def test_the_terms_failure_asks_for_the_model_terms_and_names_the_model(tmp_path):
    message = failure_of(make(tmp_path, 'fail("terms", device="")', model="org/the-model"))
    assert message.startswith("無法載入") and "使用條款" in message and "org/the-model" in message
    assert "HUGGINGFACE_TOKEN" in message


def test_a_failure_while_loading_asks_about_the_model_terms(tmp_path):
    """載入階段的失敗才是條款（或 token 權限）的問題，這句話要留在那裡。"""
    message = failure_of(make(tmp_path, 'fail("load", "403 Client Error: gated repo", device="")', device="cuda"))
    assert "403 Client Error" in message and "使用條款" in message and "pyannote.audio" in message


def test_a_load_failure_points_at_the_cpu_escape_hatch_unless_cpu_is_already_chosen(tmp_path):
    body = 'fail("load", "CUDA driver version is insufficient", device="")'
    assert "SLIDEBOX_DIARIZATION_DEVICE=cpu" in failure_of(make(tmp_path, body, device="auto"))
    assert "SLIDEBOX_DIARIZATION_DEVICE=cpu" in failure_of(make(tmp_path, body, device="cuda"))
    assert "SLIDEBOX_DIARIZATION_DEVICE" not in failure_of(make(tmp_path, body, device="cpu"))


def test_a_failure_while_running_does_not_blame_the_model_terms(tmp_path):
    """攔的 bug：跑到一半的失敗（例如顯示卡記憶體不足）也被勸「請確認已接受模型條款」。
    模型都載入成功了，條款一定接受過；操作者照著建議回去查 token，白費工夫。"""
    body = 'fail("run", "cuDNN error: CUDNN_STATUS_EXECUTION_FAILED", device="cuda")'
    message = failure_of(make(tmp_path, body, device="cuda"))
    assert "CUDNN_STATUS_EXECUTION_FAILED" in message and "SLIDEBOX_DIARIZATION_DEVICE=cpu" in message
    assert "條款" not in message


def test_cuda_running_out_of_memory_is_named_as_such_and_points_at_vram_sharing(tmp_path):
    """顯示卡同時給 Ollama、語音辨識與說話者區分用；跑到一半最像的失敗就是記憶體不夠。"""
    body = 'fail("run", "CUDA out of memory. Tried to allocate 2.00 GiB", oom=True, device="cuda")'
    message = failure_of(make(tmp_path, body, device="cuda"))
    assert "顯示卡記憶體不足" in message and "Ollama" in message and "2.00 GiB" in message
    assert "SLIDEBOX_DIARIZATION_DEVICE=cpu" in message and "條款" not in message


def test_the_cpu_hint_is_left_out_when_the_run_was_already_on_the_cpu(tmp_path):
    """對照：已經在 CPU 上了，還勸人「改用 CPU」只會讓人困惑；CPU 上的記憶體不足也不是顯示卡的事。"""
    message = failure_of(make(tmp_path, 'fail("run", "something broke", device="cpu")', device="auto"))
    assert "something broke" in message and "SLIDEBOX_DIARIZATION_DEVICE" not in message
    oom = failure_of(make(tmp_path, 'fail("run", "out of memory", oom=True, device="cpu")', device="auto"))
    assert "顯示卡" not in oom and "SLIDEBOX_DIARIZATION_DEVICE" not in oom


def test_a_decode_failure_is_passed_on_as_the_workers_own_words(tmp_path):
    message = failure_of(make(tmp_path, 'fail("decode", "音訊解碼失敗：moov atom not found")'))
    assert message == "音訊解碼失敗：moov atom not found"


def test_no_voice_at_all_is_a_failure(tmp_path):
    assert "人聲" in failure_of(make(tmp_path, 'fail("empty")'))


def test_an_unknown_stage_is_still_a_readable_failure(tmp_path):
    message = failure_of(make(tmp_path, 'fail("brand-new-stage", "weird")'))
    assert "weird" in message and "說話者區分失敗" in message


def test_only_a_failure_while_running_is_called_a_graphics_memory_problem(tmp_path):
    """對照：oom 旗標只在「模型已載入、跑到一半」這一步才解讀成顯示卡記憶體不足；
    不認得的階段不能憑一個旗標就下結論，叫人去調 VRAM。"""
    message = failure_of(make(tmp_path, 'fail("brand-new-stage", "weird", oom=True, device="cuda")', device="cuda"))
    assert "weird" in message and "顯示卡記憶體不足" not in message


def test_the_token_never_appears_in_any_failure_message_even_from_a_worker_that_forgot_to_scrub(tmp_path):
    """攔的 bug：Hugging Face 或 pyannote 的錯誤訊息帶著 token（網址、標頭），一路進了工作的 error
    欄位，再被 Pi 存進資料庫、印在畫面上。這個專案的 repo 是公開的，任務紀錄也會被分享。
    這一側不信任另一個行程的文字：自己再遮一次。"""
    for stage in ("load", "run", "decode", "brand-new-stage"):
        body = f'fail("{stage}", "401 for https://huggingface.co/x with Authorization: Bearer @TOKEN@")'
        message = failure_of(make(tmp_path, body, device="cuda"))
        assert TOKEN not in message and "hf_FAKE" not in message, stage


def test_a_configured_token_that_does_not_look_like_hf_is_still_scrubbed(tmp_path):
    odd = "plain-secret-value-0001"
    body = f'fail("run", "denied for {odd}")'
    assert odd not in failure_of(make(tmp_path, body, token=odd))


def test_a_different_hf_token_in_an_error_is_scrubbed_too(tmp_path):
    stray = "hf_STRAYtokenSTRAYtoken9876543210"
    message = failure_of(make(tmp_path, f'fail("run", "cached credential {stray} was rejected")'))
    assert stray not in message and "hf_STRAY" not in message


def test_failure_text_is_one_short_line(tmp_path):
    """攔的 bug：底層錯誤動輒數百字、含換行，原樣塞進狀態列與工作失敗訊息。"""
    body = 'fail("run", "first line\\n" + "x" * 500)'
    message = failure_of(make(tmp_path, body))
    assert "\n" not in message and "first line" in message and "x" * 300 not in message and "…" in message


# --- 區分：當掉、亂回答 ---


def test_a_worker_that_dies_without_a_result_is_reported_with_its_exit_code_and_last_words(tmp_path):
    body = '''
        print("loading weights", file=sys.stderr, flush=True)
        print("FATAL: cuDNN error: CUDNN_STATUS_NOT_INITIALIZED", file=sys.stderr, flush=True)
        sys.exit(7)
    '''
    message = failure_of(make(tmp_path, body, device="cuda"))
    assert "異常結束" in message and "結束碼 7" in message and "CUDNN_STATUS_NOT_INITIALIZED" in message
    assert "SLIDEBOX_DIARIZATION_DEVICE=cpu" in message          # 原生的當掉最常出在驅動／CUDA：逃生口適用


def test_a_crash_on_the_cpu_does_not_suggest_the_cpu(tmp_path):
    assert "SLIDEBOX_DIARIZATION_DEVICE" not in failure_of(make(tmp_path, "sys.exit(7)", device="cpu"))


def test_a_crash_message_has_only_the_last_few_lines_and_no_token(tmp_path):
    body = '''
        for i in range(30):
            print(f"line {i}", file=sys.stderr, flush=True)
        print("token was @TOKEN@", file=sys.stderr, flush=True)
        sys.exit(1)
    '''
    message = failure_of(make(tmp_path, body))
    assert "line 29" in message and "line 0" not in message and "line 20" not in message
    assert TOKEN not in message and "hf_FAKE" not in message and "\n" not in message


def test_a_worker_that_says_nothing_before_it_dies_is_still_reported(tmp_path):
    assert "沒有留下任何訊息" in failure_of(make(tmp_path, "sys.exit(5)"))


def test_a_worker_that_exits_cleanly_without_a_result_is_a_failure_not_a_success(tmp_path):
    message = failure_of(make(tmp_path, "pass"))
    assert "沒有留下結果" in message


@pytest.mark.parametrize("code, expected", [(-9, "SIGKILL"), (-11, "SIGSEGV"), (-6, "SIGABRT"), (-15, "SIGTERM"),
                                            (3, "結束碼 3"), (0, "結束碼 0"), (139, "結束碼 139")])
def test_how_the_worker_ended_is_described_in_words(code, expected):
    """負的結束碼是 POSIX 上被訊號終止（編號用 Linux 的，與目前在哪個平台無關：容器是 Linux）。"""
    assert expected in mod.describe_exit(code)


def test_being_killed_by_sigkill_hints_at_the_host_running_out_of_memory():
    assert "記憶體" in mod.describe_exit(-9)


def test_an_unknown_signal_number_is_still_described():
    assert "999" in mod.describe_exit(-999)


def test_a_complete_result_wins_over_a_crash_while_the_process_was_shutting_down(tmp_path, caplog):
    """攔的 bug：CUDA 的行程常在結束時（解構）才 segfault。結果檔是先寫暫存檔再換名的，檔案在就是完整的：
    整支影片已經跑完的工作，不該因為收尾時的當掉而全部丟掉。但要留一筆 log。"""
    body = 'succeed()\nsys.exit(139)'
    with caplog.at_level(logging.WARNING, logger="slidebox.infrastructure.pyannote_diarizer"):
        output = run(make(tmp_path, body))
    assert len(output.turns) == 2
    assert any("139" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("content", ["not json", "[]", '{"protocol": 1}', '{"protocol": 1, "ok": "yes"}', "",
                                     '{"protocol": 2, "ok": true, "device": "cpu", "turns": [[0, 1, "A"]], '
                                     '"overlap_seconds": 0}'])
def test_a_result_file_that_makes_no_sense_is_a_readable_failure(tmp_path, content):
    body = f'open(arg("--result"), "w", encoding="utf-8").write({content!r})'
    message = failure_of(make(tmp_path, body))
    assert "無法理解" in message or "版本不一致" in message


def _bad(**overrides):
    base = {"ok": True, "device": "cuda", "turns": [[0.0, 2.0, "A"]], "overlap_seconds": 0.5,
            "gpu_peak_bytes": None, "peak_rss_bytes": None}
    return {**base, **overrides}


@pytest.mark.parametrize("payload", [
    _bad(turns=[]),                                      # 成功卻沒有任何區段：不能當成一個空的結果
    _bad(turns="nothing"),
    _bad(turns=[[0.0, 2.0]]),                            # 少一欄
    _bad(turns=[[0.0, 2.0, "A", "extra"]]),
    _bad(turns=[[2.0, 1.0, "A"]]),                       # 倒著的區段
    _bad(turns=[[1.0, 1.0, "A"]]),                       # 長度是零
    _bad(turns=[[-1.0, 1.0, "A"]]),                      # 負的起點
    _bad(turns=[["0", 2.0, "A"]]),                       # 時間不是數字
    _bad(turns=[[True, 2.0, "A"]]),                      # 布林不是數字
    _bad(turns=[[0.0, None, "A"]]),
    _bad(turns=[[0.0, 2.0, ""]]),                        # 沒有標籤：不知道是誰
    _bad(turns=[[0.0, 2.0, 7]]),                         # 標籤不是文字
    _bad(turns=[[0.0, 2.0, None]]),
    _bad(overlap_seconds=-1),
    _bad(overlap_seconds="lots"),
    _bad(overlap_seconds=None),
    _bad(device="tpu"),
    _bad(device=None),
    _bad(gpu_peak_bytes=-5),
    _bad(gpu_peak_bytes="big"),
    _bad(peak_rss_bytes=1.5),
    _bad(ok="yes"),                                      # ok 不是布林：「成功」是什麼意思不能靠猜
    _bad(ok=1),
    _bad(ok=None),
])
def test_a_success_the_worker_could_not_have_written_is_refused_instead_of_trusted(tmp_path, payload):
    """歸屬正確是第一優先：誰在哪段時間說話，寧可整個失敗也不猜。壞掉的區段（倒著的、沒有標籤的、
    不是數字的）不能被悄悄丟掉剩下的照用——那會讓後面的對齊把字歸給錯的人。"""
    body = f'result(**{payload!r})'
    message = failure_of(make(tmp_path, body))
    assert "無法理解" in message


def test_non_finite_numbers_in_a_result_file_are_refused(tmp_path):
    """Python 的 json 讀得進 NaN／Infinity（雖然寫不出去）：一個 NaN 的時間會讓對齊整個亂掉。"""
    for text in ('[[0.0, NaN, "A"]]', '[[0.0, Infinity, "A"]]', '[[-Infinity, 1.0, "A"]]'):
        raw = '{"protocol": 1, "ok": true, "device": "cuda", "turns": ' + text + ', "overlap_seconds": 0}'
        body = f'open(arg("--result"), "w", encoding="utf-8").write({raw!r})'
        assert "無法理解" in failure_of(make(tmp_path, body)), text
    raw = '{"protocol": 1, "ok": true, "device": "cuda", "turns": [[0, 1, "A"]], "overlap_seconds": NaN}'
    assert "無法理解" in failure_of(make(tmp_path, f'open(arg("--result"), "w", encoding="utf-8").write({raw!r})'))


def test_a_failure_the_worker_could_not_have_written_is_refused_too(tmp_path):
    for payload in ({"ok": False}, {"ok": False, "stage": 5, "detail": "x"}, {"ok": False, "stage": "run", "detail": 5},
                    {"ok": False, "stage": "", "detail": "x"}):          # 沒有說是哪一步失敗的，不能憑空編一個
        body = f'result(**{payload!r})'
        assert "無法理解" in failure_of(make(tmp_path, body))


# --- 取消與逾時：行程真的被殺掉、也被收掉 ---


def test_cancelling_before_anything_starts_starts_nothing(tmp_path):
    diarizer = make(tmp_path)
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=lambda: True)
    assert diarizer.spy.made == []


def test_cancelling_in_the_middle_kills_the_worker_and_reaps_it(tmp_path):
    """攔的 bug：只在呼叫前後檢查取消。pipeline 一跑就是幾分鐘（長影片更久），取消要等到它自己跑完；
    行程內的版本連跑到一半卡住（CUDA 死鎖）的 pipeline 都殺不掉。現在取消就是把行程殺掉。"""
    started = threading.Event()
    body = 'progress("started")\ntime.sleep(120)\nsucceed()'
    diarizer = make(tmp_path, body)
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=started.is_set, progress=_Flag(started))
    assert_finished(diarizer.spy)
    assert diarizer.spy.made[0].returncode != 0


class _Flag(list):
    def __init__(self, event):
        super().__init__()
        self._event = event

    def append(self, item):
        super().append(item)
        self._event.set()


def test_a_cancel_that_arrives_after_the_worker_finished_still_wins(tmp_path):
    """工作行程已經交出完整的結果、取消才到（Pi 已經放棄這個工作）：取消照樣算，不能回一個成功。
    行程在還給呼叫者之前就結束了，等待的迴圈第一次看就是「跑完了」，沒有機會在迴圈裡抓到取消——
    只有跑完之後的那一次檢查抓得到。"""
    spy = Spy()
    asked = []

    def cancelled():
        asked.append(1)
        return len(asked) >= 3                    # 第 1 次：開始前；第 2 次：迴圈的第一次檢查；第 3 次：行程跑完之後

    diarizer = make(tmp_path, popen=already_finished(spy))
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=cancelled)
    assert len(asked) == 3
    assert_finished(spy)


def test_cancelling_the_worker_leaves_no_result_folder_behind(tmp_path, isolated_environment):
    started = threading.Event()
    diarizer = make(tmp_path, 'progress("started")\ntime.sleep(120)')
    with pytest.raises(OperationCancelled):
        run(diarizer, cancelled=started.is_set, progress=_Flag(started))
    assert list(isolated_environment.iterdir()) == []


def test_a_worker_that_runs_too_long_is_killed_and_the_failure_says_how_to_raise_the_limit(tmp_path):
    clock = Clock()
    started = threading.Event()

    class Advance(_Flag):
        def append(self, item):
            clock.now += 10 ** 6                           # 時間一下子過了很久
            super().append(item)

    diarizer = make(tmp_path, 'progress("started")\ntime.sleep(120)\nsucceed()', clock=clock, timeout=3600)
    message = failure_of(diarizer, progress=Advance(started))
    assert "3600" in message and "SLIDEBOX_DIARIZATION_TIMEOUT_SECONDS" in message and "中止" in message
    assert_finished(diarizer.spy)


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_time_limit_means_the_default_limit_not_no_limit(tmp_path, monkeypatch, value):
    """攔的 bug：留空被當成「不設上限」——compose 預設就是留空，卡死的行程於是永遠佔著顯示卡。"""
    if value is not None:
        monkeypatch.setenv(mod.TIMEOUT_ENV, value)
    clock, started = Clock(), threading.Event()

    class Advance(_Flag):
        def append(self, item):
            clock.now += mod.DEFAULT_TIMEOUT_SECONDS + 1
            super().append(item)

    diarizer = make(tmp_path, 'progress("started")\ntime.sleep(120)\nsucceed()', clock=clock)
    message = failure_of(diarizer, progress=Advance(started))
    assert str(mod.DEFAULT_TIMEOUT_SECONDS) in message
    assert_finished(diarizer.spy)


def test_the_time_limit_counts_from_the_start_of_the_run_not_from_zero(tmp_path):
    """攔的 bug：期限算成「上限的秒數」而不是「現在加上限」——單調時鐘早就走過那個數字了，
    每個工作一開始就逾時。時鐘從 100 秒開始、上限 3600：走到 3650 還沒到。"""
    clock = Clock()

    class Advance(list):
        def append(self, item):
            clock.now += 3550
            super().append(item)

    diarizer = make(tmp_path, 'progress("a")\ntime.sleep(1)\nsucceed()', clock=clock, timeout=3600)
    assert run(diarizer, progress=Advance()).turns


def test_the_default_limit_is_generous_and_a_zero_means_no_limit(tmp_path):
    assert mod.DEFAULT_TIMEOUT_SECONDS == 6 * 3600
    clock = Clock()

    class Advance(list):
        def append(self, item):
            clock.now += 10 ** 9
            super().append(item)

    diarizer = make(tmp_path, 'progress("a")\ntime.sleep(1)\nsucceed()', clock=clock, timeout=0)
    assert run(diarizer, progress=Advance()).turns                # 時間過了十億秒，照樣等它做完


def test_a_progress_callback_that_raises_does_not_leave_the_worker_running(tmp_path):
    """攔的 bug：回呼丟出例外（任何例外）就離開迴圈，行程卻還在跑——GPU 被一個沒人等的工作佔著。"""
    class Boom(list):
        def append(self, item):
            raise RuntimeError("callback broke")

    diarizer = make(tmp_path, 'progress("a")\ntime.sleep(120)')
    with pytest.raises(RuntimeError, match="callback broke"):
        run(diarizer, progress=Boom())
    assert_finished(diarizer.spy)


def test_a_cancel_check_that_raises_does_not_leave_the_worker_running(tmp_path):
    calls = []

    def broken():
        calls.append(1)
        if len(calls) > 3:
            raise RuntimeError("cancel check broke")
        return False

    diarizer = make(tmp_path, 'progress("a")\ntime.sleep(120)')
    with pytest.raises(RuntimeError, match="cancel check broke"):
        run(diarizer, cancelled=broken)
    assert_finished(diarizer.spy)


def test_a_worker_that_cannot_be_started_is_a_readable_failure(tmp_path):
    def refusing(command, **kwargs):
        raise PermissionError(f"[Errno 13] Permission denied: {command[0]!r} (token {TOKEN})")

    diarizer = make(tmp_path, popen=refusing)
    message = failure_of(diarizer)
    assert "SLIDEBOX_DIARIZATION_PYTHON" in message and "Permission denied" in message and TOKEN not in message


# --- 殺行程的方式（POSIX 要殺整個行程群組，不能只殺工作行程自己）---


class DyingProcess:
    pid = 4242
    returncode = None

    def __init__(self):
        self.events = []
        self._alive = True

    def poll(self):
        return None if self._alive else -9

    def kill(self):
        self.events.append("kill")
        self._alive = False

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        self._alive = False


def test_on_posix_the_whole_process_group_is_killed_then_waited_for(monkeypatch):
    """攔的 bug：只殺工作行程。它底下的 ffmpeg（解碼音訊）還在跑，PyTorch 的執行緒池與
    子行程也可能還在——GPU 與主機記憶體被占著，下一個工作一起來就撞上。"""
    killed = []
    monkeypatch.setattr(mod, "_POSIX", True)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)), raising=False)
    process = DyingProcess()
    mod.terminate(process)
    assert killed == [(4242, getattr(signal, "SIGKILL", 9))]
    assert process.events == [("wait", mod.KILL_WAIT_SECONDS)] and "kill" not in process.events


def test_on_windows_the_process_is_killed_directly_then_waited_for(monkeypatch):
    monkeypatch.setattr(mod, "_POSIX", False)
    process = DyingProcess()
    mod.terminate(process)
    assert process.events == ["kill", ("wait", mod.KILL_WAIT_SECONDS)]


def test_terminating_a_process_that_already_ended_does_nothing(monkeypatch):
    monkeypatch.setattr(mod, "_POSIX", True)
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("已經結束的行程不用再殺"), raising=False)
    process = DyingProcess()
    process._alive = False
    mod.terminate(process)
    assert process.events == []


def test_a_group_that_is_already_gone_is_not_an_error(monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(mod, "_POSIX", True)
    monkeypatch.setattr(os, "killpg", gone, raising=False)
    process = DyingProcess()
    mod.terminate(process)
    assert process.events == [("wait", mod.KILL_WAIT_SECONDS)]


def test_a_process_that_will_not_die_is_logged_not_waited_for_forever(monkeypatch, caplog):
    class Stubborn(DyingProcess):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("python", timeout)

    monkeypatch.setattr(mod, "_POSIX", False)
    with caplog.at_level(logging.WARNING, logger="slidebox.infrastructure.pyannote_diarizer"):
        mod.terminate(Stubborn())
    assert any("仍未結束" in record.getMessage() for record in caplog.records)


def test_on_posix_the_worker_gets_its_own_process_group_so_the_group_kill_reaches_only_it(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_POSIX", True)
    diarizer = make(tmp_path)
    run(diarizer)
    _, kwargs = diarizer.spy.calls[0]
    assert kwargs.get("start_new_session") is True


def test_on_windows_no_posix_only_option_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_POSIX", False)
    diarizer = make(tmp_path)
    run(diarizer)
    _, kwargs = diarizer.spy.calls[0]
    assert "start_new_session" not in kwargs


# --- 父行程與真的工作行程：用同一份協定 ---

# 取代 PyannoteEngine 要的 pipeline、torch、解碼器，其餘（參數、環境變數、進度、結果檔、結束碼）全是真的
CONTRACT_DRIVER = '''
import importlib.util, os, sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("real_worker", @WORKER@)
module = importlib.util.module_from_spec(spec)
sys.modules["real_worker"] = module
spec.loader.exec_module(module)
MODE = os.environ.get("CONTRACT_MODE", "ok")


class Annotation:
    def __init__(self, *tracks):
        self._tracks = tracks

    def itertracks(self, yield_label=False):
        for start, end, label in self._tracks:
            yield SimpleNamespace(start=start, end=end), None, label


class Pipeline:
    def __init__(self, token):
        self.token = token

    def to(self, device):
        return self

    def __call__(self, audio, hook=None, num_speakers=None, **kw):
        hook("segmentation", None, total=3, completed=1)
        if MODE == "oom":
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        tag = f"{self.token[:3]}-{num_speakers}"
        exclusive = Annotation((0.0, 2.0, tag), (3.0, 5.0, "SPEAKER_B"))
        full = Annotation((0.0, 2.0, tag), (1.5, 5.0, "SPEAKER_B"))
        return SimpleNamespace(exclusive_speaker_diarization=exclusive, speaker_diarization=full)


def factory(model, token="", device="auto", ffmpeg_exe="ffmpeg"):
    def build(name, token):
        return None if MODE == "terms" else Pipeline(token)

    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True, max_memory_allocated=lambda: 777),
                            device=lambda name: name)
    return module.PyannoteEngine(model, token=token, device=device, ffmpeg_exe=ffmpeg_exe, pipeline_factory=build,
                                 torch_module=torch, audio_loader=lambda path: {"waveform": "W", "sample_rate": 16000})


sys.exit(module.main(sys.argv[1:], engine_factory=factory))
'''


def contract_diarizer(tmp_path, monkeypatch, mode="ok", device="cuda"):
    driver = tmp_path / "contract_driver.py"
    driver.write_text(CONTRACT_DRIVER.replace("@WORKER@", repr(str(REAL_WORKER))), encoding="utf-8")
    monkeypatch.setenv("CONTRACT_MODE", mode)
    spy = Spy()
    diarizer = PyannoteSpeakerDiarizer(
        model_name="org/model", token=TOKEN, device=device, mode="pyannote", python=sys.executable,
        worker_path=str(driver), spec_finder=lambda name: object(), version_finder=lambda: "4.0.7",
        ffmpeg_finder=lambda: "ffmpeg-for-test", popen=spy, clock=Clock())
    diarizer.spy = spy
    return diarizer


def test_the_parent_and_the_real_worker_agree_on_the_protocol(tmp_path, monkeypatch):
    """攔的 bug：兩邊各自改了協定（進度行的前綴、結果檔的欄位、結束碼、參數名稱），單獨測都是綠的，
    湊在一起才知道不通——而湊在一起只有在 GPU 主機的 Docker 裡才會發生。這裡用真的 pyannote_worker.py
    （引擎換成假的 pipeline）經由真的行程、真的參數、真的環境變數、真的結果檔走一遍。"""
    seen = []
    diarizer = contract_diarizer(tmp_path, monkeypatch)
    output = run(diarizer, speakers=3, progress=seen)
    assert output.turns == (SpeakerTurn(0.0, 2.0, "hf_-3"), SpeakerTurn(3.0, 5.0, "SPEAKER_B"))   # token 與人數都到了
    assert output.overlap_seconds == 0.5
    assert diarizer.device == "cuda" and diarizer.gpu_peak_bytes == 777
    assert diarizer.worker_peak_bytes is None or diarizer.worker_peak_bytes > 0         # Windows 量不到
    statuses = [status for _, status in seen]
    assert statuses[0].startswith("載入") and "已載入（cuda）" in statuses[1]
    assert any("偵測人聲 1/3" in status for status in statuses) and statuses[-1] == "說話者區分完成（2 個聲音）"
    assert_finished(diarizer.spy)


def test_the_real_worker_reports_the_terms_not_accepted(tmp_path, monkeypatch):
    message = failure_of(contract_diarizer(tmp_path, monkeypatch, "terms"))
    assert message.startswith("無法載入 org/model") and "使用條款" in message


def test_the_real_worker_reports_cuda_running_out_of_memory(tmp_path, monkeypatch):
    message = failure_of(contract_diarizer(tmp_path, monkeypatch, "oom"))
    assert "顯示卡記憶體不足" in message and "Tried to allocate" in message and "條款" not in message


def test_the_real_worker_on_the_cpu_does_not_get_the_cpu_hint(tmp_path, monkeypatch):
    message = failure_of(contract_diarizer(tmp_path, monkeypatch, "oom", device="cpu"))
    assert "SLIDEBOX_DIARIZATION_DEVICE" not in message


def test_the_real_workers_file_is_where_the_adapter_looks_for_it():
    assert Path(mod._WORKER_FILE).resolve() == REAL_WORKER.resolve() and REAL_WORKER.is_file()
