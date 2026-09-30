"""資料模型：一段質詢發言 = 一篇文章。"""
from __future__ import annotations

from django.db import models

TEASER_LENGTH = 120


class ArticleStatus(models.TextChoices):
    PENDING = "pending", "等待處理"
    PROCESSING = "processing", "處理中"
    READY = "ready", "已完成"
    FAILED = "failed", "失敗"


class ArticleSource(models.TextChoices):
    LY = "ly", "立法院"
    TCCC = "tccc", "臺中市議會"
    NTPC = "ntpc", "新北市議會"


class Person(models.Model):
    """一個真人。同一個人先當議員、後當立委、再回議會，是同一個 Person 底下的
    多筆 Membership。認人只靠姓名與別名，同名多人時標 needs_review 交給人。"""

    name = models.CharField(max_length=100, db_index=True)
    # 其他寫法（「楊啟邦」對「楊啓邦」），同步認人時一併比對
    aliases = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True)
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    """一段任期：哪個議會、哪個黨、哪個選區、從何時到何時。

    文章存的是「當時」的政黨，所以換黨要開新的一段而不是改舊的。
    start_date 為空視為無限早、end_date 為空視為現任。
    """

    person = models.ForeignKey(Person, related_name="memberships", on_delete=models.CASCADE)
    source = models.CharField(max_length=16, choices=ArticleSource.choices, db_index=True)
    # 來源給的編號：立法院是歷屆立法委員編號（跨屆穩定）、臺中是官網的 cno、
    # 新北是官網的 C（跨屆穩定）
    external_id = models.CharField(max_length=64, blank=True, db_index=True)
    # 來源上的寫法，可能與 Person.name 不同
    name = models.CharField(max_length=100, db_index=True)
    party = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    term = models.CharField(max_length=32, blank=True)
    photo_url = models.URLField(max_length=500, blank=True)
    # 議長／副議長／空。新北的議長、副議長只主持不質詢，影音系統卻把他們列進
    # 每一段的發言議員，要靠這欄把他們拿掉。其他來源留空。
    role = models.CharField(max_length=32, blank=True)
    # 黨團（國民黨團／民進黨團／無黨團結聯盟／空）。跟政黨不一定相同——新北有 4 人
    # 政黨與黨團不同。文章標的是政黨；黨團只用來判斷「黨團時段」裡誰不屬於該黨團。
    caucus = models.CharField(max_length=64, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    # 換黨自動切段後日期待補
    needs_review = models.BooleanField(default=False)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [models.Index(fields=["source", "name"]),
                   models.Index(fields=["source", "external_id"])]

    def __str__(self) -> str:
        return f"{self.name}（{self.get_source_display()}，{self.party or '無資料'}）"

    def covers(self, day) -> bool:
        return ((self.start_date is None or self.start_date <= day)
                and (self.end_date is None or day <= self.end_date))


class Article(models.Model):
    """一段質詢發言的摘要。

    ivod_id 是唯一鍵，因為整條 pipeline 都以它為準做 upsert——排程重跑、
    手動補跑、失敗重試都不會產生重複的文章。
    """

    ivod_id = models.CharField(max_length=32, unique=True, db_index=True)
    # 哪個議會。ivod_id 這個名字是歷史包袱——現在是「來源給的識別碼」，
    # 臺中片段寫成 tccc-<ano>；改欄位名要動 API、前端與既有資料，不值得。
    source = models.CharField(max_length=16, choices=ArticleSource.choices,
                              default=ArticleSource.LY, db_index=True)
    # 講者「當時」的政黨（來源給的全名；聯合質詢多黨用頓號分隔）。之後換黨不回溯。
    party = models.CharField(max_length=200, blank=True, db_index=True)
    # 單一講者且對得到任期時才填；聯合質詢留空，只靠 party
    membership = models.ForeignKey(Membership, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="articles")
    slug = models.SlugField(max_length=64, unique=True)

    title = models.CharField(max_length=300)
    # 聯合質詢與新北的黨團時段會串起十幾位講者（實測最長 88 字），留足空間
    speaker = models.CharField(max_length=300, db_index=True)
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
