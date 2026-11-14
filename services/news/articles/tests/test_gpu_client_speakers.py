"""GPU 客戶端的多人說話者區分：/capabilities 的探測，與只在多人工作才多出來的兩個請求欄位。"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

from django.test import SimpleTestCase

from articles.gpu_client import SPEAKER_CONTRACT, GpuApiClient, GpuApiError, JobNotFound, SpeakerMode
from articles.tests.test_gpu_client import _client

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "speaker_contract_v1.json").read_text("utf-8"))


def _http_error(code):
    return urllib.error.HTTPError("http://gpu/capabilities", code, "boom", {}, io.BytesIO(b"detail"))


class CapabilitiesTests(SimpleTestCase):
    def test_it_reads_the_capabilities_endpoint_with_the_api_key(self):
        client, transport, _ = _client([CONTRACT["capabilities"]], key="secret")
        self.assertEqual(client.capabilities(), CONTRACT["capabilities"])
        call = transport.calls[0]
        self.assertEqual((call["method"], call["url"], call["key"], call["body"]),
                         ("GET", "http://gpu:8800/capabilities", "secret", None))

    def test_the_contract_version_the_pi_speaks_is_the_one_in_the_frozen_fixture(self):
        self.assertEqual(SPEAKER_CONTRACT, CONTRACT["capabilities"]["speaker_contract"])

    def test_a_gpu_without_the_endpoint_is_called_not_updated_rather_than_job_not_found(self):
        """舊版 GPU 沒有 /capabilities（404）。_call 會把 404 翻成「不認得這個工作」，那對這個呼叫是誤導。"""
        client, _, _ = _client([_http_error(404)])
        with self.assertRaises(GpuApiError) as caught:
            client.capabilities()
        self.assertIn("GPU 服務還沒更新到支援多人區分的版本", str(caught.exception))
        self.assertNotIsInstance(caught.exception, JobNotFound)

    def test_other_failures_stay_ordinary_service_errors(self):
        for failure in (_http_error(500), _http_error(401), OSError("連線被拒")):
            with self.subTest(failure=repr(failure)):
                client, _, _ = _client([failure])
                with self.assertRaises(GpuApiError) as caught:
                    client.capabilities()
                self.assertNotIn("還沒更新", str(caught.exception))

    def test_a_response_that_is_not_an_object_is_refused(self):
        client, _, _ = _client([["speaker_diarization"]])
        with self.assertRaises(GpuApiError):
            client.capabilities()


class SubmitBodyTests(SimpleTestCase):
    def test_a_council_submit_body_is_byte_identical_to_before(self):
        """議會的請求不能因為影片的功能而多出任何一個鍵（也不能換順序）。"""
        client, transport, _ = _client([{"id": "j1"}, {"id": "j2"}])
        client.submit("https://ivod.ly.gov.tw/Play/Clip/1M/900001", detailed=True,
                      speech_hint="立法院 第11屆第5會期第23次會議。發言者：範例一")
        client.submit("https://ivod.ly.gov.tw/Play/Clip/1M/900001")
        self.assertEqual(json.dumps(transport.calls[0]["body"], ensure_ascii=False),
                         '{"url": "https://ivod.ly.gov.tw/Play/Clip/1M/900001", "detailed": true, '
                         '"speech_hint": "立法院 第11屆第5會期第23次會議。發言者：範例一"}')
        self.assertEqual(json.dumps(transport.calls[1]["body"]),
                         '{"url": "https://ivod.ly.gov.tw/Play/Clip/1M/900001", "detailed": true}')

    def test_a_single_speaker_video_body_has_no_speaker_keys_either(self):
        client, transport, _ = _client([{"id": "j1"}, {"id": "j2"}, {"id": "j3"}])
        client.submit("https://x", min_slides=3, max_slides=6, speech_hint="頻道。標題")
        client.submit("https://x", min_slides=3, max_slides=6, speech_hint="頻道。標題", speaker_mode=None)
        client.submit("https://x", min_slides=3, max_slides=6, speech_hint="頻道。標題",
                      speaker_mode=SpeakerMode.SINGLE)
        bodies = [call["body"] for call in transport.calls]
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0], bodies[2])
        for body in bodies:
            self.assertEqual(list(body), ["url", "detailed", "min_slides", "max_slides", "speech_hint"])

    def test_the_diarize_body_is_exactly_the_contract_example(self):
        request = CONTRACT["request"]
        client, transport, _ = _client([{"id": "j1"}])
        client.submit(request["url"], detailed=request["detailed"], min_slides=request["min_slides"],
                      max_slides=request["max_slides"], speech_hint=request["speech_hint"],
                      speaker_mode=SpeakerMode.DIARIZE, speaker_count=request["speaker_count"])
        self.assertEqual(transport.calls[0]["body"], request)
        self.assertEqual(list(transport.calls[0]["body"]), list(request))

    def test_a_fixed_count_goes_out_as_a_number(self):
        client, transport, _ = _client([{"id": "j1"}])
        client.submit("https://x", speaker_mode="diarize", speaker_count=3)
        self.assertEqual((transport.calls[0]["body"]["speaker_mode"], transport.calls[0]["body"]["speaker_count"]),
                         ("diarize", 3))

    def test_an_automatic_count_is_sent_as_null_not_left_out(self):
        client, transport, _ = _client([{"id": "j1"}])
        client.submit("https://x", speaker_mode=SpeakerMode.DIARIZE)
        self.assertIn("speaker_count", transport.calls[0]["body"])
        self.assertIsNone(transport.calls[0]["body"]["speaker_count"])

    def test_a_mistyped_mode_or_a_lone_count_cannot_quietly_become_a_single_speaker_request(self):
        for kwargs in ({"speaker_mode": "multi"}, {"speaker_mode": "DIARIZE"}, {"speaker_count": 3},
                       {"speaker_mode": "single", "speaker_count": 2},
                       {"speaker_mode": "diarize", "speaker_count": 1},
                       {"speaker_mode": "diarize", "speaker_count": 21},
                       {"speaker_mode": "diarize", "speaker_count": True},
                       {"speaker_mode": "diarize", "speaker_count": "3"}):
            with self.subTest(kwargs=kwargs):
                client, transport, _ = _client([{"id": "j1"}])
                with self.assertRaises(ValueError):
                    client.submit("https://x", **kwargs)
                self.assertEqual(transport.calls, [])


class SubmitRangeTests(SimpleTestCase):
    """多人工作可以只處理影片的一段：range_start（一定帶）、range_end（有才帶），都是原影片的整數秒。"""

    def _body(self, **kwargs):
        client, transport, _ = _client([{"id": "j1"}])
        client.submit("https://x", min_slides=3, max_slides=6, speech_hint="頻道。標題",
                      speaker_mode=SpeakerMode.DIARIZE, speaker_count=None, **kwargs)
        return transport.calls[0]["body"]

    def test_a_range_goes_out_after_the_speaker_keys(self):
        body = self._body(range_start=600, range_end=3480)
        self.assertEqual(list(body), ["url", "detailed", "min_slides", "max_slides", "speech_hint",
                                      "speaker_mode", "speaker_count", "range_start", "range_end"])
        self.assertEqual((body["range_start"], body["range_end"]), (600, 3480))

    def test_an_open_end_is_left_out_not_sent_as_null(self):
        """省略終點＝一直到影片結尾；GPU 那邊沒有 range_end 這個鍵與 null 是同一個意思，但 Pi 只送有值的鍵。"""
        body = self._body(range_start=600)
        self.assertEqual(body["range_start"], 600)
        self.assertNotIn("range_end", body)

    def test_a_start_of_zero_is_still_sent(self):
        """從頭開始、只截到某處（--end 58:00）：range_start 是 0，不能因為「0 是假值」而不見了。"""
        body = self._body(range_start=0, range_end=1800)
        self.assertEqual((body["range_start"], body["range_end"]), (0, 1800))

    def test_an_end_alone_starts_at_zero(self):
        body = self._body(range_end=1800)
        self.assertEqual((body["range_start"], body["range_end"]), (0, 1800))

    def test_without_a_range_the_body_is_exactly_the_contract_example(self):
        request = CONTRACT["request"]
        client, transport, _ = _client([{"id": "j1"}])
        client.submit(request["url"], detailed=request["detailed"], min_slides=request["min_slides"],
                      max_slides=request["max_slides"], speech_hint=request["speech_hint"],
                      speaker_mode=SpeakerMode.DIARIZE, speaker_count=request["speaker_count"],
                      range_start=None, range_end=None)
        self.assertEqual(transport.calls[0]["body"], request)
        self.assertEqual(list(transport.calls[0]["body"]), list(request))
        self.assertNotIn("range_start", request)

    def test_other_kinds_of_request_never_carry_a_range_key(self):
        client, transport, _ = _client([{"id": "j1"}, {"id": "j2"}, {"id": "j3"}])
        client.submit("https://ivod.ly.gov.tw/Play/Clip/1M/900001", detailed=True, speech_hint="立法院")
        client.submit("https://x", min_slides=3, max_slides=6, speech_hint="頻道。標題")
        client.submit_topic("一段文字", [{"key": "a", "label": "甲", "description": "乙"}])
        for call in transport.calls:
            self.assertFalse({"range_start", "range_end"} & set(call["body"]))

    def test_a_range_without_the_multi_speaker_mode_is_refused_not_dropped(self):
        """單人與議會的請求沒有範圍這個概念：悄悄丟掉的話，操作者要的一段會變成整支影片。"""
        for kwargs in ({}, {"speaker_mode": SpeakerMode.SINGLE}, {"speaker_mode": None}):
            for rng in ({"range_start": 600}, {"range_end": 1800}, {"range_start": 0, "range_end": 1800}):
                with self.subTest(kwargs=kwargs, range=rng):
                    client, transport, _ = _client([{"id": "j1"}])
                    with self.assertRaises(ValueError) as caught:
                        client.submit("https://x", **kwargs, **rng)
                    self.assertIn("多人", str(caught.exception))
                    self.assertEqual(transport.calls, [])

    def test_a_bad_range_never_reaches_the_wire(self):
        for rng in ({"range_start": -1}, {"range_start": True}, {"range_start": "600"}, {"range_start": 6.5},
                    {"range_start": float("nan")}, {"range_start": 90001},
                    {"range_start": 600, "range_end": 600}, {"range_start": 600, "range_end": 100},
                    {"range_start": 600, "range_end": 603}, {"range_start": 600, "range_end": "3480"},
                    {"range_start": 600, "range_end": 3480.5}, {"range_start": 600, "range_end": True},
                    {"range_start": 600, "range_end": 90001}):
            with self.subTest(range=rng):
                client, transport, _ = _client([{"id": "j1"}])
                with self.assertRaises(ValueError):
                    client.submit("https://x", speaker_mode=SpeakerMode.DIARIZE, **rng)
                self.assertEqual(transport.calls, [])

    def test_the_smallest_valid_range_goes_out(self):
        body = self._body(range_start=600, range_end=605)
        self.assertEqual((body["range_start"], body["range_end"]), (600, 605))


class FakeSignatureTests(SimpleTestCase):
    def test_the_real_submit_takes_the_new_options_last_and_defaults_to_none(self):
        import inspect

        parameters = inspect.signature(GpuApiClient.submit).parameters
        self.assertEqual(list(parameters)[-4:], ["speaker_mode", "speaker_count", "range_start", "range_end"])
        for name in ("speaker_mode", "speaker_count", "range_start", "range_end"):
            self.assertIsNone(parameters[name].default, name)
