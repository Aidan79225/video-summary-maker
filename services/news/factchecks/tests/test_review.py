"""審核：核准才公開，駁回就不公開、不計分。"""
from __future__ import annotations

from datetime import date

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from articles.models import Article, ArticleStatus
from factchecks.admin import ClaimAdmin
from factchecks.models import Claim, ReviewStatus
from factchecks.review import review
from factchecks.scoring import public_claims


class ReviewTests(TestCase):
    def setUp(self):
        article = Article.objects.create(
            ivod_id="1", slug="s-1", title="t", speaker="甲", date=date(2026, 8, 25),
            ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/1", status=ArticleStatus.READY)
        self.claim = Claim.objects.create(
            article=article, index=1, quote="q", kind="law_article", statement="s",
            verdict="contradicted", method="numeric", review_status=ReviewStatus.PENDING)

    def test_approving_publishes(self):
        self.assertEqual(review(Claim.objects.all(), ReviewStatus.APPROVED), 1)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.review_status, ReviewStatus.APPROVED)
        self.assertIsNotNone(self.claim.reviewed_at)
        self.assertIn(self.claim, public_claims())

    def test_rejecting_hides(self):
        review(Claim.objects.all(), ReviewStatus.REJECTED)
        self.assertNotIn(self.claim, public_claims())

    def test_only_human_decisions_are_allowed(self):
        with self.assertRaises(ValueError):
            review(Claim.objects.all(), ReviewStatus.AUTO)

    def test_the_admin_form_cannot_bypass_the_review_actions(self):
        """攔的漏洞：在編輯頁直接改判定或審核狀態，就跳過了 review() 與 reviewed_at。"""
        request = RequestFactory().get("/")
        request.user = User(is_superuser=True, is_staff=True)
        form = ClaimAdmin(Claim, AdminSite()).get_form(request, self.claim)
        for name in ("verdict", "review_status", "reviewed_at", "method", "kind", "quote"):
            self.assertNotIn(name, form.base_fields)
