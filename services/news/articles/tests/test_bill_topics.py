"""議案分類（提案與質詢一致率用）：輸入、每晚分類與失敗處理、名稱變了要重分、抽樣、評估、上線條件、指令。

用假的 GPU 客戶端（同 test_topics），不碰網路。一致率本身在 test_chamber.py，標註頁在 test_admin.py。
"""
from __future__ import annotations

import io
import itertools
from datetime import date, datetime
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from articles import bill_topics
from articles.bill_topics import (BILL_TOPIC_MIN_LABELS, bill_input, classify_bill_topics, evaluate,
                                  live_alignment_classifiers, passing_classifier, sample_labels)
from articles.gpu_client import GpuApiError, JobFailed
from articles.ly_records import BillRecord, FetchedRecords, save
from articles.models import (BillTopic, BillTopicEvaluation, BillTopicLabel, LyBill, Membership, Person,
                             TopicEvaluation)
from articles.tests.test_topics import CLASSIFIER, FakeTopicGpu, _code_in
from articles.topics import TOPIC_TEXT_LIMIT, EvaluationAborted, labels_payload

NOW = timezone.make_aware(datetime(2026, 10, 3, 9, 30))
_ids = itertools.count(1)


def _legislators(*names):
    for name in names:
        Membership.objects.create(person=Person.objects.create(name=name), source="ly", name=name)


def _bill(name="finance 所得稅法部分條文修正草案", proposers=("甲",), session=5, term=11,
          day="2026-03-02"):
    """一件委員提案：名稱裡寫了假 GPU 要回答的領域代碼（見 test_topics.FakeTopicGpu）。"""
    n = next(_ids)
    return LyBill.objects.create(
        bill_no=f"B{n}", term=term, session_number=session, name=name, status="交付審查",
        proposers=list(proposers), cosigners=[], url=f"https://ppg.ly.gov.tw/ppg/bills/B{n}/details",
        proposed_on=date.fromisoformat(day) if day else None, synced_at=NOW)


class InputTests(SimpleTestCase):
    def test_the_bill_name_is_all_the_classifier_reads(self):
        self.assertEqual(bill_input("  所得稅法部分條文修正草案 "), "議案名稱：所得稅法部分條文修正草案")

    def test_a_referendum_text_over_the_limit_is_cut(self):
        text = bill_input("公投" * 3000)
        self.assertEqual(len(text), TOPIC_TEXT_LIMIT)
        self.assertTrue(text.startswith("議案名稱：公投"))


class ClassifiableTests(TestCase):
    def test_only_bills_a_legislator_proposed(self):
        keys = frozenset({"甲", "伍麗華SaidhaiTahovecahe"})
        self.assertTrue(bill_topics.classifiable("某法", ["台灣民眾黨立法院黨團", "甲"], keys))
        # 名字比對跟院內紀錄同一套：去空白、去間隔號
        self.assertTrue(bill_topics.classifiable("某法", ["伍麗華Saidhai‧Tahovecahe"], keys))
        self.assertFalse(bill_topics.classifiable("某法", ["台灣民眾黨立法院黨團"], keys))
        self.assertFalse(bill_topics.classifiable("某法", ["路人"], keys))
        self.assertFalse(bill_topics.classifiable("  ", ["甲"], keys))
        self.assertFalse(bill_topics.classifiable("某法", "甲", keys))


class ClassifyTests(TestCase):
    LOGGER = "articles.bill_topics"

    def setUp(self):
        _legislators("甲", "乙")

    def test_the_name_is_sent_with_the_twelve_areas_and_the_result_saved(self):
        bill = _bill("welfare 長期照顧服務法修正草案")
        gpu = FakeTopicGpu()
        report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.failed, report.remaining), (1, 0, 0))
        self.assertEqual(gpu.texts, ["議案名稱：welfare 長期照顧服務法修正草案"])
        self.assertEqual(gpu.labels, [labels_payload()])
        topic = BillTopic.objects.get()
        self.assertEqual((topic.bill_id, topic.primary, topic.secondary, topic.classifier),
                         (bill.id, "welfare", "", CLASSIFIER))
        self.assertIsNotNone(topic.labeled_at)
        self.assertIn("議案分類完成 1", str(report))

    def test_only_bills_with_a_legislator_among_the_proposers(self):
        mine = _bill(proposers=("台灣民眾黨立法院黨團", "甲"))
        _bill(proposers=("台灣民眾黨立法院黨團",))
        _bill(proposers=("不在名冊上的人",))
        gpu = FakeTopicGpu()
        report = classify_bill_topics(gpu, limit=10)
        self.assertEqual(list(BillTopic.objects.values_list("bill_id", flat=True)), [mine.id])
        self.assertEqual(report.remaining, 0)

    def test_newer_sessions_first_and_the_backlog_is_reported(self):
        old = _bill("finance 舊", session=3)
        newest = _bill("labor 新", session=5)
        middle = _bill("defense 中", session=4)
        gpu = FakeTopicGpu()
        report = classify_bill_topics(gpu, limit=2)
        self.assertEqual(gpu.texts, [bill_input(newest.name), bill_input(middle.name)])
        self.assertEqual((report.classified, report.remaining), (2, 1))
        self.assertIn("還沒分類 1", str(report))
        classify_bill_topics(FakeTopicGpu(), limit=2)
        self.assertTrue(BillTopic.objects.filter(bill=old).exists())

    def test_classified_bills_are_skipped_unless_reclassifying_and_then_the_oldest_go_first(self):
        bills = [_bill("finance 一"), _bill("labor 二"), _bill("welfare 三")]
        classify_bill_topics(FakeTopicGpu(), limit=10)
        gpu = FakeTopicGpu()
        classify_bill_topics(gpu, limit=10)
        self.assertEqual(gpu.texts, [])
        BillTopic.objects.filter(bill=bills[1]).update(labeled_at=timezone.make_aware(datetime(2026, 1, 1)))
        gpu = FakeTopicGpu(answer=lambda text: "local", classifier="new#topic-v2")
        report = classify_bill_topics(gpu, limit=1, reclassify=True)
        self.assertEqual((report.classified, gpu.texts), (1, [bill_input(bills[1].name)]))
        self.assertEqual(BillTopic.objects.get(bill=bills[1]).classifier, "new#topic-v2")

    def test_a_failed_classification_saves_nothing_and_does_not_stop_the_others(self):
        _bill("壞的")
        _bill("finance 好的")
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("模型答非所問") if "壞的" in text else _code_in(text))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.failed, report.stopped, report.remaining),
                         (1, 1, False, 1))
        self.assertIn("模型答非所問", report.errors[0])
        self.assertEqual(BillTopic.objects.get().primary, "finance")

    def test_an_answer_outside_the_list_is_a_failure_not_a_guess(self):
        _bill()
        gpu = FakeTopicGpu(answer=lambda text: {"primary": "太空", "classifier": CLASSIFIER})
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_bill_topics(gpu, limit=10)
        self.assertEqual(report.failed, 1)
        self.assertFalse(BillTopic.objects.exists())

    def test_a_bill_that_cannot_be_saved_does_not_stop_the_others(self):
        """SD 卡上的 SQLite 鎖住一下：那一件算失敗，剩下的照分。"""
        first = _bill("finance 一", session=5)
        second = _bill("labor 二", session=4)
        save_topic = BillTopic.objects.update_or_create

        def flaky(bill_id, defaults):
            if bill_id == first.id:
                raise OperationalError("database is locked")
            return save_topic(bill_id=bill_id, defaults=defaults)

        with mock.patch.object(BillTopic.objects, "update_or_create", side_effect=flaky), \
                self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_bill_topics(FakeTopicGpu(), limit=10)
        self.assertEqual((report.classified, report.failed, report.stopped), (1, 1, False))
        self.assertIn("database is locked", report.errors[0])
        self.assertEqual(list(BillTopic.objects.values_list("bill_id", flat=True)), [second.id])

    def test_an_unavailable_gpu_stops_the_whole_round(self):
        for _ in range(3):
            _bill()
        gpu = FakeTopicGpu(answer=lambda text: GpuApiError("連線被拒"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_bill_topics(gpu, limit=10)
        self.assertEqual(len(gpu.texts), 1)
        self.assertTrue(report.stopped)
        self.assertEqual((report.failed, report.remaining), (1, 3))
        self.assertIn("GPU 不可用", str(report))

    def test_a_run_whose_first_bills_are_all_rejected_stops_early(self):
        """GPU 端還沒更新時每件都回 422：送三件都被拒就停。"""
        for _ in range(6):
            _bill()
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("摘要 API 拒絕這個請求（422）"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((report.failed, report.stopped, len(gpu.texts)), (3, True, 3))
        self.assertIn("GPU 端可能還沒更新", str(report))

    def test_failures_after_a_success_do_not_stop_the_run(self):
        for name in ("finance 一", "bad 二", "bad 三", "bad 四", "welfare 五"):
            _bill(name)
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("壞") if "bad" in text else _code_in(text))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((report.stopped, report.failed, report.classified), (False, 3, 2))


class RenamedBillTests(TestCase):
    """攔的 bug：議案名稱在同步時更正了，舊名稱的分類還掛在新名稱上，之後也不會再重分。"""

    def setUp(self):
        _legislators("甲")

    def _record(self, bill: LyBill, name: str) -> BillRecord:
        return BillRecord(bill_no=bill.bill_no, term=11, session_number=5, name=name, status="交付審查",
                          proposers=("甲",), cosigners=(), url=bill.url, proposed_on=date(2026, 3, 2))

    def test_a_renamed_bill_loses_its_topic_but_keeps_its_label(self):
        renamed, same = _bill("finance 所得稅法"), _bill("labor 勞基法")
        classify_bill_topics(FakeTopicGpu(), limit=10)
        BillTopicLabel.objects.create(bill=renamed, primary="finance")
        report = save(FetchedRecords(term=11, session=None, bills=[
            self._record(renamed, "welfare 長照法"), self._record(same, same.name)]), now=NOW)
        self.assertEqual(list(BillTopic.objects.values_list("bill_id", flat=True)), [same.id])
        self.assertEqual(BillTopicLabel.objects.get().primary, "finance")
        self.assertEqual(report.renamed_bills, 1)
        self.assertIn("議案名稱改了的 1 件", str(report))
        # 下一輪用新名稱重分
        gpu = FakeTopicGpu()
        classify_bill_topics(gpu, limit=10)
        self.assertEqual(gpu.texts, ["議案名稱：welfare 長照法"])
        self.assertEqual(BillTopic.objects.get(bill=renamed).primary, "welfare")

    def test_forgetting_many_bills_goes_in_chunks(self):
        """一屆七千多件：SQLite 的綁定參數有上限，一次 DELETE 只帶 _ID_CHUNK 個 id。"""
        bills = [_bill() for _ in range(5)]
        for bill in bills:
            BillTopic.objects.create(bill=bill, primary="finance", classifier=CLASSIFIER, labeled_at=NOW)
        with mock.patch.object(bill_topics, "_ID_CHUNK", 2), CaptureQueriesContext(connection) as ctx:
            self.assertEqual(bill_topics.forget(b.id for b in bills[:4]), 4)
        deletes = [q for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("DELETE")]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(list(BillTopic.objects.values_list("bill_id", flat=True)), [bills[4].id])

    def test_a_result_for_a_name_that_changed_while_waiting_is_not_saved(self):
        bill = _bill("finance 舊的名稱")
        gpu = FakeTopicGpu()
        original_wait = gpu.wait

        def wait(job_id, timeout, poll_seconds=3.0, on_progress=None):
            LyBill.objects.filter(pk=bill.pk).update(name="welfare 新的名稱")
            return original_wait(job_id, timeout)

        gpu.wait = wait
        report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.stale), (0, 1))
        self.assertIn("內容剛被改過不存 1", str(report))
        self.assertFalse(BillTopic.objects.exists())
        classify_bill_topics(FakeTopicGpu(), limit=10)
        self.assertEqual(BillTopic.objects.get().primary, "welfare")

    def test_a_bill_removed_while_waiting_is_not_saved(self):
        bill = _bill()
        gpu = FakeTopicGpu()
        original_wait = gpu.wait

        def wait(job_id, timeout, poll_seconds=3.0, on_progress=None):
            LyBill.objects.filter(pk=bill.pk).delete()
            return original_wait(job_id, timeout)

        gpu.wait = wait
        report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((report.classified, report.stale, report.failed), (0, 1, 0))
        self.assertFalse(BillTopic.objects.exists())


class SampleTests(TestCase):
    def setUp(self):
        _legislators("甲")
        for _ in range(30):
            _bill()
        # 黨團提案與名冊上沒有的人提的不會被抽到：它們不會被分類
        _bill(proposers=("台灣民眾黨立法院黨團",))
        _bill(proposers=("路人",))

    def _drawn(self):
        return set(BillTopicLabel.objects.values_list("bill_id", flat=True))

    def test_n_blank_labels_from_the_bills_that_get_classified(self):
        report = sample_labels(count=20, seed=0)
        self.assertEqual((report.added, report.total, report.available), (20, 20, 30))
        self.assertEqual(set(BillTopicLabel.objects.values_list("primary", flat=True)), {""})
        mine = {bill.id for bill in LyBill.objects.all() if bill.proposers == ["甲"]}
        self.assertTrue(self._drawn() <= mine)
        self.assertIn("新抽 20 件，議案標註集共 20 件", str(report))

    def test_the_same_seed_draws_the_same_bills(self):
        sample_labels(count=10, seed=0)
        first = self._drawn()
        BillTopicLabel.objects.all().delete()
        sample_labels(count=10, seed=0)
        self.assertEqual(self._drawn(), first)
        BillTopicLabel.objects.all().delete()
        sample_labels(count=10, seed=1)
        self.assertNotEqual(self._drawn(), first)

    def test_running_again_only_tops_up_and_never_redraws(self):
        sample_labels(count=5)
        first = self._drawn()
        labelled = BillTopicLabel.objects.first()
        labelled.primary = "finance"
        labelled.save()
        report = sample_labels(count=12)
        self.assertEqual((report.added, report.total), (7, 12))
        self.assertTrue(first <= self._drawn())
        labelled.refresh_from_db()
        self.assertEqual(labelled.primary, "finance")
        self.assertEqual(sample_labels(count=12).added, 0)


class EvaluateTests(TestCase):
    LOGGER = "articles.bill_topics"

    def _labelled(self, count, human="finance", model="finance"):
        made = []
        for _ in range(count):
            bill = _bill(f"{model} 某法修正草案")
            BillTopicLabel.objects.create(bill=bill, primary=human)
            made.append(bill)
        return made

    def test_accuracy_is_matches_over_labels_and_16_of_20_passes(self):
        self._labelled(16)
        wrong = self._labelled(4, human="finance", model="defense")
        report = evaluate(FakeTopicGpu(), now=NOW)
        ev = BillTopicEvaluation.objects.get()
        self.assertEqual((ev.classifier, ev.labeled, ev.correct, ev.accuracy, ev.passed, ev.ran_at),
                         (CLASSIFIER, 20, 16, 0.8, True, NOW))
        self.assertEqual(ev.mistakes[0], {"bill": wrong[0].id, "bill_no": wrong[0].bill_no,
                                          "name": wrong[0].name, "human": "finance", "model": "defense"})
        text = str(report)
        self.assertIn("準確率 80.0%：通過", text)
        self.assertIn(f"判錯：{wrong[0].bill_no} defense 某法修正草案：人工「財政經濟」、模型「國防外交」", text)

    def test_the_names_are_what_is_sent(self):
        labelled = self._labelled(2)
        BillTopicLabel.objects.create(bill=_bill("labor 還沒標"))
        gpu = FakeTopicGpu()
        evaluate(gpu)
        self.assertEqual(gpu.texts, [bill_input(b.name) for b in labelled])

    def test_below_the_threshold_or_too_few_labels_do_not_pass(self):
        self._labelled(15)
        self._labelled(5, model="defense")
        report = evaluate(FakeTopicGpu())
        self.assertEqual((report.evaluation.accuracy, report.evaluation.passed), (0.75, False))
        self.assertIn("未通過（準確率未達 80%）", str(report))
        BillTopicLabel.objects.all().delete()
        self._labelled(BILL_TOPIC_MIN_LABELS - 1)
        report = evaluate(FakeTopicGpu())
        self.assertEqual((report.evaluation.accuracy, report.evaluation.passed), (1.0, False))
        self.assertIn("標註不足 20 件", str(report))

    def test_a_failed_classification_counts_as_wrong(self):
        self._labelled(19)
        broken = self._labelled(1, model="壞的")[0]
        gpu = FakeTopicGpu(answer=lambda text: JobFailed("JSON 解析失敗") if "壞的" in text
                           else _code_in(text))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = evaluate(gpu)
        ev = report.evaluation
        self.assertEqual((ev.labeled, ev.correct), (20, 19))
        self.assertEqual(ev.mistakes, [{"bill": broken.id, "bill_no": broken.bill_no, "name": broken.name,
                                        "human": "finance", "model": None, "error": "JSON 解析失敗"}])
        self.assertIn("模型「分類失敗：JSON 解析失敗」", str(report))

    def test_a_classifier_change_midway_aborts_and_saves_nothing(self):
        self._labelled(10)
        gpu = FakeTopicGpu(classifier=lambda text: "old#topic-v1" if len(gpu.texts) < 5 else "new#topic-v1")
        with self.assertRaises(EvaluationAborted) as ctx:
            evaluate(gpu)
        self.assertIn("中途換了模型", str(ctx.exception))
        self.assertEqual(len(gpu.texts), 5)
        self.assertFalse(BillTopicEvaluation.objects.exists())

    def test_an_unavailable_gpu_or_nothing_labelled_saves_nothing(self):
        with self.assertRaises(EvaluationAborted):
            evaluate(FakeTopicGpu())
        self._labelled(3)
        with self.assertRaises(GpuApiError):
            evaluate(FakeTopicGpu(answer=lambda text: GpuApiError("連線被拒")))
        with self.assertLogs(self.LOGGER, "WARNING"), self.assertRaises(EvaluationAborted) as ctx:
            evaluate(FakeTopicGpu(answer=lambda text: JobFailed("回 422")))
        self.assertIn("422", str(ctx.exception))
        self.assertFalse(BillTopicEvaluation.objects.exists())

    def test_labels_whose_bill_name_became_blank_are_skipped_and_reported(self):
        """LYAPI 把名稱改成空的：沒有輸入可以分類，不送 GPU、不進分母，報告講出略過幾件。"""
        kept = self._labelled(2)
        blank = self._labelled(1)[0]
        LyBill.objects.filter(pk=blank.pk).update(name="  ")
        gpu = FakeTopicGpu()
        report = evaluate(gpu)
        self.assertEqual(gpu.texts, [bill_input(b.name) for b in kept])
        self.assertEqual((report.skipped, report.evaluation.labeled), (1, 2))
        self.assertIn("略過 1 件已標註、但議案名稱是空的", str(report))
        # 全都是空的：中止，訊息要講出原因，不是「還沒標」
        LyBill.objects.filter(pk__in=[b.pk for b in kept]).update(name="")
        with self.assertRaises(EvaluationAborted) as ctx:
            evaluate(FakeTopicGpu())
        self.assertIn("3 件議案名稱都是空的", str(ctx.exception))

    def test_evaluating_does_not_touch_the_bills_topics(self):
        bill = self._labelled(1)[0]
        BillTopic.objects.create(bill=bill, primary="labor", classifier="old#topic-v1", labeled_at=NOW)
        evaluate(FakeTopicGpu())
        self.assertEqual(BillTopic.objects.get().primary, "labor")


def _bill_ev(classifier, passed, day):
    return BillTopicEvaluation.objects.create(
        classifier=classifier, labeled=20, correct=18 if passed else 10, accuracy=0.9 if passed else 0.5,
        passed=passed, ran_at=timezone.make_aware(datetime(2026, 10, day)))


def _topic_ev(classifier, passed, day, source="ly"):
    return TopicEvaluation.objects.create(
        source=source, classifier=classifier, labeled=20, correct=18 if passed else 10,
        accuracy=0.9 if passed else 0.5, passed=passed, ran_at=timezone.make_aware(datetime(2026, 10, day)))


class GateTests(TestCase):
    def test_the_latest_passing_evaluation_wins_and_a_later_failure_of_another_does_not_revoke_it(self):
        self.assertIsNone(passing_classifier())
        _bill_ev("a#topic-v1", True, 1)
        _bill_ev("b#topic-v1", True, 2)
        _bill_ev("c#topic-v1", False, 3)
        self.assertEqual(passing_classifier(), "b#topic-v1")

    def test_its_own_newer_failure_takes_it_offline_and_falls_back(self):
        _bill_ev("old#topic-v1", True, 1)
        _bill_ev("a#topic-v1", True, 2)
        _bill_ev("a#topic-v1", False, 3)
        self.assertEqual(passing_classifier(), "old#topic-v1")
        _bill_ev("old#topic-v1", False, 4)
        self.assertIsNone(passing_classifier())

    def test_alignment_needs_both_gates_and_the_names_may_differ(self):
        self.assertIsNone(live_alignment_classifiers())
        _bill_ev("bill#topic-v1", True, 1)
        self.assertIsNone(live_alignment_classifiers())
        # 只有臺中的議題分類通過不算：質詢是立法院的
        _topic_ev("speech#topic-v1", True, 1, source="tccc")
        self.assertIsNone(live_alignment_classifiers())
        _topic_ev("speech#topic-v1", True, 1)
        self.assertEqual(live_alignment_classifiers(), ("bill#topic-v1", "speech#topic-v1"))
        self.assertEqual(bill_topics.alignment_stamp(*live_alignment_classifiers()),
                         "bill#topic-v1｜speech#topic-v1")
        BillTopicEvaluation.objects.all().delete()
        self.assertIsNone(live_alignment_classifiers())


class CommandTests(TestCase):
    def setUp(self):
        _legislators("甲")

    def _gpu(self, module, gpu):
        return mock.patch(f"articles.management.commands.{module}.GpuApiClient", return_value=gpu)

    def test_classify_bill_topics_respects_limit_and_reclassify(self):
        # 名稱各不相同：同名的議案只送一次 GPU（見 SameNameTests）
        for i in range(3):
            _bill(name=f"finance 所得稅法第{i + 1}條修正草案")
        out = io.StringIO()
        with self._gpu("classify_bill_topics", FakeTopicGpu()):
            call_command("classify_bill_topics", limit=2, stdout=out)
        self.assertIn("議案分類完成 2、失敗 0、還沒分類 1", out.getvalue())
        gpu = FakeTopicGpu(answer=lambda text: "labor")
        with self._gpu("classify_bill_topics", gpu):
            call_command("classify_bill_topics", "--reclassify", "--limit", "10", stdout=io.StringIO())
        self.assertEqual(len(gpu.texts), 3)
        self.assertEqual(set(BillTopic.objects.values_list("primary", flat=True)), {"labor"})

    def test_sample_bill_topic_labels(self):
        for _ in range(4):
            _bill()
        out = io.StringIO()
        call_command("sample_bill_topic_labels", "--count", "3", "--seed", "5", stdout=out)
        self.assertEqual(BillTopicLabel.objects.count(), 3)
        self.assertIn("新抽 3 件", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("sample_bill_topic_labels", "--count", "0", stdout=io.StringIO())

    def test_eval_bill_topics_recomputes_profiles_when_the_gate_changes(self):
        for _ in range(BILL_TOPIC_MIN_LABELS):
            BillTopicLabel.objects.create(bill=_bill(), primary="finance")
        out = io.StringIO()
        with self._gpu("eval_bill_topics", FakeTopicGpu()), \
                mock.patch("articles.management.commands.eval_bill_topics.compute_profiles") as recompute:
            call_command("eval_bill_topics", stdout=out)
        self.assertIn("準確率 100.0%：通過", out.getvalue())
        self.assertIn("已經重算人物側寫", out.getvalue())
        recompute.assert_called_once()
        # 同一個分類器再評一次：上線的版本沒變，不必重算
        out = io.StringIO()
        with self._gpu("eval_bill_topics", FakeTopicGpu()), \
                mock.patch("articles.management.commands.eval_bill_topics.compute_profiles") as recompute:
            call_command("eval_bill_topics", stdout=out)
        self.assertNotIn("重算", out.getvalue())
        recompute.assert_not_called()

    def test_eval_bill_topics_turns_an_abort_into_a_command_error(self):
        with self._gpu("eval_bill_topics", FakeTopicGpu()), self.assertRaises(CommandError) as ctx:
            call_command("eval_bill_topics", stdout=io.StringIO())
        self.assertIn("什麼都沒存", str(ctx.exception))
        BillTopicLabel.objects.create(bill=_bill(), primary="finance")
        with self._gpu("eval_bill_topics", FakeTopicGpu(answer=lambda text: GpuApiError("連線被拒"))), \
                self.assertRaises(CommandError):
            call_command("eval_bill_topics", stdout=io.StringIO())
        self.assertFalse(BillTopicEvaluation.objects.exists())


class SameNameTests(TestCase):
    """攔的效率問題：第 11 屆七千多件委員提案只有兩千多種名稱，每件各送一次 GPU，三分之二是重複的。"""

    def setUp(self):
        _legislators("甲", "乙")

    def test_bills_with_the_same_name_are_classified_once(self):
        same = [_bill(name="finance 所得稅法第十七條條文修正草案", proposers=(p,)) for p in ("甲", "乙", "甲")]
        _bill(name="welfare 長期照顧服務法修正草案")
        gpu = FakeTopicGpu()
        report = classify_bill_topics(gpu, limit=10)
        self.assertEqual(len(gpu.texts), 2)
        self.assertEqual((report.classified, report.reused), (4, 2))
        self.assertEqual({BillTopic.objects.get(bill=b).primary for b in same}, {"finance"})

    def test_the_limit_counts_distinct_names_sent_to_the_gpu(self):
        # 新的先分：最後建的三件同名議案排在最前面，佔掉一個名額
        _bill(name="welfare 另一部法")
        _bill(name="labor 第三部法")
        for _ in range(3):
            _bill(name="finance 同一部法的修正草案")
        gpu = FakeTopicGpu()
        classify_bill_topics(gpu, limit=2)
        self.assertEqual(len(gpu.texts), 2)
        self.assertEqual(BillTopic.objects.count(), 4)

    def test_a_name_classified_on_an_earlier_night_is_reused(self):
        _bill(name="finance 所得稅法修正草案")
        classify_bill_topics(FakeTopicGpu(), limit=10)
        later = _bill(name="finance 所得稅法修正草案", proposers=("乙",))
        gpu = FakeTopicGpu()
        report = classify_bill_topics(gpu, limit=10)
        self.assertEqual((len(gpu.texts), report.reused), (0, 1))
        self.assertEqual(BillTopic.objects.get(bill=later).classifier, CLASSIFIER)

    def test_reclassify_sends_each_name_again_but_still_only_once(self):
        for _ in range(3):
            _bill(name="finance 所得稅法修正草案")
        classify_bill_topics(FakeTopicGpu(), limit=10)
        gpu = FakeTopicGpu(classifier="new-model#topic-v2")
        classify_bill_topics(gpu, limit=10, reclassify=True)
        self.assertEqual(len(gpu.texts), 1)
        self.assertEqual(set(BillTopic.objects.values_list("classifier", flat=True)), {"new-model#topic-v2"})
