"""admin：失敗的文章勾一勾就能重送。"""
from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from articles.models import Article, ArticleStatus


def _article(ivod_id, status, source="ly", error="", attempts=0, speaker="範例一"):
    return Article.objects.create(
        ivod_id=ivod_id, slug=f"2026-08-27-{ivod_id}", title="t", speaker=speaker, source=source,
        date=date(2026, 8, 27), ivod_url="https://ivod/x", status=status, error=error,
        attempts=attempts, gpu_job_id="job-9")


class AdminTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)

    def test_the_changelist_shows_the_source_and_can_filter_by_it(self):
        _article("1", ArticleStatus.READY, speaker="立委甲")
        _article("tccc-2", ArticleStatus.READY, source="tccc", speaker="議員乙")
        res = self.client.get("/admin/articles/article/?source__exact=tccc")
        self.assertEqual(res.status_code, 200)
        # 側欄的篩選器會列出所有講者，所以看表格列而不是整頁文字
        self.assertEqual([a.speaker for a in res.context["cl"].result_list], ["議員乙"])
        self.assertContains(res, "臺中市議會")

    def test_requeue_resets_failed_articles_but_leaves_ready_ones_alone(self):
        failed = _article("1", ArticleStatus.FAILED, error="第一人稱", attempts=3)
        stuck = _article("2", ArticleStatus.PROCESSING, attempts=1)
        ready = _article("3", ArticleStatus.READY)
        res = self.client.post("/admin/articles/article/", {
            "action": "requeue",
            "_selected_action": [failed.pk, stuck.pk, ready.pk],
        }, follow=True)
        self.assertEqual(res.status_code, 200)
        for a in (failed, stuck):
            a.refresh_from_db()
            self.assertEqual((a.status, a.error, a.attempts, a.gpu_job_id),
                             (ArticleStatus.PENDING, "", 0, ""))
        ready.refresh_from_db()
        self.assertEqual(ready.status, ArticleStatus.READY)
        self.assertContains(res, "已打回待處理 2 篇")
        self.assertContains(res, "略過 1 篇")

    def test_the_change_form_renders_with_the_diagnostic_fields(self):
        a = _article("1", ArticleStatus.FAILED, error="模型輸出重試後仍不符合要求")
        res = self.client.get(f"/admin/articles/article/{a.pk}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "模型輸出重試後仍不符合要求")


class PersonAdminTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("admin2", "b@example.com", "pw")
        self.client.force_login(user)

    def test_merging_moves_memberships_and_articles_and_keeps_aliases(self):
        from articles.members_sync import MemberRecord, link_article, sync
        from articles.models import Membership, Person

        sync([MemberRecord(source="tccc", external_id="66", name="楊啓邦", party="中國國民黨")])
        sync([MemberRecord(source="ly", external_id="9", name="楊啟邦", party="中國國民黨")])
        self.assertEqual(Person.objects.count(), 2)
        a = _article("tccc-1", ArticleStatus.READY, source="tccc", speaker="楊啓邦")
        link_article(a)
        keep, other = Person.objects.order_by("id")
        res = self.client.post("/admin/articles/person/", {
            "action": "merge", "_selected_action": [keep.pk, other.pk]}, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Person.objects.count(), 1)
        keep.refresh_from_db()
        self.assertEqual(keep.aliases, ["楊啟邦"])
        self.assertEqual(Membership.objects.filter(person=keep).count(), 2)
        a.refresh_from_db()
        self.assertEqual(a.membership.person_id, keep.pk)

    def test_the_membership_list_renders(self):
        from articles.members_sync import MemberRecord, sync
        sync([MemberRecord(source="ly", external_id="1", name="甲", party="民主進步黨")])
        self.assertEqual(self.client.get("/admin/articles/membership/").status_code, 200)
        self.assertEqual(self.client.get("/admin/articles/person/").status_code, 200)
