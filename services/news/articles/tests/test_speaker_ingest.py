"""多人影片的 save_result 與送出：契約範例存得進去、不合契約的一個字都不寫、失敗就是失敗（不退回單人）、
重跑整批換掉、單人與議會一個字都不變。

假的 GPU 客戶端，不碰網路、不碰 GPU。結果的範例是 GPU 與 Pi 共用的契約檔。
check_speaker_result 的每一條規則本身在 test_speaker_result.py；這裡測的是它接在 ingest 裡之後的後果：
資料庫、檔案、文章的狀態、API 看到什麼。
"""
from __future__ import annotations

import base64
import copy
import io
import re
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from articles.gpu_client import JobFailed
from articles.ingest import process_articles, register_video, save_result, wants_diarization
from articles.models import Article, ArticleSource, ArticleStatus, Slide
from articles.speakers import render_placeholders
from articles.tests import test_youtube_ingest as yt
from articles.tests.speaker_support import CONTRACT, CONTRACT_VIDEO, diarized_result, rebuild, video_keys

VIDEO = CONTRACT_VIDEO
IMAGE = b"\x00\x01fake-webp\xff"
WRITES = re.compile(r"\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
EXISTS = 'SELECT 1 AS "a" FROM "articles_article" WHERE "articles_article"."id" = '


def register(speaker_count=None, video_id=VIDEO, day=date(2026, 10, 30)):
    article, _ = register_video(video_id, title="少子化對談", speaker="示範頻道", day=day, duration_seconds=900,
                                speaker_count=speaker_count)
    return article


def with_images(payload):
    """每一段都附一張截圖：被拒絕的結果不能留下任何一個檔案。"""
    for slide in payload["slides"]:
        slide["image_base64"] = base64.b64encode(IMAGE).decode()
        slide["image_media_type"] = "image/webp"
    return payload


def files_on_disk():
    return sorted(p.relative_to(settings.MEDIA_ROOT).as_posix() for p in Path(settings.MEDIA_ROOT).rglob("*")
                  if p.is_file())


def everything_stored():
    """跟影片有關的全部：文章那一列的每個欄位、每一段的每個欄位。"""
    return list(Article.objects.order_by("pk").values()), list(Slide.objects.order_by("pk").values())


def content_of(article_id):
    """一篇文章的內容（不含只在失敗時才會變的 error、updated_at）：說話者、摘要、逐字稿、每一段的一切。"""
    a = Article.objects.get(pk=article_id)
    return (a.status, a.published_at, a.speakers, a.diarization, a.brief, a.transcript_text, a.title,
            [(s.pk, s.index, s.title, s.bullets, s.detail, s.points, s.image.name) for s in a.slides.order_by("index")])


def without(key):
    def mutate(payload):
        payload.pop(key)
    return mutate


def set_(path, value):
    """path 是 payload 裡的一串鍵（數字是清單位置）；改成 value。"""
    def mutate(payload):
        node = payload
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value
    return mutate


# 每一個都是「GPU 回來的東西不合契約」：存檔必須整個拒絕。規則本身的完整反例在 test_speaker_result.py，
# 這裡挑會讓「哪裡先寫了一半」現形的幾種：壞在第一頁、壞在最後一頁、壞在整份。
BROKEN = {
    "舊版 GPU（說話者是一串字串、沒有 diarization）": lambda p: (p.update(speakers=["講者"]), p.pop("diarization")),
    "回的是單人結果": set_(["speaker_mode"], "single"),
    "沒有 speaker_mode": without("speaker_mode"),
    "沒有 diarization": without("diarization"),
    "diarization 狀態是 failed": set_(["diarization", "status"], "failed"),
    "沒找到任何人": set_(["diarization", "found_speakers"], 0),
    "自動人數的請求，回聲卻說要 3 位": set_(["diarization", "requested_speakers"], 3),
    "沒有任何主要發言者": lambda p: [s.update(kind="minor") for s in p["speakers"]],
    "第一頁沒有重點": set_(["slides", 0, "points"], []),
    "最後一頁沒有重點": set_(["slides", -1, "points"], []),
    "重點算在其他聲音名下": set_(["slides", 0, "points", 0, "speaker"], "S3"),
    "最後一頁的重點算在沒列出的人名下": set_(["slides", -1, "points", 0, "speaker"], "S9"),
    "重點裡有 [[": set_(["slides", 0, "points", 0, "text"], "[[S1]]說了什麼"),
    "重點的時間不是數字": set_(["slides", 0, "points", 0, "start"], float("nan")),
    "另一支影片的結果": set_(["video_id"], "zzzzzzzzzzz"),
    "條列說是別人講的": set_(["slides", 0, "bullets", 0], "[[S2]]：詢問目前補助政策的成效"),
}


class IngestCase(yt.IsolatedMedia):
    """每個測試自己的截圖資料夾（見 IsolatedMedia）。"""


class PublishedResultTests(IngestCase):
    def setUp(self):
        super().setUp()
        self.article = register()
        save_result(self.article, with_images(diarized_result()))
        self.article = Article.objects.get(pk=self.article.pk)

    def test_the_contract_example_is_published(self):
        a = self.article
        self.assertEqual((a.status, a.error, a.gpu_job_id), (ArticleStatus.READY, "", ""))
        self.assertIsNotNone(a.published_at)
        self.assertIsNone(a.speaker_count)
        self.assertEqual(a.title, "少子化對談")      # 登記時的標題比 GPU 回的好

    def test_the_summary_card_is_never_stored_for_a_multi_speaker_video(self):
        payload = with_images(diarized_result())
        payload["brief"] = {"one_liner": "GPU 偷偷產的摘要卡", "key_numbers": [], "asks": ["要求"]}
        again = register(video_id="x0000000001")
        save_result(again, payload | video_keys("x0000000001"))
        self.assertIsNone(Article.objects.get(pk=again.pk).brief)
        self.assertIsNone(self.article.brief)

    def test_the_roster_keeps_what_the_gpu_said_and_starts_without_any_name(self):
        gpu = CONTRACT["result"]["speakers"]
        self.assertEqual([s["id"] for s in self.article.speakers], [s["id"] for s in gpu])
        for stored, sent in zip(self.article.speakers, gpu, strict=True):
            self.assertEqual({k: stored[k] for k in sent}, sent)
            self.assertEqual((stored["name"], stored["role"], stored["basis"], stored["merged_into"]),
                             ("", "", "none", ""))

    def test_the_diarization_summary_and_the_points_are_stored(self):
        self.assertEqual(self.article.diarization, CONTRACT["result"]["diarization"])
        stored = [s.points for s in self.article.slides.order_by("index")]
        self.assertEqual(stored, [s["points"] for s in CONTRACT["result"]["slides"]])

    def test_the_markers_stay_in_the_database_and_become_names_only_when_read(self):
        self.assertIn("[[S1]]", self.article.transcript_text)
        slide = self.article.slides.get(index=1)
        self.assertTrue(all(b.startswith("[[S") for b in slide.bullets))
        self.assertIn("[[S2]]", slide.detail)
        self.assertEqual(render_placeholders(slide.bullets[0], self.article.speakers),
                         "發言者 A：詢問目前補助政策的成效")

    def test_the_slides_are_numbered_by_position_and_their_images_are_written(self):
        slides = list(self.article.slides.order_by("index"))
        self.assertEqual([s.index for s in slides], [1, 2])
        self.assertEqual(len(files_on_disk()), 2)
        self.assertTrue(all(s.image for s in slides))

    def test_dirty_text_is_cleaned_but_the_markers_survive(self):
        dirty = diarized_result()
        dirty["transcript_text"] = dirty["transcript_text"].replace("今天我們", "今天\u202e我\x07們", 1)
        dirty["slides"][0]["bullets"][0] = "[[S1]]：詢問\u202e目前\x07補助政策的成效"
        dirty["slides"][0]["detail"] = dirty["slides"][0]["detail"].replace("詢問", "詢\u200b問", 1)
        dirty["slides"][0]["points"][0]["text"] = "詢問\u202e目前補助\x07政策的成效"
        dirty["slides"][0]["points"][0]["evidence"] = "先請教\u202e您怎麼看目前的補助政策"
        article = register(video_id="x0000000002")
        save_result(article, dirty | video_keys("x0000000002"))
        stored = Article.objects.get(pk=article.pk)
        slide = stored.slides.get(index=1)
        for text in (stored.transcript_text, *slide.bullets, slide.detail,
                     *(p["text"] + p["evidence"] for p in slide.points)):
            self.assertNotRegex(text, r"[\u202e\u200b\x07]")
        self.assertEqual(slide.bullets[0], "[[S1]]：詢問目前補助政策的成效")
        self.assertEqual(slide.points[0]["text"], "詢問目前補助政策的成效")
        self.assertIn("[[S1]]", stored.transcript_text)

    def test_a_rejected_rerun_leaves_the_published_video_exactly_as_it_was(self):
        before, files = everything_stored(), files_on_disk()
        for name, mutate in BROKEN.items():
            with self.subTest(name):
                bad = with_images(diarized_result())
                mutate(bad)
                with self.assertRaises(JobFailed):
                    save_result(Article.objects.get(pk=self.article.pk), bad)
                self.assertEqual(everything_stored(), before)
                self.assertEqual(files_on_disk(), files)


class RejectedResultTests(IngestCase):
    def test_nothing_is_written_for_any_broken_result(self):
        """資料庫沒變、沒有任何檔案、也沒有發出任何一個寫入的查詢（交易回滾不算數：根本不該動）。"""
        for number, (name, mutate) in enumerate(BROKEN.items()):
            video_id = f"x{number:010d}"
            article = register(video_id=video_id)
            before = everything_stored()
            payload = with_images(diarized_result(video_id))
            mutate(payload)
            with self.subTest(name):
                with CaptureQueriesContext(connection) as queries, self.assertRaises(JobFailed):
                    save_result(article, payload)
                self.assertEqual([q["sql"] for q in queries if WRITES.match(q["sql"])], [])
                self.assertEqual(everything_stored(), before)
                self.assertEqual(files_on_disk(), [])

    def test_a_result_from_an_old_gpu_says_the_gpu_probably_was_not_updated(self):
        article = register()
        old = with_images(yt._payload()) | {"video_id": VIDEO, "speakers": ["講者"]}
        with self.assertRaisesRegex(JobFailed, "還沒更新"):
            save_result(article, old)

    def test_a_single_voice_result_is_not_accepted_for_a_multi_speaker_request(self):
        article = register()
        with self.assertRaises(JobFailed):
            save_result(article, with_images(yt._payload()) | {"video_id": VIDEO})
        self.assertEqual(files_on_disk(), [])

    def test_a_fixed_count_has_to_be_echoed_back(self):
        article = register(speaker_count=3)
        with self.assertRaises(JobFailed):
            save_result(article, diarized_result(requested=None))      # 要了 3 位，GPU 說自動
        with self.assertRaises(JobFailed):
            save_result(article, diarized_result(requested=2))         # 要了 3 位，GPU 說 2 位
        save_result(article, diarized_result(requested=3))
        self.assertEqual(Article.objects.get(pk=article.pk).status, ArticleStatus.READY)

    def test_the_check_adds_no_database_read_to_the_save(self):
        """多人影片的 save_result 跟單人一樣，只多「這一列還在不在」的一次查詢（見 test_youtube_ingest）。"""
        article = register()
        with CaptureQueriesContext(connection) as queries:
            save_result(article, diarized_result())
        self.assertEqual(len([q for q in queries if q["sql"].startswith(EXISTS)]), 1)


class FailClosedTests(IngestCase):
    """好的結果拿不到，就是失敗：文章不發佈、不退回成單人的聲音、下次重試仍然要求多人區分。"""

    def bad(self):
        payload = diarized_result()
        payload["slides"][0]["points"][0]["speaker"] = "S3"
        return payload

    def test_a_bad_answer_fails_the_video_and_nothing_is_published(self):
        article = register()
        client = yt.RecordingClient(result=self.bad())
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(article, client, timeout=60)
        article.refresh_from_db()
        self.assertEqual((report.failed, report.processed), (1, 0))
        self.assertEqual((article.status, article.gpu_job_id, article.attempts), (ArticleStatus.FAILED, "", 1))
        self.assertIn("JobFailed", article.error)
        self.assertIsNone(article.published_at)
        self.assertEqual((article.speakers, article.diarization, article.transcript_text), ([], None, ""))
        self.assertEqual(Slide.objects.count(), 0)
        self.assertEqual(self.client.get("/api/articles?source=youtube").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/articles/{article.slug}").status_code, 404)

    def test_it_never_falls_back_to_a_single_voice_request(self):
        article = register()
        client = yt.RecordingClient(result=self.bad())
        with self.assertLogs("articles.ingest", level="WARNING"):
            process_articles(article, client, timeout=60)
        self.assertEqual([(s["speaker_mode"], s["speaker_count"]) for s in client.submits], [("diarize", None)])

    def test_the_retry_asks_for_multi_speaker_again_and_publishes_when_the_answer_is_good(self):
        article = register()
        with self.assertLogs("articles.ingest", level="WARNING"):
            process_articles(article, yt.RecordingClient(result=self.bad()), timeout=60)
        client = yt.RecordingClient(result=diarized_result())
        report = process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        article.refresh_from_db()
        self.assertEqual((report.processed, article.status, article.attempts), (1, ArticleStatus.READY, 2))
        self.assertEqual([s["speaker_mode"] for s in client.submits], ["diarize"])
        self.assertEqual(len(article.speakers), 3)

    def test_a_failed_rerun_keeps_the_old_video_online_with_its_names(self):
        article = register()
        process_articles(article, yt.RecordingClient(result=with_images(diarized_result())), timeout=60)
        call_command("label_video", article.slug, "--name", "S1=黃國昌", stdout=io.StringIO())
        before, files = content_of(article.pk), files_on_disk()
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(Article.objects.get(pk=article.pk), yt.RecordingClient(result=self.bad()),
                                      timeout=60, demote_on_failure=False, claim=False)
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual((report.failed, stored.status, stored.gpu_job_id), (1, ArticleStatus.READY, ""))
        self.assertIn("JobFailed", stored.error)
        self.assertEqual(stored.speakers[0]["name"], "黃國昌")
        self.assertEqual(content_of(article.pk), before)
        self.assertEqual(files_on_disk(), files)

    def test_a_gpu_that_answers_in_the_old_format_fails_with_the_reason_in_the_error(self):
        article = register()
        old = yt._payload() | {"video_id": VIDEO}
        with self.assertLogs("articles.ingest", level="WARNING"):
            process_articles(article, yt.RecordingClient(result=old), timeout=60)
        article.refresh_from_db()
        self.assertEqual(article.status, ArticleStatus.FAILED)
        self.assertIn("還沒更新", article.error)


class SubmitTests(IngestCase):
    def test_the_default_asks_for_diarization_with_an_automatic_count(self):
        client = yt.RecordingClient(result=diarized_result())
        process_articles(register(), client, timeout=60)
        sent = client.submits[0]
        self.assertEqual((sent["speaker_mode"], sent["speaker_count"]), ("diarize", None))

    def test_a_fixed_count_is_sent_as_it_is(self):
        for number, count in enumerate((2, 3, 20)):
            with self.subTest(count):
                video_id = f"x{number:010d}"
                client = yt.RecordingClient(result=diarized_result(video_id, requested=count))
                process_articles(register(speaker_count=count, video_id=video_id), client, timeout=60)
                sent = client.submits[0]
                self.assertEqual((sent["speaker_mode"], sent["speaker_count"]), ("diarize", count))

    def test_the_operators_single_speaker_choice_sends_no_speaker_keys(self):
        client = yt.RecordingClient()
        process_articles(register(speaker_count=1, video_id=yt.ZOO), client, timeout=60)
        self.assertEqual((client.submits[0]["speaker_mode"], client.submits[0]["speaker_count"]), (None, None))

    def test_a_job_still_running_from_the_last_round_is_picked_up_without_a_second_request(self):
        article = register()
        Article.objects.filter(pk=article.pk).update(gpu_job_id="job-old")
        client = yt.RecordingClient(result=diarized_result(), known_jobs={"job-old": {"status": "running"}})
        process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        self.assertEqual((client.submits, client.waits[0][0]), ([], "job-old"))
        self.assertEqual(Article.objects.get(pk=article.pk).status, ArticleStatus.READY)


class SingleSpeakerTests(IngestCase):
    """--speakers 1：跟第一部分一模一樣。"""

    def single(self):
        return register(speaker_count=1, video_id=yt.ZOO)

    def test_a_single_speaker_video_stores_no_speaker_data_at_all(self):
        article = self.single()
        save_result(article, yt._payload())
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual((stored.speakers, stored.diarization), ([], None))
        self.assertEqual([s.points for s in stored.slides.all()], [[], []])
        self.assertEqual(stored.brief["one_liner"], "一句話")
        self.assertEqual(stored.transcript_text, "00:00 大家好")

    def test_text_that_looks_like_a_marker_is_served_untouched(self):
        """單人影片的標題、說明可能剛好寫了 [[S1]]：它不是說話者記號，原樣給出去。"""
        article = self.single()
        save_result(article, yt._payload())
        Article.objects.filter(pk=article.pk).update(transcript_text="00:00 [[S1]]：他說 [[S2]]")
        Slide.objects.filter(article=article, index=1).update(bullets=["[[S1]]：重點"])
        data = self.client.get(f"/api/articles/{article.slug}").json()
        self.assertEqual(data["transcript_text"], "00:00 [[S1]]：他說 [[S2]]")
        self.assertEqual(data["slides"][0]["bullets"], ["[[S1]]：重點"])
        self.assertEqual((data["speakers"], data["diarization"]), ([], None))

    def test_a_multi_speaker_answer_to_a_single_speaker_request_is_refused(self):
        """接回了另一種模式的 GPU 工作、或 GPU 搞錯：記號不會被換掉，會原樣漏到網頁上，而且那些話是多人的。"""
        article = self.single()
        answers = {
            "完整的多人結果": lambda p: None,
            "說自己是單人，文字卻帶著記號": lambda p: p.update(speaker_mode="single"),
            "沒有 speaker_mode，文字帶著記號": lambda p: p.pop("speaker_mode"),
            "標明是多人、文字乾淨": lambda p: p.update(transcript_text="00:00 大家好", slides=[]),
        }
        for name, mutate in answers.items():
            with self.subTest(name):
                payload = with_images(diarized_result(yt.ZOO))
                mutate(payload)
                with self.assertRaisesRegex(JobFailed, "單人模式"):
                    save_result(article, payload)
        self.assertEqual((Slide.objects.count(), files_on_disk()), (0, []))
        self.assertEqual(Article.objects.get(pk=article.pk).status, ArticleStatus.PENDING)

    def test_a_marker_in_a_single_speaker_result_is_refused_wherever_it_hides(self):
        article = self.single()
        for where in ("transcript", "bullet", "detail"):
            with self.subTest(where):
                payload = with_images(yt._payload())
                if where == "transcript":
                    payload["transcript_text"] = "00:00 [[S1]]：他說"
                elif where == "bullet":
                    payload["slides"][1]["bullets"] = ["重點", "[[S2]]：另一個重點"]
                else:
                    payload["slides"][1]["detail"] = "完整敘述 [[S3]]"
                with self.assertRaises(JobFailed):
                    save_result(article, payload)
        self.assertEqual((Slide.objects.count(), files_on_disk()), (0, []))

    def test_the_single_mode_can_be_chosen_again_for_a_rerun_and_leaves_no_names_behind(self):
        article = register(video_id=yt.ZOO)
        save_result(article, diarized_result(yt.ZOO))
        call_command("label_video", article.slug, "--name", "S1=黃國昌", "--role", "S1=主持人", stdout=io.StringIO())
        Article.objects.filter(pk=article.pk).update(speaker_count=1)
        save_result(Article.objects.get(pk=article.pk), yt._payload())
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual((stored.speakers, stored.diarization), ([], None))
        self.assertEqual([s.points for s in stored.slides.all()], [[], []])
        self.assertNotIn("黃國昌", str(list(Slide.objects.values())))


class RerunTests(IngestCase):
    def setUp(self):
        super().setUp()
        self.article = register()
        save_result(self.article, with_images(diarized_result()))
        call_command("label_video", self.article.slug, "--name", "S1=黃國昌", "--role", "S1=主持人",
                     "--name", "S2=王小明", stdout=io.StringIO())

    def rerun(self, payload):
        with self.captureOnCommitCallbacks(execute=True):       # 舊圖片在交易提交之後才刪
            save_result(Article.objects.get(pk=self.article.pk), payload)
        return Article.objects.get(pk=self.article.pk)

    def test_the_operators_names_are_not_carried_over_to_new_numbers(self):
        stored = self.rerun(with_images(diarized_result()))
        self.assertEqual([(s["name"], s["role"], s["basis"], s["merged_into"]) for s in stored.speakers],
                         [("", "", "none", "")] * 3)

    def test_the_old_slides_their_points_and_their_images_are_gone(self):
        old = list(Slide.objects.values_list("pk", flat=True))
        old_files = files_on_disk()
        payload = with_images(diarized_result())
        payload["slides"] = payload["slides"][:1]
        stored = self.rerun(payload)
        self.assertEqual(stored.slides.count(), 1)
        self.assertFalse(set(old) & set(Slide.objects.values_list("pk", flat=True)))
        self.assertEqual((len(old_files), len(files_on_disk())), (2, 1))   # 舊的兩張清掉，只剩新的一張
        self.assertFalse(set(old_files) & set(files_on_disk()))

    def test_a_rerun_with_another_roster_replaces_the_roster_and_the_points(self):
        payload = with_images(diarized_result())
        payload["speakers"] = payload["speakers"][:2]
        payload["diarization"]["found_speakers"] = 2
        payload["diarization"]["minor_speakers"] = 0
        # 這一次整支影片都是發言者 A 在講：每一行逐字稿、每個重點都是 S1（S2 還列在清單裡，但沒有人引用）
        payload["transcript_text"] = re.sub(r"\[\[S\d+\]\]", "[[S1]]", payload["transcript_text"])
        for slide in payload["slides"]:
            for point in slide["points"]:
                point["speaker"] = "S1"
            rebuild(slide)       # 同一個人連續講的話是一段（一個記號）：整頁都是 S1，就只有一個記號
        stored = self.rerun(payload)
        self.assertEqual([s["id"] for s in stored.speakers], ["S1", "S2"])
        self.assertEqual({p["speaker"] for s in stored.slides.all() for p in s.points}, {"S1"})
        self.assertEqual(stored.transcript_text, payload["transcript_text"])
        self.assertEqual(stored.slides.get(index=2).detail.count("[[S1]]"), 1)

    def test_a_rerun_from_a_single_speaker_video_to_multi_speaker_works_too(self):
        video = register(speaker_count=1, video_id=yt.ZOO)
        save_result(video, yt._payload())
        Article.objects.filter(pk=video.pk).update(speaker_count=None)
        stored = save_result(Article.objects.get(pk=video.pk), diarized_result(yt.ZOO))
        self.assertEqual(len(stored.speakers), 3)
        self.assertIsNone(stored.brief)


class CouncilUnchangedTests(IngestCase):
    def council(self):
        return Article.objects.create(
            ivod_id="900001", slug="2026-08-27-900001", title="質詢", speaker="王委員", source="ly",
            meeting="第 1 次會議", date=date(2026, 8, 27), ivod_url="https://ivod.example/1")

    def test_speaker_data_in_the_result_of_a_council_article_is_ignored(self):
        """議會沒有「誰說的」的問題：就算 GPU 的結果帶了說話者的鍵，也一個都不存。"""
        council = self.council()
        payload = copy.deepcopy(CONTRACT["result"])
        payload["transcript_text"] = "00:00 [[S1]]：議員質詢"
        payload["brief"] = {"one_liner": "質詢要求補助", "key_numbers": [], "asks": []}
        save_result(council, payload)
        stored = Article.objects.get(pk=council.pk)
        self.assertEqual((stored.speakers, stored.diarization), ([], None))
        self.assertEqual([s.points for s in stored.slides.all()], [[], []])
        self.assertEqual(stored.transcript_text, "00:00 [[S1]]：議員質詢")
        self.assertEqual(stored.brief["one_liner"], "質詢要求補助")
        self.assertEqual(stored.slides.get(index=1).bullets[0], CONTRACT["result"]["slides"][0]["bullets"][0])

    def test_a_council_article_is_never_asked_for_diarization(self):
        council = self.council()
        client = yt.ShapeClient()
        process_articles(Article.objects.filter(pk=council.pk), client, timeout=60)
        self.assertEqual(list(client.kwargs[0]), ["detailed", "speech_hint"])

    def test_a_council_result_is_not_checked_against_the_speaker_contract(self):
        council = self.council()
        save_result(council, {"title": "t", "transcript_text": "x", "brief": None, "slides": []})
        self.assertEqual(Article.objects.get(pk=council.pk).status, ArticleStatus.READY)


class WantsDiarizationTests(SimpleTestCase):
    """誰要做多人區分：只有操作者沒有聲明單人的 YouTube 影片。議會的文章 speaker_count 是空的（None），
    這個空值在影片身上的意思是「自動多人」，在議會的文章身上什麼也不是，不能因為它是 None 就被送去區分。"""

    def test_a_video_wants_it_unless_the_operator_declared_a_single_speaker(self):
        for count, expected in ((None, True), (2, True), (3, True), (20, True), (1, False)):
            with self.subTest(speaker_count=count):
                video = Article(source=ArticleSource.YOUTUBE, speaker_count=count)
                self.assertIs(wants_diarization(video), expected)

    def test_a_council_article_never_wants_it_whatever_its_speaker_count_holds(self):
        councils = [value for value in ArticleSource.values if value != ArticleSource.YOUTUBE]
        self.assertGreaterEqual(len(councils), 3)
        for source in councils:
            for count in (None, 1, 2, 5):
                with self.subTest(source=source, speaker_count=count):
                    self.assertIs(wants_diarization(Article(source=source, speaker_count=count)), False)


class RemoveTests(IngestCase):
    def test_taking_a_multi_speaker_video_down_leaves_no_row_no_file_and_no_page(self):
        """說話者、每個重點、截圖都跟著文章走：下架之後資料庫、媒體資料夾、API 裡都不剩任何一樣。"""
        article = register()
        save_result(article, with_images(diarized_result()))
        self.assertEqual((Slide.objects.count(), len(files_on_disk())), (2, 2))
        self.assertEqual(self.client.get(f"/api/articles/{article.slug}").status_code, 200)
        self.assertEqual(self.client.get("/api/articles?source=youtube").json()["count"], 1)
        call_command("remove_video", article.slug, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual((Article.objects.count(), Slide.objects.count(), files_on_disk()), (0, 0, []))
        self.assertEqual(self.client.get(f"/api/articles/{article.slug}").status_code, 404)
        self.assertEqual(self.client.get("/api/articles?source=youtube").json()["count"], 0)
