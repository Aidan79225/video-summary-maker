"""admin 不能讓人手滑改到說話者：名單、區分摘要、每個重點是誰說的都不在表單裡，說話者模式唯讀。

說話者的名字與併法只能用 label_video（驗過、整批套用、不猜）；admin 的表單若是可以改的，一個手滑就會把
話安到別人頭上，而且沒有任何檢查。所以這些欄位根本不放進表單——POST 偷帶也不會被存。

「每個重點是誰說的」不只在 points 裡：條列、完整敘述與逐字稿裡的 [[Sn]] 記號決定網頁上那句話寫誰的名字，
摘要卡（多人影片一律是空的）則是沒有說話者的口吻。這些文字在多人影片上也鎖成唯讀；單人影片與議會的文章
照舊可以在 admin 裡修。
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from articles.models import Article, ArticleStatus, Slide
from articles.tests.speaker_support import publish_diarized

VIDEO = "jNQXAC9IVRw"
HIDDEN_FROM_THE_FORM = ("speakers", "diarization", "speaker_count", "range_start", "range_end")
# 讀取時會被換成名字的文字：改掉一個記號就是換掉那句話的主人
LOCKED_ARTICLE_FIELDS = ("transcript_text", "brief")
LOCKED_SLIDE_FIELDS = ("title", "bullets", "detail")


def form_data(response) -> dict:
    """把 admin 的修改頁原樣送回去需要的欄位（等於使用者什麼都沒改就按儲存）。"""
    data = {}
    form = response.context["adminform"].form
    for name in form.fields:
        value = form[name].value()
        if value is not None:
            data[name] = value
    for inline in response.context["inline_admin_formsets"]:
        formset = inline.formset
        for key, value in formset.management_form.initial.items():
            data[f"{formset.prefix}-{key}"] = value
        for sub in formset.forms:
            for name in sub.fields:
                value = sub[name].value()
                if value not in (None, ""):
                    data[f"{sub.prefix}-{name}"] = value
    return data


class SpeakerAdminTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp(prefix="news_admin_speakers_")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media))
        self.article = publish_diarized(VIDEO)
        self.client.force_login(get_user_model().objects.create_superuser("admin", "a@example.com", "pw"))
        self.url = f"/admin/articles/article/{self.article.pk}/change/"

    def test_the_change_form_has_no_field_for_the_speakers_the_summary_or_the_mode(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        for name in HIDDEN_FROM_THE_FORM:
            self.assertNotIn(name, form.fields)
            self.assertNotContains(response, f'name="{name}"')
        inline = response.context["inline_admin_formsets"][0].formset
        self.assertNotIn("points", inline.form.base_fields)
        self.assertNotContains(response, 'name="slides-0-points"')

    def test_the_text_that_decides_who_said_what_is_shown_but_cannot_be_edited(self):
        """攔的洞：points 藏起來了，但 [[S1]] 記號在條列、完整敘述與逐字稿裡，表單照樣給人改。"""
        response = self.client.get(self.url)
        form = response.context["adminform"].form
        for name in LOCKED_ARTICLE_FIELDS:
            with self.subTest(field=name):
                self.assertNotIn(name, form.fields)
                self.assertNotContains(response, f'name="{name}"')
                self.assertIn(name, response.context["adminform"].model_admin.get_readonly_fields(
                    response.wsgi_request, self.article))
        inline = response.context["inline_admin_formsets"][0].formset
        for name in LOCKED_SLIDE_FIELDS:
            with self.subTest(field=f"slides-0-{name}"):
                self.assertNotIn(name, inline.form.base_fields)
                self.assertNotContains(response, f'name="slides-0-{name}"')
        # 鎖起來不等於藏起來：操作者要核對的，看得到
        self.assertContains(response, "[[S1]]")
        self.assertContains(response, self.article.transcript_text.splitlines()[0])

    def test_the_rest_of_the_form_still_works(self):
        """鎖的只有記號所在的文字；標題、頻道、狀態這些欄位照常改得動，投影片的順序、時間與圖也是。"""
        response = self.client.get(self.url)
        self.assertLessEqual({"title", "speaker", "status", "source_note"}, set(response.context["adminform"].form.fields))
        inline = response.context["inline_admin_formsets"][0].formset
        self.assertLessEqual({"index", "timestamp", "image"}, set(inline.form.base_fields))

    def test_a_tampered_post_cannot_reach_the_marker_text_or_add_a_slide(self):
        before = Article.objects.get(pk=self.article.pk)
        slides = list(before.slides.order_by("index").values("title", "bullets", "detail", "points"))
        data = form_data(self.client.get(self.url))
        count = int(data["slides-TOTAL_FORMS"])
        data |= {
            "transcript_text": "00:05 [[S2]]：偷改的逐字稿",
            "brief": '{"one_liner": "偷塞的摘要卡", "key_numbers": [], "asks": []}',
            "slides-0-title": "偷改的小標",
            "slides-0-bullets": '["[[S2]]：偷改的條列"]',
            "slides-0-detail": "[[S2]]：偷改的完整敘述",
            "slides-TOTAL_FORMS": str(count + 1),
            f"slides-{count}-index": "99",
            f"slides-{count}-title": "偷加的一頁",
            f"slides-{count}-bullets": '["[[S2]]：偷加的條列"]',
            f"slides-{count}-detail": "偷加的完整敘述",
            f"slides-{count}-timestamp": "0",
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and
                         response.context["adminform"].form.errors)
        after = Article.objects.get(pk=self.article.pk)
        self.assertEqual(after.transcript_text, before.transcript_text)
        self.assertEqual(after.brief, before.brief)
        self.assertIsNone(after.brief)
        self.assertEqual(list(after.slides.order_by("index").values("title", "bullets", "detail", "points")), slides)
        self.assertNotIn("偷", str(list(Slide.objects.values())) + str(Article.objects.values()))

    def test_changing_the_source_does_not_unlock_the_text(self):
        """source 是表單裡改得動的欄位；鎖看的是說話者清單，不是來源。"""
        Article.objects.filter(pk=self.article.pk).update(source="ly")
        response = self.client.get(self.url)
        for name in LOCKED_ARTICLE_FIELDS:
            self.assertNotIn(name, response.context["adminform"].form.fields)
        inline = response.context["inline_admin_formsets"][0].formset
        for name in LOCKED_SLIDE_FIELDS:
            self.assertNotIn(name, inline.form.base_fields)

    def test_a_video_marked_as_one_speaker_and_a_council_article_stay_editable(self):
        """單人影片（--speakers 1）與議會的文章沒有說話者清單，文字裡也沒有記號：照第一部分的樣子，全部改得動。"""
        for source, speaker_count in (("youtube", 1), ("ly", None)):
            with self.subTest(source=source):
                article = Article.objects.create(
                    ivod_id=f"{source}-plain", slug=f"2026-10-30-{source}-plain", title="t", speaker="某人", source=source,
                    date=date(2026, 10, 30), ivod_url="https://example.com/x", status=ArticleStatus.READY,
                    speaker_count=speaker_count, transcript_text="00:01 一句話", brief={"one_liner": "一句話"})
                Slide.objects.create(article=article, index=1, title="小標", bullets=["重點"], detail="敘述")
                response = self.client.get(f"/admin/articles/article/{article.pk}/change/")
                self.assertEqual(response.status_code, 200)
                for name in LOCKED_ARTICLE_FIELDS:
                    self.assertIn(name, response.context["adminform"].form.fields)
                inline = response.context["inline_admin_formsets"][0].formset
                for name in LOCKED_SLIDE_FIELDS:
                    self.assertIn(name, inline.form.base_fields)
                data = form_data(response) | {"transcript_text": "00:01 改過的逐字稿", "slides-0-detail": "改過的敘述"}
                self.assertEqual(self.client.post(f"/admin/articles/article/{article.pk}/change/", data).status_code, 302)
                article.refresh_from_db()
                self.assertEqual(article.transcript_text, "00:01 改過的逐字稿")
                self.assertEqual(article.slides.get().detail, "改過的敘述")

    def test_the_speaker_mode_is_shown_but_read_only(self):
        """空的（自動區分）在 admin 裡顯示成「-」——要看得到這個欄位，也要確定它不是輸入框。"""
        response = self.client.get(self.url)
        self.assertContains(response, "speaker_count")      # 標籤在
        self.assertIn("speaker_count", response.context["adminform"].model_admin.readonly_fields)
        self.assertNotContains(response, 'id="id_speaker_count"')

    def test_the_processing_range_is_shown_but_read_only(self):
        """只處理影片的哪一段決定 GPU 工作怎麼送、結果怎麼核對：看得到，但改要用 add_video --rerun --start/--end。"""
        Article.objects.filter(pk=self.article.pk).update(range_start=600, range_end=1500)
        response = self.client.get(self.url)
        for name in ("range_start", "range_end"):
            with self.subTest(field=name):
                self.assertContains(response, name)
                self.assertIn(name, response.context["adminform"].model_admin.readonly_fields)
                self.assertNotContains(response, f'id="id_{name}"')
        self.assertContains(response, "600")
        self.assertContains(response, "1500")

    def test_saving_the_untouched_form_changes_nothing_about_the_speakers(self):
        before = Article.objects.get(pk=self.article.pk)
        points = [s.points for s in before.slides.order_by("index")]
        data = form_data(self.client.get(self.url))
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and
                         response.context["adminform"].form.errors)
        after = Article.objects.get(pk=self.article.pk)
        self.assertEqual((after.speakers, after.diarization, after.speaker_count),
                         (before.speakers, before.diarization, before.speaker_count))
        self.assertEqual([s.points for s in after.slides.order_by("index")], points)
        self.assertIsNone(after.brief)
        self.assertEqual(after.transcript_text, before.transcript_text)

    def test_a_tampered_post_cannot_reach_the_speakers_the_summary_the_mode_or_the_points(self):
        before = Article.objects.get(pk=self.article.pk)
        points = [s.points for s in before.slides.order_by("index")]
        data = form_data(self.client.get(self.url)) | {
            "speakers": '[{"id": "S1", "name": "偷改的名字", "label": "x", "kind": "participant"}]',
            "diarization": '{"status": "ok"}',
            "speaker_count": "7",
            "range_start": "5",
            "range_end": "9",
            "slides-0-points": '[{"speaker": "S2", "start": 0, "text": "偷改的重點"}]',
            "title": "只有標題是改得動的",
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        after = Article.objects.get(pk=self.article.pk)
        self.assertEqual(after.title, "只有標題是改得動的")           # 表單是通的：改得動的欄位照常存
        self.assertEqual((after.speakers, after.diarization, after.speaker_count, after.range_start, after.range_end),
                         (before.speakers, before.diarization, before.speaker_count, before.range_start,
                          before.range_end))
        self.assertEqual([s.points for s in after.slides.order_by("index")], points)
        self.assertNotIn("偷改", str(list(Slide.objects.values())) + str(Article.objects.values()))
