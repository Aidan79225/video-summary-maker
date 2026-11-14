"""GPU 映像裡的兩個 Python 環境（主環境與說話者區分用的第二環境）不能混在一起。

為什麼分開：語音辨識（ctranslate2）要 CUDA 12＋cuDNN 9，鎖檔裡的 PyTorch 要 CUDA 13＋cuDNN 9。兩個版本的
cuDNN 輪子都解到 site-packages/nvidia/cudnn/lib、檔名相同，放進同一個環境就會互相蓋掉，而 Dockerfile 的
LD_LIBRARY_PATH 又把那個資料夾指給 ctranslate2——最壞的情況是連既有的議會與單人工作的 Whisper 都壞掉。

這裡沒辦法建置映像（沒有 Docker、沒有 GPU），所以分三層守：
1. uv.lock 與 pyproject.toml：照 Dockerfile 的兩個 uv sync 算出兩個環境會裝哪些套件，套同一套規則
   （docker/check_environments.py，建置映像時也會用它檢查「真的裝了什麼」）
2. Dockerfile 的文字：兩次 uv sync 的旗標、LD_LIBRARY_PATH、PATH、工作行程要用的 Python、建置時的檢查
3. 檢查程式本身：給它混在一起的環境，它要叫出來

只讀文字、不啟動 Docker、不連網路。
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_FILE = ROOT / "docker" / "check_environments.py"
MAIN_VENV = "/app/.venv"
DIARIZE_VENV = "/opt/diarize-venv"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_environments", GUARD_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_environments"] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


# --- uv.lock：兩個環境會裝什麼 ---


def _lock() -> dict:
    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _root(lock: dict) -> dict:
    return next(p for p in lock["package"] if p["name"] == "yt-downloader")


def _closure(packages: dict, entries: list[dict]) -> set[str]:
    """從這些相依項出發，沿著相依關係（含 extras）走到底，回傳會裝的套件名稱。"""
    seen, todo = set(), list(entries)
    while todo:
        entry = todo.pop()
        key = (entry["name"], tuple(sorted(entry.get("extra", ()))))
        if key in seen:
            continue
        seen.add(key)
        package = packages.get(entry["name"])
        if package is None:
            continue
        todo.extend(package.get("dependencies", []))
        for extra in entry.get("extra", ()):
            todo.extend(package.get("optional-dependencies", {}).get(extra, []))
    return {guard.normalize(name) for name, _ in seen}


def _environments(main_groups=("api", "whisper-gpu"), diarize_groups=("diarization",)) -> tuple[set[str], set[str]]:
    """Dockerfile 的兩個 uv sync：主環境是專案本身加 api、whisper-gpu 群組；
    第二環境只有 diarization 群組（--only-group：不裝專案本身與它的相依套件）。"""
    lock = _lock()
    packages = {p["name"]: p for p in lock["package"]}
    root = _root(lock)
    groups = root["dev-dependencies"]
    main = _closure(packages, list(root["dependencies"]) + [e for g in main_groups for e in groups[g]])
    diarize = _closure(packages, [e for g in diarize_groups for e in groups[g]])
    return main, diarize


def test_the_lock_keeps_the_two_environments_apart():
    """攔的 bug：有人把 --group diarization 加回主環境的 uv sync（或鎖檔更新後兩邊的相依套件重疊了）——
    torch 與它的 CUDA 13 函式庫進了主環境，cuDNN 互相蓋掉。這裡套的是建置映像時也會跑的同一套規則。"""
    main, diarize = _environments()
    assert guard.check(main, diarize) == []


def test_the_lock_really_describes_both_environments():
    """上一個測試不能因為「兩邊都是空的」而通過。"""
    main, diarize = _environments()
    assert {"faster-whisper", "ctranslate2", "fastapi", "uvicorn", "nvidia-cudnn-cu12"} <= main
    assert {"torch", "pyannote-audio", "numpy", "nvidia-cudnn-cu13"} <= diarize
    assert len(main) > 20 and len(diarize) > 50


def test_extras_are_followed_when_working_out_what_an_environment_contains():
    """uvicorn[standard] 帶進 httptools 與 uvloop：算環境的時候不跟著 extras，主環境就少算一截。"""
    main, _ = _environments()
    assert "httptools" in main


def test_the_main_environments_cuda_packages_are_exactly_the_cu12_ones():
    """主環境要和沒有多人模式時完全一樣：CUDA 的東西只有 ctranslate2 載入時要的 cu12 那幾個。
    要改這一行就是有意的（連同 Dockerfile 的 LD_LIBRARY_PATH 一起想過）。"""
    main, _ = _environments()
    assert {n for n in main if n.startswith(("nvidia-", "cuda-"))} == {
        "nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12", "nvidia-cudnn-cu12"}
    assert not {n for n in main if n in ("torch", "triton", "pyannote-audio")}


def test_the_diarization_group_is_not_part_of_the_main_environment():
    lock = _lock()
    groups = _root(lock)["dev-dependencies"]
    assert [e["name"] for e in groups["diarization"]] == ["pyannote-audio"]
    assert "pyannote-audio" not in {e["name"] for g in ("api", "whisper-gpu") for e in groups[g]}
    assert "pyannote-audio" not in {e["name"] for e in _root(lock)["dependencies"]}


def test_the_check_would_notice_the_diarization_group_being_added_back_to_the_main_environment():
    """上面的規則真的會叫：照以前的做法（一個環境裝全部）算出來的主環境，一定被擋下。"""
    main, diarize = _environments(main_groups=("api", "whisper-gpu", "diarization"))
    problems = guard.check(main, diarize)
    text = "\n".join(problems)
    assert "torch" in text and "pyannote-audio" in text and "nvidia-cudnn-cu13" in text
    assert "主環境" in text


def test_the_check_would_notice_the_second_environment_getting_the_projects_own_dependencies():
    """--only-group 沒有生效（舊版的 uv 之類）：專案本身與它的相依套件被裝進第二個環境。"""
    _, diarize = _environments()
    main, _ = _environments()
    problems = guard.check(main, diarize | main)
    assert any("第二個環境" in p and "ctranslate2" in p for p in problems)
    assert any("-cu12" in p or "nvidia-cudnn-cu12" in p for p in problems)


# --- 檢查程式本身 ---

MAIN_OK = {"ctranslate2", "faster-whisper", "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12",
           "fastapi", "uvicorn"}
DIARIZE_OK = {"torch", "pyannote.audio", "numpy", "nvidia-cudnn-cu13", "nvidia-cublas", "cuda-toolkit"}


def test_separate_environments_pass():
    assert guard.check(MAIN_OK, DIARIZE_OK) == []


def test_names_are_compared_the_way_pip_does():
    """pyannote.audio、pyannote_audio、Pyannote-Audio 是同一個套件。"""
    assert guard.check(MAIN_OK, {"torch", "Pyannote_Audio"}) == []
    assert guard.normalize("Nvidia__CuDNN.cu12") == "nvidia-cudnn-cu12"


@pytest.mark.parametrize("extra", ["torch", "torchaudio", "torchcodec", "triton", "pyannote-audio",
                                   "nvidia-cudnn-cu13", "nvidia-cublas", "nvidia-nccl-cu13", "cuda-toolkit"])
def test_anything_of_the_second_environment_in_the_main_one_is_a_problem(extra):
    problems = guard.check(MAIN_OK | {extra}, DIARIZE_OK)
    assert len(problems) == 1 and extra in problems[0] and "主環境" in problems[0]


@pytest.mark.parametrize("missing", ["ctranslate2", "faster-whisper", "nvidia-cublas-cu12", "nvidia-cudnn-cu12"])
def test_a_main_environment_without_what_whisper_needs_is_a_problem(missing):
    problems = guard.check(MAIN_OK - {missing}, DIARIZE_OK)
    assert len(problems) == 1 and missing in problems[0]


@pytest.mark.parametrize("extra", ["nvidia-cudnn-cu12", "nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12"])
def test_a_cuda_12_package_in_the_second_environment_is_a_problem(extra):
    problems = guard.check(MAIN_OK, DIARIZE_OK | {extra})
    assert len(problems) == 1 and extra in problems[0] and "第二個環境" in problems[0]


@pytest.mark.parametrize("extra", ["ctranslate2", "faster-whisper", "fastapi", "uvicorn", "pyside6", "yt-dlp",
                                   "imageio-ffmpeg"])
def test_the_projects_own_packages_in_the_second_environment_are_a_problem(extra):
    problems = guard.check(MAIN_OK, DIARIZE_OK | {extra})
    assert len(problems) == 1 and extra in problems[0] and "--only-group" in problems[0]


@pytest.mark.parametrize("missing", ["torch", "pyannote.audio"])
def test_a_second_environment_without_the_engine_is_a_problem(missing):
    problems = guard.check(MAIN_OK, DIARIZE_OK - {missing})
    assert len(problems) == 1 and "第二個環境缺少" in problems[0]


def test_the_library_path_must_not_point_into_the_second_environment():
    problems = guard.check(MAIN_OK, DIARIZE_OK, ["/app/.venv/lib/nvidia/cudnn/lib",
                                                 "/opt/diarize-venv/lib/python3.13/site-packages/nvidia/cudnn/lib"])
    assert len(problems) == 1 and "LD_LIBRARY_PATH" in problems[0] and "/opt/diarize-venv" in problems[0]


def test_the_library_path_must_actually_contain_the_cuda_12_libraries():
    listing = {"/a": ["libcudnn.so.9", "libcudnn_ops.so.9"], "/b": ["libcublas.so.12", "libcublasLt.so.12"]}
    assert guard.check(MAIN_OK, DIARIZE_OK, ["/a", "/b"], lambda d: listing.get(d, [])) == []
    problems = guard.check(MAIN_OK, DIARIZE_OK, ["/a"], lambda d: listing.get(d, []))
    assert len(problems) == 1 and "libcublas.so.12" in problems[0]
    problems = guard.check(MAIN_OK, DIARIZE_OK, ["/missing"], lambda d: [])
    assert len(problems) == 2 and "libcudnn.so.9" in problems[0]


def test_the_command_line_reports_problems_with_exit_code_1(monkeypatch):
    import io

    answers = {"main-python": MAIN_OK | {"torch"}, "diarize-python": DIARIZE_OK}
    monkeypatch.setattr(guard, "installed", lambda python: answers[python])
    out, err = io.StringIO(), io.StringIO()
    code = guard.main(["main-python", "diarize-python"], environ={}, out=out, err=err)
    assert code == 1 and "torch" in err.getvalue() and "混在一起" in err.getvalue() and out.getvalue() == ""


def test_the_command_line_says_ok_with_exit_code_0(monkeypatch, tmp_path):
    import io

    (tmp_path / "libcudnn.so.9").write_text("")
    (tmp_path / "libcublas.so.12").write_text("")
    answers = {"main-python": MAIN_OK, "diarize-python": DIARIZE_OK}
    monkeypatch.setattr(guard, "installed", lambda python: answers[python])
    out = io.StringIO()
    code = guard.main(["main-python", "diarize-python"], environ={"LD_LIBRARY_PATH": str(tmp_path)}, out=out,
                      err=io.StringIO())
    assert code == 0 and "分得很乾淨" in out.getvalue()


def test_the_command_line_wants_exactly_two_pythons():
    import io

    err = io.StringIO()
    assert guard.main(["only-one"], environ={}, out=io.StringIO(), err=err) == 2 and "用法" in err.getvalue()


def test_the_real_listing_of_this_environment_works():
    """真的問一個 Python 它的環境裡有什麼（這個測試環境自己）：格式對、名稱正規化、裝了 pytest。"""
    names = guard.installed(sys.executable)
    assert "pytest" in names and all(n == guard.normalize(n) for n in names)


def _fake_run(returncode=0, stdout="[]", stderr="", seen=None):
    def run(command, **kwargs):
        if seen is not None:
            seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


def test_the_listing_runs_isolated_so_pythonpath_cannot_leak_another_environment_in():
    seen = []
    guard.installed("/x/python", run=_fake_run(stdout='["Torch", "nvidia_cudnn_cu12"]', seen=seen))
    command = seen[0][0]
    assert command[:3] == ["/x/python", "-I", "-c"]


def test_the_listing_normalises_what_it_reads():
    assert guard.installed("/x/python", run=_fake_run(stdout='["Nvidia_CuDNN.cu12", "Faster_Whisper"]')) \
        == {"nvidia-cudnn-cu12", "faster-whisper"}


def test_a_python_that_cannot_list_its_packages_is_an_error_not_an_empty_environment():
    """退而求其次回傳空集合的話，「主環境沒有 torch」會在看不到的情況下通過。"""
    with pytest.raises(guard.ListingFailed) as caught:
        guard.installed("/x/python", run=_fake_run(returncode=1, stdout="", stderr="no module named importlib"))
    assert "/x/python" in str(caught.value) and "no module named importlib" in str(caught.value)


def test_an_unreadable_listing_is_an_error_too():
    with pytest.raises(ValueError):
        guard.installed("/x/python", run=_fake_run(stdout="not json"))


@pytest.mark.parametrize("error", [guard.ListingFailed, ValueError, FileNotFoundError])
def test_the_build_fails_when_an_environment_cannot_be_inspected(monkeypatch, error):
    import io

    def broken(python):
        raise error(f"{python} 壞了")

    monkeypatch.setattr(guard, "installed", broken)
    out, err = io.StringIO(), io.StringIO()
    code = guard.main(["main-python", "diarize-python"], environ={}, out=out, err=err)
    assert code == 3 and "main-python 壞了" in err.getvalue() and out.getvalue() == ""


def test_a_python_that_does_not_exist_fails_the_build_with_a_message(tmp_path):
    import io

    err = io.StringIO()
    code = guard.main([str(tmp_path / "no-such-python"), sys.executable], environ={}, out=io.StringIO(), err=err)
    assert code == 3 and "沒辦法檢查" in err.getvalue()


# --- Dockerfile ---


def _dockerfile() -> list[str]:
    """Dockerfile 的指令：續行（行尾的反斜線）接成一行，註解與空行拿掉。"""
    instructions, current = [], ""
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            continue
        current += (" " if current else "") + stripped.rstrip("\\").strip()
        if not stripped.endswith("\\"):
            instructions.append(current)
            current = ""
    return instructions


def _index(pattern: str) -> int:
    for position, instruction in enumerate(_dockerfile()):
        if re.search(pattern, instruction):
            return position
    raise AssertionError(f"Dockerfile 裡找不到 {pattern}")


def _env_value(name: str) -> str:
    for instruction in _dockerfile():
        if instruction.startswith("ENV "):
            found = re.search(rf'(?:^ENV |\s){name}=("[^"]*"|\S+)', instruction)
            if found:
                return found.group(1).strip('"')
    raise AssertionError(f"Dockerfile 沒有 ENV {name}")


def test_the_main_environment_is_synced_exactly_like_before_multi_speaker():
    """主環境的 uv sync 和 master 的一模一樣（沒有 --group diarization）：議會與單人工作用的環境一個字都沒變。"""
    syncs = [i for i in _dockerfile() if re.match(r"RUN uv sync ", i)]
    assert syncs == ["RUN uv sync --frozen --no-dev --group api --group whisper-gpu --no-install-package pyside6"]


def test_the_diarization_group_is_installed_into_its_own_environment():
    syncs = [i for i in _dockerfile() if "uv sync" in i and "--only-group" in i]
    assert syncs == [f"RUN UV_PROJECT_ENVIRONMENT={DIARIZE_VENV} uv sync --frozen --only-group diarization"]
    assert _index(r"^RUN uv sync ") < _index(r"--only-group diarization")           # 先主環境，再第二個
    assert "--group" not in syncs[0].replace("--only-group", "")                     # 不夾帶別的群組


def test_no_other_instruction_installs_packages_into_either_environment():
    for instruction in _dockerfile():
        if re.search(r"\b(pip|uv pip|uv add)\b", instruction):
            raise AssertionError(f"Dockerfile 不該另外裝套件：{instruction}")


def test_the_worker_uses_the_second_environments_python():
    assert _env_value("SLIDEBOX_DIARIZATION_PYTHON") == f"{DIARIZE_VENV}/bin/python"


def test_the_library_path_still_points_only_at_the_main_environments_cuda_12_libraries():
    """ctranslate2 動態載入 cuBLAS／cuDNN 用的：值和 master 的一模一樣，而且完全沒有第二個環境的東西。"""
    value = _env_value("LD_LIBRARY_PATH")
    assert value == (f"{MAIN_VENV}/lib/python3.13/site-packages/nvidia/cublas/lib:"
                     f"{MAIN_VENV}/lib/python3.13/site-packages/nvidia/cudnn/lib")
    assert DIARIZE_VENV not in value


def test_the_path_finds_the_main_python_and_the_second_environment_is_not_on_it():
    path = _env_value("PATH")
    assert path.startswith(f"{MAIN_VENV}/bin:") and DIARIZE_VENV not in path


def test_uv_installs_with_the_same_settings_for_both_environments():
    """兩次 uv sync 吃同樣的 ENV（複製而不是硬連結、編譯 bytecode）：寫在兩次 sync 之前。"""
    assert _index(r"^ENV UV_COMPILE_BYTECODE=1") < _index(r"^RUN uv sync ")
    assert "UV_LINK_MODE=copy" in "\n".join(i for i in _dockerfile() if i.startswith("ENV UV_COMPILE_BYTECODE"))


def test_the_second_environment_is_created_outside_the_project_folder():
    """uv 預設把環境建在專案資料夾的 .venv——第二次 sync 不改位置，就會蓋掉主環境。"""
    assert f"UV_PROJECT_ENVIRONMENT={DIARIZE_VENV}" in " ".join(_dockerfile())
    assert not DIARIZE_VENV.startswith("/app")


def test_the_build_checks_that_the_two_environments_really_are_separate():
    """建置時真的去看兩個環境裡裝了什麼（上面的文字檢查只能看 Dockerfile 怎麼寫，看不到 uv 實際做了什麼）：
    不乾淨，建置就失敗，而不是做出一個悄悄壞掉的映像。"""
    guard_run = _index(rf"^RUN {MAIN_VENV}/bin/python \S*check_environments\.py {MAIN_VENV}/bin/python "
                       rf"{DIARIZE_VENV}/bin/python$")
    assert _index(r"^COPY docker/check_environments\.py ") < guard_run
    assert _index(r"^ENV LD_LIBRARY_PATH=") < guard_run                 # 檢查要看得到 LD_LIBRARY_PATH
    assert _index(r"--only-group diarization") < guard_run and _index(r"^RUN uv sync ") < guard_run


def test_the_guard_is_not_hidden_from_the_build_by_dockerignore():
    ignored = {line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")}
    assert not ignored & {"docker", "check_environments.py", "*.py", "**"} and GUARD_FILE.is_file()


def test_the_image_still_copies_the_source_that_contains_the_worker():
    assert _index(r"^COPY src \./src$") >= 0
    assert (ROOT / "src" / "slidebox" / "infrastructure" / "pyannote_worker.py").is_file()


def test_the_base_image_is_unchanged_and_both_environments_use_its_python():
    assert _dockerfile()[0] == "FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim"
