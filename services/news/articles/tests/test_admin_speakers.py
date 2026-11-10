"""admin 不能讓人手滑改到說話者：名單、區分摘要、每個重點是誰說的都不在表單裡，說話者模式唯讀。

說話者的名字與併法只能用 label_video（驗過、整批套用、不猜）；admin 的表單若是可以改的，一個手滑就會把
話安到別人頭上，而且沒有任何檢查。所以這些欄位根本不放進表單——POST 偷帶也不會被存。
"""
from __future__ import annotations

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from articles.models import Article, Slide
from articles.tests.speaker_support import publish_diarized

VIDEO = "jNQXAC9IVRw"
HIDDEN_FROM_THE_FORM = ("speakers", "diarization", "speaker_count")


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

    def test_the_speaker_mode_is_shown_but_read_only(self):
        """空的（自動區分）在 admin 裡顯示成「-」——要看得到這個欄位，也要確定它不是輸入框。"""
        response = self.client.get(self.url)
        self.assertContains(response, "speaker_count")      # 標籤在
        self.assertIn("speaker_count", response.context["adminform"].model_admin.readonly_fields)
        self.assertNotContains(response, 'id="id_speaker_count"')

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
            "slides-0-points": '[{"speaker": "S2", "start": 0, "text": "偷改的重點"}]',
            "title": "只有標題是改得動的",
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        after = Article.objects.get(pk=self.article.pk)
        self.assertEqual(after.title, "只有標題是改得動的")           # 表單是通的：改得動的欄位照常存
        self.assertEqual((after.speakers, after.diarization, after.speaker_count),
                         (before.speakers, before.diarization, before.speaker_count))
        self.assertEqual([s.points for s in after.slides.order_by("index")], points)
        self.assertNotIn("偷改", str(list(Slide.objects.values())) + str(Article.objects.values()))
