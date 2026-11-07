"""YouTube 影片不是議會：議題分類、追問、會期、側寫、每晚處理與預設的 API 範圍都不碰它。

做法是白名單（models.COUNCIL_SOURCES），不是「排除 youtube」：這裡的測試就是那份白名單的回歸，
下次再加一個非議會的來源時，這些地方也要擋得住。影片只出現在 `?source=youtube`，
也只由 add_video 處理（見 test_add_video.py）。
"""
from __future__ import annotations

import io
import itertools
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from articles.followups import CandidateIndex, is_base, sync_followups
from articles.ingest import imageless, process_pending, rerun_ready
from articles.management.commands.run_scheduler import sources_missing_members
from articles.members_sync import link_article
from articles.models import (COUNCIL_CHOICES, COUNCIL_SOURCES, Article, ArticleSource, ArticleStatus, FollowUp,
                             Membership, Person, ProfileStat, Session, Topic, TopicEvaluation, TopicLabel)
from articles.profiles import assign_sessions, attach_session, compute_profiles, parse_session, topic_stats_stale
from articles.topics import base_articles, classify_topics, sample_labels

COUNCIL_MEETING = "第4屆第8次定期會 市政總質詢"
_ids = itertools.count(1)


def _brief(asks=()):
    return {"one_liner": "一句話", "key_numbers": [],
            "asks": [{"request": request, "deadline": deadline, "response": ""}
                     for request, deadline in asks]}


def _video(speaker="某頻道", day="2026-10-30", status=ArticleStatus.READY, brief=True, asks=(),
           meeting="", published=None, title="影片標題"):
    """一支 READY 的 YouTube 影片：單一講者（頻道）、有摘要卡，是最容易被當成基礎文章的樣子。"""
    n = next(_ids)
    video_id = f"AAAAAAAA{n:03d}"
    return Article.objects.create(
        ivod_id=f"yt-{video_id}", slug=f"{day}-yt-{video_id}", source=ArticleSource.YOUTUBE,
        title=title, speaker=speaker, meeting=meeting, date=date.fromisoformat(day),
        duration_seconds=600, ivod_url=f"https://www.youtube.com/watch?v={video_id}",
        status=status, brief=_brief(asks) if brief else None, transcript_text="逐字稿",
        published_at=published or (timezone.now() if status == ArticleStatus.READY else None))


def _council(speaker="甲", source="ly", day="2026-10-01", status=ArticleStatus.READY, brief=True,
             meeting=COUNCIL_MEETING, asks=()):
    n = next(_ids)
    return Article.objects.create(
        ivod_id=f"{source}-s{n}", slug=f"{day}-{source}-s{n}", source=source, title=f"t{n}",
        speaker=speaker, meeting=meeting, date=date.fromisoformat(day), duration_seconds=600,
        ivod_url="https://example.invalid/x", status=status,
        brief=_brief(asks) if brief else None, transcript_text="逐字稿")


class TopicGpu:
    """只實作 topic 工作用得到的兩個方法，記下送來的文字。"""

    def __init__(self):
        self.texts: list[str] = []

    def submit_topic(self, text, labels):
        self.texts.append(text)
        return f"job-{len(self.texts)}"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        return {"primary": "local", "secondary": None, "classifier": "fake#topic-v1"}


class DeckGpu:
    """摘要工作的假客戶端：記下送來的網址，永遠成功。"""

    def __init__(self):
        self.submitted: list[str] = []

    def job(self, job_id):
        return None

    def cancel(self, job_id):
        pass

    def submit(self, url, detailed=True, min_slides=None, max_slides=None, speech_hint=None):
        self.submitted.append(url)
        return f"job-{len(self.submitted)}"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        return {"title": "GPU 的標題", "source_note": "", "transcript_text": "逐字稿", "brief": None,
                "slides": []}


class SourceEnumTests(TestCase):
    def test_the_council_allow_list_is_exactly_the_three_councils(self):
        self.assertEqual([s.value for s in COUNCIL_SOURCES], ["ly", "tccc", "ntpc"])
        self.assertEqual(COUNCIL_CHOICES, [("ly", "立法院"), ("tccc", "臺中市議會"), ("ntpc", "新北市議會")])
        self.assertNotIn(ArticleSource.YOUTUBE, COUNCIL_SOURCES)
        self.assertEqual(ArticleSource.YOUTUBE.label, "YouTube")

    def test_only_articles_can_be_a_video_the_council_tables_cannot(self):
        """Membership／Session／TopicEvaluation 的 choices 只列議會：admin 不會出現「YouTube」的任期。"""
        def values(model):
            return [value for value, _ in model._meta.get_field("source").choices]

        self.assertIn("youtube", values(Article))
        for model in (Membership, Session, TopicEvaluation):
            with self.subTest(model=model.__name__):
                self.assertEqual(values(model), ["ly", "tccc", "ntpc"])

    def test_the_new_speakers_field_starts_empty(self):
        self.assertEqual(_video().speakers, [])
        self.assertEqual(_council().speakers, [])


class TopicScopeTests(TestCase):
    def test_a_finished_video_with_a_brief_is_not_a_base_article(self):
        council = _council("甲")
        _video()
        self.assertEqual(list(base_articles()), [council])

    def test_classification_never_sends_a_video_to_the_gpu(self):
        """攔的 bug：base_articles 沒有來源條件時，每支影片都是一個 GPU topic 工作，
        還占掉每晚 200 篇的上限。"""
        _council("甲", source="tccc")
        _video()
        gpu = TopicGpu()
        report = classify_topics(gpu, limit=10)
        self.assertEqual(len(gpu.texts), 1)
        self.assertEqual(report.classified, 1)
        self.assertEqual(report.remaining, 0)
        self.assertFalse(Topic.objects.filter(article__source="youtube").exists())

    def test_the_labelling_set_never_draws_a_video_nor_prints_a_line_for_it(self):
        for _ in range(3):
            _video()
        _council("甲")
        report = sample_labels()
        self.assertEqual(report.added["ly"], 1)
        self.assertFalse(TopicLabel.objects.filter(article__source="youtube").exists())
        # 迴圈本身就不走影片這個來源（不只是剛好抽不到）
        self.assertEqual(set(report.added) | set(report.total), {"ly", "tccc", "ntpc"})
        self.assertNotIn("YouTube", str(report))
        self.assertEqual(len(str(report).splitlines()), 3)


class FollowupScopeTests(TestCase):
    ASK = (("提出交機時程清冊", "一個月內"),)

    def test_a_video_is_not_a_base_article_for_follow_ups_either(self):
        self.assertTrue(is_base(_council()))
        self.assertFalse(is_base(_video()))

    def test_asks_in_a_video_do_not_become_follow_ups(self):
        _council("甲", asks=self.ASK)
        _video(asks=self.ASK)
        sync_followups()
        self.assertEqual(list(FollowUp.objects.values_list("article__source", flat=True)), ["ly"])

    def test_follow_ups_that_already_exist_for_a_video_are_swept_away(self):
        video = _video(asks=self.ASK)
        FollowUp.objects.create(article=video, ask_index=0, request="提出交機時程清冊",
                                deadline_text="一個月內", due_date=date(2026, 11, 29))
        self.assertEqual(sync_followups().removed, 1)
        self.assertFalse(FollowUp.objects.exists())

    def test_videos_are_never_candidates_for_a_later_follow_up(self):
        _council("甲")
        _video()
        sources = {doc.source for docs in CandidateIndex.load()._by_speaker.values() for doc in docs}
        self.assertEqual(sources, {"ly"})


class SessionScopeTests(TestCase):
    def test_a_video_has_no_council_session_even_if_its_meeting_looks_like_one(self):
        self.assertIsNone(parse_session("youtube", "第4屆第8次定期會"))
        self.assertIsNone(parse_session("youtube", "第11屆第5會期財政委員會"))
        # 議會照舊
        self.assertEqual(parse_session("tccc", "第4屆第8次定期會 市政總質詢").name, "第4屆第8次定期會")
        self.assertEqual(parse_session("ly", "第11屆第5會期財政委員會").name, "第11屆第5會期")

    def test_attaching_and_assigning_sessions_skips_videos(self):
        video = _video(meeting="第4屆第8次定期會 市政總質詢 直播")
        self.assertIsNone(attach_session(video))
        self.assertEqual(assign_sessions(), 0)
        self.assertFalse(Session.objects.exists())

    def test_the_profile_report_does_not_mention_videos(self):
        """每晚的 compute_profiles 輸出：影片不是「會議名稱裡沒有會期」的資料問題，議題分布也沒有它的一行。"""
        _video()
        report = compute_profiles()
        self.assertEqual(report.unsessioned, {})
        self.assertNotIn("YouTube", str(report))

    def test_council_articles_without_a_session_are_still_reported(self):
        _council("甲", meeting="")
        report = compute_profiles()
        self.assertEqual(report.unsessioned, {"ly": 1})

    def test_the_topic_staleness_check_ignores_videos(self):
        _video()
        self.assertFalse(topic_stats_stale())


@override_settings(CITE_LAWS=False)
class NightlyScopeTests(TestCase):
    """影片只由 add_video 處理：一支兩小時的影片不該占住夜間批次，也不該在沒人看的時候自己開一個長工作。"""

    def test_a_pending_video_is_not_processed_even_when_it_sorts_first(self):
        council = _council("甲", day="2026-10-01", status=ArticleStatus.PENDING)
        video = _video(day="2026-10-30", status=ArticleStatus.PENDING)
        gpu = DeckGpu()
        report = process_pending(gpu, limit=1, timeout=60)
        self.assertEqual(gpu.submitted, [council.ivod_url])
        self.assertEqual(report.processed, 1)
        video.refresh_from_db()
        self.assertEqual((video.status, video.attempts), (ArticleStatus.PENDING, 0))

    def test_the_backlog_count_does_not_include_videos(self):
        """影片不在夜間的佇列裡：報告寫「仍待處理 1」會讓人以為夜間會處理它。"""
        _video(status=ArticleStatus.PENDING)
        _video(status=ArticleStatus.FAILED)
        report = process_pending(DeckGpu(), limit=10, timeout=60)
        self.assertEqual((report.processed, report.pending), (0, 0))
        self.assertEqual(str(report), "發現 0、新增 0、完成 0、失敗 0、略過 0")

    def test_failed_videos_are_not_retried_at_night(self):
        video = _video(status=ArticleStatus.FAILED)
        gpu = DeckGpu()
        process_pending(gpu, limit=10, timeout=60)
        self.assertEqual(gpu.submitted, [])
        video.refresh_from_db()
        self.assertEqual((video.status, video.attempts), (ArticleStatus.FAILED, 0))

    def test_videos_without_screenshots_are_not_swept_up_by_the_imageless_retry(self):
        _video()
        self.assertEqual(list(imageless(10)), [])

    def test_a_ready_video_is_not_regenerated_by_rerun_ready(self):
        _video()
        gpu = DeckGpu()
        self.assertEqual(rerun_ready(gpu, limit=10, timeout=60).processed, 0)
        self.assertEqual(gpu.submitted, [])

    def test_ingest_ivod_process_only_leaves_videos_alone(self):
        _video(status=ArticleStatus.PENDING)
        council = _council("甲", status=ArticleStatus.PENDING)
        gpu = DeckGpu()
        with mock.patch("articles.management.commands.ingest_ivod.GpuApiClient", return_value=gpu):
            call_command("ingest_ivod", "--process-only", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(gpu.submitted, [council.ivod_url])

    def test_the_nightly_commands_do_not_accept_youtube_as_a_source(self):
        """用命令列的寫法：call_command 的關鍵字參數不會過 argparse 的 choices 檢查，
        會直接跑下去（然後去查真的議會網站）。"""
        for command in ("ingest_ivod", "sync_members"):
            with self.subTest(command=command), self.assertRaises(CommandError) as caught:
                call_command(command, "--source", "youtube", stdout=io.StringIO(), stderr=io.StringIO())
            self.assertIn("youtube", str(caught.exception))

    @override_settings(TCCC_ENABLED=True, NTPC_ENABLED=True)
    def test_the_scheduler_never_waits_for_a_roster_of_videos(self):
        """YouTube 沒有名冊：若被當成「缺名單的來源」，每次啟動排程都會白跑一次 sync_members。"""
        _video()
        self.assertEqual(sources_missing_members(), ["ly", "tccc", "ntpc"])


class MembershipScopeTests(TestCase):
    def test_a_channel_named_like_a_council_member_does_not_borrow_the_party(self):
        """攔的 bug：任期是依來源找的；頻道「王立」不會因為立法院、臺中都有一位王立而掛上政黨。"""
        for source in ("ly", "tccc"):
            Membership.objects.create(person=Person.objects.create(name="王立"), source=source,
                                      name="王立", party="某黨")
        video = _video(speaker="王立")
        link_article(video)
        video.refresh_from_db()
        self.assertEqual((video.party, video.membership), ("", None))


class ApiScopeTests(TestCase):
    def test_the_default_feed_has_only_council_articles(self):
        council = _council("甲")
        _video()
        res = self.client.get("/api/articles").json()
        self.assertEqual([i["slug"] for i in res["items"]], [council.slug])
        self.assertEqual(res["count"], 1)

    def test_the_video_source_returns_the_videos_and_only_them(self):
        _council("甲")
        video = _video()
        res = self.client.get("/api/articles?source=youtube").json()
        self.assertEqual([i["slug"] for i in res["items"]], [video.slug])
        item = res["items"][0]
        self.assertEqual((item["source"], item["party"], item["meeting"]), ("youtube", "", ""))
        self.assertEqual(item["ivod_url"], video.ivod_url)

    def test_the_other_filters_do_not_let_a_video_into_the_default_feed(self):
        council = _council("甲", day="2026-10-30")
        _video(speaker="某頻道", day="2026-10-30", title="一句話")
        for query in ("date=2026-10-30", "q=一句話", "q=影片標題", "speaker=某頻道", "has_brief=1", "solo=1"):
            with self.subTest(query=query):
                slugs = [i["slug"] for i in self.client.get(f"/api/articles?{query}").json()["items"]]
                self.assertNotIn("yt-", "".join(slugs))
        self.assertEqual([i["slug"] for i in self.client.get("/api/articles?date=2026-10-30").json()["items"]],
                         [council.slug])

    def test_videos_are_found_by_their_own_filters_under_the_video_source(self):
        video = _video(speaker="某頻道", title="獨一無二的標題")
        for query in ("speaker=某頻道", "q=獨一無二", "date=2026-10-30"):
            with self.subTest(query=query):
                res = self.client.get(f"/api/articles?source=youtube&{query}").json()
                self.assertEqual([i["slug"] for i in res["items"]], [video.slug])

    def test_videos_are_listed_by_when_they_were_added_not_by_their_upload_date(self):
        """新加的影片排最前面，不被它的上傳日期埋到最後幾頁；同時間的新的先。"""
        now = timezone.now()
        old_upload_added_last = _video(day="2020-01-01", published=now)
        new_upload_added_first = _video(day="2026-10-30", published=now - timedelta(hours=2))
        tie_a = _video(day="2026-10-29", published=now - timedelta(hours=1))
        tie_b = _video(day="2026-10-29", published=now - timedelta(hours=1))
        slugs = [i["slug"] for i in self.client.get("/api/articles?source=youtube").json()["items"]]
        self.assertEqual(slugs, [old_upload_added_last.slug, tie_b.slug, tie_a.slug,
                                 new_upload_added_first.slug])

    def test_the_search_keeps_the_video_ordering(self):
        now = timezone.now()
        first = _video(day="2020-01-01", published=now, title="共同的字")
        second = _video(day="2026-10-30", published=now - timedelta(hours=1), title="共同的字")
        slugs = [i["slug"] for i in self.client.get("/api/articles?source=youtube&q=共同").json()["items"]]
        self.assertEqual(slugs, [first.slug, second.slug])

    def test_council_sources_keep_the_date_ordering(self):
        """只有影片改排序：議會的清單還是最新的發言日在前。"""
        now = timezone.now()
        newer_day = _council("甲", day="2026-10-02")
        older_day = _council("乙", day="2026-10-01")
        Article.objects.filter(pk=older_day.pk).update(published_at=now)
        Article.objects.filter(pk=newer_day.pk).update(published_at=now - timedelta(days=1))
        slugs = [i["slug"] for i in self.client.get("/api/articles?source=ly").json()["items"]]
        self.assertEqual(slugs, [newer_day.slug, older_day.slug])

    def test_a_video_opens_by_its_slug_and_carries_an_empty_speakers_list(self):
        video = _video()
        res = self.client.get(f"/api/articles/{video.slug}")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual((body["source"], body["ivod_url"]), ("youtube", video.ivod_url))
        self.assertEqual(body["speakers"], [])

    def test_a_council_article_detail_has_an_empty_speakers_list_too(self):
        self.assertEqual(self.client.get(f"/api/articles/{_council().slug}").json()["speakers"], [])

    def test_the_stored_speakers_come_back_as_they_are(self):
        video = _video()
        Article.objects.filter(pk=video.pk).update(speakers=[{"id": "S1", "name": "甲"}])
        self.assertEqual(self.client.get(f"/api/articles/{video.slug}").json()["speakers"],
                         [{"id": "S1", "name": "甲"}])

    def test_the_speaker_list_is_council_only_unless_asked(self):
        _council("甲")
        _video(speaker="某頻道")
        names = {(i["name"], i["source"]) for i in self.client.get("/api/speakers").json()["items"]}
        self.assertEqual(names, {("甲", "ly")})
        items = self.client.get("/api/speakers?source=youtube").json()["items"]
        self.assertEqual([(i["name"], i["source"], i["person_id"]) for i in items],
                         [("某頻道", "youtube", None)])

    def test_the_party_list_has_no_video_and_the_video_source_is_not_an_error(self):
        _council("甲")
        Article.objects.filter(source="ly").update(party="某黨")
        _video()
        self.assertEqual([p["name"] for p in self.client.get("/api/parties").json()["items"]], ["某黨"])
        res = self.client.get("/api/parties?source=youtube")
        self.assertEqual((res.status_code, res.json()), (200, {"items": []}))

    def test_the_source_parameter_cannot_drift_from_the_enum(self):
        """SourceParam 是從列舉算出來的，不是另一份手抄的清單：新增來源時 API 自動認得。"""
        from typing import get_args

        from articles import api

        self.assertEqual(get_args(api.SourceParam), tuple(ArticleSource.values))
        self.assertIn("youtube", get_args(api.SourceParam))

    def test_a_bad_source_is_still_a_422_and_every_real_one_is_accepted(self):
        for path in ("articles", "speakers", "parties"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(f"/api/{path}?source=nope").status_code, 422)
                for source in ("ly", "tccc", "ntpc", "youtube"):
                    self.assertEqual(self.client.get(f"/api/{path}?source={source}").status_code, 200)

    def test_a_video_has_no_profile(self):
        person = Person.objects.create(name="甲")
        session = Session.objects.create(source="ly", term="11", name="第11屆第5會期")
        ProfileStat.objects.create(person=person, session=session, indicator="speeches", value=1,
                                   n=1, computed_at=timezone.now())
        self.assertEqual(self.client.get(f"/api/people/{person.id}/profile?source=youtube").status_code, 404)
        self.assertEqual(self.client.get(f"/api/people/{person.id}/profile").status_code, 200)

    def test_health_counts_everything_published_but_the_latest_date_follows_the_councils_only(self):
        """latest_date 是監控夜間匯入有沒有停擺的訊號（GPU 睡著、結構改了、重試次數用完）。手動加的影片——
        包括抓不到日期時用的「今天」、--date 打錯的未來日期——不能讓一個停擺的匯入看起來是新的。"""
        _council("甲", day="2026-10-01")
        _video(day="2026-10-30")
        _video(day="2099-01-01")
        body = self.client.get("/api/health").json()
        self.assertEqual((body["articles"], body["latest_date"]), (3, "2026-10-01"))

    def test_health_with_only_videos_has_no_latest_date(self):
        _video(day="2026-10-30")
        body = self.client.get("/api/health").json()
        self.assertEqual((body["articles"], body["latest_date"]), (1, None))


class AdminScopeTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("admin-yt", "yt@example.com", "pw")
        self.client.force_login(user)

    def test_the_article_admin_lists_and_filters_videos(self):
        _video(speaker="某頻道")
        _council("議員乙", source="tccc")
        res = self.client.get("/admin/articles/article/?source__exact=youtube")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([a.speaker for a in res.context["cl"].result_list], ["某頻道"])
        self.assertContains(res, "YouTube")

    def test_only_the_article_form_offers_youtube_as_a_source(self):
        """任期、會期、評估的來源只列議會：管理頁不會出現「YouTube」的任期。
        （會期與評估在 admin 不能新增，所以看它們的清單頁與篩選器。）"""
        self.assertContains(self.client.get("/admin/articles/article/add/"), 'value="youtube"')
        self.assertNotContains(self.client.get("/admin/articles/membership/add/"), 'value="youtube"')
        for page in ("membership", "session", "topicevaluation"):
            with self.subTest(page=page):
                res = self.client.get(f"/admin/articles/{page}/")
                self.assertEqual(res.status_code, 200)
                self.assertContains(res, "新北市議會")
                self.assertNotContains(res, "YouTube")


    def test_requeueing_a_video_tells_the_admin_that_only_add_video_will_process_it(self):
        """夜間排程不處理影片：打回待處理之後沒有人會去做它，除非重新執行 add_video。"""
        failed = _video(status=ArticleStatus.FAILED)
        ready = _video()
        res = self.client.post("/admin/articles/article/", {
            "action": "requeue", "_selected_action": [failed.pk, ready.pk]}, follow=True)
        self.assertContains(res, "已打回待處理 1 篇")
        self.assertContains(res, "其中 1 篇是 YouTube 影片")
        self.assertContains(res, "add_video")
        self.assertContains(res, "add_video --rerun")
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts), (ArticleStatus.PENDING, 0))

    def test_requeueing_council_articles_does_not_mention_videos(self):
        failed = _council(status=ArticleStatus.FAILED)
        res = self.client.post("/admin/articles/article/", {
            "action": "requeue", "_selected_action": [failed.pk]}, follow=True)
        self.assertEqual([str(m) for m in res.context["messages"]], ["已打回待處理 1 篇。"])


class MigrationTests(TestCase):
    def test_the_new_choices_and_field_are_in_a_migration(self):
        """choices 或欄位變了而沒有遷移：admin 的表單驗證與資料庫會不一致，部署時才發現。"""
        call_command("makemigrations", "--check", "--dry-run", stdout=io.StringIO())
