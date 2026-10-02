"""方法頁與 README 要跟程式一致。

方法頁（web/news/src/pages/method.astro）是側寫可信度的依據：寫錯了，讀者就照錯的規則讀數字。
這裡只驗「程式改過、文件曾經沒跟上」的那幾處，以及合併分支時被弄壞過的地方；文字本身怎麼寫
不在這裡管。後端只部署 services/news 時沒有網站與根目錄的檔案，那時跳過。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from articles import ly_records

ROOT = Path(__file__).resolve().parents[4]
METHOD = ROOT / "web" / "news" / "src" / "pages" / "method.astro"
WEB_README = ROOT / "web" / "news" / "README.md"
NEWS_README = ROOT / "services" / "news" / "README.md"
ROOT_README = ROOT / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    """從 start 第一次出現到它之後第一個 end（不含）。找不到就是測試失敗，不是空字串。"""
    head = text.index(start)
    return text[head:text.index(end, head)]


def _bullets(html: str) -> list[str]:
    return re.findall(r"<li>(.*?)</li>", html, re.S)


@unittest.skipUnless(METHOD.exists(), "沒有網站的原始碼")
class MethodPageTests(SimpleTestCase):
    def setUp(self):
        self.page = _read(METHOD)
        self.chamber = _between(self.page, 'id="profile-chamber"', 'id="profile-joint"')

    def _mentions(self, *phrases: str) -> None:
        """院內紀錄那一節有寫到這些字。失敗時只列缺的字，不把整節印出來。"""
        missing = [p for p in phrases if p not in self.chamber]
        self.assertEqual(missing, [], "方法頁的院內紀錄（#profile-chamber）沒有寫到")

    def test_each_data_limit_is_its_own_bullet(self):
        """合併分支時「院內紀錄每週才同步一次」與「有沒有再提是模型判斷的」被併進同一個 <li>。"""
        limits = _between(self.page, 'id="profile-limits"', 'id="profile-todo"')
        bullets = _bullets(limits)
        self.assertGreater(len(bullets), 5)
        for bullet in bullets:
            self.assertEqual(bullet.count("<strong"), 1, bullet)

    def test_cosigners_come_from_the_bill_list(self):
        """同步是在委員提案清單加 output_fields=連署人，不是逐人逐會期查連署紀錄。"""
        self.assertIn("連署人", ly_records._BILL_FIELDS)
        self.assertFalse("逐一查" in self.chamber, "方法頁還寫著逐一查每位委員的連署紀錄")
        self.assertTrue(re.search(r"連署人[^<]*清單|清單[^<]*連署人", self.chamber),
                        "方法頁要寫連署人是從議案清單讀的")

    def test_bills_count_in_the_session_of_their_first_reading(self):
        """bill_session：一件提案算在一讀那一次院會的會期，不是 LYAPI「會期」欄位（最新進度）。"""
        row = {"會議代碼": "院會-11-1-10", "會期": 5}
        self.assertEqual(ly_records.bill_session(row), 1)
        self._mentions("一讀", "最新進度")

    def test_records_of_unknown_outcome_are_left_out_of_the_denominators(self):
        """還沒有出席名單的會議、一張記名的票都沒有的表決，不算進任何人的分母（chamber.has_ballots、
        SessionRecords._attended）。"""
        self._mentions("還沒有出席名單", "一張記名的票都沒有")

    def test_the_presiding_officers_are_not_excluded_and_the_page_says_so(self):
        """院長、副院長依慣例不投票，LYAPI 名冊沒有職務欄位，所以沒有排除（README 的限制）。"""
        self._mentions("副院長", "不投票", "沒有職務")

    def test_attendance_is_read_from_the_minutes(self):
        """parse_meeting：出席以議事錄為準，請假的委員不算出席。"""
        self._mentions("議事錄", "請假")


@unittest.skipUnless(NEWS_README.exists() and ROOT_README.exists() and WEB_README.exists(),
                     "沒有根目錄或網站的 README")
class ReadmeTests(SimpleTestCase):
    def test_the_profile_api_row_mentions_the_chamber_block(self):
        """合併時 /people/{id}/profile 那一列掉了院內紀錄區塊的那句。"""
        row = next(line for line in _read(NEWS_README).splitlines()
                   if line.startswith("| `GET /api/people/{person_id}/profile"))
        self.assertIn("`chamber` 區塊", row)
        self.assertIn("不給投入量與具體度", row)

    def test_the_legislative_caucus_comes_from_lyapi(self):
        """sync_members 也把立法院的黨團（LYAPI 的「黨團」）存進 Membership.caucus。"""
        bullet = next(line for line in _read(ROOT_README).splitlines()
                      if line.startswith("- `sync_members`"))
        sentence = next(s for s in bullet.split("。") if "Membership.caucus" in s)
        self.assertIn("立法院", sentence)
        self.assertIn("「黨團」", sentence)

    def test_each_component_is_listed_once_in_the_tree(self):
        text = _read(WEB_README)
        tree = _between(text, "components/", "lib/")
        names = re.findall(r"\b([A-Z][A-Za-z]+)\b", tree)
        self.assertIn("ProfileChamber", names)
        self.assertEqual([n for n in set(names) if names.count(n) > 1], [])
