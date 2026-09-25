"""django-ninja 的 news API。Astro 前端讀的就是這些端點。"""
from __future__ import annotations

from datetime import date as date_type

from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Query, Schema

from .models import Article, ArticleStatus

api = NinjaAPI(title="立法院質詢摘要 API", version="1.0", urls_namespace="news")

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


class KeyNumberOut(Schema):
    value: str
    unit: str
    label: str
    quote: str


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
    count: int
    latest_date: date_type | None


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
                  q: str | None = None,
                  page: int = Query(1, ge=1, le=_MAX_PAGE),
                  page_size: int = 20) -> dict:
    """只回已完成的文章——處理中或失敗的是內部狀態，不是新聞。

    date 宣告成日期型別而不是字串：前端會把訪客網址上的 ?date= 原樣轉手
    過來，字串會被直接丟進 filter() 而讓任何爬蟲或打錯的連結變成 500。
    交給 ninja 驗證就會回 422。
    """
    # page 由 Query 擋住（超過 int64 的 OFFSET 會讓 SQLite 直接 500），
    # page_size 則夾住就好——一個看起來合理的 ?page_size=100 不值得回錯誤。
    page_size = max(1, min(page_size, _MAX_PAGE_SIZE))

    queryset = Article.objects.filter(status=ArticleStatus.READY).prefetch_related("slides")
    if date:
        queryset = queryset.filter(date=date)
    if speaker:
        queryset = queryset.filter(speaker=speaker)
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


@api.get("/speakers", response=SpeakerListOut)
def speakers(request) -> dict:
    rows = (Article.objects.filter(status=ArticleStatus.READY)
            .values("speaker")
            .annotate(count=Count("id"), latest_date=Max("date"))
            .order_by("-latest_date", "-count"))
    return {"items": [
        {"name": r["speaker"], "count": r["count"], "latest_date": r["latest_date"]}
        for r in rows if r["speaker"]
    ]}
