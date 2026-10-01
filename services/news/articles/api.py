"""django-ninja 的 news API。Astro 前端讀的就是這些端點。"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from django.db.models import Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import NinjaAPI, Query, Schema

from . import profiles
from .models import Article, ArticleStatus, Membership, Person, ProfileStat, Session

api = NinjaAPI(title="議會質詢摘要 API", version="1.0", urls_namespace="news")

# 來源代碼（Article.source）。宣告成 Literal，ninja 會把非法值擋成 422，
# 而不是讓一個打錯的 ?source= 變成空清單或 500。
SourceParam = Literal["ly", "tccc", "ntpc"]

_MAX_PAGE_SIZE = 50
# 頁碼上限：SQLite 的 OFFSET 綁定超過 int64 會直接 500，而前端會把訪客
# 網址上的 ?page= 原樣轉手過來。
_MAX_PAGE = 10_000


class SlideOut(Schema):
    index: int
    title: str
    bullets: list[str]
    detail: str
    timestamp: float
    image_url: str | None


class LawSourceOut(Schema):
    law: str
    article: str
    title: str
    excerpt: str
    official_url: str
    api_url: str


class KeyNumberOut(Schema):
    value: str
    unit: str
    label: str
    quote: str
    # 舊資料沒有這三個欄位，給預設值才不會讓整篇 500
    law: str = ""
    article: str = ""
    sources: list[LawSourceOut] = []


class AskOut(Schema):
    request: str
    deadline: str
    response: str


class BriefOut(Schema):
    one_liner: str
    key_numbers: list[KeyNumberOut]
    asks: list[AskOut]


class ArticleCardOut(Schema):
    slug: str
    ivod_id: str
    source: str
    # 講者「當時」的政黨全名；聯合質詢多黨用頓號分隔；查無任期就空字串
    party: str
    title: str
    speaker: str
    meeting: str
    date: date_type
    duration_seconds: int
    ivod_url: str
    teaser: str
    cover_image_url: str | None
    slide_count: int


class ArticleDetailOut(ArticleCardOut):
    source_note: str
    transcript_text: str
    # 沒有摘要卡就是 null：前端退回只用導言的版面
    brief: BriefOut | None
    slides: list[SlideOut]


class ArticleListOut(Schema):
    count: int
    page: int
    page_size: int
    pages: int
    items: list[ArticleCardOut]


class SpeakerOut(Schema):
    name: str
    # 同名的人在不同議會分開算：立法院的「王〇〇」與市議會的不是同一位
    source: str
    count: int
    latest_date: date_type | None
    party: str = ""
    district: str = ""
    # 同來源、同名的任期所屬的人；發言者頁靠它找側寫。查無任期就是 null
    person_id: int | None = None


class SessionOut(Schema):
    id: int
    source: str
    term: str
    name: str
    # 資料涵蓋範圍（掛在這個會期的文章最早與最晚的日期），不是官方起訖
    start_date: date_type | None
    end_date: date_type | None


class PersonRefOut(Schema):
    id: int
    name: str


class IndicatorOut(Schema):
    key: str
    label: str
    unit: str
    # 分母是 0 時是 null；發言次數是整數
    value: int | float | None
    n: int
    n_unit: str
    # 樣本不足或同儕不足時是 null
    percentile: float | None
    peers: int
    sample_ok: bool
    # 網站的相對路徑：點進去就是算出這個數字的那幾篇
    evidence_url: str


class BlockOut(Schema):
    key: str
    title: str
    indicators: list[IndicatorOut]


class ProfileOut(Schema):
    person: PersonRefOut
    source: str
    session: SessionOut
    # 同來源、他有統計的所有會期，新的在前
    sessions: list[SessionOut]
    computed_at: datetime
    min_sample: int
    blocks: list[BlockOut]


class PartyOut(Schema):
    name: str
    count: int
    latest_date: date_type | None


class PartyListOut(Schema):
    items: list[PartyOut]


class SpeakerListOut(Schema):
    items: list[SpeakerOut]


class HealthOut(Schema):
    ok: bool
    articles: int
    latest_date: date_type | None


def _card(article: Article) -> dict:
    cover = article.cover
    return {
        "slug": article.slug,
        "ivod_id": article.ivod_id,
        "source": article.source,
        "party": article.party,
        "title": article.title,
        "speaker": article.speaker,
        "meeting": article.meeting,
        "date": article.date,
        "duration_seconds": article.duration_seconds,
        "ivod_url": article.ivod_url,
        "teaser": article.teaser,
        # 相對路徑，由前端接上自己的 API base。回絕對網址要猜對外主機名，
        # 在反向代理後面很容易猜錯。
        "cover_image_url": cover.image.url if cover and cover.image else None,
        "slide_count": article.slides.count(),
    }


@api.get("/health", response=HealthOut)
def health(request) -> dict:
    ready = Article.objects.filter(status=ArticleStatus.READY)
    return {
        "ok": True,
        "articles": ready.count(),
        "latest_date": ready.aggregate(latest=Max("date"))["latest"],
    }


@api.get("/articles", response=ArticleListOut)
def list_articles(request, date: date_type | None = None, speaker: str | None = None,
                  q: str | None = None, source: SourceParam | None = None,
                  party: str | None = None, session: int | None = None,
                  solo: bool = False, has_brief: bool = False,
                  page: int = Query(1, ge=1, le=_MAX_PAGE),
                  page_size: int = 20) -> dict:
    """只回已完成的文章——處理中或失敗的是內部狀態，不是新聞。

    date 宣告成日期型別而不是字串：前端會把訪客網址上的 ?date= 原樣轉手
    過來，字串會被直接丟進 filter() 而讓任何爬蟲或打錯的連結變成 500。
    交給 ninja 驗證就會回 422。

    session、solo、has_brief 是人物側寫的證據篩選：側寫上每個數字的 evidence_url
    帶的就是這幾個條件，查出來的篇數必須等於那個數字的 n（profiles 用同一套定義）。
    solo、has_brief 是 false 時不篩——「只要聯合質詢」沒有人要。
    """
    # page 由 Query 擋住（超過 int64 的 OFFSET 會讓 SQLite 直接 500），
    # page_size 則夾住就好——一個看起來合理的 ?page_size=100 不值得回錯誤。
    page_size = max(1, min(page_size, _MAX_PAGE_SIZE))

    queryset = Article.objects.filter(status=ArticleStatus.READY).prefetch_related("slides")
    if date:
        queryset = queryset.filter(date=date)
    if speaker:
        queryset = queryset.filter(_speaker_q(speaker))
    if source:
        queryset = queryset.filter(source=source)
    if party:
        queryset = queryset.filter(_joined_q("party", party))
    if session is not None:
        queryset = queryset.filter(session_id=session)
    if solo:
        # 講者欄位沒有「、」。搭配 speaker 時就只剩「講者完全等於這個名字」
        queryset = queryset.exclude(speaker__contains=_SEP)
    if has_brief:
        queryset = queryset.filter(brief__isnull=False)
    if q:
        queryset = queryset.filter(
            Q(title__icontains=q)
            | Q(slides__title__icontains=q)
            | Q(slides__detail__icontains=q)
        ).distinct()

    count = queryset.count()
    start = (page - 1) * page_size
    items = [_card(a) for a in queryset[start:start + page_size]]
    return {
        "count": count,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-count // page_size)),
        "items": items,
    }


@api.get("/articles/{slug}", response=ArticleDetailOut)
def article_detail(request, slug: str) -> dict:
    article = get_object_or_404(
        Article.objects.prefetch_related("slides"), slug=slug, status=ArticleStatus.READY)
    data = _card(article)
    data.update({
        "source_note": article.source_note,
        "transcript_text": article.transcript_text,
        "brief": article.brief,
        "slides": [{
            "index": s.index,
            "title": s.title,
            "bullets": list(s.bullets or []),
            "detail": s.detail,
            "timestamp": s.timestamp,
            "image_url": s.image.url if s.image else None,
        } for s in article.slides.all()],
    })
    return data


# 聯合質詢的文章講者是「甲、乙、丙」（見 tccc_source.SPEAKER_SEPARATOR）。
# 查某一位時要能命中這種文章，但不能用 contains——「王立」會誤中「王立任」。
_SEP = "、"


def _joined_q(field: str, value: str) -> Q:
    """欄位是「甲、乙、丙」時，查其中一個要能命中，但不能用 contains（「王立」會誤中「王立任」）。"""
    return (Q(**{field: value})
            | Q(**{f"{field}__startswith": value + _SEP})
            | Q(**{f"{field}__contains": _SEP + value + _SEP})
            | Q(**{f"{field}__endswith": _SEP + value}))


def _speaker_q(name: str) -> Q:
    return _joined_q("speaker", name)


@api.get("/speakers", response=SpeakerListOut)
def speakers(request, source: SourceParam | None = None) -> dict:
    queryset = Article.objects.filter(status=ArticleStatus.READY)
    if source:
        queryset = queryset.filter(source=source)
    # 在 Python 裡彙總：聯合質詢的「甲、乙、丙」要拆成三個人各算一篇
    stats: dict[tuple[str, str], dict] = {}
    for speaker, article_source, day in queryset.values_list("speaker", "source", "date"):
        for name in filter(None, (n.strip() for n in speaker.split(_SEP))):
            entry = stats.setdefault((name, article_source),
                                     {"name": name, "source": article_source,
                                      "count": 0, "latest_date": day})
            entry["count"] += 1
            if day > entry["latest_date"]:
                entry["latest_date"] = day
    items = sorted(stats.values(), key=lambda e: (e["latest_date"], e["count"]), reverse=True)
    # 一次讀完任期：現任的那段給政黨與選區（沒有就留空）；最新的那段給 person_id
    current: dict[tuple[str, str], Membership] = {}
    latest: dict[tuple[str, str], Membership] = {}
    for m in Membership.objects.order_by("id"):
        key = (m.name, m.source)
        if m.end_date is None:
            current[key] = m
        held = latest.get(key)
        if held is None or (m.start_date or date_type.min) >= (held.start_date or date_type.min):
            latest[key] = m
    for item in items:
        key = (item["name"], item["source"])
        m = current.get(key)
        item["party"] = m.party if m else ""
        item["district"] = m.district if m else ""
        item["person_id"] = latest[key].person_id if key in latest else None
    return {"items": items}


# --- 人物側寫 ---


def _recency(session: Session) -> tuple:
    """會期的新舊：看資料涵蓋到哪一天。"""
    return (session.end_date or date_type.min, session.start_date or date_type.min, session.id)


def _session_out(session: Session) -> dict:
    return {"id": session.id, "source": session.source, "term": session.term, "name": session.name,
            "start_date": session.start_date, "end_date": session.end_date}


def _evidence_url(name: str, session: Session, indicator: profiles.Indicator) -> str:
    """發言者頁的相對路徑，帶上算出這個數字的篩選條件。

    具體度只算單獨發言而且有摘要卡的文章，所以多帶 solo 與 brief（網站轉成
    /api/articles 的 solo、has_brief）。名字整個 URL 編碼：原住民族名有「．」。
    """
    url = f"/speaker/{quote(name, safe='')}?source={session.source}&session={session.id}"
    if indicator.block == profiles.SPECIFICITY:
        url += "&solo=1&brief=1"
    return url


def _indicator_out(stat: ProfileStat | None, indicator: profiles.Indicator, name: str,
                   session: Session) -> dict:
    value = stat.value if stat else None
    n = stat.n if stat else 0
    if value is not None and indicator is profiles.SPEECHES:
        value = int(value)
    return {
        "key": indicator.key,
        "label": indicator.label,
        "unit": indicator.unit,
        "value": value,
        "n": n,
        "n_unit": indicator.n_unit,
        "percentile": round(stat.percentile, 1) if stat and stat.percentile is not None else None,
        "peers": stat.peers if stat else 0,
        "sample_ok": indicator.sample_ok(n),
        "evidence_url": _evidence_url(name, session, indicator),
    }


@api.get("/people/{person_id}/profile", response=ProfileOut)
def person_profile(request, person_id: int, source: SourceParam | None = None,
                   session: int | None = None) -> dict:
    """一個人在一個會期的側寫：投入量與具體度，每項各自跟同儕比，不加總、不排名。

    source 省略時用他最近一個有統計的會期的來源；session 省略時用這個來源裡他有發言的
    最近一個會期，都沒有發言就用有統計的最近一個。指定了 session 而省略 source，
    就用那個會期的來源。人不存在、在這個來源沒有任何統計、或指定的會期沒有他的
    統計：404。
    """
    person = get_object_or_404(Person, pk=person_id)
    stats = list(ProfileStat.objects.filter(person=person).select_related("session"))
    by_session = {stat.session_id: stat.session for stat in stats}
    ordered = sorted(by_session.values(), key=_recency, reverse=True)

    chosen = None
    if session is not None:
        chosen = by_session.get(session)
        if chosen is None or (source and chosen.source != source):
            raise Http404("這個會期沒有他的統計")
        source = chosen.source
    if source is None:
        if not ordered:
            raise Http404("這個人還沒有任何統計")
        source = ordered[0].source
    in_source = [s for s in ordered if s.source == source]
    if not in_source:
        raise Http404("這個人在這個來源沒有任何統計")
    if chosen is None:
        spoke = {stat.session_id for stat in stats
                 if stat.indicator == profiles.SPEECHES.key and (stat.value or 0) > 0}
        chosen = next((s for s in in_source if s.id in spoke), in_source[0])

    mine = {stat.indicator: stat for stat in stats if stat.session_id == chosen.id}
    name = profiles.name_in_session(person, chosen)
    blocks = [{"key": key, "title": title,
               "indicators": [_indicator_out(mine.get(i.key), i, name, chosen) for i in indicators]}
              for key, title, indicators in profiles.blocks_for(source)]
    return {
        "person": {"id": person.id, "name": person.name},
        "source": source,
        "session": _session_out(chosen),
        "sessions": [_session_out(s) for s in in_source],
        "computed_at": timezone.localtime(max(stat.computed_at for stat in mine.values())),
        "min_sample": profiles.MIN_SAMPLE,
        "blocks": blocks,
    }


@api.get("/parties", response=PartyListOut)
def parties(request, source: SourceParam | None = None) -> dict:
    """READY 文章的政黨彙總（聯合質詢的多黨各算一篇），給篩選器用。"""
    queryset = Article.objects.filter(status=ArticleStatus.READY).exclude(party="")
    if source:
        queryset = queryset.filter(source=source)
    stats: dict[str, dict] = {}
    for party, day in queryset.values_list("party", "date"):
        for name in filter(None, (n.strip() for n in party.split(_SEP))):
            entry = stats.setdefault(name, {"name": name, "count": 0, "latest_date": day})
            entry["count"] += 1
            if day > entry["latest_date"]:
                entry["latest_date"] = day
    return {"items": sorted(stats.values(), key=lambda e: e["count"], reverse=True)}
