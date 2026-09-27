"""查證 API：只給公開的判定，分數跟著公式走。"""
from __future__ import annotations

import urllib.parse
from datetime import date

from django.test import TestCase

from articles.models import Article, ArticleStatus
from factchecks.models import Claim, Evidence, FactCheckRun, ReviewStatus, RunStatus


def _article(ivod_id="171140", speaker="邱慧洳"):
    return Article.objects.create(
        ivod_id=ivod_id, slug=f"2026-08-25-{ivod_id}", title=f"2026-08-25 {speaker}－院會",
        speaker=speaker, date=date(2026, 8, 25),
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}", status=ArticleStatus.READY)


def _claim(article, index, verdict="supported", review=ReviewStatus.AUTO):
    claim = Claim.objects.create(
        article=article, index=index, quote=f"原話 {index}", timestamp=32.0,
        kind="law_article", statement=f"主張 {index}", verdict=verdict, method="numeric",
        rationale="相同", review_status=review)
    Evidence.objects.create(claim=claim, position=1, source="law", title="醫療法 第一百零六條",
                            official_url="https://law.moj.gov.tw/x",
                            api_url="https://ly.govapi.tw/v2/x", excerpt="三萬元以上")
    return claim


class ArticleClaimsTests(TestCase):
    def test_the_detail_page_carries_public_claims_with_evidence(self):
        article = _article()
        FactCheckRun.objects.create(article=article, status=RunStatus.DONE)
        _claim(article, 1)
        _claim(article, 2, "contradicted", ReviewStatus.PENDING)
        _claim(article, 3, "contradicted", ReviewStatus.APPROVED)
        _claim(article, 4, "supported", ReviewStatus.REJECTED)
        body = self.client.get(f"/api/articles/{article.slug}").json()
        self.assertTrue(body["factcheck_checked"])
        self.assertEqual([c["index"] for c in body["claims"]], [1, 3])
        self.assertTrue(body["claims"][1]["reviewed"])
        self.assertEqual(body["claims"][0]["evidence"][0]["excerpt"], "三萬元以上")

    def test_an_unchecked_article_says_so(self):
        body = self.client.get(f"/api/articles/{_article().slug}").json()
        self.assertFalse(body["factcheck_checked"])
        self.assertEqual(body["claims"], [])


class SpeakerScoreTests(TestCase):
    def test_speakers_carry_their_score(self):
        article = _article()
        for i in range(1, 6):
            _claim(article, i)
        items = self.client.get("/api/speakers").json()["items"]
        score = items[0]["factcheck"]
        self.assertEqual(score["checked"], 5)
        self.assertAlmostEqual(score["rate"], 6 / 7)
        self.assertEqual(score["min_sample"], 5)

    def test_a_small_sample_has_no_rate(self):
        _claim(_article(), 1)
        score = self.client.get("/api/speakers").json()["items"][0]["factcheck"]
        self.assertIsNone(score["rate"])
        self.assertEqual(score["checked"], 1)

    def test_a_speakers_claims_list_links_back_to_articles(self):
        article = _article()
        _claim(article, 1)
        _claim(article, 2, "unverifiable")
        _claim(_article("2", speaker="別人"), 1)
        url = f"/api/speakers/{urllib.parse.quote('邱慧洳')}/claims"
        body = self.client.get(url).json()
        self.assertEqual(body["speaker"], "邱慧洳")
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["items"][0]["article_slug"], article.slug)
        self.assertEqual(body["score"]["unverifiable"], 1)

    def test_an_unknown_speaker_is_an_empty_record(self):
        body = self.client.get(f"/api/speakers/{urllib.parse.quote('沒有人')}/claims").json()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["score"]["checked"], 0)
