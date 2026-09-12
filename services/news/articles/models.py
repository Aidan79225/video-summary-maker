"""資料模型：一段質詢發言 = 一篇文章。"""
from __future__ import annotations

from django.db import models

PENDING = "pending"
PROCESSING = "processing"
READY = "ready"
FAILED = "failed"

STATUS_CHOICES = [
    (PENDING, "等待處理"),
    (PROCESSING, "處理中"),
    (READY, "已完成"),
    (FAILED, "失敗"),
]


class Article(models.Model):
    """一段 IVOD 發言的摘要。

    ivod_id 是唯一鍵：整條 pipeline 都以它為準做 upsert，所以排程重跑、
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

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING,
                              db_index=True)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date", "-published_at", "-id"]
        indexes = [models.Index(fields=["-date", "status"])]

    def __str__(self) -> str:
        return f"{self.date} {self.speaker}"

    @property
    def teaser(self) -> str:
        """導言：第一段的完整敘述截短。條列太零碎，當導言讀起來不像新聞。"""
        first = self.slides.first()
        if first is None:
            return ""
        text = first.detail or "；".join(first.bullets)
        return text[:120]

    @property
    def cover(self):
        return self.slides.exclude(image="").first()


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
