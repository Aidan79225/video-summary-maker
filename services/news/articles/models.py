"""資料模型：一段質詢發言 = 一篇文章。"""
from __future__ import annotations

from django.db import models

TEASER_LENGTH = 120


class ArticleStatus(models.TextChoices):
    PENDING = "pending", "等待處理"
    PROCESSING = "processing", "處理中"
    READY = "ready", "已完成"
    FAILED = "failed", "失敗"


class Article(models.Model):
    """一段 IVOD 發言的摘要。

    ivod_id 是唯一鍵，因為整條 pipeline 都以它為準做 upsert——排程重跑、
    手動補跑、失敗重試都不會產生重複的文章。
    """

    ivod_id = models.CharField(max_length=32, unique=True, db_index=True)
    slug = models.SlugField(max_length=64, unique=True)

    title = models.CharField(max_length=300)
    speaker = models.CharField(max_length=100, db_index=True)
    meeting = models.CharField(max_length=300, blank=True)
    date = models.DateField(db_index=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    ivod_url = models.URLField(max_length=500)

    source_note = models.CharField(max_length=300, blank=True)
    transcript_text = models.TextField(blank=True)
    # 摘要卡：{one_liner, key_numbers[], asks[]}。GPU 端產不出來時是 None，
    # 前端退回只用第一段當導言的樣子。存 JSON 而不拆表：它是一個整體、
    # 一起換掉、不會被單獨查詢。
    brief = models.JSONField(null=True, blank=True, default=None)

    status = models.CharField(max_length=16, choices=ArticleStatus.choices,
                              default=ArticleStatus.PENDING, db_index=True)
    error = models.TextField(blank=True)
    # 沒有上限的話，一篇永遠失敗的文章（例如影片已下架）會每天排在隊首、
    # 每次燒掉幾分鐘 GPU，而且永遠不會放棄。
    attempts = models.PositiveIntegerField(default=0)
    # 等待途中斷線時，下一輪先問問看那個工作是不是已經跑完了——否則幾分鐘
    # 的 GPU 成品會被白白丟掉、整支影片重跑一次。
    gpu_job_id = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date", "-published_at", "-id"]
        indexes = [models.Index(fields=["-date", "status"])]

    def __str__(self) -> str:
        return f"{self.date} {self.speaker}"

    @property
    def one_liner(self) -> str:
        brief = self.brief if isinstance(self.brief, dict) else {}
        value = brief.get("one_liner")
        return value.strip() if isinstance(value, str) else ""

    @property
    def teaser(self) -> str:
        """導言優先用摘要卡的一句話：它講的是整段質詢要什麼。

        沒有卡片時退回第一段的完整敘述——條列太零碎，當導言讀起來不像新聞。
        """
        if self.one_liner:
            return self.one_liner
        first = self.slides.first()
        if first is None:
            return ""
        text = first.detail or "；".join(first.bullets)
        return text[:TEASER_LENGTH]

    @property
    def cover(self) -> Slide | None:
        """用 Python 過濾而不是 `.exclude(image="")`。

        後者會 clone queryset 並丟掉 prefetch 的快取，於是清單上每張卡片都
        多打一次資料庫——一頁 20 張就是 20 次多餘查詢打在 Pi 的 SD 卡上。
        """
        return next((slide for slide in self.slides.all() if slide.image), None)


class Slide(models.Model):
    """文章裡的一段：截圖 + 小標 + 條列 + 完整敘述。"""

    article = models.ForeignKey(Article, related_name="slides", on_delete=models.CASCADE)
    index = models.PositiveIntegerField()
    title = models.CharField(max_length=300)
    bullets = models.JSONField(default=list)
    detail = models.TextField(blank=True)
    timestamp = models.FloatField(default=0.0)
    image = models.FileField(upload_to="articles/", blank=True)

    class Meta:
        ordering = ["index"]
        unique_together = [("article", "index")]

    def __str__(self) -> str:
        return f"{self.article.ivod_id}#{self.index}"
