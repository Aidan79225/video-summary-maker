"""立法院清單來源：用假的 fetch，不碰網路。"""
from __future__ import annotations

import json
from datetime import date

from django.test import SimpleTestCase

from articles.ivod_source import IvodDailySource, IvodUnavailable


def _row(ivod_id, speaker="洪毓祥", features=("ai-transcript",), duration=197):
    return {
        "IVOD_ID": ivod_id,
        "日期": "2026-08-27",
        "影片種類": "Clip",
        "委員名稱": speaker,
        "影片長度": duration,
        "IVOD_URL": f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
        "會議資料": {"標題": "第11屆第5會期第23次會議"},
        "支援功能": list(features),
    }


class FakeFetch:
    def __init__(self, pages):
        self._pages = pages
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return json.dumps(self._pages[len(self.urls) - 1], ensure_ascii=False)


def _source(pages, fetch=None):
    fetch = fetch or FakeFetch(pages)
    return IvodDailySource("https://example/ivods", fetch=fetch), fetch


class IvodSourceTests(SimpleTestCase):
    def test_rows_become_clips(self):
        source, _ = _source([{"ivods": [_row(171180)], "total_page": 1}])
        clips = source.clips_for(date(2026, 8, 27))
        self.assertEqual(len(clips), 1)
        clip = clips[0]
        self.assertEqual(clip.ivod_id, "171180")
        self.assertEqual(clip.speaker, "洪毓祥")
        self.assertEqual(clip.duration_seconds, 197)
        self.assertIn("171180", clip.ivod_url)
        self.assertIn("洪毓祥", clip.title)
        self.assertIn("第23次會議", clip.title)

    def test_the_query_asks_for_that_day_and_only_clips(self):
        """攔的 bug：不帶篩選就會抓到全部十萬筆，而且混進 8 小時的完整會議
        ——那種既不是新聞，影片主機也連不上、拿不到截圖。"""
        source, fetch = _source([{"ivods": [], "total_page": 1}])
        source.clips_for(date(2026, 8, 27))
        url = fetch.urls[0]
        self.assertIn("2026-08-27", url)
        self.assertIn("Clip", url)

    def test_clips_without_an_ai_transcript_are_left_out(self):
        """沒有逐字稿的片段送去 GPU 一定失敗——摘要 API 會直接說沒有逐字稿。
        在這裡就濾掉，不要浪費一輪往返。"""
        pages = [{"ivods": [_row(1, features=()), _row(2)], "total_page": 1}]
        source, _ = _source(pages)
        self.assertEqual([c.ivod_id for c in source.clips_for(date(2026, 8, 27))], ["2"])

    def test_every_page_is_fetched(self):
        """攔的 bug：只抓第一頁。開會日一天可以有上百段發言，
        limit 是 100——第 101 段之後會靜悄悄地不存在。"""
        pages = [
            {"ivods": [_row(1)], "total_page": 2},
            {"ivods": [_row(2)], "total_page": 2},
        ]
        source, fetch = _source(pages)
        clips = source.clips_for(date(2026, 8, 27))
        self.assertEqual([c.ivod_id for c in clips], ["1", "2"])
        self.assertEqual(len(fetch.urls), 2)

    def test_the_two_duration_formats_are_both_understood(self):
        """清單端點給秒數（197），單筆端點給 "00:03:17"。"""
        pages = [{"ivods": [_row(1, duration="00:03:17")], "total_page": 1}]
        source, _ = _source(pages)
        self.assertEqual(source.clips_for(date(2026, 8, 27))[0].duration_seconds, 197)

    def test_a_broken_response_is_reported_as_unavailable(self):
        class Failing:
            def __call__(self, url):
                raise OSError("連線逾時")

        source, _ = _source([], fetch=Failing())
        with self.assertRaises(IvodUnavailable):
            source.clips_for(date(2026, 8, 27))

    def test_rows_without_an_id_are_skipped_rather_than_crashing(self):
        pages = [{"ivods": [{"日期": "2026-08-27"}, _row(2)], "total_page": 1}]
        source, _ = _source(pages)
        self.assertEqual([c.ivod_id for c in source.clips_for(date(2026, 8, 27))], ["2"])
