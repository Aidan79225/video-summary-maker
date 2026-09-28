"""臺中市議會清單來源：用存下來的真實頁面，不碰網路。"""
from __future__ import annotations

import os
from datetime import date

from django.test import SimpleTestCase

from articles.ivod_source import SourceUnavailable
from articles.tccc_source import (TcccDailySource, parse_clip_rows, parse_councilors,
                                  parse_selected_duration)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeFetch:
    """依網址片段回頁面（較長的 key 要排前面）；error_for 裡的網址丟 OSError；
    沒準備的議員頁回一張沒有片段的空頁，模擬「這位議員那天沒發言」。"""

    def __init__(self, pages, error_for=()):
        self.pages = pages
        self.error_for = tuple(error_for)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if any(e in url for e in self.error_for):
            raise OSError("connection refused")
        for key, html in self.pages.items():
            if key in url:
                return html
        return "<html><body><table></table></body></html>"


class TlsTests(SimpleTestCase):
    def test_fetch_keeps_verification_but_drops_the_strict_flag(self):
        """Python 3.13 的預設 context 會拒絕市議會的憑證鏈（CA 憑證缺 Subject Key
        Identifier）。只能關 strict，不能關驗證。"""
        import ssl

        from articles.tccc_source import _ssl_context

        context = _ssl_context()
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class ParseTests(SimpleTestCase):
    def test_councilors_are_read_with_their_ids(self):
        rows = parse_councilors(_read("tccc_region01.html"))
        self.assertEqual(len(rows), 61)
        self.assertIn(("85", "楊啓邦"), rows)

    def test_clip_rows_include_the_selected_clip_via_the_pager(self):
        rows = parse_clip_rows(_read("tccc_region02_85.html"))
        self.assertEqual(rows[0].ano, "14833")
        self.assertEqual(rows[0].meeting, "第4屆第8次定期會 市政總質詢")
        self.assertEqual(rows[0].date, "2026-09-24")
        self.assertEqual(rows[1].ano, "14745")
        self.assertEqual(rows[1].date, "2026-09-02")
        self.assertEqual(len(rows), 10)

    def test_when_another_clip_is_selected_the_newest_is_a_plain_link(self):
        rows = parse_clip_rows(_read("tccc_region02_85_14745.html"))
        self.assertEqual([r.ano for r in rows[:2]], ["14833", "14745"])
        self.assertEqual(len(rows), 10)

    def test_selected_duration_is_hours_and_minutes(self):
        self.assertEqual(parse_selected_duration(_read("tccc_region02_85.html")), 50 * 60)
        self.assertEqual(parse_selected_duration(_read("tccc_region02_85_14745.html")), 15 * 60)
        self.assertEqual(parse_selected_duration("<html></html>"), 0)


class SourceTests(SimpleTestCase):
    def _source(self, error_for=()):
        pages = {
            "wb_region01.asp": _read("tccc_region01.html"),
            "cno=85&ano=14745": _read("tccc_region02_85_14745.html"),
            "cno=85": _read("tccc_region02_85.html"),
        }
        fetch = FakeFetch(pages, error_for)
        return TcccDailySource("https://vod.example/", fetch=fetch), fetch

    def test_only_clips_on_that_day_become_clips(self):
        source, _ = self._source()
        clips = source.clips_for(date(2026, 9, 2))
        self.assertEqual([c.ivod_id for c in clips], ["tccc-14745"])
        clip = clips[0]
        self.assertEqual(clip.speaker, "楊啓邦")
        self.assertEqual(clip.meeting, "第4屆第8次定期會 業務質詢：都發建設水利部分")
        self.assertEqual(clip.date, "2026-09-02")
        self.assertEqual(clip.duration_seconds, 15 * 60)
        self.assertEqual(clip.ivod_url, "https://vod.example/index.asp?url=12&cno=85&ano=14745")
        self.assertEqual(clip.source, "tccc")
        self.assertTrue(clip.has_transcript)

    def test_the_newest_clip_is_found_even_though_it_has_no_link(self):
        source, _ = self._source()
        clips = source.clips_for(date(2026, 9, 24))
        self.assertEqual([c.ivod_id for c in clips], ["tccc-14833"])
        self.assertEqual(clips[0].meeting, "第4屆第8次定期會 市政總質詢")

    def test_every_councilor_page_is_visited_once(self):
        source, fetch = self._source()
        source.clips_for(date(2026, 1, 1))
        listing = [u for u in fetch.urls if "wb_region02.asp" in u and "ano=" not in u]
        self.assertEqual(len(listing), 61)

    def test_a_quiet_day_yields_nothing(self):
        source, _ = self._source()
        self.assertEqual(source.clips_for(date(2026, 1, 1)), [])

    def test_one_broken_councilor_page_does_not_kill_the_day(self):
        source, _ = self._source(error_for=["cno=64"])
        with self.assertLogs("articles.tccc_source", level="WARNING"):
            clips = source.clips_for(date(2026, 9, 2))
        self.assertEqual([c.ivod_id for c in clips], ["tccc-14745"])

    def test_a_broken_councilor_list_is_unavailable(self):
        source, _ = self._source(error_for=["wb_region01.asp"])
        with self.assertRaises(SourceUnavailable):
            source.clips_for(date(2026, 9, 2))

    def test_a_list_page_without_councilors_is_unavailable(self):
        fetch = FakeFetch({"wb_region01.asp": "<html>改版了</html>"})
        with self.assertRaises(SourceUnavailable):
            TcccDailySource("https://vod.example", fetch=fetch).clips_for(date(2026, 9, 2))

    def test_a_missing_duration_page_still_registers_the_clip(self):
        source, _ = self._source(error_for=["ano=14745"])
        with self.assertLogs("articles.tccc_source", level="WARNING"):
            clips = source.clips_for(date(2026, 9, 2))
        self.assertEqual(clips[0].duration_seconds, 0)
