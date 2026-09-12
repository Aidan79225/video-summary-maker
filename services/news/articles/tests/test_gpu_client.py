"""GPU 摘要 API 的客戶端：用假的 request，不碰網路。"""
from __future__ import annotations

from django.test import SimpleTestCase

from articles.gpu_client import GpuApiClient, GpuApiError, JobFailed


class FakeTransport:
    """依序回傳預先排好的回應；記錄每次呼叫。這是傳輸層的替身，所以它丟的
    是裸的 OSError——把它翻譯成 GpuApiError 正是受測類別的責任。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, method, api_key, body, timeout):
        self.calls.append({"url": url, "method": method, "key": api_key, "body": body})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    """時間由測試推動，不用真的等。"""

    def __init__(self):
        self.value = 0.0

    def sleep(self, seconds):
        self.value += seconds

    def now(self):
        return self.value


def _client(responses, key=""):
    transport = FakeTransport(responses)
    clock = FakeClock()
    client = GpuApiClient("http://gpu:8800", key, transport=transport,
                          sleep=clock.sleep, now=clock.now)
    return client, transport, clock


class GpuClientTests(SimpleTestCase):
    def test_submitting_returns_the_job_id(self):
        client, transport, _ = _client([{"id": "abc", "status": "queued"}])
        self.assertEqual(client.submit("https://ivod/1"), "abc")
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertTrue(transport.calls[0]["body"]["detailed"])

    def test_the_api_key_travels_with_every_request(self):
        client, transport, _ = _client([{"id": "abc"}], key="secret")
        client.submit("https://ivod/1")
        self.assertEqual(transport.calls[0]["key"], "secret")

    def test_waiting_returns_the_result_once_the_job_is_done(self):
        client, _, _ = _client([
            {"status": "running", "progress_status": "產生摘要…"},
            {"status": "done", "result": {"video_id": "171180"}},
        ])
        result = client.wait("abc", timeout=600)
        self.assertEqual(result["video_id"], "171180")

    def test_a_failed_job_raises_with_its_reason(self):
        client, _, _ = _client([{"status": "failed", "error": "連不上 Ollama"}])
        with self.assertRaises(JobFailed) as ctx:
            client.wait("abc", timeout=600)
        self.assertIn("Ollama", str(ctx.exception))

    def test_a_cancelled_job_is_a_failure_for_the_caller(self):
        client, _, _ = _client([{"status": "cancelled"}])
        with self.assertRaises(JobFailed):
            client.wait("abc", timeout=600)

    def test_a_done_job_with_no_result_is_not_silently_accepted(self):
        """攔的 bug：把 None 當成結果存進資料庫，會生出一篇沒有任何段落的
        文章，而狀態是「已完成」。"""
        client, _, _ = _client([{"status": "done", "result": None}])
        with self.assertRaises(JobFailed):
            client.wait("abc", timeout=600)

    def test_waiting_gives_up_after_the_timeout(self):
        client, _, _ = _client([{"status": "running"}] * 50)
        with self.assertRaises(GpuApiError) as ctx:
            client.wait("abc", timeout=10, poll_seconds=3)
        self.assertIn("10", str(ctx.exception))

    def test_a_connection_problem_is_reported_as_a_service_problem(self):
        client, _, _ = _client([OSError("連線被拒")])
        with self.assertRaises(GpuApiError):
            client.submit("https://ivod/1")

    def test_a_response_without_an_id_is_refused(self):
        client, _, _ = _client([{"status": "queued"}])
        with self.assertRaises(GpuApiError):
            client.submit("https://ivod/1")

    def test_progress_is_reported_while_waiting(self):
        client, _, _ = _client([
            {"status": "running", "progress_status": "取得字幕…"},
            {"status": "done", "result": {}},
        ])
        seen = []
        client.wait("abc", timeout=600, on_progress=lambda job: seen.append(job))
        self.assertEqual(len(seen), 2)


class ErrorClassificationTests(SimpleTestCase):
    """一篇壞掉的文章與一個掛掉的服務，處置方式完全不同。"""

    def _http_error(self, code):
        import io
        import urllib.error
        return urllib.error.HTTPError("http://gpu/jobs", code, "boom", {},
                                      io.BytesIO(b"detail"))

    def test_a_rejected_payload_is_this_articles_problem(self):
        """422 是「這一筆送的內容不合法」——標記這一篇失敗，繼續下一篇。"""
        client, _, _ = _client([self._http_error(422)])
        with self.assertRaises(JobFailed):
            client.submit("https://ivod/1")

    def test_a_deployment_mistake_stops_the_batch_instead(self):
        """攔的 bug：把 405/400 也當成單篇失敗。那種錯對每一篇都一樣，會把
        當晚 20 篇的重試次數全部燒掉，五天後整批永久放棄。"""
        for code in (400, 401, 403, 405, 500, 502):
            with self.subTest(code=code):
                client, _, _ = _client([self._http_error(code)])
                with self.assertRaises(GpuApiError):
                    client.submit("https://ivod/1")

    def test_a_job_the_gpu_no_longer_knows_reads_as_none(self):
        """GPU 重開過，記憶體裡的佇列沒了。那不是服務掛掉，是那個 id 沒了
        ——呼叫端要據此重送，而不是停下整批。"""
        client, _, _ = _client([self._http_error(404)])
        self.assertIsNone(client.job("nope"))
