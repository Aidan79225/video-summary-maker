"""GPU 主機的部署設定（compose.gpu.yaml、.env.gpu.example、Dockerfile、README）不要和程式漂開。

程式讀哪些環境變數、容器要不要把使用統計關掉、模型快取放在哪裡，都寫在這幾個設定檔裡，
不在任何一個 Python 檔案裡：程式把變數改了名字，沒有人會發現，要到 Docker 裡才悄悄不生效。
這裡只做文字比對，不啟動 Docker、不連網路。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import serve_api
from slidebox.infrastructure.pyannote_diarizer import (
    DEFAULT_TIMEOUT_SECONDS,
    DEVICE_ENV,
    METRICS_ENV,
    MODE_ENV,
    PYTHON_ENV,
    TIMEOUT_ENV,
    TOKEN_ENV,
    PyannoteSpeakerDiarizer,
)
from slidebox.usecases import conversation, speakers
from slidebox.usecases.conversation import (
    ANCHOR_CONFLICT,
    ANCHOR_RADIUS,
    BAD_QUOTE,
    CHUNK_CHARS_ENV,
    DEFAULT_CHUNK_CHARS,
    MAX_BULLETS,
    NUMBER_MISMATCH,
    QUOTE_ONLY_ENV,
    parse_chunk_chars,
    parse_quote_only,
)
from slidebox.usecases.diarized_pipeline import MAX_DROPPED_RATIO, AttributionCounts, summary_line
from slidebox.usecases.quotes import MAX_RESCUED_QUOTE_CHARS, MIN_QUOTE_CHARS, QUOTE_MIN_LONGEST
from slidebox.usecases.speakers import MAX_PARTICIPANTS, MAX_PUBLISHED_SPEAKERS

ROOT = Path(__file__).resolve().parents[1]
DIARIZATION_SETTINGS = (TOKEN_ENV, MODE_ENV, DEVICE_ENV, CHUNK_CHARS_ENV, QUOTE_ONLY_ENV)
# 說話者區分的工作行程（pyannote_worker）的設定。PYTHON_ENV 由映像自己設（Dockerfile），不進 compose 與 .env
WORKER_SETTINGS = (TIMEOUT_ENV,)
HF_CACHE = "/root/.cache/huggingface"
TOKEN_SHAPE = re.compile(r"hf_[A-Za-z0-9]{8,}")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# --- compose.gpu.yaml：只認這個檔案實際用到的寫法（每項一行），格式變了就讓測試明講 ---

def _api_block() -> list[str]:
    lines = _read("compose.gpu.yaml").splitlines()
    assert "  api:" in lines, "compose.gpu.yaml 裡找不到 api 服務"
    block = []
    for line in lines[lines.index("  api:") + 1:]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return block


def _entries(header: str) -> list[str]:
    """api 服務裡 `header:` 底下的每一項（略過註解與空行）。"""
    block = _api_block()
    assert f"    {header}:" in block, f"api 服務沒有 {header}"
    items = []
    for line in block[block.index(f"    {header}:") + 1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith("      "):
            break
        items.append(line.strip())
    return items


def _api_environment() -> dict[str, str]:
    environment = {}
    for item in _entries("environment"):
        name, _, value = item.partition(": ")
        environment[name] = value.strip().strip('"')
    return environment


def _api_mounts() -> dict[str, str]:
    """容器裡的路徑 → 來源（volume 名稱或主機路徑）。"""
    mounts = {}
    for item in _entries("volumes"):
        assert item.startswith("- "), item
        source, _, target = item[2:].partition(":")
        mounts[target] = source
    return mounts


def _declared_volumes() -> set[str]:
    names, inside = set(), False
    for line in _read("compose.gpu.yaml").splitlines():
        if line == "volumes:":
            inside = True
        elif inside and line.startswith("  ") and line.strip().endswith(":"):
            names.add(line.strip()[:-1])
        elif inside and line.strip() and not line.startswith(" "):
            break
    return names


def _default_of(value: str) -> str:
    """`${NAME:-預設值}` 的預設值。"""
    found = re.fullmatch(r"\$\{\w+:-([^}]*)\}", value)
    assert found, f"不是 ${{NAME:-預設值}} 的寫法：{value}"
    return found.group(1)


def _env_example() -> dict[str, str]:
    values = {}
    for line in _read(".env.gpu.example").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def _adapter_accepts(mode: str, device: str) -> tuple[bool, str]:
    """真正的 adapter 認不認這組設定（假的 token、假的套件查詢：不 import pyannote、不連網路）。"""
    return PyannoteSpeakerDiarizer(
        token="hf_FAKEtokenForConfigTest0123456789", mode=mode, device=device,
        spec_finder=lambda _name: object()).available()


def _adapter_accepts_timeout(value: str) -> tuple[bool, str]:
    """同上，看工作行程的時間上限這個設定值。"""
    return PyannoteSpeakerDiarizer(
        token="hf_FAKEtokenForConfigTest0123456789", mode="pyannote", device="auto", timeout_seconds=value,
        spec_finder=lambda _name: object()).available()


# --- compose.gpu.yaml ---

@pytest.mark.parametrize("name", DIARIZATION_SETTINGS)
def test_compose_hands_each_diarization_setting_to_the_api_container(name):
    """攔的 bug：程式改了環境變數的名字（或新增了一個），compose 沒跟著改——
    在 .env.gpu 裡設了也進不了容器，Docker 裡的設定悄悄不生效。"""
    environment = _api_environment()
    assert name in environment, f"compose.gpu.yaml 沒有把 {name} 傳進 api 容器"
    assert environment[name].startswith("${" + name), f"{name} 沒有從 .env.gpu 傳進來：{environment[name]}"


def test_compose_defaults_leave_multi_speaker_switched_on_and_valid():
    """攔的 bug：預設值寫成 off（沒設任何東西，多人模式就悄悄關掉），或寫了 adapter 不認得的值
    （/capabilities 永遠回「不行」）；切窗字數也不能在這裡另外寫死一個和程式不同的數字。"""
    environment = _api_environment()
    mode, device = _default_of(environment[MODE_ENV]), _default_of(environment[DEVICE_ENV])
    assert (mode, device) == ("pyannote", "auto")
    assert _adapter_accepts(mode, device) == (True, "")
    assert _default_of(environment[CHUNK_CHARS_ENV]) in ("", str(DEFAULT_CHUNK_CHARS))
    assert _default_of(environment[TOKEN_ENV]) == ""      # token 不寫死在 compose 裡


@pytest.mark.parametrize("name", WORKER_SETTINGS)
def test_compose_hands_each_worker_setting_to_the_api_container(name):
    environment = _api_environment()
    assert name in environment, f"compose.gpu.yaml 沒有把 {name} 傳進 api 容器"
    assert environment[name].startswith("${" + name), f"{name} 沒有從 .env.gpu 傳進來：{environment[name]}"


def test_compose_leaves_the_worker_time_limit_at_the_codes_default_and_valid():
    """沒設就是程式的預設值（不在 compose 裡另外寫死一個），而且 adapter 認得這個值。"""
    default = _default_of(_api_environment()[TIMEOUT_ENV])
    assert default in ("", str(DEFAULT_TIMEOUT_SECONDS))
    assert _adapter_accepts_timeout(default) == (True, "")


def test_compose_does_not_override_the_python_the_image_points_the_worker_at():
    """攔的 bug：compose 寫了 `SLIDEBOX_DIARIZATION_PYTHON: ${SLIDEBOX_DIARIZATION_PYTHON:-}`——沒設的時候
    它變成空字串，蓋掉 Dockerfile 的 ENV：工作行程改用主環境的 Python（那裡沒有 torch），
    多人模式永遠說「沒有安裝 pyannote.audio」，而映像本身一點問題也沒有。"""
    assert PYTHON_ENV not in _api_environment()
    assert PYTHON_ENV not in _env_example()


def test_compose_turns_usage_telemetry_off():
    """攔的 bug：容器把使用統計送回 pyannote／Hugging Face（這些影片是別人的內容）。"""
    environment = _api_environment()
    assert environment[METRICS_ENV] == "0"
    assert environment["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_compose_keeps_the_model_cache_on_a_named_volume():
    """攔的 bug：Hugging Face 的快取留在容器裡——每次重建都重抓 whisper 與 pyannote 的模型；
    改成主機路徑也不行（Windows 的路徑掛進 Linux 容器會有權限與換行問題），要有宣告過的具名 volume。"""
    source = _api_mounts().get(HF_CACHE)
    assert source, f"api 服務沒有掛載 {HF_CACHE}"
    assert re.fullmatch(r"[A-Za-z0-9][\w.-]*", source), f"{source} 不是具名 volume"
    assert source in _declared_volumes(), f"{source} 沒有在最上層的 volumes 宣告"


def test_compose_contains_no_token():
    assert not TOKEN_SHAPE.search(_read("compose.gpu.yaml"))


# --- .env.gpu.example ---

def test_env_example_ships_no_secret_values():
    """攔的 bug：把自己的 key 或 token 填進範例檔就提交了——這個 repo 是公開的。"""
    values = _env_example()
    for name in ("SLIDEBOX_API_KEY", TOKEN_ENV):
        assert name in values, f"範例檔沒有 {name}"
        assert values[name] == "", f"{name} 在範例檔裡不能有值"
    assert not TOKEN_SHAPE.search(_read(".env.gpu.example"))


def test_env_example_lists_every_diarization_setting_with_a_value_the_code_accepts():
    """攔的 bug：範例檔漏了一個設定（沒人知道有它），或範例值本身就是錯的——
    照著複製下去，多人模式就被「設定值寫錯」擋住。"""
    values = _env_example()
    for name in DIARIZATION_SETTINGS:
        assert name in values, f"範例檔沒有 {name}"
    assert _adapter_accepts(values[MODE_ENV], values[DEVICE_ENV]) == (True, "")
    chunk = values[CHUNK_CHARS_ENV]
    assert chunk == "" or parse_chunk_chars(chunk) == int(chunk)


def test_env_example_lists_the_worker_time_limit_with_a_value_the_code_accepts():
    values = _env_example()
    for name in WORKER_SETTINGS:
        assert name in values, f"範例檔沒有 {name}"
    assert _adapter_accepts_timeout(values[TIMEOUT_ENV]) == (True, "")


# --- Dockerfile ---

def test_dockerfile_installs_the_diarization_group():
    """攔的 bug：映像沒裝 pyannote——/capabilities 永遠說「沒有安裝 pyannote.audio」。
    它裝在第二個環境（/opt/diarize-venv），不是主環境：為什麼、以及兩個環境各自該有什麼，
    見 tests/test_gpu_environments.py。"""
    assert re.search(r"^RUN UV_PROJECT_ENVIRONMENT=/opt/diarize-venv uv sync .*--only-group diarization\b",
                     _read("Dockerfile"), re.MULTILINE)


def test_dockerfile_lets_python_find_the_modules_run_with_dash_m():
    """攔的 bug：serve_api.py 自己把 src 加進搜尋路徑，但 `python -m slidebox_api.diarize_check`
    不會——容器裡找不到模組，README 教的檢查指令第一步就失敗。"""
    text = _read("Dockerfile")
    assert "PYTHONPATH=/app/src" in text
    assert "WORKDIR /app" in text and "COPY src ./src" in text      # /app/src 真的是原始碼放的地方


# --- README 與 serve_api.py 的說明 ---

def _multi_speaker_section() -> str:
    """README 裡〈多人說話者模式〉那一節（到下一個二級標題為止）。"""
    readme = _read("README.md")
    assert "### 多人說話者模式" in readme, "README 沒有〈多人說話者模式〉這一節"
    return readme.split("### 多人說話者模式", 1)[1].split("\n## ", 1)[0]


def test_readme_section_documents_every_setting_and_the_audit_tool():
    """攔的 bug：加了設定值卻沒寫進說明——使用者不知道有逃生口（cpu）、不知道要接受模型條款、
    不知道要調 Pi 的逾時、不知道要先用 diarize_check 聽過再相信。"""
    section = _multi_speaker_section()
    for needle in (*DIARIZATION_SETTINGS, METRICS_ENV, "HF_HUB_DISABLE_TELEMETRY", "GPU_JOB_TIMEOUT_SECONDS",
                   "https://huggingface.co/pyannote/speaker-diarization-community-1",
                   "exec api python -m slidebox_api.diarize_check --sample",
                   "`speaker_contract`", "fixtures/speaker_contract_v1.json", "whisper-cache"):
        assert needle in section, f"README 的多人說話者模式一節沒有提到 {needle}"
    assert not TOKEN_SHAPE.search(section)


def test_readme_explains_the_two_environments_and_what_the_checks_do_and_do_not_prove():
    """攔的 bug：說明還停在「一個環境」——照著跑 `exec api python -c "import torch…"` 會因為主環境沒有 torch 失敗；
    把 `--sample` 的結束碼 0 讀成「顯示卡沒問題」（其實可能一直在 CPU 上跑）；Windows 主機上的指令只能驗證 CPU。"""
    section = _multi_speaker_section()
    for needle in (TIMEOUT_ENV, PYTHON_ENV, "/opt/diarize-venv", "/app/.venv", "docker/check_environments.py",
                   "env -u LD_LIBRARY_PATH", "CUDA UMD Version", "SLIDEBOX_DIARIZATION_DEVICE=cuda",
                   "這條路只能驗證 CPU", "不是「顯示卡沒問題」",
                   "沒有真的建置過", "find /app/.venv /opt/diarize-venv -name 'libcudnn.so*'"):
        assert needle in section, f"README 的多人說話者模式一節沒有提到 {needle}"
    for stale in ("確認 token、模型、驅動都正常", 'exec api python -c "import torch', "`--group diarization` 建置"):
        assert stale not in section, f"README 還留著舊的說法：{stale}"


def test_the_documented_chunk_size_default_is_the_codes_default():
    """攔的 bug：程式的預設值改了，說明還寫著舊的數字。"""
    assert str(DEFAULT_CHUNK_CHARS) in _multi_speaker_section()
    assert f"預設 {DEFAULT_CHUNK_CHARS}" in (serve_api.__doc__ or "")


@pytest.mark.parametrize("name", DIARIZATION_SETTINGS)
def test_serve_api_docstring_lists_each_diarization_setting(name):
    assert name in (serve_api.__doc__ or "")


# --- 安全模式（SLIDEBOX_QUOTE_ONLY）與歸屬核對的說明 ---

def test_the_safe_mode_is_off_by_default_in_every_file_and_the_documented_values_are_the_codes():
    """攔的 bug：預設被寫成開（沒設任何東西，每個重點就悄悄變成原話）；或說明裡的「1、true、yes」
    和程式認的不一樣。"""
    compose_default = _default_of(_api_environment()[QUOTE_ONLY_ENV])
    assert compose_default == "" and parse_quote_only(compose_default) is False
    example = _env_example()[QUOTE_ONLY_ENV]
    assert example == "" and parse_quote_only(example) is False
    section = _multi_speaker_section()
    for value in ("1", "true", "yes"):
        assert parse_quote_only(value) is True and f"`{value}`" in section


def test_the_readme_log_line_is_the_one_the_code_writes():
    """攔的 bug：記錄裡那一行的格式改了，說明裡的範例還是舊的。"""
    counts = AttributionCounts(proposed=37, kept=31, rescued=4, contradicted=4, unverifiable=2, slide_rule=0,
                               anchor_conflicts=1)
    assert summary_line(counts) in _multi_speaker_section()


def test_the_readme_numbers_about_checking_points_are_the_codes_numbers():
    """攔的 bug：門檻改了，說明還寫著舊的數字。"""
    section = _multi_speaker_section()
    for needle in (f"合計至少 {MIN_QUOTE_CHARS} 個字", f"最長的片段至少 {QUOTE_MIN_LONGEST} 個字",
                   f"{MIN_QUOTE_CHARS}～{MAX_RESCUED_QUOTE_CHARS} 個字", f"前後 {ANCHOR_RADIUS} 輪",
                   f"每頁最多顯示 {MAX_BULLETS} 個重點"):
        assert needle in section, needle
    assert MAX_DROPPED_RATIO == 0.25 and "四分之一" in section


def test_the_readme_limits_on_the_number_of_voices_are_the_codes_numbers():
    """攔的 bug：發佈上限改了（或新聞服務改了、這邊跟著改），說明還寫著舊的數字，
    操作者就不知道 13 個聲音為什麼失敗、也不知道該怎麼辦。"""
    section = _multi_speaker_section()
    assert f"主要發言者超過 {MAX_PARTICIPANTS} 位" in section
    assert f"合計超過 {MAX_PUBLISHED_SPEAKERS} 個" in section
    assert f"最多 {MAX_PUBLISHED_SPEAKERS} 個" in section
    assert "--speakers N" in section


def test_the_readme_names_only_error_codes_that_exist():
    section = _multi_speaker_section()
    for code in (BAD_QUOTE, ANCHOR_CONFLICT, NUMBER_MISMATCH):
        assert f"`{code}`" in section


def test_the_readme_says_what_is_saved_and_when_the_way_the_code_does():
    """攔的 bug：說明寫「各窗摘要」也會存、重送從那裡接著做，程式其實要所有的窗都問完、整份的可靠度檢查也過了
    才存一次摘要（test_build_deck_diarized 釘住這個行為）。操作者因此低估逾時要調多大、以為重試會接著做完。
    另外說明提到的兩個手動版本號要真的存在，存檔指紋含程式原始碼雜湊這件事才不會講成空話。"""
    section = _multi_speaker_section()
    assert "各窗摘要" not in section and "不是一個窗一個窗存的" in section
    for name, module in (("PROMPT_VERSION", conversation), ("ALIGN_VERSION", speakers)):
        assert f"`{name}`" in section and hasattr(module, name), name


# --- 說話者區分的工作行程（兩個 Python 環境）的設定說明 ---

@pytest.mark.parametrize("name", (*WORKER_SETTINGS, PYTHON_ENV))
def test_serve_api_docstring_lists_each_worker_setting(name):
    assert name in (serve_api.__doc__ or "")


def test_the_documented_worker_time_limit_default_is_the_codes_default():
    """攔的 bug：程式的預設上限改了，說明還寫著舊的數字。"""
    default = str(DEFAULT_TIMEOUT_SECONDS)
    assert default in _multi_speaker_section()
    assert default in _read("compose.gpu.yaml") and default in _read(".env.gpu.example")
    assert f"預設 {default}" in (serve_api.__doc__ or "")
