"""新北市議會清單來源：用存下來的真實頁面，不碰網路。

名冊（議長、副議長、黨團）也從存下來的官網頁面組出來，講者規則測的是真實資料。
"""
from __future__ import annotations

import os
import re
import urllib.error
from datetime import date
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase, override_settings

from articles.ivod_source import SourceUnavailable
from articles.members_sync import (NTPC_CAUCUSES, MemberRecord, MembersUnavailable,
                                   parse_ntpc_caucus, parse_ntpc_list, parse_ntpc_profile, sync)
from articles.models import Membership
from articles.names import normalize_name
from articles.ntpc_source import (MIN_SECONDS, Card, NtpcDailySource, NtpcRoster, Slot,
                                  classify, day_chair, parse_cards, parse_echo, parse_file_key,
                                  speakers_for)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
BASE = "https://vod.example"

# 2026-08-18（業務質詢）：報告事項、國民黨團、民進黨團×2、兩位個人時段；
# 馬見Lahuy．Ipin 那段被上架兩次（950e5ed5 與 812869ef 是同一個檔案）
REPORT_0818 = "6cb1425c-b1ef-4d88-a265-0210df61a8b1"
KMT_0818 = "23e98eba-fc75-438e-94a0-a294ea4a4491"
DPP1_0818 = "c3f38ff6-3cde-4a53-93c2-1c4b42effd3c"
DPP2_0818 = "4e7fe692-ab6e-4f4e-9f9a-819fa7cfae6d"
LIWENG_0818 = "f453c011-e935-4a70-ae6f-d5b61a6bb255"
MAJIAN_0818 = "950e5ed5-7686-43f7-b86f-7168e82e0620"
MAJIAN_DUP_0818 = "812869ef-79a2-4094-83e2-67eac4c6faf4"
# 2026-09-16（市政總質詢）：報告事項 21 秒、上午、下午
REPORT_0916 = "594f31a1-069b-4066-9c75-fbc125dd4603"
MORNING_0916 = "ebc80ece-7491-4288-be73-7c59f6b4815c"
AFTERNOON_0916 = "4d143c92-f66b-46eb-bd52-6cbf7657165b"
# 2026-09-17（多黨混合：市長報告總預算）
BUDGET1_0917 = "a8e416e9-c390-45eb-84d7-aa8a9c89fc6a"
BUDGET2_0917 = "84dc4706-2cf0-4735-8cd5-03a5ff0584a8"

# 真實播放器頁有存下來的直接用；沒存到的用同一個模板換 GUID 與檔案 key
_REAL_PLAYERS = {KMT_0818, DPP2_0818, MAJIAN_0818, MAJIAN_DUP_0818, MORNING_0916, AFTERNOON_0916}
_SYNTHETIC_KEYS = {
    REPORT_0818: "0408R1150818010",
    DPP1_0818: "0408R1150818030",
    LIWENG_0818: "0408R1150818050",
    REPORT_0916: "0408R1150916010",
    BUDGET1_0917: "0408R1150917020",
    BUDGET2_0917: "0408R1150917030",
}
_CARD_MARK = '<div class="col-md-3 col-lg-2" style="display:block;">'


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _player(guid):
    if guid in _REAL_PLAYERS:
        return _read(f"ntpc_player_{guid[:8]}.html")
    # 串流路徑是 /Book_SD/{KEY}/{KEY}{SEQ}.mp4/：資料夾與檔名都要換
    key = _SYNTHETIC_KEYS[guid]
    template = _read("ntpc_player_23e98eba.html")
    return (template.replace(KMT_0818, guid)
            .replace("0408R1150818/0408R1150818020", f"{key[:-3]}/{key}"))


def _split_cards(page):
    head, *cards = page.split(_CARD_MARK)
    return head, [_CARD_MARK + c for c in cards]


def _day_page(day_slash, total, cards, template="ntpc_search_2026-09-16.html"):
    """用真實頁面的外框與卡片拼出「某一天的查詢結果」：回顯日期與總筆數照給的改。"""
    head, _ = _split_cards(_read(template))
    head = re.sub(r"開會日期:[^;]*;", f"開會日期:{day_slash}~{day_slash};", head, count=1)
    head = re.sub(r"總筆數:\d+", f"總筆數:{total}", head, count=1)
    return head + "".join(cards)


def _roster():
    """從存下來的官網頁面組名冊：議長、副議長看個人頁的「現任」，黨團看黨團頁。"""
    listing = parse_ntpc_list(_read("ntpc_councilor_all.html"))
    caucus_of = {}
    for number, caucus in NTPC_CAUCUSES.items():
        for cid in parse_ntpc_caucus(_read(f"ntpc_party_group_P{number}.html")):
            caucus_of.setdefault(cid, caucus)
    roles = {cid: parse_ntpc_profile(_read(f"ntpc_councilor_C{cid}.html"))[1]
             for cid in ("482", "483")}
    return NtpcRoster.from_rows((name, roles.get(cid, ""), caucus_of.get(cid, ""))
                                for cid, name, _ in listing)


class FakeHttp:
    """查詢（POST）依序回 search 裡的頁面，是例外就丟；GET 的播放器頁依 GUID 回，
    其他 GET 依路徑片段回 pages。記下每一個請求。"""

    def __init__(self, search, pages=None, players=None):
        self.search = list(search)
        self.pages = pages or {}
        self.players = players or {}
        self.posts, self.gets, self.resets = [], [], 0

    def post_form(self, path, fields):
        self.posts.append((path, dict(fields)))
        return self._answer(self.search.pop(0))

    def get(self, path):
        self.gets.append(path)
        if "VideoPlayer" in path:
            guid = parse_qs(urlsplit(path).query)["assetID"][0]
            return self._answer(self.players.get(guid) or _player(guid))
        for key, body in self.pages.items():
            if key in path:
                return self._answer(body)
        raise AssertionError(f"沒準備這個路徑：{path}")

    def reset(self):
        self.resets += 1

    @staticmethod
    def _answer(item):
        if isinstance(item, BaseException):
            raise item
        return item

    def player_guids(self):
        return [parse_qs(urlsplit(p).query)["assetID"][0] for p in self.gets if "VideoPlayer" in p]


def _http_error(code):
    return urllib.error.HTTPError(f"{BASE}/VodCloudV2/VOD/Search", code, "error", None, None)


def _source(http, roster=None, include_mixed=True, sleep=None):
    return NtpcDailySource(BASE, roster=_roster() if roster is None else roster, http=http,
                           sleep=sleep or (lambda s: None), include_mixed=include_mixed)


class NameTests(SimpleTestCase):
    def test_spaces_and_interpuncts_are_normalised(self):
        """官網「宋雨蓁 Nikar‧Falong」對影音「宋雨蓁Nikar．Falong」。"""
        self.assertEqual(normalize_name("宋雨蓁 Nikar‧Falong"), "宋雨蓁Nikar．Falong")
        self.assertEqual(normalize_name("蘇錦雄 Paylang·Caya"), "蘇錦雄Paylang．Caya")
        self.assertEqual(normalize_name("馬見Lahuy・Ipin"), "馬見Lahuy．Ipin")
        self.assertEqual(normalize_name("　林 國春\t"), "林國春")
        self.assertEqual(normalize_name("馬見Lahuy．Ipin"), "馬見Lahuy．Ipin")


class ParseTests(SimpleTestCase):
    def test_cards_are_read_with_labels_roc_dates_and_durations(self):
        cards = parse_cards(_read("ntpc_search_2026-09-16.html"))
        self.assertEqual([c.guid for c in cards], [REPORT_0916, MORNING_0916, AFTERNOON_0916])
        morning = cards[1]
        self.assertEqual(morning.session, "第4屆第8次定期會")
        # 「議　　程」中間是兩個 U+3000，照樣認得出來
        self.assertEqual(morning.agenda, "市政總質詢")
        self.assertEqual(morning.speakers, ("周雅玲", "林裔綺", "張嘉玲", "許昭興", "陳鴻源",
                                            "彭佳芸", "廖宜琨", "鄭宇恩", "鍾宏仁"))
        self.assertEqual(morning.date, "2026-09-16")          # 民國 115-09-16
        self.assertEqual(morning.start, "10:00:12")
        self.assertEqual(morning.duration_seconds, 1 * 3600 + 59 * 60 + 26)
        self.assertEqual((cards[0].agenda, cards[0].duration_seconds), ("市政總質詢-報告事項", 21))

    def test_indigenous_names_on_cards_are_kept_normalised(self):
        cards = {c.guid: c for c in parse_cards(_read("ntpc_search_2026-08-18.html"))}
        self.assertEqual(cards[MAJIAN_0818].speakers, ("馬見Lahuy．Ipin",))
        self.assertEqual(cards[MAJIAN_0818].agenda,
                         "一至六審各機關聯合業務報告及質詢-馬見Lahuy．Ipin議員")

    def test_the_echo_gives_the_filter_and_the_total(self):
        self.assertEqual(parse_echo(_read("ntpc_search_2026-09-16.html")),
                         ("定期會及臨時會;開會日期:2026/09/16~2026/09/16;排序:開會日期;", 3))
        self.assertEqual(parse_echo(_read("ntpc_search_empty_2026-09-18_to_09-29.html"))[1], 0)
        self.assertIsNone(parse_echo("<html></html>"))

    def test_the_player_page_gives_the_file_key(self):
        self.assertEqual(parse_file_key(_read("ntpc_player_ebc80ece.html")), "0408R1150916020")
        # 重複上架：兩個 GUID、同一個檔案
        self.assertEqual(parse_file_key(_read("ntpc_player_950e5ed5.html")),
                         parse_file_key(_read("ntpc_player_812869ef.html")))
        self.assertEqual(parse_file_key("<html>改版了</html>"), "")


class ClassifyTests(SimpleTestCase):
    """決策 2：只收質詢。"""

    def test_interpellations_are_kept(self):
        self.assertEqual(classify("市政總質詢"), Slot.GENERAL)
        self.assertEqual(classify(" 市政總質詢 "), Slot.GENERAL)
        self.assertEqual(classify("一至六審各機關聯合業務報告及質詢-國民黨團發言"), Slot.CAUCUS)
        self.assertEqual(classify("二、三審各機關聯合業務報告及質詢-民進黨團聯合發言"), Slot.CAUCUS)
        self.assertEqual(classify("一至六審各機關聯合業務報告及質詢-李翁議員月娥"), Slot.INDIVIDUAL)
        self.assertEqual(classify("一至六審各機關聯合業務報告及質詢-馬見Lahuy．Ipin議員"),
                         Slot.INDIVIDUAL)
        self.assertEqual(classify("四、五審各機關聯合業務報告及質詢-另一種寫法"), Slot.BUSINESS)

    def test_mixed_party_sessions_follow_the_setting(self):
        for agenda in ("市長施政報告", "市長報告116年度總預算編製之經過",
                       "市府針對「免費營養午餐經費及供餐品質」專案報告"):
            self.assertEqual(classify(agenda), Slot.MIXED, agenda)
            self.assertIsNone(classify(agenda, include_mixed=False), agenda)

    def test_everything_else_is_skipped(self):
        for agenda in ("報告事項", "市政總質詢-報告事項", "三讀議案第一讀會", "討論議案-第二審查會",
                       "預備會議-討論事項", "開幕典禮", "委員會聯席審議-114年度新北市總決算審核報告",
                       "聽取審計部新北市審計處對-114年度新北市總決算審核報告",
                       "第一審查會-「新北市新建殯葬設施可行性評估」報告"):
            self.assertIsNone(classify(agenda), agenda)


def _card(agenda, *speakers, start="14:00:00", seconds=3600, day="2026-08-12"):
    return Card(guid="0" * 8 + "-0000-0000-0000-" + "0" * 12, session="第4屆第8次定期會",
                agenda=agenda, speakers=tuple(speakers), date=day, start=start,
                duration_seconds=seconds)


class SpeakerRuleTests(SimpleTestCase):
    """決策 3：議長／副議長移除 → 個人時段／黨團時段／多黨混合／市政總質詢。"""

    def setUp(self):
        self.roster = _roster()

    def test_the_roster_knows_the_chairs_and_caucuses(self):
        self.assertEqual(self.roster.chairs, frozenset({"蔣根煌", "陳鴻源"}))
        self.assertEqual(self.roster.caucus_of["彭佳芸"], "民進黨團")
        self.assertEqual(self.roster.caucus_of["宋雨蓁Nikar．Falong"], "無黨團結聯盟")
        self.assertEqual(len(self.roster.caucus_of), 64)

    def test_the_general_interpellation_only_loses_the_chair(self):
        morning = parse_cards(_read("ntpc_search_2026-09-16.html"))[1]
        names = speakers_for(morning, Slot.GENERAL, self.roster, chair="陳鴻源")
        self.assertEqual(names, ["周雅玲", "林裔綺", "張嘉玲", "許昭興", "彭佳芸", "廖宜琨",
                                 "鄭宇恩", "鍾宏仁"])

    def test_a_general_interpellation_keeps_a_councilor_who_chaired_another_day(self):
        card = _card("市政總質詢", "彭佳芸", "陳鴻源", "鄭宇恩")
        self.assertEqual(speakers_for(card, Slot.GENERAL, self.roster, chair="彭佳芸"),
                         ["彭佳芸", "鄭宇恩"])

    def test_a_caucus_slot_drops_the_convener_from_the_other_caucus(self):
        """2026-08-12 民進黨團時段：主持的召集人蔡健棠（國民黨團）也被列進來。"""
        card = _card("一至六審各機關聯合業務報告及質詢-民進黨團聯合發言",
                     "山田摩衣", "李倩萍", "林銘仁", "翁震州", "陳乃瑜", "陳永福", "陳啟能",
                     "彭一書", "蔡健棠", "戴瑋姗", "鍾宏仁")
        names = speakers_for(card, Slot.CAUCUS, self.roster)
        self.assertNotIn("蔡健棠", names)
        self.assertEqual(len(names), 10)

    def test_an_individual_slot_is_only_its_owner(self):
        card = _card("一至六審各機關聯合業務報告及質詢-李翁議員月娥", "李翁月娥", "彭佳芸")
        self.assertEqual(speakers_for(card, Slot.INDIVIDUAL, self.roster), ["李翁月娥"])
        card = _card("一至六審各機關聯合業務報告及質詢-宋雨蓁 Nikar‧Falong議員", "宋雨蓁Nikar．Falong")
        self.assertEqual(speakers_for(card, Slot.INDIVIDUAL, self.roster), ["宋雨蓁Nikar．Falong"])

    def test_a_mixed_slot_drops_the_day_chair_from_the_opening_report(self):
        report = _card("報告事項", "顏蔚慈", start="14:00:15", seconds=44)
        mixed = _card("市長施政報告", "石一佑", "顏蔚慈", "蔣根煌", "林國春", start="14:01:00")
        chair = day_chair([mixed, report])
        self.assertEqual(chair, "顏蔚慈")
        self.assertEqual(speakers_for(mixed, Slot.MIXED, self.roster, chair), ["石一佑", "林國春"])

    def test_the_day_chair_is_only_a_single_name_on_the_opening_report(self):
        """2026-09-17 開場報告事項列了三個人：認不出主席，就不移除任何人。"""
        cards = [c for c in parse_cards(_read("ntpc_search_session_4_8_R_page01.html"))
                 if c.date == "2026-09-17"]
        self.assertEqual(day_chair(cards), "")
        later_single = _card("報告事項", "林秉宥", start="16:58:22")
        opening_many = _card("報告事項", "林金結", "黃淑君", start="16:29:17")
        self.assertEqual(day_chair([later_single, opening_many]), "")
        self.assertEqual(day_chair([_card("市政總質詢-報告事項", "陳鴻源", start="09:59:51")]),
                         "陳鴻源")
        self.assertEqual(day_chair([]), "")

    def test_an_empty_roster_only_uses_rules_that_need_no_roster(self):
        """名冊還沒同步：個人時段照字樣認人；其他時段只能移除開場報告事項認出的主席。"""
        empty = NtpcRoster()
        caucus = _card("一至六審各機關聯合業務報告及質詢-民進黨團聯合發言",
                       "石一佑", "蔡健棠", "彭佳芸", "陳鴻源")
        self.assertEqual(speakers_for(caucus, Slot.CAUCUS, empty, chair="彭佳芸"),
                         ["石一佑", "蔡健棠", "陳鴻源"])
        individual = _card("一至六審各機關聯合業務報告及質詢-李翁議員月娥", "李翁月娥", "彭佳芸")
        self.assertEqual(speakers_for(individual, Slot.INDIVIDUAL, empty), ["李翁月娥"])
        general = _card("市政總質詢", "周雅玲", "陳鴻源")
        self.assertEqual(speakers_for(general, Slot.GENERAL, empty, chair="陳鴻源"), ["周雅玲"])


class SourceTests(SimpleTestCase):
    def test_a_general_interpellation_day(self):
        http = FakeHttp([_read("ntpc_search_2026-09-16.html")])
        clips = _source(http).clips_for(date(2026, 9, 16))
        self.assertEqual([c.ivod_id for c in clips],
                         ["ntpc-0408R1150916020", "ntpc-0408R1150916030"])
        morning, afternoon = clips
        self.assertEqual(morning.speaker, "周雅玲、林裔綺、張嘉玲、許昭興、彭佳芸、廖宜琨、鄭宇恩、鍾宏仁")
        self.assertEqual(afternoon.speaker, "林裔綺、許昭興")
        self.assertEqual(morning.meeting, "第4屆第8次定期會 市政總質詢")
        self.assertEqual(morning.date, "2026-09-16")
        self.assertEqual(morning.duration_seconds, 7166)
        self.assertEqual(morning.ivod_url,
                         f"{BASE}/VodCloudV2/VOD/ViewMetaData?assetID={MORNING_0916}")
        self.assertEqual(morning.source, "ntpc")
        self.assertTrue(morning.has_transcript)
        # 21 秒的報告事項不收，連播放器頁都不必查
        self.assertEqual(http.player_guids(), [MORNING_0916, AFTERNOON_0916])

    def test_the_search_sends_all_nine_fields(self):
        http = FakeHttp([_read("ntpc_search_2026-09-16.html")])
        _source(http).clips_for(date(2026, 9, 16))
        path, fields = http.posts[0]
        self.assertEqual(path, "/VodCloudV2/VOD/Search")
        self.assertEqual(fields, {"pageindex": "1", "MJ": "", "MP": "", "MType": "",
                                  "sMDate": "2026/09/16", "eMDate": "2026/09/16", "Keyword": "",
                                  "cEPName": "", "Sort": "MDate"})

    def test_a_business_interpellation_day(self):
        http = FakeHttp([_read("ntpc_search_2026-08-18.html")])
        clips = _source(http).clips_for(date(2026, 8, 18))
        self.assertEqual([(c.ivod_id, c.speaker) for c in clips], [
            ("ntpc-0408R1150818020", "江怡臻、宋明宗、周勝考、林金結、林國春、洪佳君、陳偉杰、黃永昌、楊春妹、蘇泓欽"),
            ("ntpc-0408R1150818030", "山田摩衣、卓冠廷"),
            ("ntpc-0408R1150818040", "石一佑、李宇翔、張嘉玲、張維倩、張錦豪、彭佳芸"),
            ("ntpc-0408R1150818050", "李翁月娥"),
            ("ntpc-0408R1150818060", "馬見Lahuy．Ipin"),
        ])
        self.assertEqual(clips[3].meeting, "第4屆第8次定期會 一至六審各機關聯合業務報告及質詢-李翁議員月娥")
        self.assertEqual(clips[3].duration_seconds, 7 * 60 + 15)

    def test_the_same_file_under_two_guids_is_one_clip(self):
        http = FakeHttp([_read("ntpc_search_2026-08-18.html")])
        clips = _source(http).clips_for(date(2026, 8, 18))
        majian = [c for c in clips if c.ivod_id == "ntpc-0408R1150818060"]
        self.assertEqual(len(majian), 1)
        # ivod_url 用第一個看到的 GUID
        self.assertTrue(majian[0].ivod_url.endswith(MAJIAN_0818))
        self.assertIn(MAJIAN_DUP_0818, http.player_guids())

    def test_requests_are_sequential_one_second_apart(self):
        sleeps = []
        http = FakeHttp([_read("ntpc_search_2026-09-16.html")])
        _source(http, sleep=sleeps.append).clips_for(date(2026, 9, 16))
        # 查詢 + 兩個播放器頁 = 3 個請求，第一個不等
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_short_clips_are_skipped(self):
        self.assertEqual(MIN_SECONDS, 180)
        afternoon = _split_cards(_read("ntpc_search_2026-09-16.html"))[1][2]
        real = '<span class="timecode">01:00:10</span>'
        self.assertIn(real, afternoon)
        too_short = afternoon.replace(real, '<span class="timecode">00:02:59</span>')
        just_enough = afternoon.replace(real, '<span class="timecode">00:03:00</span>')
        for card, expected in ((too_short, []), (just_enough, ["ntpc-0408R1150916030"])):
            http = FakeHttp([_day_page("2026/09/16", 1, [card])])
            self.assertEqual([c.ivod_id for c in _source(http).clips_for(date(2026, 9, 16))],
                             expected)

    def test_a_clip_with_nobody_left_is_not_registered(self):
        roster = NtpcRoster(chairs=frozenset(), caucus_of={"江怡臻": "國民黨團"})
        http = FakeHttp([_read("ntpc_search_2026-08-18.html")])
        with self.assertLogs("articles.ntpc_source", level="INFO"):
            clips = _source(http, roster=roster).clips_for(date(2026, 8, 18))
        # 民進黨團兩段在這份名冊上沒有任何成員：不登記，也不查播放器頁
        self.assertNotIn("ntpc-0408R1150818030", [c.ivod_id for c in clips])
        self.assertNotIn(DPP1_0818, http.player_guids())
        self.assertEqual(clips[0].speaker, "江怡臻")

    def test_mixed_sessions_are_optional(self):
        cards = [c for c in _split_cards(_read("ntpc_search_session_4_8_R_page01.html"))[1][:4]]
        page = _day_page("2026/09/17", 4, cards)
        clips = _source(FakeHttp([page])).clips_for(date(2026, 9, 17))
        self.assertEqual([(c.ivod_id, c.speaker) for c in clips], [
            ("ntpc-0408R1150917020", "林國春、洪佳君、陳儀君、黃心華、楊春妹"),
            ("ntpc-0408R1150917030", "石一佑、李宇翔、李倩萍、李翁月娥、陳世軒、黃淑君、鄭宇恩"),
        ])
        self.assertEqual(clips[0].meeting, "第4屆第8次定期會 市長報告116年度總預算編製之經過")
        clips = _source(FakeHttp([page]), include_mixed=False).clips_for(date(2026, 9, 17))
        self.assertEqual(clips, [])

    def test_an_empty_day_makes_no_further_requests(self):
        page = _read("ntpc_search_empty_2026-09-18_to_09-29.html").replace(
            "2026/09/18~2026/09/29", "2026/09/18~2026/09/18")
        http = FakeHttp([page])
        self.assertEqual(_source(http).clips_for(date(2026, 9, 18)), [])
        self.assertEqual(http.gets, [])

    def test_an_empty_roster_degrades_and_warns_once(self):
        source = NtpcDailySource(BASE, http=FakeHttp([_read("ntpc_search_2026-08-18.html"),
                                                      _read("ntpc_search_2026-09-16.html")]),
                                 sleep=lambda s: None)
        with self.assertLogs("articles.ntpc_source", level="WARNING") as logs:
            first = source.clips_for(date(2026, 8, 18))
            second = source.clips_for(date(2026, 9, 16))
        self.assertEqual(sum("名冊" in line for line in logs.output), 1)
        by_id = {c.ivod_id: c.speaker for c in first + second}
        # 當天主席彭佳芸（開場報告事項只列她）被當成主持人拿掉；個人時段照常
        self.assertEqual(by_id["ntpc-0408R1150818040"], "石一佑、李宇翔、張嘉玲、張維倩、張錦豪")
        self.assertEqual(by_id["ntpc-0408R1150818050"], "李翁月娥")
        # 市政總質詢的主席是副議長，開場報告事項認得出來
        self.assertNotIn("陳鴻源", by_id["ntpc-0408R1150916020"])


class PagingTests(SimpleTestCase):
    """每頁 12 筆，下一頁用同一個 session 的 ToPage。"""

    def _pages(self):
        head, cards = _split_cards(_read("ntpc_search_2026-08-18.html"))
        return head + "".join(cards[:4]), head + "".join(cards[4:])

    def test_pages_are_followed_until_the_total_is_reached(self):
        first, second = self._pages()
        http = FakeHttp([first], pages={"ToPage=2": second})
        clips = _source(http).clips_for(date(2026, 8, 18))
        self.assertEqual(len(clips), 5)
        self.assertEqual([p for p in http.gets if "ToPage" in p], ["/VodCloudV2/VOD/ToPage?ToPage=2"])

    def test_a_page_without_new_cards_stops_the_paging(self):
        first, _ = self._pages()
        http = FakeHttp([first], pages={"ToPage=2": first})
        with self.assertLogs("articles.ntpc_source", level="WARNING") as logs:
            clips = _source(http).clips_for(date(2026, 8, 18))
        self.assertTrue(any("只拿到 4 筆" in line for line in logs.output))
        self.assertEqual(len(clips), 3)          # 前四張：報告事項不收，其餘三段

    def test_a_lost_session_while_paging_is_unavailable(self):
        first, _ = self._pages()
        lost = _read("ntpc_search_empty_2026-09-18_to_09-29.html")
        http = FakeHttp([first], pages={"ToPage=2": lost})
        with self.assertRaises(SourceUnavailable):
            _source(http).clips_for(date(2026, 8, 18))


class FailureTests(SimpleTestCase):
    def test_a_500_gets_a_fresh_session_and_one_retry(self):
        http = FakeHttp([_http_error(500), _read("ntpc_search_2026-09-16.html")])
        with self.assertLogs("articles.ntpc_source", level="WARNING"):
            clips = _source(http).clips_for(date(2026, 9, 16))
        self.assertEqual(len(clips), 2)
        self.assertEqual((len(http.posts), http.resets), (2, 1))

    def test_two_500s_are_unavailable(self):
        http = FakeHttp([_http_error(500), _http_error(500)])
        with self.assertLogs("articles.ntpc_source", level="WARNING"), \
                self.assertRaises(SourceUnavailable):
            _source(http).clips_for(date(2026, 9, 16))
        self.assertEqual((len(http.posts), http.resets), (2, 1))

    def test_other_errors_are_unavailable_without_retry(self):
        for error in (_http_error(503), OSError("connection refused")):
            http = FakeHttp([error])
            with self.assertRaises(SourceUnavailable):
                _source(http).clips_for(date(2026, 9, 16))
            self.assertEqual((len(http.posts), http.resets), (1, 0))

    def test_an_ignored_filter_is_unavailable(self):
        """篩選被靜默忽略時頁面照樣有卡片；只有回顯看得出來。"""
        # 查的是 9/18，回顯卻是一段日期區間
        http = FakeHttp([_read("ntpc_search_empty_2026-09-18_to_09-29.html")])
        with self.assertRaises(SourceUnavailable):
            _source(http).clips_for(date(2026, 9, 18))
        # 回顯根本沒有日期（依會期查的頁面）
        http = FakeHttp([_read("ntpc_search_session_4_8_R_page01.html")])
        with self.assertRaises(SourceUnavailable):
            _source(http).clips_for(date(2026, 9, 17))
        http = FakeHttp(["<html>維護中</html>"])
        with self.assertRaises(SourceUnavailable):
            _source(http).clips_for(date(2026, 9, 17))

    def test_one_missing_player_page_skips_only_that_clip(self):
        http = FakeHttp([_read("ntpc_search_2026-09-16.html")],
                        players={MORNING_0916: OSError("timeout")})
        with self.assertLogs("articles.ntpc_source", level="WARNING"):
            clips = _source(http).clips_for(date(2026, 9, 16))
        self.assertEqual([c.ivod_id for c in clips], ["ntpc-0408R1150916030"])

    def test_no_file_key_at_all_is_unavailable(self):
        http = FakeHttp([_read("ntpc_search_2026-09-16.html")],
                        players={MORNING_0916: "<html>改版了</html>",
                                 AFTERNOON_0916: OSError("timeout")})
        with self.assertLogs("articles.ntpc_source", level="WARNING"), \
                self.assertRaises(SourceUnavailable):
            _source(http).clips_for(date(2026, 9, 16))


class TlsTests(SimpleTestCase):
    def test_the_client_keeps_verification_but_drops_the_strict_flag(self):
        import ssl

        from articles.ntpc_source import _ssl_context

        context = _ssl_context()
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


def _ntpc(name, cid, role="", caucus="", party="中國國民黨", **kw):
    return MemberRecord(source="ntpc", external_id=cid, name=name, party=party, role=role,
                        caucus=caucus, **kw)


class RosterFromDbTests(TestCase):
    def test_only_current_ntpc_memberships_count(self):
        sync([_ntpc("蔣根煌", "483", role="議長", caucus="國民黨團"),
              _ntpc("彭佳芸", "593", caucus="民進黨團", party="民主進步黨"),
              _ntpc("宋雨蓁Nikar．Falong", "589", caucus="無黨團結聯盟", party="無黨籍"),
              MemberRecord(source="tccc", external_id="1", name="張家銨", party="民主進步黨")])
        sync([_ntpc("前議長", "1", role="議長", end_date=date(2022, 12, 24))])
        roster = NtpcRoster.from_db()
        self.assertEqual(roster.chairs, frozenset({"蔣根煌"}))
        self.assertEqual(dict(roster.caucus_of), {"蔣根煌": "國民黨團", "彭佳芸": "民進黨團",
                                                  "宋雨蓁Nikar．Falong": "無黨團結聯盟"})

    def test_an_empty_table_is_an_empty_roster(self):
        self.assertTrue(NtpcRoster.from_db().is_empty)


class IngestCommandTests(TestCase):
    """ingest_ivod：--source ntpc；名冊是空的就先同步一次。"""

    def _command(self):
        from io import StringIO

        from articles.management.commands.ingest_ivod import Command

        return Command(stdout=StringIO(), stderr=StringIO())

    def test_an_empty_roster_is_synced_once_before_querying(self):
        records = [_ntpc("蔣根煌", "483", role="議長", caucus="國民黨團")]
        with mock.patch("articles.management.commands.ingest_ivod.NtpcMemberSource") as cls:
            cls.return_value.fetch.return_value = records
            command = self._command()
            [source] = command._sources("ntpc")
            self.assertEqual(cls.return_value.fetch.call_count, 1)
            self.assertEqual(Membership.objects.filter(source="ntpc").count(), 1)
            self.assertEqual(source.name, "新北市議會")
            self.assertEqual(source._roster.chairs, frozenset({"蔣根煌"}))
            # 第二次：名冊已經有了，不再同步
            command._sources("ntpc")
            self.assertEqual(cls.return_value.fetch.call_count, 1)

    def test_a_failed_sync_continues_with_an_empty_roster(self):
        with mock.patch("articles.management.commands.ingest_ivod.NtpcMemberSource") as cls:
            cls.return_value.fetch.side_effect = MembersUnavailable("連不上")
            command = self._command()
            [source] = command._sources("ntpc")
        self.assertTrue(source._roster.is_empty)
        self.assertIn("連不上", command.stderr.getvalue())

    @override_settings(NTPC_INCLUDE_MIXED=False)
    def test_the_mixed_setting_is_passed_through(self):
        sync([_ntpc("蔣根煌", "483", role="議長")])
        [source] = self._command()._sources("ntpc")
        self.assertFalse(source._include_mixed)

    def test_no_flag_follows_the_enabled_settings(self):
        sync([_ntpc("蔣根煌", "483", role="議長")])
        for tccc, ntpc, expected in ((True, True, ["立法院", "臺中市議會", "新北市議會"]),
                                     (False, True, ["立法院", "新北市議會"]),
                                     (True, False, ["立法院", "臺中市議會"])):
            with override_settings(TCCC_ENABLED=tccc, NTPC_ENABLED=ntpc):
                self.assertEqual([s.name for s in self._command()._sources(None)], expected)

    @override_settings(NTPC_ENABLED=False)
    def test_the_flag_picks_ntpc_regardless_of_the_setting(self):
        sync([_ntpc("蔣根煌", "483", role="議長")])
        self.assertEqual([s.name for s in self._command()._sources("ntpc")], ["新北市議會"])


class SyncMembersCommandTests(SimpleTestCase):
    def test_the_ntpc_source_can_be_picked(self):
        from articles.management.commands.sync_members import Command

        self.assertEqual([s.name for s in Command()._sources("ntpc")], ["新北市議會"])
        self.assertEqual([s.name for s in Command()._sources(None)],
                         ["立法院", "臺中市議會", "新北市議會"])


class SchedulerStartupTests(TestCase):
    """排程啟動時：任何一個啟用中的來源還沒有名冊，就先同步一次。"""

    def _missing(self):
        from articles.management.commands.run_scheduler import sources_missing_members

        return sources_missing_members()

    def test_every_enabled_source_is_checked(self):
        self.assertEqual(self._missing(), ["ly", "tccc", "ntpc"])
        sync([MemberRecord(source="ly", external_id="1", name="甲", party="民主進步黨"),
              MemberRecord(source="tccc", external_id="1", name="乙", party="中國國民黨")])
        # 原本只看「整張表是不是空的」：立法院與臺中都有了，新北就永遠等到週日
        self.assertEqual(self._missing(), ["ntpc"])
        sync([_ntpc("蔣根煌", "483", role="議長")])
        self.assertEqual(self._missing(), [])

    @override_settings(TCCC_ENABLED=False, NTPC_ENABLED=False)
    def test_disabled_sources_are_not_waited_for(self):
        sync([MemberRecord(source="ly", external_id="1", name="甲", party="民主進步黨")])
        self.assertEqual(self._missing(), [])
