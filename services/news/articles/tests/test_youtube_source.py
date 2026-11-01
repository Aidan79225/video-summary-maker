"""YouTube 影片的網址解析、影片資料（oEmbed＋影片頁）、日期換算、頁數範圍與預檢。

全部離線：影片頁與 oEmbed 用 fixtures 裡存下來的真實頁面（縮減到解析器會讀的欄位），
抓取用注入的假 callable。直播中、尚未首播、剛結束的直播、不公開、私人、機器人檢查這幾種
頁面沒辦法穩定抓到真的，是把真實頁面的旗標改過來（_page 的 patch 參數），欄位名稱與型別
都照真實頁面。
"""
from __future__ import annotations

import http.server
import json
import os
import threading
import urllib.request
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from articles import youtube_source as yt
from articles.ivod_source import SourceUnavailable
from articles.youtube_source import (
    HttpResponse,
    MetadataUnavailable,
    PreflightOptions,
    UrlRejected,
    VideoMeta,
    VideoRefused,
    canonical_url,
    clean_speaker,
    fetch_metadata,
    parse_oembed,
    parse_video_url,
    parse_watch_page,
    placeholder_title,
    preflight,
    slide_range,
    taipei_date,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ZOO, STREAM, TALK, MUSIC, SHORT, LIVE = (
    "jNQXAC9IVRw", "f4r2yWk4LV0", "nmzKH9t4Zos", "dQw4w9WgXcQ", "etcRgjG4qRo", "0sfDth4lMLc")
MISSING = "Xq9Zk2LmN7o"


def _read(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _watch(video_id: str) -> str:
    return _read(f"youtube_watch_{video_id}.html")


def _page(video_id: str, patch=None, **changes) -> str:
    """真實的影片頁，把播放器回應的幾個旗標改掉（見模組說明）。

    patch(player_response) 就地修改；changes 是 {"videoDetails.isLive": True} 這種路徑寫法。
    """
    text = _watch(video_id)
    marker = "var ytInitialPlayerResponse = "
    head = text.index(marker) + len(marker)
    obj, end = json.JSONDecoder().raw_decode(text, head)
    for path, value in changes.items():
        node = obj
        *parents, leaf = path.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    if patch:
        patch(obj)
    return text[:head] + json.dumps(obj, ensure_ascii=False) + text[end:]


class FakeFetch:
    """依網址關鍵字回傳預先寫好的回應（或丟例外）；記下每一次呼叫。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


def _ok(body, url="https://www.youtube.com/watch?v=x", status=200):
    return HttpResponse(status, url, body if isinstance(body, bytes) else body.encode("utf-8"))


class ParseUrlTests(SimpleTestCase):
    ID = STREAM

    def assertParsed(self, text, video_id=STREAM):
        ref = parse_video_url(text)
        self.assertEqual(ref.video_id, video_id, text)
        self.assertEqual(ref.url, f"https://www.youtube.com/watch?v={video_id}")
        return ref

    def test_every_accepted_shape_gives_the_same_video_and_the_canonical_url(self):
        i = self.ID
        for text in (
            f"https://www.youtube.com/watch?v={i}", f"http://www.youtube.com/watch?v={i}",
            f"https://youtube.com/watch?v={i}", f"https://m.youtube.com/watch?v={i}",
            f"https://music.youtube.com/watch?v={i}", f"https://www.youtube.com/watch?v={i}&t=30s",
            f"https://www.youtube.com/watch?feature=share&v={i}", f"https://www.youtube.com/watch?v={i}#t=30",
            f"https://www.youtube.com/watch/?v={i}",
            f"https://youtu.be/{i}", f"https://youtu.be/{i}?t=3", f"https://youtu.be/{i}?si=abcdEFGH",
            f"https://youtu.be/{i}/", f"https://www.youtu.be/{i}",
            f"https://www.youtube.com/shorts/{i}", f"https://www.youtube.com/live/{i}",
            f"https://www.youtube.com/live/{i}?feature=share", f"https://www.youtube.com/embed/{i}",
            f"https://www.youtube-nocookie.com/embed/{i}", f"https://www.youtube.com/v/{i}",
            f"https://www.youtube.com/e/{i}",
            f"www.youtube.com/watch?v={i}", f"youtube.com/watch?v={i}", f"youtu.be/{i}",
            f"//www.youtube.com/watch?v={i}", f"  https://www.youtube.com/watch?v={i}\n",
            f"HTTPS://WWW.YOUTUBE.COM/watch?v={i}", f"https://www.youtube.com:443/watch?v={i}",
            f"https://www.youtube.com/watch?v={i}&v={i}", i,
        ):
            with self.subTest(text=text):
                self.assertParsed(text)

    def test_the_canonical_url_ignores_time_and_tracking_parameters(self):
        self.assertEqual(parse_video_url(f"https://youtu.be/{self.ID}?t=1h2m&si=zzz").url,
                         canonical_url(self.ID))

    def test_an_id_that_starts_with_a_dash_is_a_normal_id(self):
        """約 64 分之一的 id 以「-」開頭：不能被當成選項、也不能被吃掉。"""
        dash = "-abcdefghij"
        self.assertParsed(dash, dash)
        self.assertParsed(f"https://youtu.be/{dash}", dash)
        self.assertParsed(f"https://www.youtube.com/watch?v={dash}&t=3", dash)
        self.assertEqual(yt.ivod_id_for(dash), "yt--abcdefghij")

    def test_a_playlist_that_comes_with_a_video_is_ignored_and_says_so(self):
        ref = self.assertParsed(f"https://www.youtube.com/watch?v={self.ID}&list=PLabcdefghijklmnop&index=2")
        self.assertEqual(len(ref.notes), 1)
        self.assertIn("播放清單", ref.notes[0])
        self.assertEqual(parse_video_url(f"https://youtu.be/{self.ID}").notes, ())

    def assertRejected(self, text, *words):
        with self.assertRaises(UrlRejected) as caught:
            parse_video_url(text)
        message = str(caught.exception)
        for word in words:
            self.assertIn(word, message, text)
        return message

    def test_playlists_channels_search_and_clips_are_refused_with_a_reason(self):
        self.assertRejected("https://www.youtube.com/playlist?list=PLabcdefghijklmnopqrstuvwxyz012345",
                            "播放清單")
        self.assertRejected("https://www.youtube.com/watch?list=PLabcdefghijklmnopqrstuvwxyz012345", "播放清單")
        for channel in ("https://www.youtube.com/@KC-Huang", "https://www.youtube.com/@KC-Huang/live",
                        "https://www.youtube.com/channel/UCNWAsexY9eiS1N6JDns-0kw/live",
                        "https://www.youtube.com/c/SomeChannel", "https://www.youtube.com/user/SomeUser"):
            with self.subTest(channel=channel):
                self.assertRejected(channel, "頻道", "影片自己的網址")
        self.assertRejected("https://www.youtube.com/results?search_query=test", "搜尋")
        self.assertRejected("https://www.youtube.com/clip/UgkxAbCdEfGhIjKlMnOpQrStUvWxYz012345",
                            "clip", "沒有影片 id")

    def test_an_id_that_is_not_exactly_eleven_characters_is_never_truncated(self):
        """yt-dlp 會把太長的 id 默默切成前 11 碼；這裡寧可拒絕，也不處理一支沒人指名的影片。"""
        for text in (f"https://www.youtube.com/watch?v={self.ID}ZZ", "https://www.youtube.com/watch?v=short",
                     f"https://youtu.be/{self.ID}ZZ", f"https://www.youtube.com/shorts/{self.ID[:-1]}",
                     f"https://www.youtube.com/embed/{self.ID}abc", "https://youtu.be/"):
            with self.subTest(text=text):
                self.assertRejected(text, "11 碼")

    def test_obviously_wrong_shapes_are_refused(self):
        i = self.ID
        self.assertRejected(f"https://www.youtube.com/watch?v={i}&v=jNQXAC9IVRw", "不只一個", "v=")
        self.assertRejected("https://www.youtube.com/watch", "v=")
        self.assertRejected("https://www.youtube.com/embed/videoseries?list=PLabcdefghijklmnop", "不是單一影片")
        self.assertRejected("https://www.youtube.com/live_stream?channel=UCabc", "不是單一影片")
        self.assertRejected("https://www.youtube.com/", "影片 id")
        self.assertRejected("https://www.youtube.com/feed/subscriptions", "影片")
        self.assertRejected(f"https://www.youtube.com/watch/{i}", "影片 id")

    def test_the_host_has_to_be_youtube_exactly(self):
        i = self.ID
        for text in (f"https://evilyoutube.com/watch?v={i}", f"https://www.youtube.com.evil.com/watch?v={i}",
                     f"https://www.youtube.com@evil.com/watch?v={i}", f"https://notyoutu.be/{i}",
                     f"https://www.youtube.com.evil.com:443/watch?v={i}",
                     "https://ivod.ly.gov.tw/Play/Clip/1M/171180"):
            with self.subTest(text=text):
                self.assertRejected(text, "不是 YouTube")

    def test_userinfo_ports_and_schemes_are_refused(self):
        i = self.ID
        self.assertRejected(f"https://someone@www.youtube.com/watch?v={i}", "帳號")
        self.assertRejected(f"https://www.youtube.com:8443/watch?v={i}", "連接埠")
        self.assertRejected(f"https://www.youtube.com:99999/watch?v={i}", "格式")
        for text in (f"ftp://www.youtube.com/watch?v={i}", "file:///C:/x", f"javascript://www.youtube.com/%0a{i}"):
            with self.subTest(text=text):
                self.assertRejected(text, "http")

    def test_nothing_and_several_urls_are_refused(self):
        self.assertRejected("", "沒有給")
        self.assertRejected("   \n", "沒有給")
        self.assertRejected(f"https://youtu.be/{self.ID} https://youtu.be/{ZOO}", "空白")

    def test_words_that_look_like_ids_are_not_ids(self):
        """videoseries 與 live_stream 剛好 11 個字，卻是網址裡的固定字。"""
        self.assertRejected("videoseries", "不是單一影片")
        self.assertRejected("https://youtu.be/live_stream", "不是單一影片")

    def test_a_bare_eleven_character_word_is_an_id_but_a_host_is_not(self):
        self.assertParsed("abcdefghijk", "abcdefghijk")
        self.assertRejected("youtube.com", "影片 id")

    def test_the_stored_id_round_trips(self):
        self.assertEqual(yt.ivod_id_for(ZOO), f"yt-{ZOO}")
        self.assertEqual(yt.video_id_from_ivod_id(f"yt-{ZOO}"), ZOO)
        self.assertIsNone(yt.video_id_from_ivod_id("tccc-14833"))
        self.assertIsNone(yt.video_id_from_ivod_id("yt-short"))
        self.assertEqual(placeholder_title(ZOO), f"YouTube 影片 {ZOO}")


class DashIdTests(SimpleTestCase):
    """單獨的影片 id 以「-」開頭（約 64 分之一）時，argparse 會當成選項。"""

    def test_a_bare_id_with_a_leading_dash_goes_behind_a_double_dash(self):
        self.assertEqual(yt.dash_ids_last(["-abcdefghij", "--speakers", "1"]),
                         ["--speakers", "1", "--", "-abcdefghij"])
        self.assertEqual(yt.dash_ids_last(["https://youtu.be/x", "-abcdefghij", "-ABCDEFGH_k"]),
                         ["https://youtu.be/x", "--", "-abcdefghij", "-ABCDEFGH_k"])

    def test_everything_else_is_left_exactly_as_it_was(self):
        for args in (["--speakers", "1", "https://youtu.be/jNQXAC9IVRw"], ["jNQXAC9IVRw"], [], ["-v", "2"],
                     ["--traceback", "jNQXAC9IVRw"],                 # --traceback 剛好也是 11 個字
                     ["--", "-abcdefghij"], ["-abcdefghij", "--", "x"], ["-abcdefghijk"],     # 已有 --；12 個字
                     ["--abcdefghi"]):
            with self.subTest(args=args):
                self.assertEqual(yt.dash_ids_last(args), args)
        self.assertIsNone(yt.dash_ids_last(None))

    def test_the_parser_is_wrapped_so_the_real_command_line_works(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("urls", nargs="+")
        parser.add_argument("--speakers")
        parsed = yt.allow_dash_ids(parser).parse_args(["-abcdefghij", "--speakers", "1"])
        self.assertEqual((parsed.urls, parsed.speakers), (["-abcdefghij"], "1"))


class ParseOembedTests(SimpleTestCase):
    def test_a_real_response(self):
        data = parse_oembed(_read(f"youtube_oembed_{STREAM}.json").encode("utf-8"))
        self.assertEqual((data.title, data.author_name, data.author_url),
                         ("【國昌直播】來看看我上週在做什麼_1019", "黃國昌", "https://www.youtube.com/@KC-Huang"))

    def test_junk_is_nothing(self):
        for body in (b"", b"Bad Request", b"[]", b'{"type":"video"}', b"\xff\xfe", b'{"title":3}', None):
            with self.subTest(body=body):
                self.assertIsNone(parse_oembed(body))


class ParseWatchPageTests(SimpleTestCase):
    def parse(self, video_id, text=None):
        return parse_watch_page(text if text is not None else _watch(video_id), video_id)

    def test_an_ordinary_video(self):
        meta = self.parse(TALK)
        self.assertEqual((meta.title, meta.channel, meta.duration_seconds, meta.date),
                         ("為什麼整個 GitHub 都在瘋傳 REA？別人的程式碼，現在是你的了?", "P & H", 507,
                          date(2026, 10, 13)))
        self.assertEqual((meta.playable, meta.is_live, meta.is_upcoming, meta.is_post_live, meta.unlisted),
                         (True, False, False, False, False))
        self.assertEqual((meta.category, meta.shorts_eligible, meta.page_read), ("Education", False, True))
        self.assertEqual(meta.channel_url, "https://www.youtube.com/@PH-WorkFlow")

    def test_the_channel_link_is_upgraded_to_https(self):
        """microformat 的 ownerProfileUrl 是 http://。"""
        self.assertEqual(self.parse(ZOO).channel_url, "https://www.youtube.com/@jawed")

    def test_the_date_is_the_taipei_day_not_the_pacific_one(self):
        """動物園那支：影片頁寫太平洋時間 2005-04-23 20:31，台北已經是 04-24。"""
        meta = self.parse(ZOO)
        self.assertEqual((meta.date, meta.duration_seconds), (date(2005, 4, 24), 19))

    def test_a_finished_live_stream_is_dated_by_when_the_broadcast_started(self):
        """上傳時間（太平洋 10-19 10:26）換成台北已經是 10-20；直播是 10-19 19:51（台北）開始的，
        標題也寫 _1019。"""
        meta = self.parse(STREAM)
        self.assertEqual(meta.date, date(2026, 10, 19))
        self.assertEqual(meta.duration_seconds, 5715)
        self.assertEqual((meta.is_live, meta.is_upcoming, meta.is_post_live), (False, False, False))

    def test_a_live_stream_is_flagged(self):
        meta = self.parse(LIVE)
        self.assertTrue(meta.is_live)
        self.assertEqual((meta.duration_seconds, meta.category), (0, "Music"))

    def test_a_short_and_a_music_video_are_recognised(self):
        self.assertTrue(self.parse(SHORT).shorts_eligible)
        self.assertEqual(self.parse(MUSIC).category, "Music")
        self.assertFalse(self.parse(MUSIC).shorts_eligible)

    def test_a_missing_video_is_not_playable_and_the_reason_is_kept(self):
        """影片不存在影片頁照樣回 200，只能靠 playabilityStatus。"""
        meta = self.parse(MISSING)
        self.assertEqual((meta.playable, meta.playability_reason, meta.page_read),
                         (False, "This video is unavailable", True))

    def test_flags_the_page_only_carries_while_the_video_is_in_that_state(self):
        upcoming = self.parse(STREAM, _page(STREAM, **{
            "playabilityStatus.status": "LIVE_STREAM_OFFLINE",
            "playabilityStatus.reason": "This live event will begin in 2 hours.",
            "videoDetails.isUpcoming": True}))
        self.assertEqual((upcoming.is_upcoming, upcoming.playable), (True, False))
        post_live = self.parse(STREAM, _page(STREAM, **{"videoDetails.isPostLiveDvr": True}))
        self.assertTrue(post_live.is_post_live)
        unlisted = self.parse(ZOO, _page(ZOO, **{"microformat.playerMicroformatRenderer.isUnlisted": True}))
        self.assertTrue(unlisted.unlisted)
        live_now = self.parse(STREAM, _page(STREAM, **{
            "microformat.playerMicroformatRenderer.liveBroadcastDetails.isLiveNow": True}))
        self.assertTrue(live_now.is_live)

    def test_the_reason_can_be_buried_in_the_error_screen(self):
        text = _page(ZOO, patch=lambda pr: pr["playabilityStatus"].update(
            status="UNPLAYABLE", errorScreen={"playerErrorMessageRenderer": {
                "reason": {"simpleText": "Video unavailable"}}}))
        meta = self.parse(ZOO, text)
        self.assertEqual((meta.playable, meta.playability_reason), (False, "Video unavailable"))

    def test_without_the_player_response_the_microdata_fills_in(self):
        """第二層：播放器回應整段不見（改版、被截斷）。標題、日期、長度、頻道、直播都在 head 的 microdata。"""
        text = _watch(STREAM)
        text = text[:text.index("<script")] + "</head></html>"
        meta = self.parse(STREAM, text)
        self.assertEqual((meta.title, meta.channel, meta.duration_seconds, meta.date),
                         ("【國昌直播】來看看我上週在做什麼_1019", "黃國昌", 5716, date(2026, 10, 19)))
        self.assertEqual((meta.category, meta.playable), ("People & Blogs", None))
        self.assertTrue(meta.page_read)

    def test_without_json_or_microdata_the_regex_finds_what_it_can(self):
        """第三層：JSON 被截斷到解不出來，microdata 也沒有，只剩文字裡的欄位。"""
        text = _watch(ZOO)
        cut = text.index('"microformat"')
        broken = text[cut:text.index('"isShortsEligible"')]          # 沒有收尾的括號：raw_decode 解不出來
        text = ("<html><head><title>Me at the zoo - YouTube</title></head><script>"
                "var ytInitialPlayerResponse = {" + broken)
        meta = self.parse(ZOO, text)
        self.assertEqual((meta.title, meta.channel, meta.duration_seconds, meta.date),
                         ("Me at the zoo", "jawed", 19, date(2005, 4, 24)))
        self.assertIsNone(meta.playable)

    def test_the_title_tag_is_the_last_resort(self):
        meta = self.parse(ZOO, "<html><head><title>只剩標題 - YouTube</title></head></html>")
        self.assertEqual((meta.title, meta.channel, meta.duration_seconds, meta.date), ("只剩標題", None, 0, None))
        self.assertTrue(meta.page_read)

    def test_a_page_that_is_not_a_video_page_is_nothing(self):
        for text in ("", "<html><body>hello</body></html>", "<html><title></title></html>",
                     "ytInitialPlayerResponse = "):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(ZOO, text))

    def test_the_regex_layer_is_not_used_when_the_json_decodes(self):
        """整個頁面還有推薦影片的 JSON：播放器回應解得出來時，缺的欄位就是沒有，不去撈別支的數字。"""
        def drop_the_length(pr):
            del pr["videoDetails"]["lengthSeconds"]
            del pr["microformat"]["playerMicroformatRenderer"]["lengthSeconds"]

        text = _page(ZOO, patch=drop_the_length).replace('<meta itemprop="duration" content="PT0M19S">', "")
        decoy = '<script>var related = {"lengthSeconds":"999","startTimestamp":"2020-01-01T00:00:00Z"};</script>'
        text = text.replace("</head>", decoy + "</head>")
        meta = self.parse(ZOO, text)
        self.assertEqual((meta.duration_seconds, meta.date), (0, date(2005, 4, 24)))

    def test_a_broken_player_response_does_not_hide_the_microdata(self):
        text = _watch(TALK).replace('"videoDetails"', '"videoDetails" BROKEN')
        meta = self.parse(TALK, text)
        self.assertEqual((meta.title, meta.duration_seconds), (
            "為什麼整個 GitHub 都在瘋傳 REA？別人的程式碼，現在是你的了?", 507))

    def test_dates_without_a_time_are_kept_as_they_are(self):
        text = _page(ZOO, patch=lambda pr: pr["microformat"]["playerMicroformatRenderer"].update(
            uploadDate="2005-04-23", publishDate="2005-04-23"))
        self.assertEqual(self.parse(ZOO, text).date, date(2005, 4, 23))


class DatesTests(SimpleTestCase):
    def test_taipei_dates(self):
        for text, expected in (
            ("2005-04-23T20:31:52-07:00", date(2005, 4, 24)),
            ("2026-10-19T10:26:14-07:00", date(2026, 10, 20)),
            ("2026-10-19T11:51:25+00:00", date(2026, 10, 19)),
            ("2026-10-19T16:00:00Z", date(2026, 10, 20)),
            ("2026-10-19T15:59:59Z", date(2026, 10, 19)),
            ("2026-10-19T00:30:00+08:00", date(2026, 10, 19)),
            ("2026-10-19", date(2026, 10, 19)),
            ("2026-10-19T10:26:14", date(2026, 10, 19)),
        ):
            with self.subTest(text=text):
                self.assertEqual(taipei_date(text), expected)

    def test_unreadable_dates_are_none(self):
        for text in (None, "", "昨天", "2026-13-45", "2026-10-19T25:00:00Z"):
            with self.subTest(text=text):
                self.assertIsNone(taipei_date(text))

    def test_it_still_works_when_the_system_has_no_time_zone_database(self):
        """Taipei 沒有夏令時間，固定 +8 就對：容器裡沒有 tzdata 也不能讓日期換算壞掉。"""
        from zoneinfo import ZoneInfoNotFoundError

        yt._taipei.cache_clear()
        try:
            with mock.patch.object(yt, "ZoneInfo", side_effect=ZoneInfoNotFoundError("Asia/Taipei")):
                self.assertEqual(taipei_date("2005-04-23T20:31:52-07:00"), date(2005, 4, 24))
        finally:
            yt._taipei.cache_clear()


class SlideRangeTests(SimpleTestCase):
    def test_the_range_follows_the_length(self):
        for seconds, expected in (
            (19, (3, 6)),          # 19 秒的影片只產得出 3 頁，預設的 8～15 會讓它失敗
            (120, (3, 6)),
            (507, (3, 6)),
            (1080, (3, 9)),        # 18 分鐘：min = ceil(18/6) = 3、max = ceil(18/2) = 9
            (1800, (5, 15)),
            (3600, (8, 30)),
            (5715, (8, 48)),       # 國昌直播：95 分鐘
            (10800, (8, 50)),      # 180 分鐘：上限夾在 GPU 的 50
            (30000, (8, 50)),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(slide_range(seconds), expected)

    def test_the_boundaries_round_up(self):
        self.assertEqual(slide_range(360), (3, 6))       # 6 分鐘：ceil(1) = 1 → 夾到 3
        self.assertEqual(slide_range(18 * 60 + 1), (4, 10))
        self.assertEqual(slide_range(48 * 60), (8, 24))
        self.assertEqual(slide_range(6 * 60 * 8 + 1), (8, 25))

    def test_an_unknown_length_is_left_to_the_gpu_defaults(self):
        self.assertEqual(slide_range(0), (None, None))

    def test_the_operator_overrides_either_end(self):
        self.assertEqual(slide_range(3600, min_override=4, max_override=12), (4, 12))
        self.assertEqual(slide_range(3600, min_override=4), (4, 30))
        self.assertEqual(slide_range(3600, max_override=20), (8, 20))
        self.assertEqual(slide_range(0, min_override=2, max_override=5), (2, 5))
        self.assertEqual(slide_range(0, max_override=5), (None, 5))

    def test_a_range_that_contradicts_itself_is_refused_with_both_numbers(self):
        with self.assertRaises(ValueError) as caught:
            slide_range(3600, max_override=5)
        self.assertIn("8", str(caught.exception))
        self.assertIn("--max-slides", str(caught.exception))
        with self.assertRaises(ValueError):
            slide_range(3600, min_override=40, max_override=10)

    def test_the_gpu_accepts_one_to_fifty_pages_only(self):
        for kwargs in ({"min_override": 0}, {"max_override": 51}, {"min_override": 51}, {"max_override": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                slide_range(3600, **kwargs)


class SpeakerNameTests(SimpleTestCase):
    def test_the_joint_interrogation_separator_is_replaced(self):
        """「、」是聯合質詢的分隔字元：頻道名稱裡有的話，API 會把一個頻道拆成兩個講者。"""
        self.assertEqual(clean_speaker("甲、乙頻道"), "甲・乙頻道")

    def test_whitespace_is_tidied_and_the_column_limit_respected(self):
        self.assertEqual(clean_speaker("  黃國昌 \n 直播 "), "黃國昌 直播")
        self.assertEqual(len(clean_speaker("長" * 400)), 300)
        self.assertEqual(clean_speaker(None), "")


def _meta(**kw) -> VideoMeta:
    base = dict(video_id=TALK, title="標題", channel="頻道", date=date(2026, 10, 13), duration_seconds=507,
                playable=True, page_read=True, unlisted=False, category="Education", shorts_eligible=False)
    base.update(kw)
    return VideoMeta(**base)


class PreflightTests(SimpleTestCase):
    def refused(self, meta, options=None, *words):
        with self.assertRaises(VideoRefused) as caught:
            preflight(meta, options or PreflightOptions())
        message = str(caught.exception)
        for word in words:
            self.assertIn(word, message)
        return caught.exception

    def test_an_ordinary_public_video_passes_without_warnings(self):
        self.assertEqual(preflight(_meta(), PreflightOptions()), [])

    def test_videos_that_cannot_be_played_are_refused_and_say_why(self):
        self.refused(_meta(playable=False, playability_reason="影片不存在或已被移除（oEmbed 回 404）"),
                     None, "不存在")
        self.refused(_meta(playable=False, playability_reason="Private video"), None, "Private video", "無法播放")

    def test_live_and_upcoming_streams_are_refused_with_no_override(self):
        error = self.refused(_meta(is_live=True), PreflightOptions(force=True, allow_unlisted=True,
                                                                  allow_music=True, max_minutes=9999),
                             "直播中")
        self.assertIsNone(error.flag)
        upcoming = _meta(is_upcoming=True, playable=False, playability_reason="This live event will begin in 2 hours.")
        self.refused(upcoming, None, "尚未首播")

    def test_shorts_are_refused_for_the_cropping_reason(self):
        error = self.refused(_meta(shorts_eligible=True), None, "Shorts", "16:9")
        self.assertIsNone(error.flag)

    def test_an_unlisted_video_needs_its_flag(self):
        error = self.refused(_meta(unlisted=True), None, "不公開", "--allow-unlisted")
        self.assertEqual(error.flag, "--allow-unlisted")
        self.assertEqual(preflight(_meta(unlisted=True), PreflightOptions(allow_unlisted=True)), [])

    def test_a_music_video_needs_its_flag(self):
        error = self.refused(_meta(category="Music"), None, "音樂", "--allow-music")
        self.assertEqual(error.flag, "--allow-music")
        self.assertEqual(preflight(_meta(category="Music"), PreflightOptions(allow_music=True)), [])
        self.refused(_meta(category="音樂"), None, "--allow-music")

    def test_a_long_video_needs_a_higher_limit_and_the_message_names_it(self):
        error = self.refused(_meta(duration_seconds=95 * 60), PreflightOptions(max_minutes=60),
                             "95 分鐘", "--max-minutes", "60")
        self.assertEqual(error.flag, "--max-minutes")
        self.assertEqual(preflight(_meta(duration_seconds=95 * 60), PreflightOptions(max_minutes=100)), [])
        # 預設 180 分鐘；剛好等於上限不算超過
        self.assertEqual(preflight(_meta(duration_seconds=180 * 60), PreflightOptions()), [])
        self.refused(_meta(duration_seconds=180 * 60 + 1), None, "--max-minutes")

    def test_a_stream_that_just_ended_is_a_warning_that_needs_force(self):
        error = self.refused(_meta(is_post_live=True), None, "轉檔", "--force")
        self.assertEqual(error.flag, "--force")
        warnings = preflight(_meta(is_post_live=True), PreflightOptions(force=True))
        self.assertEqual(len(warnings), 1)
        self.assertIn("轉檔", warnings[0])

    def test_when_the_page_was_not_read_the_checks_that_need_it_are_reported_not_skipped_silently(self):
        meta = VideoMeta(video_id=TALK, title="標題", channel="頻道", page_read=False,
                         problems=("影片頁連不上",))
        warnings = preflight(meta, PreflightOptions())
        self.assertEqual(len(warnings), 1)
        for word in ("公開", "直播", "音樂", "長度"):
            self.assertIn(word, warnings[0])

    def test_a_video_with_an_unknown_length_warns_that_the_limit_was_not_checked(self):
        warnings = preflight(_meta(duration_seconds=0), PreflightOptions())
        self.assertEqual(len(warnings), 1)
        self.assertIn("長度", warnings[0])

    def test_the_most_specific_reason_wins(self):
        """又是直播又是音樂：先說直播中（沒有任何旗標能蓋過它），不是叫人加 --allow-music 再被擋一次。"""
        self.refused(_meta(is_live=True, category="Music", unlisted=True), None, "直播中")
        self.refused(_meta(shorts_eligible=True, category="Music", unlisted=True), None, "Shorts")


class FetchMetadataTests(SimpleTestCase):
    def oembed_ok(self):
        """真實的 oEmbed 回應（存下來的是國昌直播那一支）。"""
        return FakeFetch(_ok(_read(f"youtube_oembed_{STREAM}.json")))

    def page(self, video_id=STREAM):
        return FakeFetch(_ok(_watch(video_id)))

    def fetch(self, oembed, page, video_id=STREAM):
        return fetch_metadata(video_id, fetch_oembed=oembed, fetch_page=page)

    def test_both_layers_merge_and_only_two_requests_are_made(self):
        oembed, page = self.oembed_ok(), self.page()
        meta = self.fetch(oembed, page)
        self.assertEqual((meta.title, meta.channel, meta.channel_url),
                         ("【國昌直播】來看看我上週在做什麼_1019", "黃國昌", "https://www.youtube.com/@KC-Huang"))
        self.assertEqual((meta.date, meta.duration_seconds, meta.playable), (date(2026, 10, 19), 5715, True))
        self.assertEqual(meta.problems, ())
        self.assertEqual(len(oembed.calls), 1)
        self.assertEqual(len(page.calls), 1)
        self.assertEqual(page.calls[0], "https://www.youtube.com/watch?v=" + STREAM)

    def test_oembed_is_asked_about_the_canonical_url(self):
        oembed = self.oembed_ok()
        self.fetch(oembed, self.page())
        self.assertEqual(oembed.calls[0], "https://www.youtube.com/oembed?url="
                         "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Df4r2yWk4LV0&format=json")

    def test_the_title_and_channel_come_from_oembed_first(self):
        oembed = FakeFetch(_ok(json.dumps({"title": "oEmbed 的標題", "author_name": "oEmbed 的頻道",
                                           "author_url": "https://www.youtube.com/@x"})))
        meta = self.fetch(oembed, self.page())
        self.assertEqual((meta.title, meta.channel), ("oEmbed 的標題", "oEmbed 的頻道"))

    def test_a_malformed_id_or_a_missing_video_stops_at_oembed_without_asking_the_page(self):
        for status, word in ((400, "有效"), (404, "不存在")):
            with self.subTest(status=status):
                page = self.page()
                meta = self.fetch(FakeFetch(_ok(b"Not Found", status=status)), page)
                self.assertFalse(meta.playable)
                self.assertIn(word, meta.playability_reason)
                self.assertEqual(page.calls, [])

    def test_oembed_failures_that_say_nothing_about_the_video_fall_through_to_the_page(self):
        """401／403（可能只是不允許嵌入）、429、5xx、逾時都不是「影片不存在」。"""
        for oembed in (FakeFetch(_ok(b"", status=401)), FakeFetch(_ok(b"", status=403)),
                       FakeFetch(_ok(b"", status=429)), FakeFetch(_ok(b"", status=503)),
                       FakeFetch(error=TimeoutError("逾時")), FakeFetch(_ok(b"<html>not json</html>"))):
            with self.subTest(oembed=oembed.response or oembed.error):
                meta = self.fetch(oembed, self.page())
                self.assertEqual(meta.title, "【國昌直播】來看看我上週在做什麼_1019")
                self.assertTrue(meta.playable)
                self.assertEqual(len(meta.problems), 1)

    def test_a_missing_video_is_found_out_from_the_page_when_oembed_cannot_say(self):
        meta = self.fetch(FakeFetch(_ok(b"", status=403)), self.page(MISSING), MISSING)
        self.assertEqual((meta.playable, meta.playability_reason), (False, "This video is unavailable"))

    def test_without_the_page_oembed_still_gives_the_title_and_channel(self):
        for page in (FakeFetch(error=OSError("連線被拒")),
                     FakeFetch(_ok(b"", url="https://consent.youtube.com/m?continue=x")),
                     FakeFetch(_ok(b"", url="https://www.google.com/sorry/index?continue=x", status=429)),
                     FakeFetch(_ok(b"", status=429)), FakeFetch(_ok(b"x", status=500)),
                     FakeFetch(_ok(b"", url="https://www.google.com/x", status=302)),
                     FakeFetch(_ok(b"<html>nothing here</html>"))):
            with self.subTest(page=page.response or page.error):
                meta = self.fetch(self.oembed_ok(), page)
                self.assertEqual((meta.title, meta.channel), ("【國昌直播】來看看我上週在做什麼_1019", "黃國昌"))
                self.assertFalse(meta.page_read)
                self.assertIsNone(meta.date)
                self.assertEqual(meta.duration_seconds, 0)
                self.assertEqual(len(meta.problems), 1)

    def test_each_block_is_named_in_the_problem(self):
        cases = ((_ok(b"", url="https://consent.youtube.com/m?continue=x"), "consent"),
                 (_ok(b"", url="https://www.google.com/sorry/index?continue=x", status=429), "機器人"),
                 (_ok(b"", status=429), "429"), (_ok(b"", status=500), "500"))
        for response, word in cases:
            with self.subTest(word=word):
                meta = self.fetch(self.oembed_ok(), FakeFetch(response))
                self.assertIn(word, meta.problems[0])

    def test_a_bot_check_inside_the_page_is_a_block_not_a_property_of_the_video(self):
        text = _page(ZOO, **{"playabilityStatus.status": "LOGIN_REQUIRED",
                             "playabilityStatus.reason": "Sign in to confirm you’re not a bot"})
        meta = self.fetch(self.oembed_ok(), FakeFetch(_ok(text)), ZOO)
        self.assertTrue(meta.playable is None)
        self.assertFalse(meta.page_read)
        self.assertIn("機器人", meta.problems[0])

    def test_other_login_walls_are_the_videos_own_state(self):
        text = _page(ZOO, **{"playabilityStatus.status": "LOGIN_REQUIRED",
                             "playabilityStatus.reason": "This video is private"})
        meta = self.fetch(self.oembed_ok(), FakeFetch(_ok(text)), ZOO)
        self.assertEqual((meta.playable, meta.playability_reason), (False, "This video is private"))

    def test_when_nothing_can_be_read_the_error_lists_every_reason(self):
        with self.assertRaises(MetadataUnavailable) as caught:
            self.fetch(FakeFetch(error=OSError("無法解析主機")), FakeFetch(error=TimeoutError("逾時")))
        message = str(caught.exception)
        self.assertIn("oEmbed", message)
        self.assertIn("影片頁", message)
        self.assertIsInstance(caught.exception, SourceUnavailable)

    def test_the_page_is_decoded_as_utf8(self):
        meta = self.fetch(self.oembed_ok(), FakeFetch(_ok(_watch(STREAM).encode("utf-8"))))
        self.assertEqual(meta.channel, "黃國昌")


class DefaultFetchTests(SimpleTestCase):
    """真正的 _http_get：用本機的伺服器（127.0.0.1），不碰 YouTube。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 開發機若設了 HTTP_PROXY，連本機的請求也會被送去代理
        cls.env = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        cls.env.start()
        cls.requests: list[dict] = []
        outer = cls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                # urllib 會把標頭名稱改成「User-agent」這種寫法，所以統一用小寫比
                outer.requests.append({"path": self.path,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path == "/ok":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 5000)
                elif self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not Found")
                elif self.path == "/away":
                    self.send_response(302)
                    self.send_header("Location", "https://www.google.com/sorry/index?continue=x")
                    self.end_headers()
                elif self.path == "/relative":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                else:
                    self.send_response(500)
                    self.end_headers()

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.env.stop()
        super().tearDownClass()

    def setUp(self):
        self.requests.clear()

    def test_the_page_request_looks_like_a_browser_and_does_not_ask_for_compression(self):
        response = yt.http_page(self.base + "/ok")
        self.assertEqual((response.status, len(response.body)), (200, 5000))
        sent = self.requests[0]["headers"]
        self.assertIn("Mozilla/5.0", sent["user-agent"])
        self.assertIn("en-US", sent["accept-language"])
        self.assertEqual(sent["accept-encoding"], "identity")
        self.assertEqual(sent["cookie"], "SOCS=CAI")

    def test_oembed_does_not_send_the_consent_cookie(self):
        yt.http_oembed(self.base + "/ok")
        self.assertNotIn("cookie", self.requests[0]["headers"])
        self.assertIn("en-US", self.requests[0]["headers"]["accept-language"])

    def test_an_error_status_is_a_response_not_an_exception(self):
        response = yt.http_page(self.base + "/missing")
        self.assertEqual((response.status, response.body), (404, b"Not Found"))
        self.assertEqual(yt.http_page(self.base + "/boom").status, 500)

    def test_the_body_is_capped(self):
        with mock.patch.object(yt, "_PAGE_MAX_BYTES", 1000):
            self.assertEqual(len(yt.http_page(self.base + "/ok").body), 1000)

    def test_a_redirect_off_the_youtube_hosts_is_reported_not_followed(self):
        response = yt.http_page(self.base + "/away")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.url, "https://www.google.com/sorry/index?continue=x")
        self.assertEqual([r["path"] for r in self.requests], ["/away"])

    def test_even_a_redirect_to_the_same_local_host_is_not_followed(self):
        """允許的主機是 YouTube 的幾個，不是「跟請求同一台」。"""
        response = yt.http_page(self.base + "/relative")
        self.assertEqual((response.status, response.url), (302, self.base + "/ok"))

    def test_only_youtube_hosts_may_be_redirected_to(self):
        handler = yt._StayOnYoutube()
        request = urllib.request.Request("https://www.youtube.com/watch?v=x")
        for target, followed in (
            ("https://www.youtube.com/watch?v=y", True), ("https://consent.youtube.com/m?continue=x", True),
            ("https://youtube.com/watch?v=y", True), ("https://m.youtube.com/watch?v=y", True),
            ("https://www.google.com/sorry/index", False), ("https://youtube.com.evil.com/x", False),
            ("https://evilyoutube.com/x", False), ("https://accounts.google.com/ServiceLogin", False),
            ("ftp://www.youtube.com/x", False),
        ):
            with self.subTest(target=target):
                new = handler.redirect_request(request, None, 302, "Found", {}, target)
                self.assertEqual(new is not None, followed)

    def test_a_connection_that_cannot_be_made_is_an_oserror(self):
        with self.assertRaises(OSError):
            yt.http_page("http://127.0.0.1:1/never")

    def test_the_http_exceptions_that_urllib_lets_through_are_oserrors_too(self):
        """被截斷的回應是 http.client.IncompleteRead，不是 OSError——呼叫端只接 OSError。"""
        import http.client

        class Truncated:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return "https://www.youtube.com/watch?v=x"

            def read(self, n):
                raise http.client.IncompleteRead(b"partial", 100)

        with mock.patch("urllib.request.OpenerDirector.open", return_value=Truncated()):
            with self.assertRaises(OSError):
                yt.http_page("https://www.youtube.com/watch?v=x")


class ModuleShapeTests(SimpleTestCase):
    def test_it_depends_on_the_standard_library_only(self):
        """Pi 的映像刻意沒有 yt-dlp 或任何網路套件。"""
        import ast
        import pathlib
        import sys

        tree = ast.parse(pathlib.Path(yt.__file__).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add((node.module or "").split(".")[0])
        self.assertEqual({r for r in roots if r not in sys.stdlib_module_names}, set())

    def test_the_unavailable_errors_are_source_unavailable(self):
        self.assertTrue(issubclass(MetadataUnavailable, SourceUnavailable))
