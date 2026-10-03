"""議題分布：政策領域、分類的輸入、每晚分類、標註集、評估、上線條件、委員會職掌。

用假的 GPU 客戶端，不碰網路。指標的計算在 test_profiles.py，API 在 test_api.py，admin 在
test_admin.py。
"""
from __future__ import annotations

import io
import itertools
from datetime import date, datetime
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from articles.gpu_client import GpuApiError, JobFailed, RequestRejected
from articles.ingest import save_result
from articles.models import (Article, ArticleStatus, Membership, Person, ProfileStat, Slide, Topic,
                             TopicEvaluation, TopicLabel)
from articles.profiles import parse_session
from articles.topics import (
    COMMITTEE_AREAS,
    TOPIC_BY_KEY,
    TOPIC_KEYS,
    TOPIC_MIN_LABELS,
    TOPIC_TEXT_LIMIT,
    TOPICS,
    EvaluationAborted,
    base_articles,
    classifier_input,
    classify_topics,
    committee_areas,
    evaluate,
    labels_payload,
    parse_committee,
    parse_result,
    passing_classifiers,
    passing_evaluations,
    sample_labels,
)

CLASSIFIER = "fake-model#topic-v1"
LY_MEETING = "第11屆第5會期財政委員會第3次全體委員會議"
_ids = itertools.count(1)


def _article(speaker="甲", source="ly", one_liner="finance 的一句話", titles=("第一段",),
             status=ArticleStatus.READY, brief=True, meeting=LY_MEETING, day="2026-03-10"):
    """一篇文章：一句話裡寫了假 GPU 要回答的領域代碼（見 FakeTopicGpu）。"""
    n = next(_ids)
    article = Article.objects.create(
        ivod_id=f"{source}-t{n}", slug=f"{day}-{source}-t{n}", source=source, title="t",
        speaker=speaker, meeting=meeting, date=date.fromisoformat(day),
        ivod_url="https://example.invalid/x", status=status,
        brief={"one_liner": one_liner, "key_numbers": [], "asks": []} if brief else None)
    for index, title in enumerate(titles, start=1):
        Slide.objects.create(article=article, index=index, title=title)
    return article


def _code_in(text: str) -> str:
    """假 GPU 的答案：文字裡出現的第一個領域代碼，沒有就是「其他」。"""
    return next((key for key in TOPIC_KEYS if key in text), "local")


class FakeTopicGpu:
    """假的 GPU 客戶端，只實作 topic 工作用得到的兩個方法。

    answer(text) 回傳領域代碼、例外（丟出去）或整個成品 dict（原樣回傳）；classifier 可以是
    字串或 text → 字串（模擬評估途中換了模型）。
    """

    def __init__(self, answer=_code_in, classifier=CLASSIFIER):
        self.answer = answer
        self.classifier = classifier
        self.texts: list[str] = []
        self.labels: list[list[dict]] = []

    def submit_topic(self, text, labels):
        self.texts.append(text)
        self.labels.append(labels)
        return f"job-{len(self.texts)}"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        text = self.texts[int(job_id.removeprefix("job-")) - 1]
        outcome = self.answer(text)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        classifier = self.classifier(text) if callable(self.classifier) else self.classifier
        return {"primary": outcome, "secondary": None, "classifier": classifier}


class TaxonomyTests(SimpleTestCase):
    def test_twelve_fixed_areas_with_unique_codes_and_names(self):
        self.assertEqual(len(TOPICS), 12)
        self.assertEqual(len(set(TOPIC_KEYS)), 12)
        self.assertEqual(len({a.label for a in TOPICS}), 12)
        self.assertEqual(TOPIC_BY_KEY["finance"].label, "財政經濟")
        self.assertEqual(TOPICS[-1].key, "local")

    def test_the_labels_sent_to_the_gpu_carry_key_label_and_description(self):
        payload = labels_payload()
        self.assertEqual(len(payload), 12)
        self.assertEqual(payload[0], {"key": "defense", "label": "國防外交",
                                      "description": "國防、軍事、外交、兩岸、僑務"})


class ClassifierInputTests(TestCase):
    def test_the_one_liner_and_slide_titles_without_the_meeting_name(self):
        article = _article(one_liner="國防部無人機交機不到一半",
                           titles=("無人機採購進度", "  ", "交機時程"),
                           meeting="第11屆第5會期外交及國防委員會第3次全體委員會議")
        text = classifier_input(article)
        self.assertEqual(text, "一句話：國防部無人機交機不到一半\n各段小標：\n- 無人機採購進度\n- 交機時程")
        self.assertNotIn("委員會", text)

    def test_without_titles_it_is_just_the_one_liner(self):
        self.assertEqual(classifier_input(_article(one_liner="一句話", titles=())), "一句話：一句話")

    def test_a_text_over_the_limit_drops_titles_from_the_end(self):
        article = _article(one_liner="一句話", titles=[f"{i:03d}" + "長" * 290 for i in range(20)])
        text = classifier_input(article)
        self.assertLessEqual(len(text), TOPIC_TEXT_LIMIT)
        self.assertTrue(text.startswith("一句話：一句話\n各段小標：\n- 000"))
        self.assertNotIn("019", text)


class BaseArticleTests(TestCase):
    def test_finished_solo_speeches_with_a_brief(self):
        base = _article("甲")
        _article("甲、乙")
        _article("甲", brief=False)
        _article("甲", status=ArticleStatus.PENDING)
        _article("甲", status=ArticleStatus.FAILED)
        self.assertEqual(list(base_articles()), [base])


class ParseResultTests(SimpleTestCase):
    def test_a_good_result(self):
        result = parse_result({"primary": "finance", "secondary": "labor", "classifier": " m#topic-v1 "})
        self.assertEqual((result.primary, result.secondary, result.classifier),
                         ("finance", "labor", "m#topic-v1"))

    def test_a_secondary_equal_to_the_primary_or_unknown_is_dropped(self):
        for secondary in ("finance", "space", None, 3):
            with self.subTest(secondary=secondary):
                self.assertEqual(parse_result({"primary": "finance", "secondary": secondary,
                                               "classifier": "m"}).secondary, "")

    def test_a_primary_outside_the_list_fails_rather_than_guessing(self):
        for primary in ("財政經濟", "space", None, ["finance"]):
            with self.subTest(primary=primary), self.assertRaises(JobFailed):
                parse_result({"primary": primary, "classifier": "m"})

    def test_a_result_without_a_classifier_or_not_a_dict_fails(self):
        for result in ({"primary": "finance"}, {"primary": "finance", "classifier": " "}, None, []):
            with self.subTest(result=result), self.assertRaises(JobFailed):
                parse_result(result)


class ClassifyTests(TestCase):
    LOGGER = "articles.topics"

    def test_only_base_articles_are_classified(self):
        base = _article("甲", one_liner="welfare 長照")
        _article("甲、乙", one_liner="defense")
        _article("甲", brief=False)
        _article("甲", status=ArticleStatus.PENDING, one_liner="labor")
        gpu = FakeTopicGpu()
        report = classify_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.failed, report.remaining), (1, 0, 0))
        topic = Topic.objects.get()
        self.assertEqual((topic.article_id, topic.primary, topic.secondary, topic.classifier),
                         (base.id, "welfare", "", CLASSIFIER))
        self.assertIsNotNone(topic.labeled_at)
        # 送出去的是分類的輸入與完整的領域清單
        self.assertEqual(gpu.texts, [classifier_input(base)])
        self.assertEqual(gpu.labels, [labels_payload()])

    def test_classified_articles_are_skipped_unless_reclassifying(self):
        first = _article(one_liner="finance")
        classify_topics(FakeTopicGpu(), limit=10)
        second = _article(one_liner="labor")
        gpu = FakeTopicGpu()
        classify_topics(gpu, limit=10)
        self.assertEqual(gpu.texts, [classifier_input(second)])

        # 換了模型：--reclassify 連已經有的也重分
        gpu = FakeTopicGpu(answer=lambda text: "local", classifier="new-model#topic-v2")
        report = classify_topics(gpu, limit=10, reclassify=True)
        self.assertEqual(report.classified, 2)
        self.assertEqual({(t.article_id, t.primary, t.classifier) for t in Topic.objects.all()},
                         {(first.id, "local", "new-model#topic-v2"),
                          (second.id, "local", "new-model#topic-v2")})

    def test_reclassifying_in_batches_goes_round_the_oldest_first(self):
        articles = [_article(one_liner="finance") for _ in range(3)]
        classify_topics(FakeTopicGpu(), limit=10)
        Topic.objects.filter(article=articles[1]).update(
            labeled_at=timezone.make_aware(datetime(2026, 1, 1)))
        gpu = FakeTopicGpu()
        classify_topics(gpu, limit=1, reclassify=True)
        self.assertEqual(gpu.texts, [classifier_input(articles[1])])

    def test_the_limit_is_kept_and_the_backlog_reported(self):
        for _ in range(5):
            _article()
        report = classify_topics(FakeTopicGpu(), limit=2)
        self.assertEqual((report.classified, report.remaining), (2, 3))
        self.assertIn("還沒分類 3", str(report))

    def test_a_failed_classification_leaves_the_article_alone(self):
        article = _article(one_liner="finance")
        before = Article.objects.get(pk=article.pk)
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("模型答非所問"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_topics(gpu, limit=10)
        after = Article.objects.get(pk=article.pk)
        self.assertEqual((after.status, after.error, after.attempts, after.updated_at),
                         (before.status, before.error, before.attempts, before.updated_at))
        self.assertFalse(Topic.objects.exists())
        self.assertEqual((report.failed, report.remaining), (1, 1))
        self.assertIn("模型答非所問", report.errors[0])

    def test_an_answer_outside_the_list_is_a_failure_not_a_guess(self):
        _article()
        gpu = FakeTopicGpu(answer=lambda text: {"primary": "太空", "classifier": CLASSIFIER})
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_topics(gpu, limit=10)
        self.assertEqual(report.failed, 1)
        self.assertFalse(Topic.objects.exists())

    def test_one_bad_article_does_not_stop_the_others(self):
        _article(one_liner="壞的")
        _article(one_liner="finance")
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("x") if "壞的" in text else _code_in(text))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.failed, report.stopped), (1, 1, False))

    def test_an_article_that_cannot_be_saved_does_not_stop_the_others(self):
        """SD 卡上的 SQLite 鎖住一下：那一篇算失敗，剩下的照分。"""
        first = _article(one_liner="finance")
        second = _article(one_liner="labor")
        save = Topic.objects.update_or_create

        def flaky(article, defaults):
            if article == first:
                raise OperationalError("database is locked")
            return save(article=article, defaults=defaults)

        with mock.patch.object(Topic.objects, "update_or_create", side_effect=flaky), \
                self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_topics(FakeTopicGpu(), limit=10)
        self.assertEqual((report.classified, report.failed, report.stopped), (1, 1, False))
        self.assertIn("database is locked", report.errors[0])
        self.assertEqual(list(Topic.objects.values_list("article_id", flat=True)), [second.id])

    def test_an_unavailable_gpu_stops_the_whole_round(self):
        for _ in range(3):
            _article()
        gpu = FakeTopicGpu(answer=lambda text: GpuApiError("連線被拒"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_topics(gpu, limit=10)
        self.assertEqual(len(gpu.texts), 1)
        self.assertTrue(report.stopped)
        self.assertEqual((report.failed, report.remaining), (1, 3))
        self.assertIn("GPU 不可用", str(report))


class RegenerationTests(TestCase):
    def test_regenerating_an_article_deletes_its_topic_but_keeps_the_label(self):
        article = _article(one_liner="finance")
        classify_topics(FakeTopicGpu(), limit=10)
        TopicLabel.objects.create(article=article, primary="finance")
        save_result(article, {"title": "t", "slides": [],
                              "brief": {"one_liner": "labor 新的一句話", "key_numbers": [], "asks": []}})
        self.assertFalse(Topic.objects.exists())
        self.assertEqual(TopicLabel.objects.get().primary, "finance")
        # 下一輪重新分類，讀的是新的一句話
        classify_topics(FakeTopicGpu(), limit=10)
        self.assertEqual(Topic.objects.get().primary, "labor")


class SampleTests(TestCase):
    def setUp(self):
        for _ in range(30):
            _article("甲", "ly")
        for _ in range(8):
            _article("乙", "tccc", meeting="第4屆第8次定期會")
        # 不是基礎文章的不會被抽到
        _article("甲、乙", "ly")
        _article("甲", "ly", brief=False)
        _article("甲", "ly", status=ArticleStatus.PENDING)

    def _drawn(self):
        return {(label.article.source, label.article_id)
                for label in TopicLabel.objects.select_related("article")}

    def test_each_source_gets_n_blank_labels_from_its_base_articles(self):
        report = sample_labels(per_source=20, seed=0)
        self.assertEqual((report.added["ly"], report.added["tccc"], report.added["ntpc"]), (20, 8, 0))
        self.assertEqual((report.total["ly"], report.total["tccc"]), (20, 8))
        self.assertEqual(set(TopicLabel.objects.values_list("primary", flat=True)), {""})
        base = set(base_articles().values_list("id", flat=True))
        self.assertTrue({article_id for _, article_id in self._drawn()} <= base)
        self.assertIn("立法院：新抽 20 篇", str(report))

    def test_the_same_seed_draws_the_same_articles(self):
        sample_labels(per_source=10, seed=0)
        first = self._drawn()
        TopicLabel.objects.all().delete()
        sample_labels(per_source=10, seed=0)
        self.assertEqual(self._drawn(), first)
        TopicLabel.objects.all().delete()
        sample_labels(per_source=10, seed=1)
        self.assertNotEqual(self._drawn(), first)

    def test_running_again_only_tops_up_and_never_redraws(self):
        sample_labels(per_source=5)
        first = self._drawn()
        labelled = TopicLabel.objects.filter(article__source="ly").first()
        labelled.primary = "finance"
        labelled.save()
        report = sample_labels(per_source=12)
        self.assertEqual((report.added["ly"], report.total["ly"]), (7, 12))
        self.assertTrue(first <= self._drawn())
        labelled.refresh_from_db()
        self.assertEqual(labelled.primary, "finance")
        self.assertEqual(sample_labels(per_source=12).added["ly"], 0)


class EvaluateTests(TestCase):
    LOGGER = "articles.topics"
    WHEN = timezone.make_aware(datetime(2026, 10, 3, 9, 30))

    def _labelled(self, count, human="finance", model="finance", source="ly", speaker="甲"):
        meeting = LY_MEETING if source == "ly" else "第4屆第8次定期會"
        made = []
        for _ in range(count):
            article = _article(speaker, source, one_liner=f"{model} 的一句話", meeting=meeting)
            TopicLabel.objects.create(article=article, primary=human)
            made.append(article)
        return made

    def test_accuracy_is_matches_over_labels_and_16_of_20_passes(self):
        self._labelled(16)
        wrong = self._labelled(4, human="finance", model="defense")
        report = evaluate(FakeTopicGpu(), now=self.WHEN)
        ev = TopicEvaluation.objects.get()
        self.assertEqual((ev.source, ev.classifier, ev.labeled, ev.correct, ev.accuracy, ev.passed,
                          ev.ran_at),
                         ("ly", CLASSIFIER, 20, 16, 0.8, True, self.WHEN))
        self.assertEqual(ev.mistakes[0], {"article": wrong[0].id, "slug": wrong[0].slug,
                                          "speaker": "甲", "human": "finance", "model": "defense"})
        self.assertEqual(len(ev.mistakes), 4)
        text = str(report)
        self.assertIn("準確率 80.0%：通過", text)
        self.assertIn(f"判錯：{wrong[0].slug} 甲：人工「財政經濟」、模型「國防外交」", text)

    def test_below_the_threshold_does_not_pass(self):
        self._labelled(15)
        self._labelled(5, model="defense")
        report = evaluate(FakeTopicGpu())
        self.assertEqual((report.evaluations[0].accuracy, report.evaluations[0].passed), (0.75, False))
        self.assertIn("未通過（準確率未達 80%）", str(report))

    def test_too_few_labels_do_not_pass_even_when_all_are_right(self):
        self._labelled(TOPIC_MIN_LABELS - 1)
        report = evaluate(FakeTopicGpu())
        ev = report.evaluations[0]
        self.assertEqual((ev.accuracy, ev.passed), (1.0, False))
        self.assertIn("標註不足 20 篇", str(report))

    def test_one_row_per_source_with_labels(self):
        self._labelled(20)
        self._labelled(3, source="tccc", speaker="乙")
        evaluate(FakeTopicGpu())
        rows = {ev.source: (ev.labeled, ev.passed) for ev in TopicEvaluation.objects.all()}
        self.assertEqual(rows, {"ly": (20, True), "tccc": (3, False)})

    def test_only_labelled_base_articles_are_sent(self):
        labelled = self._labelled(2)
        TopicLabel.objects.create(article=_article(one_liner="labor"))          # 還沒標
        joint = _article("甲、乙", one_liner="labor")
        TopicLabel.objects.create(article=joint, primary="labor")              # 已經不是基礎文章
        gpu = FakeTopicGpu()
        report = evaluate(gpu)
        self.assertEqual(gpu.texts, [classifier_input(a) for a in labelled])
        self.assertEqual((report.skipped, report.evaluations[0].labeled), (1, 2))
        self.assertIn("略過 1 篇", str(report))

    def test_a_failed_classification_counts_as_wrong(self):
        self._labelled(19)
        broken = self._labelled(1, model="壞的")[0]
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("JSON 解析失敗") if "壞的" in text
                           else _code_in(text))
        report = evaluate(gpu)
        ev = report.evaluations[0]
        self.assertEqual((ev.labeled, ev.correct), (20, 19))
        self.assertEqual(ev.mistakes, [{"article": broken.id, "slug": broken.slug, "speaker": "甲",
                                        "human": "finance", "model": None,
                                        "error": "JSON 解析失敗"}])
        self.assertIn("模型「分類失敗：JSON 解析失敗」", str(report))

    def test_a_classifier_change_midway_aborts_and_saves_nothing(self):
        self._labelled(10)
        self._labelled(10, source="tccc", speaker="乙")
        gpu = FakeTopicGpu(classifier=lambda text: "old#topic-v1" if len(gpu.texts) < 5
                           else "new#topic-v1")
        with self.assertRaises(EvaluationAborted) as ctx:
            evaluate(gpu)
        self.assertIn("中途換了模型", str(ctx.exception))
        # 一發現就停，不必把剩下的也送完
        self.assertEqual(len(gpu.texts), 5)
        self.assertFalse(TopicEvaluation.objects.exists())

    def test_an_unavailable_gpu_saves_nothing(self):
        self._labelled(20)
        with self.assertRaises(GpuApiError):
            evaluate(FakeTopicGpu(answer=lambda text: GpuApiError("連線被拒")))
        self.assertFalse(TopicEvaluation.objects.exists())

    def test_nothing_labelled_or_nothing_classified_aborts(self):
        with self.assertRaises(EvaluationAborted):
            evaluate(FakeTopicGpu())
        self._labelled(3)
        with self.assertRaises(EvaluationAborted):
            evaluate(FakeTopicGpu(answer=lambda text: JobFailed("x")))
        self.assertFalse(TopicEvaluation.objects.exists())

    def test_evaluating_does_not_touch_the_articles_topics(self):
        article = self._labelled(1)[0]
        Topic.objects.create(article=article, primary="labor", classifier="old#topic-v1",
                             labeled_at=self.WHEN)
        evaluate(FakeTopicGpu())
        self.assertEqual(Topic.objects.get().primary, "labor")


class GateTests(TestCase):
    def _ev(self, source, classifier, passed, day):
        return TopicEvaluation.objects.create(
            source=source, classifier=classifier, labeled=20, correct=18 if passed else 10,
            accuracy=0.9 if passed else 0.5, passed=passed,
            ran_at=timezone.make_aware(datetime(2026, 10, day)))

    def test_no_passing_evaluation_means_no_gate(self):
        self._ev("ly", "a#topic-v1", False, 1)
        self.assertEqual(passing_classifiers(), {})

    def test_the_latest_passing_evaluation_wins_and_a_later_failure_does_not_revoke_it(self):
        self._ev("ly", "a#topic-v1", True, 1)
        newer = self._ev("ly", "b#topic-v1", True, 2)
        self._ev("ly", "c#topic-v1", False, 3)
        tccc = self._ev("tccc", "a#topic-v1", True, 1)
        self.assertEqual(passing_evaluations(), {"ly": newer, "tccc": tccc})
        self.assertEqual(passing_classifiers(), {"ly": "b#topic-v1", "tccc": "a#topic-v1"})


class CommitteeTests(SimpleTestCase):
    def test_entries_are_split_into_session_and_committee(self):
        self.assertEqual(parse_committee("第11屆第5會期：財政委員會"), ("第11屆第5會期", "財政委員會"))
        # 全形數字、前導零、半形冒號都整理成同一個會期名稱
        self.assertEqual(parse_committee("第１１屆第05會期:內政委員會 "), ("第11屆第5會期", "內政委員會"))

    def test_malformed_entries_are_skipped(self):
        for entry in ("", "財政委員會", "第11屆第5會期：", "第11屆：財政委員會", None, 3):
            with self.subTest(entry=entry):
                self.assertIsNone(parse_committee(entry))

    def test_the_session_name_is_the_one_articles_are_filed_under(self):
        name, _ = parse_committee("第11屆第5會期：財政委員會")
        self.assertEqual(name, parse_session("ly", LY_MEETING).name)

    def test_areas_are_the_union_of_that_sessions_committees(self):
        committees = ["第11屆第4會期：財政委員會", "第11屆第5會期：經濟委員會",
                      "第11屆第5會期：程序委員會", "第11屆第5會期：交通委員會", ""]
        self.assertEqual(committee_areas(committees, "第11屆第5會期"),
                         {"finance", "agriculture", "environment", "transport", "digital"})
        self.assertEqual(committee_areas(committees, "第11屆第4會期"), {"finance"})
        self.assertEqual(committee_areas(committees, "第11屆第3會期"), frozenset())

    def test_committees_without_a_portfolio_give_no_areas(self):
        for committee in ("程序委員會", "修憲委員會", "經費稽核委員會"):
            self.assertEqual(committee_areas([f"第11屆第5會期：{committee}"], "第11屆第5會期"),
                             frozenset())

    def test_the_table_is_the_published_one(self):
        expected = {
            "內政委員會": {"內政治安"},
            "外交及國防委員會": {"國防外交"},
            "經濟委員會": {"財政經濟", "農業", "環境能源"},
            "財政委員會": {"財政經濟"},
            "教育及文化委員會": {"教育文化", "數位科技"},
            "交通委員會": {"交通建設", "數位科技"},
            "司法及法制委員會": {"司法法制"},
            "社會福利及衛生環境委員會": {"衛生福利", "勞動", "環境能源"},
        }
        self.assertEqual({name: {TOPIC_BY_KEY[key].label for key in keys}
                          for name, keys in COMMITTEE_AREAS.items()}, expected)


class CommandTests(TestCase):
    def _gpu(self, module, gpu):
        return mock.patch(f"articles.management.commands.{module}.GpuApiClient", return_value=gpu)

    def test_classify_topics_respects_limit_and_reclassify(self):
        for _ in range(3):
            _article()
        out = io.StringIO()
        with self._gpu("classify_topics", FakeTopicGpu()):
            call_command("classify_topics", limit=2, stdout=out)
        self.assertIn("分類完成 2、失敗 0、還沒分類 1", out.getvalue())
        gpu = FakeTopicGpu(answer=lambda text: "labor")
        with self._gpu("classify_topics", gpu):
            call_command("classify_topics", "--reclassify", "--limit", "10", stdout=io.StringIO())
        self.assertEqual(len(gpu.texts), 3)
        self.assertEqual(set(Topic.objects.values_list("primary", flat=True)), {"labor"})

    def test_sample_topic_labels(self):
        for _ in range(4):
            _article()
        out = io.StringIO()
        call_command("sample_topic_labels", "--per-source", "3", "--seed", "5", stdout=out)
        self.assertEqual(TopicLabel.objects.count(), 3)
        self.assertIn("立法院：新抽 3 篇", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("sample_topic_labels", "--per-source", "0", stdout=io.StringIO())

    def test_eval_topics_prints_the_result_and_recomputes_profiles_when_the_gate_changes(self):
        person = Person.objects.create(name="甲")
        Membership.objects.create(person=person, source="ly", name="甲")
        for _ in range(TOPIC_MIN_LABELS):
            article = _article("甲", one_liner="finance")
            TopicLabel.objects.create(article=article, primary="finance")
            Topic.objects.create(article=article, primary="finance", classifier=CLASSIFIER,
                                 labeled_at=timezone.now())
        out = io.StringIO()
        with self._gpu("eval_topics", FakeTopicGpu()):
            call_command("eval_topics", stdout=out)
        self.assertIn("準確率 100.0%：通過", out.getvalue())
        self.assertIn("已經重算人物側寫", out.getvalue())
        self.assertEqual(ProfileStat.objects.get(indicator="topic:finance").value, 20)

        # 同一個分類器再評一次：上線的版本沒變，不必重算
        out = io.StringIO()
        with self._gpu("eval_topics", FakeTopicGpu()):
            call_command("eval_topics", stdout=out)
        self.assertNotIn("重算", out.getvalue())

    def test_eval_topics_turns_an_abort_into_a_command_error(self):
        with self._gpu("eval_topics", FakeTopicGpu()), self.assertRaises(CommandError) as ctx:
            call_command("eval_topics", stdout=io.StringIO())
        self.assertIn("什麼都沒存", str(ctx.exception))
        _article()
        TopicLabel.objects.create(article=Article.objects.get(), primary="finance")
        gpu = FakeTopicGpu(answer=lambda text: GpuApiError("連線被拒"))
        with self._gpu("eval_topics", gpu), self.assertRaises(CommandError):
            call_command("eval_topics", stdout=io.StringIO())
        self.assertFalse(TopicEvaluation.objects.exists())


class SameClassifierFailsTests(TestCase):
    """攔的 bug：同一個分類器在更多標註上重評沒通過，它還是上線、頁面還掛著舊的準確率。"""

    def _ev(self, classifier, passed, day):
        return TopicEvaluation.objects.create(
            source="ly", classifier=classifier, labeled=20, correct=18 if passed else 10,
            accuracy=0.9 if passed else 0.5, passed=passed,
            ran_at=timezone.make_aware(datetime(2026, 10, day)))

    def test_its_own_newer_failure_takes_it_offline(self):
        self._ev("a#topic-v1", True, 1)
        self._ev("a#topic-v1", False, 2)
        self.assertEqual(passing_classifiers(), {})

    def test_falls_back_to_an_older_classifier_whose_own_latest_evaluation_passed(self):
        self._ev("old#topic-v1", True, 1)
        self._ev("a#topic-v1", True, 2)
        self._ev("a#topic-v1", False, 3)
        self.assertEqual(passing_classifiers(), {"ly": "old#topic-v1"})

    def test_passing_again_brings_it_back(self):
        self._ev("a#topic-v1", True, 1)
        self._ev("a#topic-v1", False, 2)
        self._ev("a#topic-v1", True, 3)
        self.assertEqual(passing_classifiers(), {"ly": "a#topic-v1"})


class StaleTextTests(TestCase):
    def test_a_result_for_text_that_changed_while_waiting_is_not_saved(self):
        """攔的 bug：等 GPU 的時候文章被重產，舊文字的分類被存到新內容上，之後也不會再重分。"""
        article = _article(one_liner="finance 舊的一句話")
        gpu = FakeTopicGpu()
        original_wait = gpu.wait

        def wait(job_id, timeout, poll_seconds=3.0, on_progress=None):
            Article.objects.filter(pk=article.pk).update(
                brief={"one_liner": "welfare 新的一句話", "key_numbers": [], "asks": []})
            return original_wait(job_id, timeout)

        gpu.wait = wait
        report = classify_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.stale), (0, 1))
        self.assertFalse(Topic.objects.exists())
        # 下一輪用新內容重分
        report = classify_topics(FakeTopicGpu(), limit=10)
        self.assertEqual(Topic.objects.get().primary, "welfare")


class EarlyFailureTests(TestCase):
    def test_a_run_whose_first_jobs_are_all_rejected_stops_early(self):
        """GPU 端還沒更新時每篇都回 422：送三篇都被拒就停，不要同一個錯誤重複兩百次。"""
        for _ in range(6):
            _article()
        gpu = FakeTopicGpu(answer=lambda text: RequestRejected("摘要 API 拒絕這個請求（422）：url Field required"))
        report = classify_topics(gpu, limit=10)
        self.assertEqual((report.failed, report.stopped), (3, True))
        self.assertIn("GPU 端可能還沒更新", str(report))

    def test_failures_after_a_success_do_not_stop_the_run(self):
        for one_liner in ("finance 一", "bad 二", "bad 三", "bad 四", "welfare 五"):
            _article(one_liner=one_liner)
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("壞") if "bad" in text else _code_in(text))
        report = classify_topics(gpu, limit=10)
        self.assertFalse(report.stopped)
        self.assertEqual(report.failed, 3)

    def test_an_aborted_evaluation_names_the_first_error(self):
        label = TopicLabel.objects.create(article=_article(), primary="finance")
        self.assertTrue(label.pk)
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("摘要 API 拒絕這個請求（422）：url Field required"))
        with self.assertRaises(EvaluationAborted) as caught:
            evaluate(gpu)
        self.assertIn("422", str(caught.exception))



class PermanentFailureTests(TestCase):
    """攔的 bug：每次都分類失敗的文章沒有 Topic、每一輪都排在最前面；把「工作跑了但失敗」也算進
    開頭三篇就停的規則，整個積壓與 --reclassify 就永遠動不了，log 還怪 GPU 沒更新。"""

    def test_articles_that_always_fail_do_not_block_the_rest(self):
        for _ in range(50):
            _article(one_liner="finance 舊的", day="2026-03-01")
        for _ in range(3):
            _article(one_liner="bad 這篇每次都失敗", day="2026-03-09")
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("SummarizerOutputInvalid") if "bad" in text
                           else _code_in(text))
        report = classify_topics(gpu, limit=200)
        self.assertFalse(report.stopped)
        self.assertEqual((report.classified, report.failed), (50, 3))

    def test_reclassify_is_not_blocked_by_articles_that_always_fail(self):
        old = [_article(one_liner="finance 舊的", day="2026-03-01") for _ in range(5)]
        for article in old:
            Topic.objects.create(article=article, primary="finance", classifier="old#topic-v0",
                                 labeled_at=timezone.now())
        for _ in range(3):
            _article(one_liner="bad 失敗", day="2026-03-09")
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("壞") if "bad" in text else _code_in(text))
        classify_topics(gpu, limit=200, reclassify=True)
        self.assertEqual(set(Topic.objects.values_list("classifier", flat=True)), {CLASSIFIER})


class LiveReevaluationTests(TestCase):
    """攔的 bug：重評現在上線的分類器時 GPU 中途出狀況，失敗算錯讓它掉到門檻以下，整個來源的
    議題區塊當場消失。"""

    def _labelled(self, n):
        for i in range(n):
            TopicLabel.objects.create(article=_article(one_liner=f"finance 第{i}篇"), primary="finance")

    def test_failures_while_reevaluating_the_live_classifier_do_not_take_it_offline(self):
        self._labelled(20)
        evaluate(FakeTopicGpu())
        self.assertEqual(passing_classifiers(), {"ly": CLASSIFIER})
        calls = {"n": 0}

        def flaky(text):
            calls["n"] += 1
            return JobFailed("ConnectError: Connection refused") if calls["n"] > 15 else _code_in(text)

        report = evaluate(FakeTopicGpu(answer=flaky))
        self.assertEqual(report.incomplete, {"ly": 5})
        self.assertEqual(passing_classifiers(), {"ly": CLASSIFIER})
        self.assertIn("這次成績不存", str(report))

    def test_failures_still_count_as_wrong_for_a_classifier_that_is_not_live(self):
        self._labelled(20)
        calls = {"n": 0}

        def flaky(text):
            calls["n"] += 1
            return JobFailed("壞") if calls["n"] > 15 else _code_in(text)

        report = evaluate(FakeTopicGpu(answer=flaky))
        [ev] = report.evaluations
        self.assertEqual((ev.correct, ev.passed), (15, False))


class EvalRecomputeTests(TestCase):
    def test_a_recompute_that_failed_is_retried_on_the_next_eval_run(self):
        """上一次 eval_topics 換了上線的分類器、但重算失敗（SD 卡上的 SQLite 被鎖住）：側寫的議題列
        還是舊的。下一次 eval_topics 上線的分類器沒變，也要發現議題列過期、重算。"""
        person = Person.objects.create(name="甲")
        Membership.objects.create(person=person, source="ly", name="甲")
        for i in range(20):
            TopicLabel.objects.create(article=_article(one_liner=f"finance 第{i}篇"), primary="finance")
        # 正式環境裡會期在匯入時就建好了
        from articles.profiles import assign_sessions
        assign_sessions()
        gpu = FakeTopicGpu()
        with mock.patch("articles.management.commands.eval_topics.GpuApiClient", return_value=gpu), \
                mock.patch("articles.management.commands.eval_topics.compute_profiles",
                           side_effect=OperationalError("database is locked")):
            with self.assertRaises(OperationalError):
                call_command("eval_topics", stdout=io.StringIO())
        self.assertFalse(ProfileStat.objects.exclude(classifier="").exists())
        out = io.StringIO()
        with mock.patch("articles.management.commands.eval_topics.GpuApiClient", return_value=FakeTopicGpu()):
            call_command("eval_topics", stdout=out)
        self.assertIn("已經重算人物側寫", out.getvalue())
        self.assertEqual(set(ProfileStat.objects.exclude(classifier="").values_list("classifier", flat=True)),
                         {CLASSIFIER})
