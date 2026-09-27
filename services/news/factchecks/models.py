"""查核結果：一篇文章有多則主張，每則主張附上它的證據。

「不符」落地時一律待審：把具名的人標成講錯是這個功能唯一會傷人的地方，
而語音辨識的錯字最容易被誤判成不符。人工核准之前不公開、不計分。
"""
from __future__ import annotations

from django.db import models

from articles.models import Article


class RunStatus(models.TextChoices):
    PENDING = "pending", "等待查核"
    PROCESSING = "processing", "查核中"
    DONE = "done", "已完成"
    FAILED = "failed", "失敗"


class Verdict(models.TextChoices):
    SUPPORTED = "supported", "相符"
    PARTIAL = "partial", "部分相符"
    CONTRADICTED = "contradicted", "不符"
    UNVERIFIABLE = "unverifiable", "無法查證"


class Method(models.TextChoices):
    NUMERIC = "numeric", "數字比對"
    MODEL = "model", "模型判讀"
    NONE = "none", "未比對"


class ClaimKind(models.TextChoices):
    LAW_ARTICLE = "law_article", "法條內容"
    BILL_CONTENT = "bill_content", "議案內容"
    BILL_STATUS = "bill_status", "議案進度"


class ReviewStatus(models.TextChoices):
    AUTO = "auto", "自動發佈"
    PENDING = "pending_review", "待審核"
    APPROVED = "approved", "已核准"
    REJECTED = "rejected", "已駁回"


PUBLIC_REVIEW_STATUSES = (ReviewStatus.AUTO, ReviewStatus.APPROVED)
HUMAN_REVIEW_STATUSES = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)


class FactCheckRun(models.Model):
    """一篇文章的查核進度。沿用匯入流程的規則：可重跑、嘗試次數有上限、
    斷線後先接回 GPU 上既有的工作。"""
    article = models.OneToOneField(Article, related_name="factcheck_run",
                                   on_delete=models.CASCADE)
    status = models.CharField(max_length=16, choices=RunStatus.choices,
                              default=RunStatus.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    gpu_job_id = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    model = models.CharField(max_length=100, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.article} 查核（{self.get_status_display()}）"


class Claim(models.Model):
    article = models.ForeignKey(Article, related_name="claims", on_delete=models.CASCADE)
    index = models.PositiveIntegerField()
    quote = models.TextField()
    timestamp = models.FloatField(default=0.0)
    kind = models.CharField(max_length=20, choices=ClaimKind.choices)
    statement = models.TextField()
    subject = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=16, choices=Verdict.choices, db_index=True)
    method = models.CharField(max_length=16, choices=Method.choices)
    rationale = models.TextField(blank=True)
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices,
                                     default=ReviewStatus.AUTO, db_index=True)
    reviewer_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["index"]
        unique_together = [("article", "index")]

    def __str__(self) -> str:
        return f"{self.article.ivod_id}#{self.index} {self.get_verdict_display()}"

    @property
    def is_public(self) -> bool:
        return self.review_status in PUBLIC_REVIEW_STATUSES


class Evidence(models.Model):
    claim = models.ForeignKey(Claim, related_name="evidence", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    source = models.CharField(max_length=16)
    title = models.CharField(max_length=300)
    official_url = models.URLField(max_length=1000, blank=True)
    api_url = models.URLField(max_length=1000, blank=True)
    excerpt = models.TextField(blank=True)

    class Meta:
        ordering = ["position"]

    def __str__(self) -> str:
        return self.title
