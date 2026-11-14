"""工作失敗時存起來的文字（job.error 與進度狀態）：翻成中文、遮掉機密與路徑。

這些字串會被 GET /jobs/{id} 回傳、被 Pi 寫進資料庫再印在畫面上，repo 與任務紀錄又是公開、
可以被分享的：原樣轉送「ClassName: English text」會把 GPU 主機的絕對路徑和（萬一帶在例外裡的）
token 一起送出去。
"""
from __future__ import annotations

import errno
import logging

import pytest

from slidebox.domain.errors import AttributionUnreliable, DiarizationUnavailable
from slidebox_api.failures import FailureReporter
from slidebox_api.jobs import JobStatus, JobStore
from slidebox_api.runner import JobWorker

HF_TOKEN = "hf_FAKEtokenFAKEtokenFAKEtoken1234567890"
OUT = "/data/output"
PLAIN_SECRET = "plain-secret-value-0001"


def reporter(**options) -> FailureReporter:
    return FailureReporter(secrets=(HF_TOKEN, PLAIN_SECRET), paths=(OUT,), **options)


def disk_full(path=f"{OUT}/少子化對談 [abcdefghijk]/slides.html") -> OSError:
    return OSError(errno.ENOSPC, "No space left on device", path)


# --- 已知的基礎設施錯誤：翻成中文 ---


def test_a_full_disk_is_reported_in_chinese_without_the_english_text_or_the_path():
    """攔的 bug：硬碟滿了，Pi 收到的是 "OSError: [Errno 28] No space left on device: '/data/output/…'"：
    英文、容器裡的絕對路徑，操作者看不出該去清 GPU 主機的磁碟。"""
    text = reporter().describe(disk_full())
    assert text.startswith("OSError: ") and "GPU 主機磁碟已滿" in text
    assert "No space" not in text and OUT not in text and "slides.html" not in text


def test_a_disk_quota_is_a_full_disk_too():
    quota = OSError(errno.EDQUOT, "Disk quota exceeded", f"{OUT}/x") if hasattr(errno, "EDQUOT") else disk_full()
    assert "GPU 主機磁碟已滿" in reporter().describe(quota)


@pytest.mark.parametrize("error", [
    PermissionError(errno.EACCES, "Permission denied", f"{OUT}/x/slides.html"),
    OSError(errno.EROFS, "Read-only file system", f"{OUT}/x/slides.html"),
])
def test_a_file_that_cannot_be_written_is_reported_in_chinese(error):
    text = reporter().describe(error)
    assert "無法寫入" in text and "Permission denied" not in text and OUT not in text


def test_running_out_of_host_memory_is_reported_in_chinese():
    assert "GPU 主機記憶體不足" in reporter().describe(MemoryError())


@pytest.mark.parametrize("error", [
    RuntimeError("CUDA failed with error out of memory"),                       # ctranslate2（語音辨識）
    RuntimeError("CUDA out of memory. Tried to allocate 1.80 GiB"),             # torch
])
def test_the_graphics_card_running_out_of_memory_is_reported_in_chinese(error):
    text = reporter().describe(error)
    assert "顯示卡記憶體不足" in text and "Ollama" in text and "Tried to allocate" not in text


def test_the_translation_keeps_the_exception_name_so_the_reader_can_tell_what_it_was():
    assert reporter().describe(MemoryError()).startswith("MemoryError: ")
    assert reporter().describe(PermissionError(errno.EACCES, "denied", "/x")).startswith("PermissionError: ")


def test_a_translated_error_still_says_which_errno_it_was():
    assert "ENOSPC" in reporter().describe(disk_full())


# --- 其他的失敗：照舊「例外名稱: 訊息」，不亂翻 ---


def test_the_projects_own_errors_are_stored_exactly_as_before():
    assert reporter().describe(AttributionUnreliable("丟掉 3/10 個重點")) == "AttributionUnreliable: 丟掉 3/10 個重點"
    assert reporter().describe(DiarizationUnavailable("沒有設定 HUGGINGFACE_TOKEN")) == \
        "DiarizationUnavailable: 沒有設定 HUGGINGFACE_TOKEN"


def test_an_unknown_error_is_not_translated_into_something_it_is_not():
    assert reporter().describe(KeyError("slides")) == "KeyError: 'slides'"
    assert "Address already in use" in reporter().describe(OSError(errno.EADDRINUSE, "Address already in use"))


def test_an_oserror_without_an_errno_is_left_alone():
    assert reporter().describe(OSError("boom")) == "OSError: boom"


def test_the_stored_text_is_at_most_800_characters_as_before():
    assert len(reporter().describe(RuntimeError("x" * 5000))) == 800


# --- 機密與路徑：每一則存起來的文字都要先過濾 ---


def test_the_configured_token_is_removed_whatever_it_looks_like():
    """攔的 bug：意外的例外帶著 token（標頭、網址）一路進了 job.error。設定的那把用原字串取代，
    不一定是 hf_ 開頭（代理、測試用的假值），只靠樣式比對會漏。"""
    text = reporter().describe(RuntimeError(f"denied for {PLAIN_SECRET} and {HF_TOKEN}"))
    assert PLAIN_SECRET not in text and HF_TOKEN not in text and "hf_FAKE" not in text


def test_any_hf_token_shaped_string_is_removed_even_if_it_is_not_the_configured_one():
    stray = "hf_STRAYtokenSTRAYtoken9876543210"
    text = FailureReporter().describe(RuntimeError(f"Authorization: Bearer {stray}"))     # 什麼都沒設定也一樣要擋
    assert stray not in text and "hf_***" in text


def test_the_output_folder_is_removed_from_every_message_in_every_spelling():
    for spelled in (f"{OUT}/a/b.html", f"{OUT}\\a\\b.html"):
        text = reporter().describe(RuntimeError(f"cannot open '{spelled}'"))
        assert OUT not in text and "a" in text and "b.html" in text
    windows = FailureReporter(paths=(r"C:\Users\someone\slidebox_api_output",))
    # Windows 上 OSError 的訊息把反斜線寫成兩個
    text = windows.describe(RuntimeError(r"cannot open 'C:\\Users\\someone\\slidebox_api_output\\x.html'"))
    assert "someone" not in text and "slidebox_api_output" not in text
    assert "someone" not in windows.scrub(r"C:\Users\someone\slidebox_api_output\x.html")
    assert "someone" not in windows.scrub("C:/Users/someone/slidebox_api_output/x.html")


def test_a_path_that_is_too_short_to_mean_anything_is_not_scrubbed_out_of_every_sentence():
    """攔的 bug：輸出資料夾設成 "/"，整則訊息裡的每個斜線都被取代。"""
    assert FailureReporter(paths=("/",)).scrub("a/b/c") == "a/b/c"
    assert FailureReporter(paths=("",)).scrub("a/b/c") == "a/b/c"


def test_blank_secrets_are_ignored_instead_of_blanking_the_message():
    assert FailureReporter(secrets=("", "   ")).scrub("keep this text") == "keep this text"


def test_the_filter_runs_before_the_cut_so_a_token_at_the_boundary_cannot_survive_half_removed():
    """攔的 bug：先截斷再遮掉，剛好被切成兩半的 token 就不再符合樣式，原樣留下前半段。"""
    prefix = len("RuntimeError: ")
    text = reporter().describe(RuntimeError("x" * (800 - prefix - 10) + HF_TOKEN))     # 800 字的界線切在 token 的第 10 個字
    assert "hf_" not in text and HF_TOKEN[:6] not in text


def test_the_scrub_is_idempotent():
    once = reporter().scrub(f"{OUT}/x {HF_TOKEN}")
    assert reporter().scrub(once) == once


# --- 工作執行緒：每一則存起來的文字都經過它 ---


def _worker(execute, **options):
    store = JobStore()
    return store, JobWorker(store, execute, **options)


def test_a_failed_job_stores_the_translated_and_scrubbed_message():
    def boom(job, progress, is_cancelled):
        raise disk_full()

    store, worker = _worker(boom, failures=reporter())
    job = store.submit("https://example.com/v")
    worker.run_once()
    assert job.status == JobStatus.FAILED
    assert "GPU 主機磁碟已滿" in job.error and OUT not in job.error and "No space" not in job.error


def test_an_unexpected_error_that_carries_the_token_is_scrubbed_before_it_is_stored():
    def boom(job, progress, is_cancelled):
        raise RuntimeError(f"upstream said: Authorization: Bearer {HF_TOKEN}")

    store, worker = _worker(boom, failures=reporter())
    job = store.submit("https://example.com/v")
    worker.run_once()
    assert job.status == JobStatus.FAILED and HF_TOKEN not in job.error and "hf_FAKE" not in job.error


def test_progress_text_is_scrubbed_too():
    """進度狀態也是會被 GET /jobs/{id} 回傳、被存起來的字串。"""
    seen = {}

    def run(job, progress, is_cancelled):
        progress(0.5, f"下載到 {OUT}/.slidebox_audio_tmp/x.webm（token {PLAIN_SECRET}）")
        seen.update(fraction=job.progress_fraction, status=job.progress_status)      # 工作結束後會被蓋成 1.0
        return {}

    store, worker = _worker(run, failures=reporter())
    store.submit("https://example.com/v")
    worker.run_once()
    assert OUT not in seen["status"] and PLAIN_SECRET not in seen["status"]
    assert seen["fraction"] == 0.5 and "下載到" in seen["status"]


def test_a_worker_built_without_a_reporter_still_scrubs_hf_tokens():
    """攔的 bug：只有 serve_api 傳了設定的人才有保護；測試或別處直接 JobWorker(store, execute) 就沒有。"""
    def boom(job, progress, is_cancelled):
        raise RuntimeError(f"token {HF_TOKEN} rejected")

    store, worker = _worker(boom)
    job = store.submit("https://example.com/v")
    worker.run_once()
    assert HF_TOKEN not in job.error


def test_the_default_message_format_is_unchanged_for_ordinary_failures():
    def boom(job, progress, is_cancelled):
        raise ValueError("不支援的工作種類：topic")

    store, worker = _worker(boom)
    job = store.submit("https://example.com/v")
    worker.run_once()
    assert job.error == "ValueError: 不支援的工作種類：topic"


def test_the_original_error_goes_to_the_gpu_hosts_log_scrubbed(caplog):
    """翻成中文之後，原本的英文訊息（errno、哪個檔案）只留在 GPU 主機自己的 log，給操作者查；
    log 也不能帶 token。"""
    def boom(job, progress, is_cancelled):
        raise OSError(errno.ENOSPC, f"No space left on device (token {HF_TOKEN})", f"{OUT}/x/slides.html")

    store, worker = _worker(boom, failures=reporter())
    job = store.submit("https://example.com/v")
    with caplog.at_level(logging.WARNING, logger="slidebox_api.runner"):
        worker.run_once()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "No space left on device" in logged and job.id in logged
    assert HF_TOKEN not in logged and "hf_FAKE" not in logged
    assert "GPU 主機磁碟已滿" in job.error


def test_cancelling_still_stores_no_error():
    from slidebox.domain.errors import OperationCancelled

    def stop(job, progress, is_cancelled):
        raise OperationCancelled()

    store, worker = _worker(stop, failures=reporter())
    job = store.submit("https://example.com/v")
    worker.run_once()
    assert job.status == JobStatus.CANCELLED and job.error == ""
