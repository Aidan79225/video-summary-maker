"""呼叫 GPU 主機上的摘要 API。

Pi 上不跑任何模型，也不裝 slidebox——兩邊只透過 HTTP 說話。

這個類別對外的契約是「只會丟 GpuApiError 或 JobFailed」。轉譯刻意放在
類別裡而不是傳輸函式裡：傳輸是可抽換的（測試會換掉），錯誤契約不是。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

_SUBMIT_TIMEOUT = 30.0
_POLL_TIMEOUT = 30.0


class GpuApiError(Exception):
    """摘要 API 這次不能用。暫時性問題，下次排程會再試。"""


class JobFailed(Exception):
    """工作跑完了，但失敗。通常是這一支影片的問題，不是服務的問題。"""


def _http(url: str, method: str, api_key: str, body: dict | None,
          timeout: float) -> dict:
    """裸的 HTTP 傳輸。不翻譯例外——那是 GpuApiClient 的責任。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class GpuApiClient:
    def __init__(self, base: str, api_key: str = "", transport=_http,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic):
        self._base = base.rstrip("/")
        self._key = api_key
        self._transport = transport
        self._sleep = sleep
        self._now = now

    def health(self) -> dict:
        return self._call("/health", "GET", timeout=_POLL_TIMEOUT)

    def submit(self, url: str, detailed: bool = True, min_slides: int | None = None,
               max_slides: int | None = None) -> str:
        body: dict = {"url": url, "detailed": detailed}
        if min_slides:
            body["min_slides"] = min_slides
        if max_slides:
            body["max_slides"] = max_slides
        job = self._call("/jobs", "POST", body=body, timeout=_SUBMIT_TIMEOUT)
        job_id = job.get("id")
        if not job_id:
            raise GpuApiError("摘要 API 沒有回傳工作 id")
        return str(job_id)

    def wait(self, job_id: str, timeout: float, poll_seconds: float = 3.0,
             on_progress: Callable[[dict], None] | None = None) -> dict:
        """等一個工作跑完，回傳它的 result。

        逾時不代表工作死了——GPU 那邊可能還在跑。所以只放棄等待，不取消：
        下一次排程重跑時，成品很可能已經好了。
        """
        deadline = self._now() + timeout
        while True:
            job = self._call(f"/jobs/{job_id}", "GET", timeout=_POLL_TIMEOUT)
            if on_progress is not None:
                on_progress(job)
            status = job.get("status")
            if status == "done":
                result = job.get("result")
                if not isinstance(result, dict):
                    raise JobFailed("工作回報完成，但沒有結果")
                return result
            if status == "failed":
                raise JobFailed(str(job.get("error") or "未知的失敗")[:500])
            if status == "cancelled":
                raise JobFailed("工作被取消")
            if self._now() >= deadline:
                raise GpuApiError(
                    f"等待工作 {job_id} 超過 {int(timeout)} 秒仍未完成"
                    "（GPU 那邊可能還在跑）")
            self._sleep(poll_seconds)

    def _call(self, path: str, method: str, body: dict | None = None,
              timeout: float = _POLL_TIMEOUT) -> dict:
        try:
            payload = self._transport(f"{self._base}{path}", method, self._key, body,
                                      timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise GpuApiError(f"摘要 API 回應 {e.code}：{detail}") from e
        except (OSError, ValueError) as e:
            raise GpuApiError(f"摘要 API 連線失敗：{str(e)[:200]}") from e
        if not isinstance(payload, dict):
            raise GpuApiError("摘要 API 的回應格式不符預期")
        return payload
