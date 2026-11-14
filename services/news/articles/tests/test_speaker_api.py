"""API 讀取時把文字裡的 [[Sn]] 換成名字：中性標籤、名字、併人、名字裡的特殊字元、未知的編號、截斷、殘渣。

資料用契約範例的結果走真正的 save_result 存進去（見 speaker_support.publish_diarized），名字與併法用
label_video 設（或直接寫資料庫，測 label_video 不會讓的壞資料）。議會與單人影片原封不動的證明在
test_council_golden.py。
"""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from datetime import date

from django.core.management import call_command
from django.test import TestCase, override_settings

from articles.ingest import register_video
from articles.models import TEASER_LENGTH, Article, ArticleStatus, Slide
from articles.speakers import UNDECIDABLE_TEXT, UNKNOWN_TEXT
from articles.tests.speaker_support import publish_diarized

VIDEO = "jNQXAC9IVRw"
SLUG = f"2026-10-30-yt-{VIDEO}"
SPEAKER_KEYS = ["basis", "display", "flags", "id", "kind", "label", "name", "role", "seconds", "turns"]


class ApiTestCase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp(prefix="news_speaker_api_")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media))
        self.article = publish_diarized(VIDEO)

    def detail(self, slug=SLUG):
        response = self.client.get(f"/api/articles/{slug}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def label(self, *args):
        call_command("label_video", SLUG, *args, stdout=io.StringIO())

    def set_speaker(self, speaker_id, **fields):
        article = Article.objects.get(pk=self.article.pk)
        next(s for s in article.speakers if s["id"] == speaker_id).update(fields)
        article.save(update_fields=["speakers"])

    def set_text(self, **fields):
        Article.objects.filter(pk=self.article.pk).update(**fields)

    def first_slide(self, **fields):
        Slide.objects.filter(article=self.article, index=1).update(**fields)


class ShapeTests(ApiTestCase):
    def test_a_multi_speaker_video_carries_its_speakers_diarization_and_points(self):
        data = self.detail()
        self.assertEqual([sorted(s) for s in data["speakers"]], [SPEAKER_KEYS] * 3)
        self.assertEqual([(s["id"], s["kind"], s["display"], s["basis"]) for s in data["speakers"]],
                         [("S1", "participant", "發言者 A", "none"), ("S2", "participant", "發言者 B", "none"),
                          ("S3", "minor", "其他聲音 1", "none")])
        self.assertEqual(data["diarization"],
                         {"status": "ok", "warnings": [], "requested_speakers": None, "found_speakers": 2})
        points = data["slides"][0]["points"]
        self.assertEqual([sorted(p) for p in points], [["display", "speaker", "start", "text"]] * 4)
        self.assertEqual(points[0], {"speaker": "S1", "display": "發言者 A", "start": 5.0,
                                     "text": "詢問目前補助政策的成效"})

    def test_what_the_page_does_not_need_never_leaves_the_server(self):
        """證據原話、取樣的句子、模型版本與指紋、各種內部數字：只留在資料庫（label_video 要用的留在那裡）。"""
        text = json.dumps(self.detail(), ensure_ascii=False)
        for secret in ("evidence", "samples", "pyannote", "0123456789abcdef", "fingerprint", "unassigned_ratio",
                       "first_seen", "merged_into", "speech_seconds"):
            self.assertNotIn(secret, text)

    def test_the_brief_of_a_multi_speaker_video_is_null(self):
        self.assertIsNone(self.detail()["brief"])

    def test_the_list_still_has_the_card_shape_and_a_teaser_with_names_not_markers(self):
        data = self.client.get("/api/articles?source=youtube").json()
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("speakers", data["items"][0])
        self.assertTrue(data["items"][0]["teaser"].startswith("發言者 A：詢問目前補助政策的成效"))


class SubstitutionTests(ApiTestCase):
    def test_until_somebody_is_named_every_marker_becomes_the_neutral_label(self):
        data = self.detail()
        self.assertEqual(data["slides"][0]["bullets"][0], "發言者 A：詢問目前補助政策的成效")
        self.assertEqual(data["slides"][0]["bullets"][1], "發言者 B：現金補助有幫助，但托育資源更重要")
        self.assertTrue(data["slides"][0]["detail"].startswith("發言者 A：詢問目前補助政策的成效。 發言者 B："))
        self.assertIn("00:05 發言者 A：今天我們來談少子化", data["transcript_text"])
        self.assertIn("01:02 其他聲音 1：我想補充，我們社區就是這樣。", data["transcript_text"])

    def test_names_replace_the_labels_everywhere_at_once(self):
        self.label("--name", "S1=黃國昌", "--name", "S2=王小明", "--role", "S2=來賓")
        data = self.detail()
        self.assertEqual(data["slides"][0]["bullets"][0], "黃國昌：詢問目前補助政策的成效")
        self.assertIn("王小明：現金補助有幫助", data["slides"][0]["detail"])
        self.assertIn("00:14 王小明：現金補助有幫助", data["transcript_text"])
        self.assertTrue(data["slides"][0]["points"][1]["display"] == "王小明")
        by_id = {s["id"]: s for s in data["speakers"]}
        self.assertEqual((by_id["S2"]["name"], by_id["S2"]["role"], by_id["S2"]["display"], by_id["S2"]["basis"]),
                         ("王小明", "來賓", "王小明", "operator"))
        self.assertEqual(by_id["S3"]["display"], "其他聲音 1")

    def test_a_merged_number_is_shown_as_the_one_it_was_merged_into_in_every_place(self):
        self.label("--name", "S1=黃國昌", "--role", "S1=主持人", "--merge", "S3=S1")
        data = self.detail()
        self.assertIn("01:02 黃國昌：我想補充", data["transcript_text"])
        by_id = {s["id"]: s for s in data["speakers"]}
        self.assertEqual((by_id["S3"]["display"], by_id["S3"]["name"], by_id["S3"]["role"], by_id["S3"]["basis"]),
                         ("黃國昌", "黃國昌", "主持人", "operator"))
        self.assertEqual(by_id["S3"]["label"], "其他聲音 1")

    def test_a_merge_into_somebody_with_no_name_shows_that_ones_neutral_label(self):
        self.label("--merge", "S3=S2")
        data = self.detail()
        self.assertIn("01:02 發言者 B：我想補充", data["transcript_text"])
        self.assertEqual({s["id"]: s["basis"] for s in data["speakers"]}["S3"], "none")

    def test_the_points_keep_the_speaker_they_were_given_and_only_the_display_follows_the_merge(self):
        self.label("--name", "S1=黃國昌", "--merge", "S2=S1")
        points = self.detail()["slides"][0]["points"]
        self.assertEqual([(p["speaker"], p["display"]) for p in points],
                         [("S1", "黃國昌"), ("S2", "黃國昌"), ("S1", "黃國昌"), ("S2", "黃國昌")])

    def test_a_name_is_never_a_regex_template(self):
        """攔的 bug：re.sub(pattern, name, text)。名字裡的 \\1、\\g<0>、$1 會被當成反向參照，弄壞文字或丟例外。"""
        for name in ("A.*B", "$1", r"\1", r"\g<0>", r"\g<x>", "(?P<x>a)", "a|b", "[x]", "^$", "\\", "{0}", "%s", "a\\"):
            with self.subTest(name=name):
                self.set_speaker("S1", name=name)
                data = self.detail()
                self.assertEqual(data["slides"][0]["bullets"][0], f"{name}：詢問目前補助政策的成效")
                self.assertTrue(data["transcript_text"].splitlines()[0].startswith(f"00:05 {name}："))
                self.assertEqual(data["speakers"][0]["display"], name)

    def test_a_number_the_list_does_not_have_is_worded_and_the_undecidable_one_too(self):
        self.set_text(transcript_text="00:00 [[S0]]：嗯\n00:03 [[S9]]：喔\n00:04 [[S99999999999999999999]]：欸")
        self.first_slide(bullets=["[[S9]]：一個不存在的人", "[[S0]]：無法判斷的人"], detail="[[S9]]：甲。 [[S0]]：乙。")
        data = self.detail()
        self.assertEqual(data["transcript_text"].splitlines(),
                         [f"00:00 {UNDECIDABLE_TEXT}：嗯", f"00:03 {UNKNOWN_TEXT}：喔", f"00:04 {UNKNOWN_TEXT}：欸"])
        self.assertEqual(data["slides"][0]["bullets"], [f"{UNKNOWN_TEXT}：一個不存在的人",
                                                        f"{UNDECIDABLE_TEXT}：無法判斷的人"])
        self.assertEqual(data["slides"][0]["detail"], f"{UNKNOWN_TEXT}：甲。 {UNDECIDABLE_TEXT}：乙。")

    def test_a_name_that_looks_like_a_marker_is_not_replaced_a_second_time(self):
        self.set_speaker("S1", name="[[S2]]")
        data = self.detail()
        self.assertNotIn("[[", json.dumps(data, ensure_ascii=False))
        self.assertTrue(data["slides"][0]["bullets"][0].startswith("S2：詢問"))

    def test_nothing_that_looks_like_a_marker_survives_in_any_response_text(self):
        """斷掉、格式不對的記號也不能漏到網頁上：整份回應裡不剩 [[ 或 ]]。"""
        self.set_text(transcript_text="00:00 [[S1]]：好\n00:01 [[S1：壞\n00:02 S1]]：壞\n00:03 [[[S1]]]：壞\n00:04 [[]]")
        self.first_slide(bullets=["[[S1", "S2]]：x", "[[ S1 ]]"], detail="[[Sx]] [[S1]] ]]")
        text = json.dumps(self.detail(), ensure_ascii=False)
        self.assertNotIn("[[", text)
        self.assertNotIn("]]", text)

    def test_the_title_of_a_slide_is_not_touched(self):
        """小標不是說話者的話，也不在換記號的範圍裡（GPU 的小標有記號會在存進來時就被擋下）。"""
        self.first_slide(title="補助政策與托育名額")
        self.assertEqual(self.detail()["slides"][0]["title"], "補助政策與托育名額")

    def test_a_substitution_changes_the_response_only_never_the_stored_text(self):
        self.label("--name", "S1=黃國昌")
        self.detail()
        stored = Article.objects.get(pk=self.article.pk)
        self.assertIn("[[S1]]", stored.transcript_text)
        self.assertIn("[[S1]]", stored.slides.first().detail)


class TeaserTests(ApiTestCase):
    def test_the_marker_is_replaced_before_the_cut_so_no_half_marker_leaks(self):
        """第 120 個字剛好落在 [[S1]] 中間：先截斷會漏出「[[S」。"""
        detail = "字" * (TEASER_LENGTH - 3) + "[[S1]]：後面還有很多話"
        self.assertEqual(detail[:TEASER_LENGTH][-3:], "[[S")
        self.first_slide(detail=detail)
        teaser = self.detail()["teaser"]
        self.assertNotIn("[[", teaser)
        self.assertNotIn("[", teaser)
        self.assertEqual(teaser, ("字" * (TEASER_LENGTH - 3) + "發言者 A：後面還有很多話")[:TEASER_LENGTH])

    def test_a_long_name_is_what_gets_cut_not_the_marker(self):
        self.set_speaker("S1", name="名" * 40)
        self.first_slide(detail="[[S1]]：" + "話" * 200)
        teaser = self.detail()["teaser"]
        self.assertEqual(len(teaser), TEASER_LENGTH)
        self.assertEqual(teaser, ("名" * 40 + "：" + "話" * 200)[:TEASER_LENGTH])

    def test_without_a_detail_the_bullets_are_joined_and_replaced_too(self):
        self.first_slide(detail="", bullets=["[[S1]]：甲", "[[S2]]：乙"])
        self.assertEqual(self.detail()["teaser"], "發言者 A：甲；發言者 B：乙")

    def test_the_card_in_the_list_gets_the_same_teaser(self):
        self.label("--name", "S1=黃國昌")
        detail = self.detail()["teaser"]
        listed = self.client.get("/api/articles?source=youtube").json()["items"][0]["teaser"]
        self.assertEqual(listed, detail)
        self.assertTrue(listed.startswith("黃國昌："))

    def test_the_teaser_is_the_same_wherever_it_is_computed(self):
        article = Article.objects.get(pk=self.article.pk)
        self.assertEqual(article.teaser, self.detail()["teaser"])


class NotStructuredTests(ApiTestCase):
    """沒有說話者清單的影片（單人）、清單壞掉的影片：文字原封不動，不亂換。"""

    def single(self):
        article, _ = register_video("aaaaaaaaaaa", title="單人演講", speaker="頻道", day=date(2026, 10, 30),
                                    speaker_count=1)
        payload = {"video_id": "aaaaaaaaaaa", "title": "單人演講", "source_note": "", "transcript_text": "00:00 [[S1]]：嗨",
                   "brief": None, "slides": [{"index": 1, "title": "x", "bullets": ["[[S1]]：條列"],
                                              "detail": "[[S1]]：敘述", "timestamp": 0.0}]}
        # 單人流程不接受帶著記號的結果（check_single_result）：直接寫進去，測 API 本身不亂換
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.READY, transcript_text=payload["transcript_text"])
        Slide.objects.create(article=article, index=1, title="x", bullets=payload["slides"][0]["bullets"],
                             detail=payload["slides"][0]["detail"])
        return article

    def test_a_single_speaker_video_keeps_its_text_and_has_no_speakers(self):
        article = self.single()
        data = self.detail(article.slug)
        self.assertEqual((data["speakers"], data["diarization"]), ([], None))
        self.assertEqual(data["transcript_text"], "00:00 [[S1]]：嗨")
        self.assertEqual(data["slides"][0]["bullets"], ["[[S1]]：條列"])
        self.assertEqual(data["slides"][0]["detail"], "[[S1]]：敘述")
        self.assertEqual(data["slides"][0]["points"], [])
        self.assertEqual(data["teaser"], "[[S1]]：敘述")

    def test_a_list_of_strings_from_an_old_gpu_is_not_a_speaker_list(self):
        self.set_text(speakers=["說話者 1", "說話者 2"])
        data = self.detail()
        self.assertEqual((data["speakers"], data["diarization"]), ([], None))
        self.assertIn("[[S1]]", data["transcript_text"])

    def test_rows_without_an_id_are_not_a_speaker_list(self):
        self.set_text(speakers=[{"name": "沒有編號"}])
        self.assertEqual(self.detail()["speakers"], [])

    def test_a_damaged_row_is_tamed_not_a_500(self):
        article = Article.objects.get(pk=self.article.pk)
        article.speakers[1].update(label=5, kind="boss", seconds="x", turns=None, basis="hacked", flags="abc",
                                   name=["x"])
        article.save(update_fields=["speakers"])
        data = self.detail()
        damaged = data["speakers"][1]
        self.assertEqual((damaged["kind"], damaged["basis"], damaged["seconds"], damaged["turns"], damaged["flags"]),
                         ("participant", "none", 0.0, 0, []))

    def test_damaged_points_and_diarization_are_tamed_too(self):
        self.first_slide(points=["junk", {"text": "x"}, {"speaker": "S1", "start": "x", "text": 5}])
        Article.objects.filter(pk=self.article.pk).update(diarization={"status": "weird"})
        data = self.detail()
        self.assertIsNone(data["diarization"])
        self.assertEqual(data["slides"][0]["points"], [{"speaker": "S1", "display": "發言者 A", "start": 0.0, "text": ""}])

    def test_a_video_whose_speaker_row_was_unpublished_is_not_served(self):
        Article.objects.filter(pk=self.article.pk).update(status=ArticleStatus.FAILED)
        self.assertEqual(self.client.get(f"/api/articles/{SLUG}").status_code, 404)
