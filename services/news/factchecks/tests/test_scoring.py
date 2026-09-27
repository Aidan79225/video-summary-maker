"""查證相符率：公開公式。"""
from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase, TestCase

from articles.models import Article, ArticleStatus
from factchecks.models import Claim, ReviewStatus, Verdict
from factchecks.scoring import MIN_SAMPLE, Score, score_of, scores_by_speaker


class FormulaTests(SimpleTestCase):
    def test_no_claims_has_no_rate(self):
        self.assertIsNone(Score().rate)
        self.assertEqual(Score().checked, 0)

    def test_fewer_than_the_minimum_sample_has_no_rate(self):
        """一兩則就給百分比，一則不符就是 0%——那不是查證，是運氣。"""
        self.assertIsNone(Score(supported=MIN_SAMPLE - 1).rate)

    def test_laplace_smoothing(self):
        self.assertAlmostEqual(Score(supported=5).rate, 6 / 7)
        self.assertAlmostEqual(Score(supported=13, partial=1, contradicted=1).rate,
                               14.5 / 17)

    def test_unverifiable_claims_are_not_counted(self):
        score = Score(supported=5, unverifiable=20)
        self.assertEqual(score.checked, 5)
        self.assertAlmostEqual(score.rate, 6 / 7)

    def test_counting_verdicts(self):
        score = score_of(["supported", "supported", "partial", "contradicted", "unverifiable"])
        self.assertEqual((score.supported, score.partial, score.contradicted, score.unverifiable),
                         (2, 1, 1, 1))


class SpeakerScoreTests(TestCase):
    def _article(self, ivod_id, speaker, status=ArticleStatus.READY):
        return Article.objects.create(
            ivod_id=ivod_id, slug=f"2026-08-25-{ivod_id}", title="t", speaker=speaker,
            date=date(2026, 8, 25), ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
            status=status)

    def _claim(self, article, index, verdict, review=ReviewStatus.AUTO):
        return Claim.objects.create(article=article, index=index, quote="q", kind="law_article",
                                    statement="s", verdict=verdict, method="numeric",
                                    review_status=review)

    def test_only_public_claims_of_published_articles_count(self):
        a = self._article("1", "邱慧洳")
        self._claim(a, 1, Verdict.SUPPORTED)
        self._claim(a, 2, Verdict.CONTRADICTED, ReviewStatus.PENDING)
        self._claim(a, 3, Verdict.CONTRADICTED, ReviewStatus.APPROVED)
        self._claim(a, 4, Verdict.SUPPORTED, ReviewStatus.REJECTED)
        hidden = self._article("2", "邱慧洳", status=ArticleStatus.FAILED)
        self._claim(hidden, 1, Verdict.SUPPORTED)
        score = scores_by_speaker()["邱慧洳"]
        self.assertEqual((score.supported, score.contradicted), (1, 1))

    def test_speakers_are_scored_separately(self):
        self._claim(self._article("1", "甲"), 1, Verdict.SUPPORTED)
        self._claim(self._article("2", "乙"), 1, Verdict.PARTIAL)
        scores = scores_by_speaker()
        self.assertEqual(scores["甲"].supported, 1)
        self.assertEqual(scores["乙"].partial, 1)
