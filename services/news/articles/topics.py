"""人物側寫第二步：議題分布。政策領域、分類的輸入、每晚分類、標註集與評估、上線條件、委員會職掌。

原則照 issue #24：模型只做分類、數字由程式算；**模型參與的指標要有人工標註集，準確率低於門檻
就不上線**。所以這裡的幾件事是咬在一起的：

- 分類（classify_topics）：替基礎文章送 GPU 的 `topic` 工作，存成 Topic，並記下是哪個分類器
  （模型＋提示詞版本）分的。
- 評估（evaluate）：把人工標註過的文章用「現在的」分類器重分一次，逐來源比對主領域，存成
  TopicEvaluation。
- 上線條件（passing_evaluations）：某來源只用「最新一筆通過的評估」那個分類器分出來的 Topic。
  換了模型或提示詞，新分的 Topic 版本對不上就不算，直到重新評估通過。

政策領域寫在這裡、是唯一的來源：GPU 端不寫死，每次送工作時把清單帶過去。代碼用在資料庫與網址，
名稱給模型看、也給讀者看。
"""
from __future__ import annotations

import logging
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from .gpu_client import GpuApiClient, GpuApiError, JobFailed
from .members_sync import SPEAKER_SEPARATOR
from .models import Article, ArticleSource, ArticleStatus, Topic, TopicEvaluation, TopicLabel

logger = logging.getLogger(__name__)


# --- 政策領域 ---


@dataclass(frozen=True)
class Area:
    key: str
    label: str
    # 給模型看的說明：這個領域包含哪些事
    description: str


# 順序有意義：分布同篇數時照這個順序排，方法頁也照這個順序列
TOPICS: tuple[Area, ...] = (
    Area("defense", "國防外交", "國防、軍事、外交、兩岸、僑務"),
    Area("finance", "財政經濟", "預算、稅收、金融、產業、經濟政策、物價"),
    Area("interior", "內政治安", "警政、治安、消防、移民、戶政、選務"),
    Area("education", "教育文化", "教育、學校、文化、體育"),
    Area("welfare", "衛生福利", "醫療、健保、長照、社福、托育、食安"),
    Area("transport", "交通建設", "交通、道路、捷運、公共工程、觀光"),
    Area("environment", "環境能源", "環保、污染、氣候、能源、電力、水資源"),
    Area("justice", "司法法制", "司法、檢調、監所、法律制度"),
    Area("agriculture", "農業", "農、漁、牧、農產品、農村"),
    Area("labor", "勞動", "勞工、就業、薪資、勞保與年金"),
    Area("digital", "數位科技", "數位發展、資安、通訊、科技研發"),
    Area("local", "地方建設／其他", "都市計畫、住宅、區里建設、議事程序、以上都不是的"),
)
TOPIC_KEYS: tuple[str, ...] = tuple(area.key for area in TOPICS)
TOPIC_BY_KEY: dict[str, Area] = {area.key: area for area in TOPICS}
TOPIC_CHOICES: tuple[tuple[str, str], ...] = tuple((area.key, area.label) for area in TOPICS)

# /api/articles?topic=any：有「通過版本」的分類、哪個領域都可以。聚焦度、廣度與委員會職掌的
# n 是「有通過版本 Topic 的基礎文章」；只用 solo＋brief 篩的話，還沒分類（或分類失敗）的
# 文章也會混進證據清單，點進去的篇數就跟 n 對不上。
ANY_TOPIC = "any"

# 上線門檻：標註至少這麼多篇、主領域準確率至少這麼高（issue #24：每個來源各 20 篇、80%）
TOPIC_MIN_LABELS = 20
TOPIC_MIN_ACCURACY = 0.8

# GPU 端 topic 工作的文字上限（字數）。超過的請求會被 422 拒絕，所以送出前先在這裡截
TOPIC_TEXT_LIMIT = 4000

# 廣度：占比達到 1/10 的領域才算一個。用整數比（篇數 × 10 ≥ 基礎文章數），不用浮點數的 0.1——
# 剛好 10% 的那一個不該因為尾數被算掉
BREADTH_SHARE_DENOMINATOR = 10

# 分類只要幾秒；照摘要工作預設的 3 秒輪詢，一晚 200 篇會多等十分鐘
_POLL_SECONDS = 1.0


def labels_payload() -> list[dict[str, str]]:
    """送給 GPU 的領域清單：`[{key, label, description}]`。GPU 端不寫死，改這裡就好。"""
    return [{"key": a.key, "label": a.label, "description": a.description} for a in TOPICS]


# --- 基礎文章與分類的輸入 ---


def base_articles() -> QuerySet[Article]:
    """分類、抽樣、評估、指標共用的基礎文章：已完成、單獨發言、有摘要卡。

    跟具體度同一批，也跟 /api/articles 的 solo=1&has_brief=1 同一個定義——證據清單才對得上。
    聯合質詢分不出每個人講了哪個議題；沒有摘要卡就沒有一句話可讀。
    """
    return (Article.objects.filter(status=ArticleStatus.READY, brief__isnull=False)
            .exclude(speaker__contains=SPEAKER_SEPARATOR))


def classifier_input(article: Article) -> str:
    """分類器讀到的文字：摘要卡的一句話＋各段小標。

    **不放會議名稱**：「財政委員會」幾乎會直接決定答案，立委的委員會職掌比對就變成自己比自己。
    admin 的標註頁也用這個函式顯示——標的人跟模型讀的是同一段文字，評估才公平。
    超過 GPU 的上限時從最後一個小標開始捨，一句話永遠留著。
    """
    head = f"一句話：{article.one_liner}"
    # 用 .all() 而不是 order_by()：呼叫端 prefetch 過的話不必再查一次（Slide 預設照 index 排）
    titles = [t for t in (slide.title.strip() for slide in article.slides.all()) if t]
    while True:
        text = "\n".join([head, "各段小標：", *(f"- {t}" for t in titles)]) if titles else head
        if len(text) <= TOPIC_TEXT_LIMIT or not titles:
            return text[:TOPIC_TEXT_LIMIT]
        titles.pop()


# --- GPU 的成品 ---


class TopicField(StrEnum):
    """GPU 回傳的 topic 成品欄位。協定的一部分，Pi 上沒有 slidebox，各留一份。"""
    PRIMARY = "primary"
    SECONDARY = "secondary"
    CLASSIFIER = "classifier"


@dataclass(frozen=True)
class TopicResult:
    primary: str
    # 空字串＝沒有次領域
    secondary: str
    # 模型＋提示詞版本（「qwen3:14b#topic-v1」）
    classifier: str


def parse_result(result: object) -> TopicResult:
    """檢查 GPU 回來的分類。主領域不在清單裡、沒有分類器名稱：當成這篇失敗，不猜。

    次領域只是附帶的：不在清單裡或跟主領域相同就當成沒有，不讓它拖垮整篇。
    """
    if not isinstance(result, dict):
        raise JobFailed("分類結果的格式不符預期")
    primary = result.get(TopicField.PRIMARY)
    if not isinstance(primary, str) or primary not in TOPIC_BY_KEY:
        raise JobFailed(f"分類結果的主領域不在清單裡：{str(primary)[:50]!r}")
    classifier = result.get(TopicField.CLASSIFIER)
    if not isinstance(classifier, str) or not classifier.strip():
        raise JobFailed("分類結果沒有分類器名稱")
    secondary = result.get(TopicField.SECONDARY)
    if not isinstance(secondary, str) or secondary not in TOPIC_BY_KEY or secondary == primary:
        secondary = ""
    return TopicResult(primary, secondary, classifier.strip())


def classify_text(client: GpuApiClient, text: str, timeout: float) -> TopicResult:
    """送一個 topic 工作、等它跑完。錯誤契約同 GpuApiClient：只丟 GpuApiError 或 JobFailed。"""
    job_id = client.submit_topic(text, labels_payload())
    return parse_result(client.wait(job_id, timeout=timeout, poll_seconds=_POLL_SECONDS))


# --- 每晚分類 ---


@dataclass
class ClassifyReport:
    classified: int = 0
    failed: int = 0
    # 基礎文章裡還沒有 Topic 的：積壓要講出來，否則一晚 200 篇的上限會無聲地一直排著
    remaining: int = 0
    # GPU 連不上，這一輪提前結束
    stopped: bool = False
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        text = f"分類完成 {self.classified}、失敗 {self.failed}、還沒分類 {self.remaining}"
        return text + ("（GPU 不可用，這一輪提前結束）" if self.stopped else "")


def classify_topics(client: GpuApiClient, limit: int, reclassify: bool = False,
                    timeout: float | None = None) -> ClassifyReport:
    """替基礎文章分政策領域，一次一篇。

    預設只分還沒有 Topic 的；reclassify=True 連已經有的也重分（換模型或提示詞之後用），
    沒有 Topic 的先、再來是分得最久的，分批跑幾次就會輪完一遍。

    失敗只記 log、不動文章狀態——分不出領域不是文章的錯，文章照樣在站上。GPU 連不上就整輪
    停止：那對後面每一篇都一樣（同摘要的處理）。
    """
    timeout = timeout or settings.GPU_JOB_TIMEOUT_SECONDS
    queryset = base_articles()
    if not reclassify:
        queryset = queryset.filter(topic__isnull=True)
    queryset = (queryset.order_by(F("topic__labeled_at").asc(nulls_first=True), "-date", "id")
                .prefetch_related("slides")[:max(0, limit)])
    report = ClassifyReport()
    for article in list(queryset):
        try:
            result = classify_text(client, classifier_input(article), timeout)
            # 存檔也放在 try 裡：SD 卡上的 SQLite 偶爾會鎖住，一篇存不進去不該讓這一輪剩下的
            # 文章整晚都不分類（下面的 except Exception 接的主要就是這種）
            Topic.objects.update_or_create(article=article, defaults={
                "primary": result.primary, "secondary": result.secondary,
                "classifier": result.classifier, "labeled_at": timezone.now()})
        except GpuApiError as e:
            _record_failure(article, e, report)
            report.stopped = True
            logger.warning("GPU 不可用，議題分類這一輪提前結束")
            break
        except JobFailed as e:
            _record_failure(article, e, report)
            continue
        except Exception as e:  # noqa: BLE001
            logger.exception("文章 %s 分類時發生預期外的錯誤", article.ivod_id)
            _record_failure(article, e, report)
            continue
        report.classified += 1
    report.remaining = base_articles().filter(topic__isnull=True).count()
    return report


def _record_failure(article: Article, error: Exception, report: ClassifyReport) -> None:
    report.failed += 1
    report.errors.append(f"{article.ivod_id}: {error}")
    logger.warning("文章 %s 分類失敗：%s", article.ivod_id, error)


# --- 標註集 ---


@dataclass
class SampleReport:
    # 來源 → 這次新抽的篇數
    added: Counter = field(default_factory=Counter)
    # 來源 → 抽完之後的標註集大小
    total: Counter = field(default_factory=Counter)

    def __str__(self) -> str:
        lines = []
        for source in ArticleSource:
            lines.append(f"{source.label}：新抽 {self.added[source.value]} 篇，"
                         f"標註集共 {self.total[source.value]} 篇")
        return "\n".join(lines)


def sample_labels(per_source: int = TOPIC_MIN_LABELS, seed: int = 0) -> SampleReport:
    """每個來源從基礎文章裡隨機抽，建立空白的 TopicLabel（之後在 admin 標）。

    已經抽過的不重抽，只補到每個來源 per_source 篇——標到一半再跑一次，不會把標好的換掉。
    固定種子、候選照 id 排：同樣的資料、同樣的種子就抽到同樣的文章，可以重現。
    每個來源用自己的亂數序列，多一個來源不會改變其他來源抽到的文章。
    """
    report = SampleReport()
    for source in ArticleSource.values:
        have = TopicLabel.objects.filter(article__source=source).count()
        need = per_source - have
        if need > 0:
            candidates = list(base_articles().filter(source=source, topic_label__isnull=True)
                              .order_by("id").values_list("id", flat=True))
            picked = random.Random(f"{seed}:{source}").sample(candidates,
                                                              min(need, len(candidates)))
            TopicLabel.objects.bulk_create([TopicLabel(article_id=i) for i in sorted(picked)])
            report.added[source] = len(picked)
            have += len(picked)
        report.total[source] = have
    return report


# --- 評估 ---


class EvaluationAborted(Exception):
    """這次評估不算：中途換了模型、或一篇都沒分出來。什麼都不存。"""


@dataclass
class EvaluationReport:
    evaluations: list[TopicEvaluation] = field(default_factory=list)
    # 已標註、但文章已經不是基礎文章（例如重產之後沒有摘要卡）：沒有輸入可以分類
    skipped: int = 0

    def __str__(self) -> str:
        lines = []
        for ev in self.evaluations:
            verdict = "通過" if ev.passed else "未通過"
            if not ev.passed and ev.labeled < TOPIC_MIN_LABELS:
                verdict += f"（標註不足 {TOPIC_MIN_LABELS} 篇）"
            elif not ev.passed:
                verdict += f"（準確率未達 {TOPIC_MIN_ACCURACY:.0%}）"
            lines.append(f"{ArticleSource(ev.source).label}：分類器 {ev.classifier}，"
                         f"已標註 {ev.labeled} 篇、主領域相同 {ev.correct} 篇，"
                         f"準確率 {ev.accuracy * 100:.1f}%：{verdict}")
            for m in ev.mistakes:
                model = (TOPIC_BY_KEY[m["model"]].label if m.get("model") in TOPIC_BY_KEY
                         else f"分類失敗：{m.get('error') or '未知'}")
                human = TOPIC_BY_KEY[m["human"]].label if m["human"] in TOPIC_BY_KEY else m["human"]
                lines.append(f"  判錯：{m['slug']} {m['speaker']}：人工「{human}」、模型「{model}」")
        if self.skipped:
            lines.append(f"略過 {self.skipped} 篇已標註、但已經不是基礎文章的（沒有摘要卡或不是單獨發言）")
        return "\n".join(lines)


def evaluate(client: GpuApiClient, timeout: float | None = None,
             now: datetime | None = None) -> EvaluationReport:
    """把已標註的文章用現在的模型與提示詞重分一次，逐來源比對主領域，每個來源存一筆評估。

    準確率 = 主領域相同的篇數 ÷ 已標註篇數；分類失敗的那篇算錯（分母照算），不能因為模型
    答不出來反而讓準確率變高。通過 = 已標註至少 TOPIC_MIN_LABELS 篇、準確率至少
    TOPIC_MIN_ACCURACY。

    一次評估裡 GPU 回來的分類器必須都一樣：不一樣代表中途換了模型，兩半的成績不能加在一起，
    整次中止、什麼都不存。GPU 連不上（GpuApiError）也一樣直接冒出去、什麼都不存。
    """
    timeout = timeout or settings.GPU_JOB_TIMEOUT_SECONDS
    labelled = TopicLabel.objects.exclude(primary="")
    labels = list(labelled.filter(article__in=base_articles())
                  .select_related("article").prefetch_related("article__slides")
                  .order_by("article__source", "article_id"))
    report = EvaluationReport(skipped=labelled.count() - len(labels))
    if not labels:
        raise EvaluationAborted("還沒有任何已標註的基礎文章：先跑 sample_topic_labels，再到 admin 標註")

    outcomes: list[tuple[TopicLabel, TopicResult | None, str]] = []
    classifiers: set[str] = set()
    for label in labels:
        try:
            result = classify_text(client, classifier_input(label.article), timeout)
        except JobFailed as e:
            outcomes.append((label, None, str(e)[:200]))
            continue
        classifiers.add(result.classifier)
        if len(classifiers) > 1:
            raise EvaluationAborted(
                f"這次評估裡 GPU 回來的分類器不一樣（{'、'.join(sorted(classifiers))}），"
                "中途換了模型或提示詞；整次不算，請再跑一次")
        outcomes.append((label, result, ""))
    if not classifiers:
        raise EvaluationAborted("沒有任何一篇分類成功，無法評估（GPU 端的錯誤見上面的 log）")
    classifier = classifiers.pop()

    by_source: dict[str, list[tuple[TopicLabel, TopicResult | None, str]]] = defaultdict(list)
    for outcome in outcomes:
        by_source[outcome[0].article.source].append(outcome)
    ran_at = now or timezone.now()
    with transaction.atomic():
        for source, items in by_source.items():
            report.evaluations.append(_save_evaluation(source, classifier, items, ran_at))
    return report


def _save_evaluation(source: str, classifier: str,
                     items: list[tuple[TopicLabel, TopicResult | None, str]],
                     ran_at: datetime) -> TopicEvaluation:
    mistakes = []
    for label, result, error in items:
        if result is not None and result.primary == label.primary:
            continue
        mistake = {"article": label.article_id, "slug": label.article.slug,
                   "speaker": label.article.speaker, "human": label.primary,
                   "model": result.primary if result else None}
        if error:
            mistake["error"] = error
        mistakes.append(mistake)
    labeled = len(items)
    correct = labeled - len(mistakes)
    accuracy = correct / labeled
    return TopicEvaluation.objects.create(
        source=source, classifier=classifier, labeled=labeled, correct=correct, accuracy=accuracy,
        passed=labeled >= TOPIC_MIN_LABELS and accuracy >= TOPIC_MIN_ACCURACY,
        mistakes=mistakes, ran_at=ran_at)


# --- 上線條件 ---


def passing_evaluations() -> dict[str, TopicEvaluation]:
    """來源 → 最新一筆**通過的**評估。沒有通過的來源不在裡面：那個來源完全沒有議題指標。

    看的是「通過的裡面最新的」，不是「最新的那筆有沒有通過」：試一個新模型沒通過，不該讓
    已經驗過的舊版本下架——舊版本分的 Topic 還在，數字仍然是驗過的。
    """
    latest: dict[str, TopicEvaluation] = {}
    for evaluation in TopicEvaluation.objects.filter(passed=True).order_by("source", "-ran_at", "-id"):
        latest.setdefault(evaluation.source, evaluation)
    return latest


def passing_classifiers() -> dict[str, str]:
    """來源 → 通過的分類器名稱。指標與證據篩選都只認這個版本分出來的 Topic。"""
    return {source: ev.classifier for source, ev in passing_evaluations().items()}


# --- 委員會職掌（立法院） ---

# 寫在程式裡、方法頁公開。程序、修憲、經費稽核委員會沒有政策職掌，不在表上
COMMITTEE_AREAS: dict[str, tuple[str, ...]] = {
    "內政委員會": ("interior",),
    "外交及國防委員會": ("defense",),
    "經濟委員會": ("finance", "agriculture", "environment"),
    "財政委員會": ("finance",),
    "教育及文化委員會": ("education", "digital"),
    "交通委員會": ("transport", "digital"),
    "司法及法制委員會": ("justice",),
    "社會福利及衛生環境委員會": ("welfare", "labor", "environment"),
}

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_COMMITTEE_RE = re.compile(r"^\s*第(\d+)屆第(\d+)會期\s*[：:]\s*(\S.*?)\s*$")


def parse_committee(entry: object) -> tuple[str, str] | None:
    """LYAPI 的「第11屆第5會期：財政委員會」→ ("第11屆第5會期", "財政委員會")。

    會期名稱照 profiles.parse_session 的寫法用數字重組（全形轉半形、去掉前導零），才對得上
    文章掛的會期。LYAPI 偶爾給空字串，不成形的一律略過、不猜。
    """
    if not isinstance(entry, str):
        return None
    match = _COMMITTEE_RE.match(entry.translate(_FULLWIDTH_DIGITS))
    if not match:
        return None
    term, number, committee = int(match.group(1)), int(match.group(2)), match.group(3)
    return f"第{term}屆第{number}會期", committee


def committee_areas(committees: Iterable[object], session_name: str) -> frozenset[str]:
    """他在這個會期所屬委員會的職掌領域；沒有這個會期的資料（或只有沒職掌的委員會）是空集合。

    一個會期可能同時在好幾個委員會（例如財政＋程序），職掌取聯集。
    """
    areas: set[str] = set()
    for entry in committees:
        parsed = parse_committee(entry)
        if parsed and parsed[0] == session_name:
            areas.update(COMMITTEE_AREAS.get(parsed[1], ()))
    return frozenset(areas)
