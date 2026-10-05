"""admin：失敗的文章勾一勾就能重送。"""
from __future__ import annotations

import re
from datetime import date, datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

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


class ProfileAdminTests(TestCase):
    """會期與側寫快取是算出來的：列得出來、看得到，但不能在 admin 裡新增或改值。"""

    def setUp(self):
        from articles.members_sync import MemberRecord, sync
        from articles.profiles import compute_profiles

        user = get_user_model().objects.create_superuser("admin3", "c@example.com", "pw")
        self.client.force_login(user)
        sync([MemberRecord(source="tccc", external_id="66", name="楊啓邦", party="中國國民黨")])
        self.article = _article("tccc-1", ArticleStatus.READY, source="tccc", speaker="楊啓邦")
        Article.objects.filter(pk=self.article.pk).update(meeting="第4屆第8次定期會 市政總質詢")
        compute_profiles()

    def test_sessions_and_stats_are_listed(self):
        from articles.models import ProfileStat, Session

        res = self.client.get("/admin/articles/session/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "第4屆第8次定期會")
        res = self.client.get("/admin/articles/profilestat/?indicator=speeches")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([s.person.name for s in res.context["cl"].result_list], ["楊啓邦"])
        stat = ProfileStat.objects.first()
        self.assertEqual(self.client.get(f"/admin/articles/profilestat/{stat.pk}/change/").status_code,
                         200)
        session = Session.objects.get()
        self.assertEqual(self.client.get(f"/admin/articles/session/{session.pk}/change/").status_code,
                         200)

    def test_nothing_derived_can_be_added_by_hand(self):
        self.assertEqual(self.client.get("/admin/articles/session/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/articles/profilestat/add/").status_code, 403)

    def test_the_article_form_shows_its_session(self):
        res = self.client.get(f"/admin/articles/article/{self.article.pk}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="session"')


class TopicLabelAdminTests(TestCase):
    """議題標註頁：分類器讀到的文字、文章連結、在清單上直接選主領域；**看不到模型的分類**。"""

    # 刻意取一個不會出現在頁面其他地方的分類器名稱，出現了就是洩漏
    SECRET = "secret-model-xyz#topic-v9"

    def setUp(self):
        from articles.models import Slide, Topic, TopicLabel

        user = get_user_model().objects.create_superuser("admin4", "d@example.com", "pw")
        self.client.force_login(user)
        self.article = _article("t1", ArticleStatus.READY, speaker="王立")
        Article.objects.filter(pk=self.article.pk).update(
            brief={"one_liner": "國防部無人機交機不到一半", "key_numbers": [], "asks": []},
            meeting="第11屆第5會期外交及國防委員會第3次全體委員會議")
        Slide.objects.create(article=self.article, index=1, title="無人機採購進度")
        # 模型說是「勞動」——人還沒標
        Topic.objects.create(article=self.article, primary="labor", secondary="welfare",
                             classifier=self.SECRET, labeled_at=timezone.now())
        self.label = TopicLabel.objects.create(article=self.article)
        done = _article("t2", ArticleStatus.READY, speaker="甲")
        TopicLabel.objects.create(article=done, primary="finance")

    def _changelist(self, query=""):
        return self.client.get(f"/admin/articles/topiclabel/{query}")

    def test_the_list_shows_what_the_classifier_reads_and_links_to_the_article(self):
        res = self._changelist()
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "一句話：國防部無人機交機不到一半")
        self.assertContains(res, "- 無人機採購進度")
        self.assertContains(res, f"/admin/articles/article/{self.article.pk}/change/")
        # 主領域是清單上的下拉選單，選項是 12 個領域加「還沒標」
        self.assertContains(res, 'name="form-0-primary"')
        self.assertContains(res, "（還沒標）")
        self.assertContains(res, "地方建設／其他")

    def test_the_model_prediction_is_never_shown(self):
        """盲標：模型說「勞動」，人還沒標——頁面上不能有任何地方透露模型的答案。"""
        pages = [self._changelist(), self._changelist("?labelled=no"),
                 self.client.get(f"/admin/articles/topiclabel/{self.label.pk}/change/"),
                 self.client.get(f"/admin/articles/article/{self.article.pk}/change/")]
        for res in pages:
            self.assertEqual(res.status_code, 200)
            # 下拉選單本來就列出全部 12 個領域，拿掉之後頁面上不能出現模型選的那兩個
            body = re.sub(r"<select.*?</select>", "", res.content.decode(), flags=re.S)
            for leak in (self.SECRET, "labor", "勞動", "welfare", "衛生福利"):
                self.assertNotIn(leak, body, res.request["PATH_INFO"])
        # 還沒標的那一列，下拉選單停在「還沒標」，不是模型的「勞動」
        unlabelled = self._changelist("?labelled=no")
        self.assertContains(unlabelled, '<option value="" selected>（還沒標）</option>', html=True)
        self.assertNotContains(unlabelled, '<option value="labor" selected>勞動</option>', html=True)

    def test_the_unlabelled_filter(self):
        res = self._changelist("?labelled=no")
        self.assertEqual([label.article.speaker for label in res.context["cl"].result_list], ["王立"])
        res = self._changelist("?labelled=yes")
        self.assertEqual([label.article.speaker for label in res.context["cl"].result_list], ["甲"])

    def test_labelling_in_the_list_saves_the_primary_and_the_time(self):
        res = self.client.post("/admin/articles/topiclabel/?labelled=no", {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
            "form-0-id": str(self.label.pk), "form-0-primary": "defense",
            "form-0-note": "講的是國防預算", "_save": "儲存",
        })
        self.assertEqual(res.status_code, 302)
        self.label.refresh_from_db()
        self.assertEqual((self.label.primary, self.label.note), ("defense", "講的是國防預算"))
        self.assertIsNotNone(self.label.labeled_at)

    def test_a_primary_outside_the_list_is_refused(self):
        res = self.client.post("/admin/articles/topiclabel/", {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
            "form-0-id": str(self.label.pk), "form-0-primary": "space", "form-0-note": "",
            "_save": "儲存",
        })
        self.assertEqual(res.status_code, 200)
        self.label.refresh_from_db()
        self.assertEqual(self.label.primary, "")

    def test_labels_come_only_from_sampling(self):
        self.assertEqual(self.client.get("/admin/articles/topiclabel/add/").status_code, 403)


class TopicReadOnlyAdminTests(TestCase):
    """模型的分類與評估紀錄是算出來的：看得到，但不能在 admin 裡新增或改值。"""

    def setUp(self):
        from articles.models import Topic, TopicEvaluation

        user = get_user_model().objects.create_superuser("admin5", "e@example.com", "pw")
        self.client.force_login(user)
        article = _article("t1", ArticleStatus.READY, speaker="王立")
        self.topic = Topic.objects.create(article=article, primary="finance", classifier="m#topic-v1",
                                          labeled_at=timezone.now())
        self.evaluation = TopicEvaluation.objects.create(
            source="ly", classifier="m#topic-v1", labeled=20, correct=17, accuracy=0.85, passed=True,
            mistakes=[], ran_at=timezone.now())

    def test_topics_and_evaluations_are_listed_and_viewable(self):
        res = self.client.get("/admin/articles/topic/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "財政經濟")
        res = self.client.get("/admin/articles/topicevaluation/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "85.0%")
        for url in (f"/admin/articles/topic/{self.topic.pk}/change/",
                    f"/admin/articles/topicevaluation/{self.evaluation.pk}/change/"):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200)
            self.assertNotContains(res, 'name="_save"')

    def test_nothing_can_be_added_by_hand(self):
        self.assertEqual(self.client.get("/admin/articles/topic/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/articles/topicevaluation/add/").status_code, 403)


class TopicEvaluationDeleteTests(TestCase):
    """刪掉通過的評估會改變上線條件：上線的分類器變了就當場重算側寫，數字才跟區塊上寫的版本一致。"""

    def setUp(self):
        from articles.models import Membership, Person, Topic, TopicEvaluation
        from articles.profiles import compute_profiles

        user = get_user_model().objects.create_superuser("admin6", "f@example.com", "pw")
        self.client.force_login(user)
        person = Person.objects.create(name="王立")
        Membership.objects.create(person=person, source="ly", name="王立")
        # 舊版本分成財經、新版本分成勞動：看側寫裡是哪一個，就知道數字是哪個版本算的
        for n, (primary, classifier) in enumerate((("finance", "old#topic-v1"),
                                                    ("labor", "new#topic-v1")), start=1):
            article = Article.objects.create(
                ivod_id=f"e{n}", slug=f"2026-03-1{n}-e{n}", title="t", speaker="王立", source="ly",
                meeting="第11屆第5會期財政委員會第3次全體委員會議", date=date(2026, 3, 10 + n),
                ivod_url="https://ivod/x", status=ArticleStatus.READY,
                brief={"one_liner": "一句話", "key_numbers": [], "asks": []})
            Topic.objects.create(article=article, primary=primary, classifier=classifier,
                                 labeled_at=timezone.now())

        def evaluation(classifier, passed, day):
            return TopicEvaluation.objects.create(
                source="ly", classifier=classifier, labeled=20, correct=18 if passed else 10,
                accuracy=0.9 if passed else 0.5, passed=passed,
                ran_at=timezone.make_aware(datetime(2026, 10, day)))

        self.old = evaluation("old#topic-v1", True, 1)
        self.new = evaluation("new#topic-v1", True, 2)
        self.failed = evaluation("newest#topic-v1", False, 3)
        compute_profiles()

    def _counted(self):
        from articles.models import ProfileStat

        return {s.indicator.removeprefix("topic:"): s.value
                for s in ProfileStat.objects.filter(indicator__startswith="topic:") if s.value}

    def test_deleting_the_live_evaluation_falls_back_and_recomputes_at_once(self):
        self.assertEqual(self._counted(), {"labor": 1.0})
        res = self.client.post(f"/admin/articles/topicevaluation/{self.new.pk}/delete/",
                               {"post": "yes"}, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "已經重算人物側寫")
        self.assertEqual(self._counted(), {"finance": 1.0})

    def test_deleting_every_passing_evaluation_takes_the_topics_down(self):
        from articles.models import ProfileStat

        res = self.client.post("/admin/articles/topicevaluation/", {
            "action": "delete_selected", "_selected_action": [self.old.pk, self.new.pk],
            "post": "yes"}, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "已經重算人物側寫")
        self.assertFalse(ProfileStat.objects.filter(indicator__startswith="topic").exists())

    def test_deleting_an_evaluation_that_is_not_live_changes_nothing(self):
        from articles.models import ProfileStat

        before = list(ProfileStat.objects.order_by("id").values_list("id", flat=True))
        res = self.client.post(f"/admin/articles/topicevaluation/{self.failed.pk}/delete/",
                               {"post": "yes"}, follow=True)
        self.assertNotContains(res, "已經重算人物側寫")
        self.assertEqual(list(ProfileStat.objects.order_by("id").values_list("id", flat=True)), before)


# --- 追問率 ---

FOLLOWUP_TRANSCRIPT = "00:00 王立 部長，我再問一次「交機時程清冊到現在還沒給」，什麼時候給？"


def _followup_pair():
    """王立 3/2 要求交清冊（回應「部長允諾」），3/20 那篇後來又問了。"""
    source = _article("s1", ArticleStatus.READY, speaker="王立")
    Article.objects.filter(pk=source.pk).update(brief={
        "one_liner": "國防部無人機交機不到一半", "key_numbers": [],
        "asks": [{"request": "提出無人機交機時程清冊", "deadline": "一個月內", "response": "部長允諾"}]})
    candidate = _article("s2", ArticleStatus.READY, speaker="王立")
    Article.objects.filter(pk=candidate.pk).update(
        brief={"one_liner": "無人機交機時程清冊還沒交", "key_numbers": [], "asks": []},
        transcript_text=FOLLOWUP_TRANSCRIPT)
    source.refresh_from_db()
    candidate.refresh_from_db()
    return source, candidate


class FollowUpLabelAdminTests(TestCase):
    """追問標註頁：舊的要求、當時的回應、後來那篇的一句話與逐字稿片段、兩篇的連結；在清單上直接
    標有沒有追問；**看不到模型的判斷**。"""

    # 刻意取不會出現在頁面其他地方的字串，出現了就是洩漏
    SECRET_JUDGE = "secret-model-xyz#followup-v9#ffff"
    SECRET_QUOTE = "模型自己挑的那句引用"

    def setUp(self):
        from articles.models import FollowUp, FollowUpLabel

        user = get_user_model().objects.create_superuser("admin7", "g@example.com", "pw")
        self.client.force_login(user)
        self.source, self.candidate = _followup_pair()
        # 模型說有追問——人還沒標
        FollowUp.objects.create(article=self.source, ask_index=0, request="提出無人機交機時程清冊",
                                deadline_text="一個月內", due_date=date(2026, 9, 27),
                                followed_by=self.candidate, quote=self.SECRET_QUOTE,
                                classifier=self.SECRET_JUDGE, checked=[self.candidate.id])
        self.label = FollowUpLabel.objects.create(article=self.source, ask_index=0,
                                                  request="提出無人機交機時程清冊", candidate=self.candidate)

    def _changelist(self, query=""):
        return self.client.get(f"/admin/articles/followuplabel/{query}")

    def test_the_list_shows_both_sides_and_links_to_both_articles(self):
        res = self._changelist()
        self.assertEqual(res.status_code, 200)
        for text in ("提出無人機交機時程清冊", "（期限：一個月內）", "部長允諾", "無人機交機時程清冊還沒交",
                     "交機時程清冊到現在還沒給"):
            self.assertContains(res, text)
        for article in (self.source, self.candidate):
            self.assertContains(res, f"/admin/articles/article/{article.pk}/change/")
        self.assertContains(res, 'name="form-0-followed"')
        self.assertContains(res, "（還沒標）")
        self.assertContains(res, "沒有追問")

    def test_the_model_judgment_is_never_shown(self):
        pages = [self._changelist(), self._changelist("?labelled=no"),
                 self.client.get(f"/admin/articles/followuplabel/{self.label.pk}/change/"),
                 self.client.get(f"/admin/articles/article/{self.candidate.pk}/change/")]
        for res in pages:
            self.assertEqual(res.status_code, 200)
            body = res.content.decode()
            for leak in (self.SECRET_JUDGE, self.SECRET_QUOTE):
                self.assertNotIn(leak, body, res.request["PATH_INFO"])
        # 還沒標的那一列，下拉選單停在「還沒標」，不是模型的「有追問」
        self.assertContains(self._changelist("?labelled=no"),
                            '<option value="unknown" selected>（還沒標）</option>', html=True)

    def test_labelling_in_the_list_saves_the_answer_and_the_time(self):
        def post(value):
            return self.client.post("/admin/articles/followuplabel/", {
                "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
                "form-0-id": str(self.label.pk), "form-0-followed": value,
                "form-0-note": "追的是同一份清冊", "_save": "儲存"})

        self.assertEqual(post("false").status_code, 302)
        self.label.refresh_from_db()
        self.assertEqual((self.label.followed, self.label.note), (False, "追的是同一份清冊"))
        self.assertIsNotNone(self.label.labeled_at)
        self.assertEqual(self._changelist("?labelled=yes").context["cl"].result_list[0], self.label)
        # 改回還沒標：時間也清掉
        post("unknown")
        self.label.refresh_from_db()
        self.assertEqual((self.label.followed, self.label.labeled_at), (None, None))

    def test_labels_come_only_from_sampling(self):
        self.assertEqual(self.client.get("/admin/articles/followuplabel/add/").status_code, 403)


class FollowUpReadOnlyAdminTests(TestCase):
    def setUp(self):
        from articles.models import FollowUp, FollowUpEvaluation

        user = get_user_model().objects.create_superuser("admin8", "h@example.com", "pw")
        self.client.force_login(user)
        source, candidate = _followup_pair()
        self.followup = FollowUp.objects.create(
            article=source, ask_index=0, request="提出無人機交機時程清冊", deadline_text="一個月內",
            due_date=date(2026, 9, 27), followed_by=candidate, quote="q", classifier="m#followup-v1#x")
        self.evaluation = FollowUpEvaluation.objects.create(
            classifier="m#followup-v1#x", labeled=30, correct=27, accuracy=0.9, passed=True,
            ran_at=timezone.now())

    def test_followups_and_evaluations_are_listed_and_viewable(self):
        res = self.client.get("/admin/articles/followup/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "提出無人機交機時程清冊")
        res = self.client.get("/admin/articles/followupevaluation/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "90.0%")
        for url in (f"/admin/articles/followup/{self.followup.pk}/change/",
                    f"/admin/articles/followupevaluation/{self.evaluation.pk}/change/"):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200)
            self.assertNotContains(res, 'name="_save"')

    def test_nothing_can_be_added_by_hand(self):
        self.assertEqual(self.client.get("/admin/articles/followup/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/articles/followupevaluation/add/").status_code, 403)

    def test_deleting_the_live_evaluation_recomputes_at_once(self):
        from articles.models import Membership, Person, ProfileStat
        from articles.profiles import compute_profiles

        Membership.objects.create(person=Person.objects.create(name="王立"), source="ly", name="王立")
        Article.objects.update(meeting="第11屆第5會期財政委員會第3次全體委員會議")
        compute_profiles()
        self.assertTrue(ProfileStat.objects.filter(indicator="followup_rate").exists())
        res = self.client.post(f"/admin/articles/followupevaluation/{self.evaluation.pk}/delete/",
                               {"post": "yes"}, follow=True)
        self.assertContains(res, "已經重算人物側寫")
        self.assertFalse(ProfileStat.objects.filter(indicator="followup_rate").exists())


# --- 議案分類（提案與質詢一致率） ---


def _ly_bill(bill_no="B1", name="所得稅法部分條文修正草案", proposers=("王立",)):
    from articles.models import LyBill

    return LyBill.objects.create(
        bill_no=bill_no, term=11, session_number=5, name=name, status="交付審查",
        proposers=list(proposers), cosigners=[], url=f"https://ppg.ly.gov.tw/ppg/bills/{bill_no}/details",
        proposed_on=date(2026, 3, 2), synced_at=timezone.now())


class BillTopicLabelAdminTests(TestCase):
    """議案議題標註頁：議案名稱（分類器讀到的文字）、議事網連結、在清單上直接選主領域；
    **看不到模型的分類**。"""

    # 刻意取一個不會出現在頁面其他地方的分類器名稱，出現了就是洩漏
    SECRET = "secret-bill-model-xyz#topic-v9"

    def setUp(self):
        from articles.models import BillTopic, BillTopicLabel

        user = get_user_model().objects.create_superuser("admin9", "i@example.com", "pw")
        self.client.force_login(user)
        self.bill = _ly_bill()
        # 模型說是「勞動」——人還沒標
        BillTopic.objects.create(bill=self.bill, primary="labor", secondary="welfare",
                                 classifier=self.SECRET, labeled_at=timezone.now())
        self.label = BillTopicLabel.objects.create(bill=self.bill)
        BillTopicLabel.objects.create(bill=_ly_bill("B2", "國防法修正草案"), primary="defense")

    def _changelist(self, query=""):
        return self.client.get(f"/admin/articles/billtopiclabel/{query}")

    def test_the_list_shows_the_bill_name_and_links_to_the_official_page(self):
        res = self._changelist()
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "議案名稱：所得稅法部分條文修正草案")
        self.assertContains(res, "https://ppg.ly.gov.tw/ppg/bills/B1/details")
        self.assertContains(res, "第11屆第5會期")
        self.assertContains(res, 'name="form-0-primary"')
        self.assertContains(res, "（還沒標）")
        self.assertContains(res, "地方建設／其他")

    def test_the_model_prediction_is_never_shown(self):
        """盲標：模型說「勞動」，人還沒標——標註頁與議案頁都不能透露模型的答案。"""
        pages = [self._changelist(), self._changelist("?labelled=no"),
                 self.client.get(f"/admin/articles/billtopiclabel/{self.label.pk}/change/"),
                 self.client.get(f"/admin/articles/lybill/{self.bill.pk}/change/"),
                 self.client.get("/admin/articles/lybill/")]
        for res in pages:
            self.assertEqual(res.status_code, 200)
            # 下拉選單本來就列出全部 12 個領域，拿掉之後頁面上不能出現模型選的那兩個
            body = re.sub(r"<select.*?</select>", "", res.content.decode(), flags=re.S)
            for leak in (self.SECRET, "labor", "勞動", "welfare", "衛生福利"):
                self.assertNotIn(leak, body, res.request["PATH_INFO"])
        unlabelled = self._changelist("?labelled=no")
        self.assertContains(unlabelled, '<option value="" selected>（還沒標）</option>', html=True)
        self.assertNotContains(unlabelled, '<option value="labor" selected>勞動</option>', html=True)

    def test_the_unlabelled_filter(self):
        for query, expected in (("?labelled=no", ["B1"]), ("?labelled=yes", ["B2"])):
            rows = self._changelist(query).context["cl"].result_list
            self.assertEqual([label.bill.bill_no for label in rows], expected)

    def test_labelling_in_the_list_saves_the_primary_and_the_time(self):
        def post(value):
            return self.client.post("/admin/articles/billtopiclabel/?labelled=no", {
                "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
                "form-0-id": str(self.label.pk), "form-0-primary": value,
                "form-0-note": "稅", "_save": "儲存"})

        self.assertEqual(post("finance").status_code, 302)
        self.label.refresh_from_db()
        self.assertEqual((self.label.primary, self.label.note), ("finance", "稅"))
        self.assertIsNotNone(self.label.labeled_at)
        # 不在清單裡的代碼擋下來
        self.assertEqual(post("space").status_code, 200)
        self.label.refresh_from_db()
        self.assertEqual(self.label.primary, "finance")

    def test_labels_come_only_from_sampling(self):
        self.assertEqual(self.client.get("/admin/articles/billtopiclabel/add/").status_code, 403)


class BillTopicReadOnlyAdminTests(TestCase):
    """模型的議案分類與評估紀錄：看得到，但不能新增或改值；刪掉上線的評估會當場重算側寫。"""

    def setUp(self):
        from articles.models import BillTopic, BillTopicEvaluation, Membership, Person, TopicEvaluation

        user = get_user_model().objects.create_superuser("admin10", "j@example.com", "pw")
        self.client.force_login(user)
        Membership.objects.create(person=Person.objects.create(name="王立"), source="ly", name="王立")
        self.topic = BillTopic.objects.create(bill=_ly_bill(), primary="finance", classifier="m#topic-v1",
                                              labeled_at=timezone.now())
        self.evaluation = BillTopicEvaluation.objects.create(
            classifier="m#topic-v1", labeled=20, correct=17, accuracy=0.85, passed=True, mistakes=[],
            ran_at=timezone.now())
        TopicEvaluation.objects.create(source="ly", classifier="s#topic-v1", labeled=20, correct=18,
                                       accuracy=0.9, passed=True, ran_at=timezone.now())

    def test_topics_and_evaluations_are_listed_and_viewable(self):
        res = self.client.get("/admin/articles/billtopic/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "財政經濟")
        res = self.client.get("/admin/articles/billtopicevaluation/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "85.0%")
        for url in (f"/admin/articles/billtopic/{self.topic.pk}/change/",
                    f"/admin/articles/billtopicevaluation/{self.evaluation.pk}/change/"):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200)
            self.assertNotContains(res, 'name="_save"')

    def test_nothing_can_be_added_by_hand(self):
        self.assertEqual(self.client.get("/admin/articles/billtopic/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/articles/billtopicevaluation/add/").status_code, 403)

    def test_deleting_the_live_evaluation_recomputes_at_once(self):
        from articles.models import ProfileStat, Session
        from articles.profiles import compute_profiles

        Session.objects.create(source="ly", name="第11屆第5會期", term="11")
        compute_profiles()
        self.assertTrue(ProfileStat.objects.filter(indicator="proposal_alignment").exists())
        res = self.client.post(f"/admin/articles/billtopicevaluation/{self.evaluation.pk}/delete/",
                               {"post": "yes"}, follow=True)
        self.assertContains(res, "已經重算人物側寫")
        self.assertFalse(ProfileStat.objects.filter(indicator="proposal_alignment").exists())
