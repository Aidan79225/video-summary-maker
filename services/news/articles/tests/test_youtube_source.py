"""YouTube 影片的網址解析、影片資料（oEmbed＋影片頁）、日期換算、頁數範圍與預檢。

全部離線：影片頁與 oEmbed 用 fixtures 裡存下來的真實頁面（縮減到解析器會讀的欄位），
抓取用注入的假 callable。直播中、尚未首播、剛結束的直播、不公開、私人、機器人檢查這幾種
頁面沒辦法穩定抓到真的，是把真實頁面的旗標改過來（_page 的 patch 參數），欄位名稱與型別
都照真實頁面。
"""
from __future__ import annotations

import http.server
import io
import json
import os
import threading
import time
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
    clean_text,
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

    def test_options_between_the_positionals_do_not_split_them(self):
        """搬到最後面的 id 不能因為前面有選項就變成「多出來的引數」（unrecognized arguments: -- -abcdefghij）。
        兩個以上的網址加選項，普通的 id 也一樣：以前 `A --speakers 1 B` 就會失敗。"""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("urls", nargs="+")
        parser.add_argument("--speakers")
        parser.add_argument("--dry-run", action="store_true")
        wrapped = yt.allow_dash_ids(parser)
        ids = {"aaaaaaaaaaa", "bbbbbbbbbbb", "-abcdefghij", "-ABCDEFGH_k"}
        for args in (["aaaaaaaaaaa", "-abcdefghij", "--speakers", "1"],
                     ["-abcdefghij", "aaaaaaaaaaa", "--speakers", "1"],
                     ["aaaaaaaaaaa", "--speakers", "1", "-abcdefghij"],
                     ["--speakers", "1", "aaaaaaaaaaa", "-abcdefghij", "--dry-run"],
                     ["aaaaaaaaaaa", "--speakers", "1", "bbbbbbbbbbb"],
                     ["aaaaaaaaaaa", "-abcdefghij", "-ABCDEFGH_k", "--dry-run", "--speakers", "1"]):
            with self.subTest(args=args):
                parsed = wrapped.parse_args(list(args))
                self.assertEqual(sorted(parsed.urls), sorted(a for a in args if a in ids))
                self.assertEqual(parsed.speakers, "1")

    def test_a_missing_positional_is_still_an_error(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("urls", nargs="+")
        parser.add_argument("--speakers")
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            yt.allow_dash_ids(parser).parse_args(["--speakers", "1"])


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

    def test_the_player_response_counts_as_read_only_when_its_playability_status_was_decoded(self):
        """page_read 只說「讀到了頁面、認得出某個欄位」；不公開、直播、Shorts 與能不能播放要靠播放器回應，
        所以另有 player_read。"""
        for video_id in (ZOO, STREAM, TALK, MUSIC, SHORT, LIVE, MISSING):
            with self.subTest(video_id=video_id):
                meta = self.parse(video_id)
                self.assertTrue(meta.page_read)
                self.assertTrue(meta.player_read)
        stream = _watch(STREAM)
        for label, text in (
            ("microdata only", stream[:stream.index("<script")] + "</head></html>"),
            ("title tag only", "<html><head><title>只剩標題 - YouTube</title></head></html>"),
            ("the variable was renamed", _watch(ZOO).replace("ytInitialPlayerResponse", "ytInitialPlayerResp0nse")),
            ("the status is missing", _page(ZOO, patch=lambda pr: pr["playabilityStatus"].pop("status"))),
            ("the status is not a string", _page(ZOO, **{"playabilityStatus.status": 7})),
        ):
            with self.subTest(label=label):
                meta = self.parse(ZOO, text)
                self.assertTrue(meta.page_read)
                self.assertFalse(meta.player_read)

    def test_every_real_playable_page_carries_both_flags_the_checks_rely_on(self):
        """「旗標缺了就是沒檢查」這條規則的根據：固定資料裡真實的頁面，可以播放的每一頁（一般、直播中、剛結束的
        直播、音樂、Shorts）都帶布林值的 isUnlisted 與 isShortsEligible；只有播不了的那一頁（影片不存在）沒有
        microformat，但它在預檢的第一關就被拒絕了。"""
        for video_id in (ZOO, STREAM, TALK, MUSIC, SHORT, LIVE):
            with self.subTest(video_id=video_id):
                meta = self.parse(video_id)
                self.assertTrue(meta.playable)
                self.assertIsInstance(meta.unlisted, bool)
                self.assertIsInstance(meta.shorts_eligible, bool)
        missing = self.parse(MISSING)
        self.assertEqual((missing.playable, missing.unlisted, missing.shorts_eligible), (False, None, None))

    def test_text_that_looks_like_the_assignment_cannot_stand_in_for_the_real_player_response(self):
        """標題與描述會原樣出現在真正的腳本之前（實測：<title> 在 781,736、腳本在 789,733）。影片標題取成
        `var ytInitialPlayerResponse = {};` 的話，第一個解得出來的物件是 {}：unlisted、直播、Shorts 的檢查
        就全部悄悄失效。"""
        fake = "var ytInitialPlayerResponse = {};"
        text = _page(ZOO, **{"microformat.playerMicroformatRenderer.isUnlisted": True})
        text = text.replace("<title>Me at the zoo - YouTube</title>", f"<title>{fake}</title>", 1)
        text = text.replace('<meta itemprop="name" content="Me at the zoo">', f'<meta itemprop="name" content="{fake}">', 1)
        real = 'var ytInitialPlayerResponse = {"playab'
        self.assertLess(text.index(fake), text.index(real))
        meta = self.parse(ZOO, text)
        self.assertTrue(meta.player_read)
        self.assertEqual((meta.playable, meta.unlisted), (True, True))
        # 真的那個不見了的話，剩下的假貨不算數
        meta = self.parse(ZOO, text.replace(real, 'var xInitialPlayerResponse = {"playab'))
        self.assertFalse(meta.player_read)
        self.assertIsNone(meta.playable)

    def test_objects_that_are_not_player_responses_are_skipped_and_the_scan_goes_on(self):
        real = '{"playabilityStatus":{"status":"OK"},"videoDetails":{"title":"t"}}'
        self.assertEqual(yt._player_response(f"ytInitialPlayerResponse = {{}}; ytInitialPlayerResponse = {{\"a\":1}}; "
                                             f"ytInitialPlayerResponse = {real}"), json.loads(real))
        self.assertIsNone(yt._player_response('ytInitialPlayerResponse = {}; ytInitialPlayerResponse = [1]; '
                                              'ytInitialPlayerResponse = {"videoDetails": 5}'))
        # 只有 videoDetails（沒有播放狀態）也是播放器回應，只是 player_read 不成立
        self.assertEqual(yt._player_response('ytInitialPlayerResponse = {"videoDetails":{"title":"t"}}'),
                         {"videoDetails": {"title": "t"}})
        # 影片不存在：只有 playabilityStatus
        self.assertIsNotNone(yt._player_response(_watch(MISSING)))


def _shell(player_json: str, head: str = "") -> str:
    return (f"<html><head><title>T - YouTube</title>{head}</head><body><script>"
            f"var ytInitialPlayerResponse = {player_json};</script></body></html>")


def _response(**over) -> str:
    base = {"playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": ZOO, "title": "T", "lengthSeconds": "19", "author": "A"},
            "microformat": {"playerMicroformatRenderer": {"uploadDate": "2005-04-23T20:31:52-07:00",
                                                          "category": "Education"}}}
    base.update(over)
    return json.dumps(base)


class MalformedUpstreamTests(SimpleTestCase):
    """影片頁與 oEmbed 是別人的網站：改版、被截斷、型別不對、被刻意餵怪東西時，fetch_metadata 不能丟 traceback。
    每一層（播放器回應、microdata、正規表示式）各自攔下、記成問題，換下一層。"""

    def parse(self, text):
        return parse_watch_page(text, ZOO)

    def test_wrongly_typed_fields_in_the_player_response_do_not_raise(self):
        error_screen = {"playabilityStatus": {"status": "ERROR", "errorScreen": "boom"}}
        runs_null = {"playabilityStatus": {"status": "ERROR", "errorScreen": {
            "playerErrorMessageRenderer": {"reason": {"runs": None}}}}}
        runs_number = {"playabilityStatus": {"status": "ERROR", "errorScreen": {
            "playerErrorMessageRenderer": {"reason": {"runs": [{"text": 1}]}}}}}
        for label, text in (
            ("microformat is a list", _shell(_response(microformat=["x"]))),
            ("microformat is a string", _shell(_response(microformat="x"))),
            ("microformat is null", _shell(_response(microformat=None))),
            ("errorScreen is a string", _shell(_response(**error_screen))),
            ("runs is null", _shell(_response(**runs_null))),
            ("a run's text is a number", _shell(_response(**runs_number))),
            ("videoDetails is a list", _shell(_response(videoDetails=[1]))),
            ("playabilityStatus is a string", _shell(_response(playabilityStatus="OK"))),
        ):
            with self.subTest(label=label):
                meta = self.parse(text)
                self.assertEqual(meta.title, "T")

    def test_the_reason_still_comes_out_when_only_part_of_the_error_screen_is_malformed(self):
        text = _shell(_response(playabilityStatus={"status": "ERROR", "errorScreen": {
            "playerErrorMessageRenderer": {"reason": {"runs": [{"text": 1}, {"text": "Video unavailable"}]}}}}))
        meta = self.parse(text)
        self.assertEqual((meta.playable, meta.playability_reason), (False, "Video unavailable"))

    def test_numbers_that_are_not_plain_digits_are_unknown_not_an_exception(self):
        for label, length in (("superscript two", "²"), ("5000 digits", "9" * 5000), ("negative", "-5"),
                              ("fullwidth digits", "１２３"), ("with a unit", "19s"), ("a number", 19)):
            with self.subTest(label=label):
                text = _shell(_response(videoDetails={"title": "T", "lengthSeconds": length}))
                self.assertEqual(self.parse(text).duration_seconds, 0)
        self.assertEqual(self.parse(_shell(_response(videoDetails={"title": "T", "lengthSeconds": "5715"}))
                                    ).duration_seconds, 5715)

    def test_the_microdata_and_regex_layers_survive_huge_numbers_too(self):
        micro = ('<html><head><meta itemprop="duration" content="PT' + "9" * 5000 + 'S">'
                 '<meta itemprop="name" content="x"></head></html>')
        self.assertEqual(self.parse(micro).duration_seconds, 0)
        regex = ('<html><head><title>x - YouTube</title></head><script>ytInitialPlayerResponse = {broken '
                 '"lengthSeconds":"' + "9" * 5000 + '"</script></html>')
        self.assertEqual(self.parse(regex).duration_seconds, 0)

    def test_dates_that_cannot_be_converted_leave_the_date_unknown(self):
        for upload in ("0001-01-01T00:00:00+08:00", "9999-12-31T23:59:59-07:00"):
            with self.subTest(upload=upload):
                text = _shell(_response(microformat={"playerMicroformatRenderer": {"uploadDate": upload}}))
                self.assertIsNone(self.parse(text).date)

    def test_a_player_response_nested_beyond_the_parsers_limit_is_skipped(self):
        text = _shell('{"playabilityStatus":{"status":"OK"},"videoDetails":' + "[" * 100000 + "]" * 100000 + "}")
        meta = self.parse(text)
        self.assertEqual(meta.title, "T")
        self.assertFalse(meta.player_read)

    def test_a_layer_that_blows_up_is_recorded_and_the_next_layer_runs(self):
        for layer, error, word in (("_from_player_response", AttributeError("x"), "播放器回應"),
                                   ("_from_microdata", TypeError("x"), "microdata"),
                                   ("_from_regex", ValueError("x"), "正規表示式")):
            with self.subTest(layer=layer), mock.patch.object(yt, layer, side_effect=error):
                # 播放器回應解不出來才會走到正規表示式那一層
                text = _watch(ZOO) if layer != "_from_regex" else _watch(ZOO).replace(
                    "ytInitialPlayerResponse", "ytInitialPlayerResp0nse")
                meta = self.parse(text)
                self.assertIsNotNone(meta)
                self.assertTrue(any(word in problem for problem in meta.problems), meta.problems)
        with mock.patch.object(yt, "_from_player_response", side_effect=RecursionError()):
            meta = self.parse(_watch(ZOO))
        # 前一層做到一半的結果不留：播放器回應丟了例外就是「沒讀到」，標題等欄位來自後面的層
        self.assertEqual((meta.title, meta.channel, meta.duration_seconds), ("Me at the zoo", "jawed", 19))
        self.assertFalse(meta.player_read)
        self.assertIsNone(meta.playable)

    def test_the_problems_of_a_layer_reach_the_merged_result(self):
        oembed = FakeFetch(_ok(json.dumps({"title": "oEmbed 的標題", "author_name": "頻道"})))
        with mock.patch.object(yt, "_from_player_response", side_effect=AttributeError("x")):
            meta = fetch_metadata(ZOO, fetch_oembed=oembed, fetch_page=FakeFetch(_ok(_watch(ZOO))))
        self.assertEqual(meta.title, "oEmbed 的標題")
        self.assertFalse(meta.player_read)
        self.assertTrue(any("播放器回應" in problem for problem in meta.problems))

    def test_oembed_bodies_that_break_the_json_parser_are_nothing(self):
        for label, body in (("nested too deep", b"[" * 100000), ("a huge integer", b'{"title":' + b"9" * 5000 + b"}"),
                            ("wrong types", b'{"title": 5, "author_name": ["x"]}')):
            with self.subTest(label=label):
                self.assertIsNone(parse_oembed(body))

    def test_a_bad_oembed_body_falls_through_to_the_page(self):
        meta = fetch_metadata(ZOO, fetch_oembed=FakeFetch(_ok(b"[" * 100000)), fetch_page=FakeFetch(_ok(_watch(ZOO))))
        self.assertEqual((meta.title, meta.channel), ("Me at the zoo", "jawed"))
        self.assertTrue(meta.player_read)
        self.assertEqual(len(meta.problems), 1)

    def test_a_page_that_blows_up_everywhere_is_a_problem_not_a_traceback(self):
        oembed = FakeFetch(_ok(json.dumps({"title": "t", "author_name": "c"})))
        with mock.patch.object(yt, "parse_watch_page", side_effect=OverflowError("x")):
            meta = fetch_metadata(ZOO, fetch_oembed=oembed, fetch_page=FakeFetch(_ok(_watch(ZOO))))
        self.assertEqual((meta.title, meta.page_read), ("t", False))
        self.assertEqual(len(meta.problems), 1)

    def test_a_redirect_to_a_url_that_cannot_be_parsed_is_not_a_traceback(self):
        """Location: http://[::1/x 會讓 urlsplit 丟 ValueError；_page_problem 也不能被它絆倒。"""
        response = HttpResponse(302, "http://[::1/x", b"")
        meta = fetch_metadata(ZOO, fetch_oembed=FakeFetch(_ok(json.dumps({"title": "t", "author_name": "c"}))),
                              fetch_page=FakeFetch(response))
        self.assertFalse(meta.page_read)
        self.assertEqual(len(meta.problems), 1)


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

    def test_dates_the_time_zone_arithmetic_cannot_represent_are_none_not_an_exception(self):
        """換成台北時間會超出 date 的範圍（OverflowError），讀不懂就是 None。"""
        for text in ("0001-01-01T00:00:00+08:00", "9999-12-31T23:59:59-07:00"):
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

    def test_a_channel_name_is_made_safe_to_store_and_print(self):
        self.assertEqual(clean_speaker("Chan\x00nel\t‮、Two\n名"), "Channel ・Two 名")
        self.assertEqual(clean_speaker("頻道\x1b[2J"), "頻道[2J")


class CleanTextTests(SimpleTestCase):
    """標題與頻道名稱是上傳者寫的，會原樣印在操作者的終端機、存進資料庫、變成公開網址的一部分。"""

    def test_nothing_that_moves_a_terminal_cursor_or_reorders_text_survives(self):
        import unicodedata

        dirty = "Line1\nLine2\x1b[2J\x1b]0;pwned\x07 ‮RTL‬⁦x⁩ \x00end\x7f\x85"
        cleaned = clean_text(dirty)
        self.assertEqual(cleaned, "Line1 Line2[2J]0;pwned RTLx end")
        for char in cleaned:
            self.assertNotIn(unicodedata.category(char), ("Cc", "Cf", "Cs"), repr(char))
        # 每一個雙向覆寫／隔離字元都不留
        for code in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]:
            self.assertEqual(clean_text(f"a{chr(code)}b"), "ab", hex(code))

    def test_line_breaks_and_tabs_become_one_space_not_a_glued_word(self):
        self.assertEqual(clean_text("Line1\nLine2\r\n\tLine3 Line4　Line5"), "Line1 Line2 Line3 Line4 Line5")
        self.assertEqual(clean_text("  a   b  "), "a b")

    def test_format_characters_and_lone_surrogates_go_too(self):
        # 零寬空格 U+200B、BOM U+FEFF、軟連字號 U+00AD、LRM／RLM U+200E／U+200F、字詞連接符 U+2060、孤立的代理字元
        dirty = "a\u200bb\ufeffc\u00add\u200ee\u200ff\u2060g\ud800h"
        self.assertEqual(clean_text(dirty), "abcdefgh")

    FAMILY = "\U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466"      # 家庭：四個表情符號用 ZWJ 接成一個
    PERSIAN = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"                # 波斯文的「我要」：ZWNJ 是字的一部分
    INDIC = "\u0915\u094d\u200d\u0937"                                          # 天城文：ZWJ 決定這個合體字的字形

    def test_the_two_joiners_survive_because_emoji_and_names_need_them(self):
        """ZWJ（U+200D）把幾個表情符號接成一個（家庭、職業）；ZWNJ（U+200C）在波斯文、印地文等是字的一部分，拿掉會
        改變字形甚至意思。標題與頻道名稱要跟 YouTube 上看到的一樣。"""
        for text in (self.FAMILY, self.PERSIAN, self.INDIC, f"頻道 {self.FAMILY} / {self.PERSIAN}",
                     "Ａ\u200cＢ\u200dＣ"):
            with self.subTest(text=text):
                self.assertEqual(clean_text(text), text)
                self.assertEqual(clean_speaker(text), text)

    def test_every_other_control_format_or_surrogate_character_is_still_removed(self):
        """例外只有 U+200C 與 U+200D 兩個：其餘每一個 Cc／Cf／Cs 字元（雙向覆寫與隔離 U+202A–202E、U+2066–2069，
        零寬空格、BOM、NUL、ESC…）照舊拿掉。空白類的控制字元（換行、tab）換成一個空格。"""
        import unicodedata

        checked = 0
        for code in range(0x110000):
            char = chr(code)
            if code in (0x200C, 0x200D) or unicodedata.category(char) not in ("Cc", "Cf", "Cs"):
                continue
            checked += 1
            self.assertEqual(clean_text(f"a{char}b"), "a b" if char.isspace() else "ab", hex(code))
        self.assertGreater(checked, 2000)                  # 光是孤立的代理字元就有 2048 個
        for code in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]:
            self.assertEqual(clean_text(f"{self.FAMILY}{chr(code)}{self.PERSIAN}"), self.FAMILY + self.PERSIAN,
                             hex(code))

    def test_the_attacks_the_cleaning_exists_for_are_still_stopped_next_to_the_joiners(self):
        """ESC 序列、OSC、NUL、BEL、雙向覆寫與隔離夾在表情符號與波斯文之間，照樣拿得乾乾淨淨，連接符不受影響。"""
        dirty = (f"\x1b[2J{self.FAMILY}\u202e{self.PERSIAN}\x1b]0;pwned\x07\u2066x\u2069\x00\u200b"
                 f"\u202a{self.INDIC}\u202c")
        self.assertEqual(clean_text(dirty), f"[2J{self.FAMILY}{self.PERSIAN}]0;pwnedx{self.INDIC}")

    def test_joiners_with_nothing_to_join_are_nothing(self):
        """只剩連接符的標題看起來是空的：要像空字串一樣走預設（佔位標題、「YouTube」頻道），不是留下一個看不見的
        標題。"""
        for text in ("\u200d", "\u200c\u200d", " \u200d \u200c ", "\x00\u200d\x1b"):
            with self.subTest(text=text):
                self.assertEqual(clean_text(text), "")
        # 前後有字的連接符（連字、字尾的 ZWNJ）照舊保留
        self.assertEqual(clean_text("\u200dx\u200c"), "\u200dx\u200c")

    def test_ordinary_text_is_untouched(self):
        for text in ("Me at the zoo", "【國昌直播】來看看我上週在做什麼_1019", "50%25off", "Q&A? #1 + more 😀",
                     "100% 純", "ＡＢＣ"):
            with self.subTest(text=text):
                self.assertEqual(clean_text(text), text)

    def test_nothing_is_nothing(self):
        self.assertEqual((clean_text(None), clean_text(""), clean_text("\x00\x1b")), ("", "", ""))


def _meta(**kw) -> VideoMeta:
    base = dict(video_id=TALK, title="標題", channel="頻道", date=date(2026, 10, 13), duration_seconds=507,
                playable=True, page_read=True, player_read=True, unlisted=False, category="Education",
                shorts_eligible=False)
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

    def test_when_the_page_was_not_read_the_video_is_refused_unless_the_operator_accepts_it(self):
        """讀不到影片頁：不公開、直播、Shorts、音樂與長度都檢查不了。以前只有一行警告（沒人攔得住）；
        現在預設拒絕，要明確加 --allow-unverified 才放行，而且放行時警告照舊。"""
        meta = VideoMeta(video_id=TALK, title="標題", channel="頻道", page_read=False,
                         problems=("影片頁連不上",))
        error = self.refused(meta, None, "影片頁", "--allow-unverified")
        self.assertEqual(error.flag, "--allow-unverified")
        warnings = preflight(meta, PreflightOptions(allow_unverified=True))
        self.assertEqual(len(warnings), 1)
        for word in ("公開", "直播", "音樂", "長度"):
            self.assertIn(word, warnings[0])

    def test_a_page_whose_player_response_was_not_decoded_is_just_as_unverified(self):
        """microdata 或 <title> 讀得到、播放器回應（playabilityStatus）沒解出來：不公開、直播中、Shorts 全是「不知道」，
        不能因為頁面「讀到了」就當成檢查過。"""
        meta = _meta(player_read=False, playable=None, unlisted=None, shorts_eligible=None)
        error = self.refused(meta, None, "播放器", "--allow-unverified")
        self.assertEqual(error.flag, "--allow-unverified")
        warnings = preflight(meta, PreflightOptions(allow_unverified=True))
        self.assertEqual(len(warnings), 1)
        for word in ("公開", "直播", "音樂", "Shorts", "長度"):
            self.assertIn(word, warnings[0])

    def test_no_other_option_stands_in_for_the_unverified_flag(self):
        meta = VideoMeta(video_id=TALK, title="標題", page_read=False)
        everything_else = PreflightOptions(max_minutes=9999, force=True, allow_unlisted=True, allow_music=True)
        self.refused(meta, everything_else, "--allow-unverified")

    def test_the_unverified_flag_lets_nothing_known_to_be_wrong_through(self):
        """--allow-unverified 只管「沒辦法檢查」。檢查得出來的拒絕照舊，而且先說：oEmbed 回 404 就說影片不存在，
        不是叫人加 --allow-unverified。"""
        yes = PreflightOptions(allow_unverified=True)
        not_found = VideoMeta(video_id=TALK, playable=False, playability_reason="影片不存在或已被移除（oEmbed 回 404）")
        self.assertNotIn("--allow-unverified", str(self.refused(not_found, None, "不存在")))
        self.refused(not_found, yes, "不存在")
        self.refused(_meta(is_live=True), yes, "直播中")
        self.refused(_meta(shorts_eligible=True), yes, "Shorts")
        self.refused(_meta(unlisted=True), yes, "--allow-unlisted")
        self.refused(_meta(duration_seconds=181 * 60), yes, "--max-minutes")

    def test_a_more_specific_refusal_comes_before_the_unverified_one(self):
        """影片頁只讀到一半（microdata 有長度）：太長就先說太長。"""
        meta = _meta(player_read=False, playable=None, unlisted=None, shorts_eligible=None,
                     duration_seconds=181 * 60)
        self.refused(meta, None, "--max-minutes")

    def test_a_playable_page_without_a_flag_real_pages_always_carry_is_unverified_and_the_message_names_it(self):
        """播放器回應解出來、影片也能播，microformat 或旗標卻不見了（改版、改名）：unlisted 與 shorts 停在 None。
        以前當成沒問題，悄悄放行、連警告都沒有——「沒有 isUnlisted」被讀成「不是不公開」。真實的頁面每一頁都帶
        這兩個旗標（見 ParseWatchPageTests），所以缺了就是沒檢查：預設拒絕，訊息說缺的是哪一個。"""
        for label, changes, names, present in (
            ("no isUnlisted", dict(unlisted=None), ["isUnlisted"], "isShortsEligible"),
            ("no isShortsEligible", dict(shorts_eligible=None), ["isShortsEligible"], "isUnlisted"),
            ("no microformat at all", dict(unlisted=None, shorts_eligible=None),
             ["isUnlisted", "isShortsEligible"], None),
        ):
            with self.subTest(label=label):
                meta = _meta(**changes)
                error = self.refused(meta, None, "--allow-unverified", *names)
                self.assertEqual(error.flag, "--allow-unverified")
                self.assertIn("只給 --title", str(error))                  # 三個覆寫不能代替它，這裡一樣
                warnings = preflight(meta, PreflightOptions(allow_unverified=True))
                self.assertEqual(len(warnings), 1)
                for name in names:
                    self.assertIn(name, warnings[0])
                if present:
                    self.assertNotIn(present, str(error))
                    self.assertNotIn(present, warnings[0])

    def test_what_is_known_to_be_wrong_is_still_said_first_when_a_flag_is_missing_too(self):
        """另一個旗標讀得到、而且是壞消息：先說那個（可以用它自己的旗標蓋過），不是叫人加 --allow-unverified。"""
        self.refused(_meta(unlisted=None, shorts_eligible=True), None, "Shorts")
        error = self.refused(_meta(unlisted=True, shorts_eligible=None), None, "--allow-unlisted")
        self.assertEqual(error.flag, "--allow-unlisted")
        self.refused(_meta(shorts_eligible=None, duration_seconds=181 * 60), None, "--max-minutes")
        self.refused(_meta(unlisted=None, is_live=True), None, "直播中")

    def test_fields_real_pages_may_legitimately_omit_do_not_cause_a_refusal(self):
        """沒有類別、日期、頻道、長度（直播中與剛結束的直播本來就可能沒有）：頂多少一個警告，不能因此拒絕。
        只有「檢查本身依賴、而且真實頁面每一頁都有」的旗標缺了才算沒檢查。"""
        meta = _meta(category=None, date=None, channel=None, duration_seconds=0)
        warnings = preflight(meta, PreflightOptions())
        self.assertEqual(len(warnings), 1)
        self.assertIn("長度", warnings[0])

    def test_a_page_that_cannot_be_played_is_refused_for_that_not_for_the_missing_flags(self):
        """影片不存在的頁面沒有 microformat（兩個旗標都是 None），但它第一關就因為播不了被拒絕，不叫人加旗標。"""
        meta = parse_watch_page(_watch(MISSING), MISSING)
        self.assertEqual(yt._missing_flags(meta), [])                 # 播不了的影片不談旗標
        error = self.refused(meta, None, "無法播放")
        self.assertIsNone(error.flag)
        self.assertNotIn("--allow-unverified", str(error))
        self.refused(meta, PreflightOptions(allow_unverified=True), "無法播放")

    def test_a_fully_read_page_is_untouched_by_all_this(self):
        self.assertEqual(preflight(_meta(), PreflightOptions(allow_unverified=True)), [])
        self.assertFalse(PreflightOptions().allow_unverified)
        # 每一支真實的、能公開的頁面都照舊通過，沒有任何警告（音樂有自己的旗標）
        for video_id, options in ((ZOO, PreflightOptions()), (STREAM, PreflightOptions()),
                                  (TALK, PreflightOptions()), (MUSIC, PreflightOptions(allow_music=True))):
            with self.subTest(video_id=video_id):
                self.assertEqual(preflight(parse_watch_page(_watch(video_id), video_id), options), [])

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

    def test_a_fully_read_page_is_page_read_and_player_read(self):
        meta = self.fetch(self.oembed_ok(), self.page())
        self.assertEqual((meta.page_read, meta.player_read), (True, True))
        blocked = self.fetch(self.oembed_ok(), FakeFetch(_ok(b"", status=429)))
        self.assertEqual((blocked.page_read, blocked.player_read), (False, False))

    def test_every_way_the_checks_can_be_blinded_ends_in_a_refusal_not_a_warning(self):
        """oEmbed 連不公開的影片也回 200，所以「標題讀得到」不代表檢查得了。影片頁被擋、只剩 consent 殼、
        播放器回應不見了（只剩 microdata）、頁面被截斷：全部要拒絕，而且說得出怎麼蓋過。"""
        stream = _watch(STREAM)
        shell = "<html><head><title>Before you continue to YouTube</title></head><body></body></html>"
        micro_only = stream[:stream.index("<script")] + "</head></html>"
        truncated = stream[:stream.index("ytInitialPlayerResponse") + 90]
        renamed = stream.replace("ytInitialPlayerResponse", "ytInitialPlayerResp0nse")
        for label, page in (
            ("HTTP 429", FakeFetch(_ok(b"", status=429))),
            ("redirected to the consent page", FakeFetch(_ok(b"", url="https://consent.youtube.com/m?continue=x"))),
            ("a consent shell that answers 200", FakeFetch(_ok(shell))),
            ("timeout", FakeFetch(error=TimeoutError("逾時"))),
            ("microdata only", FakeFetch(_ok(micro_only))),
            ("cut inside the player response", FakeFetch(_ok(truncated))),
            ("the variable was renamed", FakeFetch(_ok(renamed))),
        ):
            with self.subTest(label=label):
                meta = self.fetch(self.oembed_ok(), page)
                with self.assertRaises(VideoRefused) as caught:
                    preflight(meta, PreflightOptions())
                self.assertEqual(caught.exception.flag, "--allow-unverified")
                self.assertTrue(preflight(meta, PreflightOptions(allow_unverified=True)))

    def test_a_real_page_that_loses_its_microformat_or_a_flag_is_refused_not_waved_through(self):
        """播放器回應（playabilityStatus）還在，microformat 或旗標不見了、被改名：以前 unlisted 與 shorts 停在 None，
        預檢悄悄放行——一支「不公開」的影片就這樣被登記、送 GPU、發佈到公開的網站。"""
        def unlisted_but_microformat_renamed(pr):
            pr["microformat"]["playerMicroformatRenderer"]["isUnlisted"] = True
            pr["microformat_"] = pr.pop("microformat")

        def renamed(key):
            def patch(pr):
                renderer = pr["microformat"]["playerMicroformatRenderer"]
                renderer[key + "_"] = renderer.pop(key)
            return patch

        def dropped(key):
            return lambda pr: pr["microformat"]["playerMicroformatRenderer"].pop(key)

        for label, patch, names in (
            ("unlisted video whose microformat was renamed", unlisted_but_microformat_renamed,
             ["isUnlisted", "isShortsEligible"]),
            ("isUnlisted renamed", renamed("isUnlisted"), ["isUnlisted"]),
            ("isShortsEligible dropped", dropped("isShortsEligible"), ["isShortsEligible"]),
        ):
            with self.subTest(label=label):
                meta = self.fetch(self.oembed_ok(), FakeFetch(_ok(_page(ZOO, patch=patch))), ZOO)
                self.assertTrue(meta.page_read and meta.player_read and meta.playable)
                with self.assertRaises(VideoRefused) as caught:
                    preflight(meta, PreflightOptions())
                self.assertEqual(caught.exception.flag, "--allow-unverified")
                for name in names:
                    self.assertIn(name, str(caught.exception))
                self.assertTrue(preflight(meta, PreflightOptions(allow_unverified=True)))


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
                elif self.path == "/badredirect":
                    self.send_response(302)
                    self.send_header("Location", "http://[::1/x")
                    self.end_headers()
                elif self.path == "/short":
                    # 說會送 1000 個位元組，只送 3 個就關掉連線
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b"abc")
                    self.wfile.flush()
                elif self.path == "/drip":
                    # 每 0.1 秒只送一個位元組：每次讀取都不逾時，但永遠讀不完
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        pass
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

    def test_a_redirect_to_a_url_that_cannot_be_parsed_is_an_oserror_not_a_valueerror(self):
        """Location: http://[::1/x 讓 urllib 自己丟 ValueError；呼叫端只接 OSError。"""
        with self.assertRaises(OSError):
            yt.http_page(self.base + "/badredirect")

    def test_a_body_shorter_than_its_content_length_is_a_broken_connection(self):
        """被截斷的回應不能當成完整的 200：缺掉的那一段可能剛好有「不公開」的旗標。"""
        with self.assertRaises(OSError):
            yt.http_page(self.base + "/short")

    def test_there_is_an_overall_deadline_on_top_of_the_per_read_timeout(self):
        """每一次讀取都沒逾時、卻一直只來一個位元組的對方，不能讓指令掛上幾天（只有 Ctrl-C 能停）。"""
        started = time.monotonic()
        with self.assertRaises(TimeoutError):          # TimeoutError 是 OSError，呼叫端接得住
            yt._http_get(self.base + "/drip", yt._PAGE_HEADERS, 5.0, 1000, 0.5)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_an_ordinary_response_is_not_hurried_by_the_deadline(self):
        response = yt._http_get(self.base + "/ok", yt._PAGE_HEADERS, 5.0, 100000, 5.0)
        self.assertEqual(len(response.body), 5000)

    def test_the_page_gets_thirty_seconds_and_oembed_fifteen(self):
        with mock.patch.object(yt, "_http_get") as get:
            yt.http_page("https://www.youtube.com/watch?v=x")
            yt.http_oembed("https://www.youtube.com/oembed?x")
        self.assertEqual(get.call_args_list[0].args[2:], (yt._PAGE_TIMEOUT, yt._PAGE_MAX_BYTES, 30.0))
        self.assertEqual(get.call_args_list[1].args[2:], (yt._OEMBED_TIMEOUT, yt._OEMBED_MAX_BYTES, 15.0))

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

            read1 = read

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
