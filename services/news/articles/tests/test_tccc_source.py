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


def _councilor_list(*pairs):
    return "".join(f'<a href="index.asp?url=12&cno={c}" target="_top"><font color="blue">{n}</font></a>'
                   for c, n in pairs)


def _row(cno, ano, meeting, day):
    return (f'<tr><td><a href="index.asp?url=12&cno={cno}&ano={ano}&pageno=1" target="_top">{meeting}</a></td>'
            f'<td valign="top" nowrap="nowrap">{day}</td></tr>')


def _clip_page(player, day, meeting, hhmm="00:50"):
    return (f'<iframe class="embed-responsive-item" src="{player}"></iframe>'
            f'<font color="#0D57BB">{meeting}</font>'
            f'<td>會議日期：</td><td width="100%">{day}</td>'
            f'<td>影片長度：</td><td>{hhmm}</td>')


class JointInterpellationTests(SimpleTestCase):
    """聯合質詢：同一支影片掛在三位議員名下，只登記一篇、講者用頓號串起。"""

    JOINT = "https://rds.ginnet.cloud/player/x/joint"
    SOLO = "https://rds.ginnet.cloud/player/x/solo"
    DAY = "2026-09-24"

    def _source(self):
        meeting_joint = "市政總質詢(甲、乙、丙等議員聯合質詢)"
        pages = {
            "wb_region01.asp": _councilor_list(("1", "甲"), ("2", "乙"), ("3", "丙"), ("4", "丁")),
            # 每位議員的清單頁有分頁連結（選中的是更早的一筆），所以不會觸發二次抓取
            "cno=1&ano=11": _clip_page(self.JOINT, self.DAY, meeting_joint),
            "cno=2&ano=12": _clip_page(self.JOINT, self.DAY, meeting_joint),
            "cno=3&ano=13": _clip_page(self.JOINT, self.DAY, meeting_joint),
            "cno=4&ano=14": _clip_page(self.SOLO, self.DAY, "市政總質詢", "00:45"),
            "cno=1": _row("1", "11", meeting_joint, self.DAY) + "&ano=1&PageNo=1",
            "cno=2": _row("2", "12", meeting_joint, self.DAY) + "&ano=2&PageNo=1",
            "cno=3": _row("3", "13", meeting_joint, self.DAY) + "&ano=3&PageNo=1",
            "cno=4": _row("4", "14", "市政總質詢", self.DAY) + "&ano=4&PageNo=1",
        }
        return TcccDailySource("https://vod.example", fetch=FakeFetch(pages))

    def test_the_same_video_is_registered_once_with_all_speakers(self):
        clips = self._source().clips_for(date(2026, 9, 24))
        self.assertEqual([(c.ivod_id, c.speaker) for c in clips],
                         [("tccc-11", "甲、乙、丙"), ("tccc-14", "丁")])
        self.assertEqual(clips[0].duration_seconds, 50 * 60)
        self.assertEqual(clips[1].duration_seconds, 45 * 60)


class NoPagerTests(SimpleTestCase):
    """片段少於一頁時沒有分頁連結，要靠第二次抓取拿到最新那筆；兩次要合併。"""

    def test_both_the_newest_and_the_selected_clip_survive(self):
        newest_page = _clip_page("https://p/new", "2026-09-24", "市政總質詢")
        # 第一次（不帶 ano）：最新的 20 被選中沒有連結，只看得到 19
        first = _row("1", "19", "業務質詢", "2026-09-02") + newest_page
        # 第二次（ano=19）：19 被選中沒有連結，換成 20 有連結
        second = _row("1", "20", "市政總質詢", "2026-09-24") + \
            _clip_page("https://p/old", "2026-09-02", "業務質詢", "00:15")
        pages = {
            "wb_region01.asp": _councilor_list(("1", "甲")),
            "cno=1&ano=19": second,
            "cno=1&ano=20": newest_page,
            "cno=1": first,
        }
        source = TcccDailySource("https://vod.example", fetch=FakeFetch(pages))
        self.assertEqual([c.ivod_id for c in source.clips_for(date(2026, 9, 24))], ["tccc-20"])
        self.assertEqual([c.ivod_id for c in source.clips_for(date(2026, 9, 2))], ["tccc-19"])


class CommandSourceTests(SimpleTestCase):
    """--source 只查一個來源；沒給就看 TCCC_ENABLED。"""

    def _names(self, only, tccc_enabled=True):
        from django.test import override_settings

        from articles.management.commands.ingest_ivod import Command

        with override_settings(TCCC_ENABLED=tccc_enabled):
            return [s.name for s in Command()._sources(only)]

    def test_no_flag_queries_both_when_taichung_is_enabled(self):
        self.assertEqual(self._names(None), ["立法院", "臺中市議會"])

    def test_no_flag_skips_taichung_when_disabled(self):
        self.assertEqual(self._names(None, tccc_enabled=False), ["立法院"])

    def test_the_flag_picks_one_source_regardless_of_the_setting(self):
        self.assertEqual(self._names("tccc", tccc_enabled=False), ["臺中市議會"])
        self.assertEqual(self._names("ly"), ["立法院"])
