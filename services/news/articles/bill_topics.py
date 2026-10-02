"""人物側寫：提案與質詢一致率要用的議案分類。輸入、每晚分類、標註集與評估、上線條件。

設計見 docs/superpowers/specs/2026-10-03-profile-proposal-alignment-design.md。一致率比的是「他主提案
的領域分布」與「他質詢的領域分布」，所以要替**委員提案**分政策領域。分類器跟議題分布是同一個 GPU 的
`topic` 工作與同一份領域清單（topics.TOPICS），但它只在質詢摘要上評估過：議案名稱短、寫法也不同
（「○○法部分條文修正草案」），在摘要上準不代表在名稱上準。所以議案另有自己的標註集與門檻，做法照
topics.py：

- 分類（classify_bill_topics）：送「議案名稱：<議案名稱>」，存成 BillTopic，記下分類器版本。
- 評估（evaluate）：已標註的議案用現在的分類器重分一次，存成 BillTopicEvaluation。
- 上線條件（passing_evaluation）：每個分類器只看自己最新的一次評估，最新那次通過的裡取最晚的。
- 一致率要**兩道門檻都過**：立法院的議題分類（質詢摘要）與這裡的議案分類（live_alignment_classifiers）。

一致率本身（重疊公式、最小樣本、同儕）在 chamber.py：它是院內紀錄區塊的一張卡。
"""
from __future__ import annotations

import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import topics
from .gpu_client import GpuApiClient, GpuApiError, JobFailed
from .ly_records import name_key
from .models import ArticleSource, BillTopic, BillTopicEvaluation, BillTopicLabel, LyBill, Membership
from .topics import TOPIC_BY_KEY, EvaluationAborted, TopicResult

logger = logging.getLogger(__name__)

# 上線門檻：跟議題分布一樣，標註至少 20 件、主領域準確率至少 80%
BILL_TOPIC_MIN_LABELS = 20
BILL_TOPIC_MIN_ACCURACY = 0.8

# ProfileStat.classifier 記「議案分類器｜質詢分類器」：兩個名字都要對得上現在上線的版本，API 才給卡片
ALIGNMENT_SEPARATOR = "｜"

# 開頭連續失敗幾件就停（同 topics.classify_topics）
_EARLY_FAILURES = 3
# 一次 DELETE 最多帶幾個 id。SQLite 的綁定參數有上限，舊版是 999
_ID_CHUNK = 500


# --- 分類的輸入與對象 ---


def bill_input(name: str) -> str:
    """分類器讀到的文字：「議案名稱：<議案名稱>」。

    不送案由：議案清單裡沒有，要逐件向 LYAPI 查（一屆七千多件）。admin 的標註頁也用這個函式顯示——
    標的人跟模型讀的是同一段文字，評估才公平。公投案的主文整段都在名稱裡，超過 GPU 的上限就截掉。
    """
    return f"議案名稱：{(name or '').strip()}"[:topics.TOPIC_TEXT_LIMIT]


def legislator_keys() -> frozenset[str]:
    """立法院所有任期的名字（比對用的 name_key）。一次讀完，在記憶體裡比對每件議案的提案人。"""
    names = Membership.objects.filter(source=ArticleSource.LY).values_list("name", flat=True)
    return frozenset(name_key(name) for name in names)


def classifiable(name: str, proposers: object, keys: frozenset[str]) -> bool:
    """只分「有立委是主提案人」的委員提案，而且要有名稱可讀。

    黨團提案的提案人是黨團（「台灣民眾黨立法院黨團」），算不到任何一個人的一致率裡，分了也用不到。
    提案人要對得到立法院的任期，跟 chamber 把議案算給誰同一套名字比對（去空白、去間隔號）。
    """
    if not (name or "").strip() or not isinstance(proposers, list):
        return False
    return any(isinstance(p, str) and name_key(p) in keys for p in proposers)


# --- 每晚分類 ---


@dataclass
class BillClassifyReport(topics.ClassifyReport):
    """跟議題分類同一個形狀，報告前面多「議案」兩個字：排程的 log 裡兩種分類才分得出來。"""

    def __str__(self) -> str:
        return "議案" + super().__str__()


def classify_bill_topics(client: GpuApiClient, limit: int, reclassify: bool = False,
                         timeout: float | None = None) -> BillClassifyReport:
    """替有立委主提案的委員提案分政策領域，一次一件，新的會期先。

    預設只分還沒有 BillTopic 的；reclassify=True 連已經有的也重分（換模型或提示詞之後用），
    沒有的先、再來是分得最久的。

    失敗處理同 topics.classify_topics：失敗只記 log；GPU 連不上整輪停；開頭連續三件被拒也停（GPU 端
    多半還沒更新）；等 GPU 的時候議案名稱被同步改掉了，結果不存、下一輪重分。
    """
    timeout = timeout or settings.GPU_JOB_TIMEOUT_SECONDS
    keys = legislator_keys()
    report = BillClassifyReport()
    for bill_id, bill_no, name in _work(keys, limit, reclassify):
        try:
            text = bill_input(name)
            result = topics.classify_text(client, text, timeout)
            # 等 GPU 的這段時間裡，每週的同步可能改了名稱（ly_records 會刪掉 BillTopic）或刪了這件：
            # 存之前重讀一次，不一樣就不存，否則舊名稱的分類會掛在新名稱上、而且之後不會再重分
            if _current_input(bill_id) != text:
                report.stale += 1
                continue
            # 存檔也放在 try 裡：SD 卡上的 SQLite 偶爾會鎖住，一件存不進去不該讓剩下的整晚都不分
            BillTopic.objects.update_or_create(bill_id=bill_id, defaults={
                "primary": result.primary, "secondary": result.secondary,
                "classifier": result.classifier, "labeled_at": timezone.now()})
        except GpuApiError as e:
            _record_failure(bill_no, e, report)
            report.stopped = True
            logger.warning("GPU 不可用，議案分類這一輪提前結束")
            break
        except JobFailed as e:
            _record_failure(bill_no, e, report)
            if report.classified == 0 and report.failed >= _EARLY_FAILURES:
                report.stopped = True
                report.stop_reason = f"開頭 {report.failed} 件都被 GPU 拒絕，GPU 端可能還沒更新"
                logger.warning("議案分類：%s", report.stop_reason)
                break
            continue
        except Exception as e:  # noqa: BLE001
            logger.exception("議案 %s 分類時發生預期外的錯誤", bill_no)
            _record_failure(bill_no, e, report)
            continue
        report.classified += 1
    report.remaining = _unclassified(keys)
    return report


def _work(keys: frozenset[str], limit: int, reclassify: bool) -> list[tuple[int, str, str]]:
    """這一輪要分的 (id, 議案編號, 名稱)，最多 limit 件。

    「有立委主提案」要比對 JSON 欄位裡的名字，只能在 Python 裡篩：一屆七千多件，讀一次只要幾個欄位。
    不用 id__in 把篩完的帶回資料庫——SQLite 的綁定參數有上限。
    """
    queryset = LyBill.objects.all()
    if not reclassify:
        queryset = queryset.filter(topic__isnull=True)
    rows = queryset.order_by(F("topic__labeled_at").asc(nulls_first=True), "-term", "-session_number",
                             F("proposed_on").desc(nulls_last=True), "-id").values_list(
        "id", "bill_no", "name", "proposers")
    work: list[tuple[int, str, str]] = []
    for bill_id, bill_no, name, proposers in rows:
        if len(work) >= max(0, limit):
            break
        if classifiable(name, proposers, keys):
            work.append((bill_id, bill_no, name))
    return work


def _current_input(bill_id: int) -> str | None:
    name = LyBill.objects.filter(pk=bill_id).values_list("name", flat=True).first()
    return None if name is None else bill_input(name)


def _unclassified(keys: frozenset[str]) -> int:
    """還沒有 BillTopic、但該分的委員提案：積壓要講出來，否則一晚 200 件的上限會無聲地一直排著。"""
    rows = LyBill.objects.filter(topic__isnull=True).values_list("name", "proposers")
    return sum(1 for name, proposers in rows if classifiable(name, proposers, keys))


def _record_failure(bill_no: str, error: Exception, report: BillClassifyReport) -> None:
    report.failed += 1
    report.errors.append(f"{bill_no}: {error}")
    logger.warning("議案 %s 分類失敗：%s", bill_no, error)


def forget(bill_ids: Iterable[int]) -> int:
    """刪掉這幾件的議案分類，讓下一輪重分（名稱變了：分類讀的就是名稱）。人工標註不刪。"""
    ids = list(bill_ids)
    for start in range(0, len(ids), _ID_CHUNK):
        BillTopic.objects.filter(bill_id__in=ids[start:start + _ID_CHUNK]).delete()
    return len(ids)


# --- 標註集 ---


@dataclass
class SampleReport:
    added: int = 0
    total: int = 0
    # 這次抽之前、還沒抽過而且該分的委員提案件數
    available: int = 0

    def __str__(self) -> str:
        return (f"新抽 {self.added} 件，議案標註集共 {self.total} 件"
                f"（還有 {self.available - self.added} 件委員提案可抽）")


def sample_labels(count: int = BILL_TOPIC_MIN_LABELS, seed: int = 0) -> SampleReport:
    """從已同步、有立委主提案的委員提案隨機抽，建立空白的 BillTopicLabel（之後在 admin 標）。

    只從「會被分類的那一批」抽：評估量的是分類器在它實際要分的議案上準不準。已經抽過的不重抽，
    只補到 count 件——標到一半再跑一次，不會把標好的換掉。固定種子、候選照 id 排：同樣的資料、
    同樣的種子就抽到同樣的議案。
    """
    keys = legislator_keys()
    report = SampleReport(total=BillTopicLabel.objects.count())
    candidates = [bill_id for bill_id, name, proposers in
                  LyBill.objects.filter(topic_label__isnull=True).order_by("id")
                  .values_list("id", "name", "proposers") if classifiable(name, proposers, keys)]
    report.available = len(candidates)
    need = count - report.total
    if need <= 0:
        return report
    picked = random.Random(f"bill:{seed}").sample(candidates, min(need, len(candidates)))
    # 照 id 建：admin 照議案排，抽的順序不會變成線索
    BillTopicLabel.objects.bulk_create([BillTopicLabel(bill_id=i) for i in sorted(picked)])
    report.added = len(picked)
    report.total += len(picked)
    return report


# --- 評估 ---


@dataclass
class EvaluationReport:
    evaluation: BillTopicEvaluation | None = None
    # 已標註、但議案名稱已經是空的（LYAPI 改壞了）：沒有輸入可以分類
    skipped: int = 0

    def __str__(self) -> str:
        lines = []
        ev = self.evaluation
        if ev is not None:
            verdict = "通過" if ev.passed else "未通過"
            if not ev.passed and ev.labeled < BILL_TOPIC_MIN_LABELS:
                verdict += f"（標註不足 {BILL_TOPIC_MIN_LABELS} 件）"
            elif not ev.passed:
                verdict += f"（準確率未達 {BILL_TOPIC_MIN_ACCURACY:.0%}）"
            lines.append(f"議案分類器 {ev.classifier}：已標註 {ev.labeled} 件、主領域相同 {ev.correct} 件，"
                         f"準確率 {ev.accuracy * 100:.1f}%：{verdict}")
            for m in ev.mistakes:
                lines.append(f"  判錯：{m['bill_no']} {m['name'][:40]}：人工「{_label(m['human'])}」、"
                             f"模型「{_model_label(m)}」")
        if self.skipped:
            lines.append(f"略過 {self.skipped} 件已標註、但議案名稱是空的")
        return "\n".join(lines)


def _label(key: str) -> str:
    return TOPIC_BY_KEY[key].label if key in TOPIC_BY_KEY else key


def _model_label(mistake: dict) -> str:
    if mistake.get("model") is None:
        return f"分類失敗：{mistake.get('error') or '未知'}"
    return _label(mistake["model"])


_Outcome = tuple[BillTopicLabel, TopicResult | None, str]


def evaluate(client: GpuApiClient, timeout: float | None = None,
             now: datetime | None = None) -> EvaluationReport:
    """把已標註的議案用現在的分類器重分一次，比對主領域，存一筆評估。

    準確率 = 主領域相同的件數 ÷ 已標註件數；分類失敗的那件算錯（分母照算）。通過 = 已標註至少
    20 件、準確率至少 80%。一次評估裡 GPU 回來的分類器必須都一樣，不一樣就整次中止、什麼都不存；
    GPU 連不上（GpuApiError）也直接冒出去、什麼都不存（同 topics.evaluate）。
    """
    timeout = timeout or settings.GPU_JOB_TIMEOUT_SECONDS
    labelled = list(BillTopicLabel.objects.exclude(primary="").select_related("bill").order_by("bill_id"))
    usable = [label for label in labelled if label.bill.name.strip()]
    report = EvaluationReport(skipped=len(labelled) - len(usable))
    if not usable:
        # 中止時報告印不出來：有標註、只是名稱全是空的，要在訊息裡講，不然看起來像「還沒標」
        blank = f"（已標註的 {report.skipped} 件議案名稱都是空的）" if report.skipped else ""
        raise EvaluationAborted(f"還沒有任何可評估的已標註議案{blank}：先跑 sample_bill_topic_labels，"
                                "再到 admin 標註")

    outcomes: list[_Outcome] = []
    classifiers: set[str] = set()
    for label in usable:
        try:
            result = topics.classify_text(client, bill_input(label.bill.name), timeout)
        except JobFailed as e:
            # 記下來：全部失敗時中止訊息要能說出原因（例如 GPU 端還沒更新、回 422）
            logger.warning("評估：議案 %s 分類失敗：%s", label.bill.bill_no, e)
            outcomes.append((label, None, str(e)[:200]))
            continue
        classifiers.add(result.classifier)
        if len(classifiers) > 1:
            raise EvaluationAborted(
                f"這次評估裡 GPU 回來的分類器不一樣（{'、'.join(sorted(classifiers))}），"
                "中途換了模型或提示詞；整次不算，請再跑一次")
        outcomes.append((label, result, ""))
    if not classifiers:
        first = next((error for _, _, error in outcomes if error), "")
        raise EvaluationAborted(f"沒有任何一件分類成功，無法評估。第一個錯誤：{first}"
                                "（GPU 端還沒更新的話，會是 422）")
    with transaction.atomic():
        report.evaluation = _save_evaluation(classifiers.pop(), outcomes, now or timezone.now())
    return report


def _save_evaluation(classifier: str, outcomes: list[_Outcome], ran_at: datetime) -> BillTopicEvaluation:
    mistakes = []
    for label, result, error in outcomes:
        if result is not None and result.primary == label.primary:
            continue
        mistake = {"bill": label.bill_id, "bill_no": label.bill.bill_no, "name": label.bill.name[:200],
                   "human": label.primary, "model": result.primary if result else None}
        if error:
            mistake["error"] = error
        mistakes.append(mistake)
    labeled = len(outcomes)
    correct = labeled - len(mistakes)
    accuracy = correct / labeled
    return BillTopicEvaluation.objects.create(
        classifier=classifier, labeled=labeled, correct=correct, accuracy=accuracy,
        passed=labeled >= BILL_TOPIC_MIN_LABELS and accuracy >= BILL_TOPIC_MIN_ACCURACY,
        mistakes=mistakes, ran_at=ran_at)


# --- 上線條件 ---


def passing_evaluation() -> BillTopicEvaluation | None:
    """上線的那筆評估；None 是沒有通過的議案分類器（沒有一致率、紀錄清單也不標領域）。

    規則同 topics.passing_evaluations：每個分類器只看**它自己最新的一次**評估；最新那次有通過
    的分類器裡，取評估得最晚的。試新模型沒通過，不會讓驗過的舊版本下架；同一個分類器在更多
    標註上重評沒通過，它就下架。
    """
    newest: dict[str, BillTopicEvaluation] = {}
    for evaluation in BillTopicEvaluation.objects.order_by("classifier", "-ran_at", "-id"):
        newest.setdefault(evaluation.classifier, evaluation)
    live = None
    for evaluation in newest.values():
        if evaluation.passed and (live is None or (evaluation.ran_at, evaluation.id)
                                  > (live.ran_at, live.id)):
            live = evaluation
    return live


def passing_classifier() -> str | None:
    evaluation = passing_evaluation()
    return evaluation.classifier if evaluation else None


def live_alignment_classifiers() -> tuple[str, str] | None:
    """(議案分類器, 立法院的質詢分類器)：兩道門檻都過才有，否則 None（一致率不算、不給）。

    兩個名字可以不同：議案與質詢各自評估，通過的不一定是同一個版本。
    """
    bill = passing_classifier()
    speech = topics.passing_classifiers().get(ArticleSource.LY)
    return (bill, speech) if bill and speech else None


def alignment_stamp(bill_classifier: str, speech_classifier: str) -> str:
    """ProfileStat.classifier 上的「議案分類器｜質詢分類器」。"""
    return f"{bill_classifier}{ALIGNMENT_SEPARATOR}{speech_classifier}"


def session_areas(term: int, session_number: int, classifier: str) -> dict[int, str]:
    """一個會期的議案 id → 主領域，只認 classifier 這個版本分的（一個查詢，不逐件查）。"""
    return dict(BillTopic.objects.filter(bill__term=term, bill__session_number=session_number,
                                         classifier=classifier, primary__in=topics.TOPIC_KEYS)
                .values_list("bill_id", "primary"))
