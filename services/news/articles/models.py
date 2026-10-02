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
    # 立法院也存（LYAPI 名冊的「黨團」，第 11 屆有 2 位無黨籍參加國民黨團）：院內紀錄的黨團一致率用。
    caucus = models.CharField(max_length=64, blank=True)
    # 所屬委員會（只有立法院）：LYAPI 的原樣字串清單，「第11屆第5會期：財政委員會」，一個會期
    # 可能有好幾個。存原樣而不拆表：只有議題分布的「委員會職掌」會讀它，每週同步整批覆寫。
    committees = models.JSONField(default=list, blank=True)
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


class Session(models.Model):
    """一個會期：人物側寫的比較單位（同一個議會、同一個會期的人互相比）。

    會期從文章的會議名稱解析出來（見 profiles.parse_session），不是另外維護的表。
    start_date／end_date 是**資料涵蓋範圍**——掛在這個會期的文章（任何狀態）最早與
    最晚的日期，不是官方起訖：同儕比較時大家的涵蓋範圍一樣才公平，市議會的官方起訖
    也沒有結構化來源。每晚重算時更新。
    """

    source = models.CharField(max_length=16, choices=ArticleSource.choices, db_index=True)
    # 屆次的數字（"11"），給頁面分組用
    term = models.CharField(max_length=16)
    # 立法院「第11屆第5會期」（臨時會併入它所屬的會期）；議會「第4屆第8次定期會」
    name = models.CharField(max_length=64)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["source", "-start_date", "-id"]
        constraints = [models.UniqueConstraint(fields=["source", "name"],
                                               name="unique_session_per_source")]

    def __str__(self) -> str:
        return f"{self.get_source_display()} {self.name}"


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
    # 從 meeting 解析出的會期。解析不出來（例如「立法院朝野黨團協商」）就留空，
    # 那篇不計入任何側寫指標。會期被刪掉時文章留著，下次重算再掛回去。
    session = models.ForeignKey(Session, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="articles")
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


class ProfileStat(models.Model):
    """人物側寫的快取：一個人在一個會期的一項指標。每晚整批重算（profiles.compute_profiles）。

    以 Person 為鍵而不是 Membership：同一會期內換黨會切成兩段任期，但那是同一個人。
    sample_ok 不存：投入量永遠成立、具體度就是 n >= MIN_SAMPLE，存了只會跟公式不同步。
    """

    person = models.ForeignKey(Person, related_name="profile_stats", on_delete=models.CASCADE)
    session = models.ForeignKey(Session, related_name="profile_stats", on_delete=models.CASCADE)
    # profiles.INDICATORS 的 key（speeches、speaking_minutes、numbers_per_speech…）
    indicator = models.CharField(max_length=32)
    # 分母是 0 時是 null（例如一篇基礎文章都沒有的具體度）
    value = models.FloatField(null=True, blank=True)
    n = models.PositiveIntegerField(default=0)
    # 樣本不足或同儕不足時是 null
    percentile = models.FloatField(null=True, blank=True)
    # 同儕人數：頁面寫「在 N 位同儕中」
    peers = models.PositiveIntegerField(default=0)
    # 議題分布的列是哪個分類器分出來的（其他指標留空）。API 只在它等於現在上線的分類器時才給
    # 議題區塊：重算失敗、或跟評估同時跑時，不會把 A 版算的數字掛上 B 版的名字
    classifier = models.CharField(max_length=200, blank=True, default="")
    computed_at = models.DateTimeField()

    class Meta:
        ordering = ["session", "person", "indicator"]
        constraints = [models.UniqueConstraint(fields=["person", "session", "indicator"],
                                               name="unique_profile_stat")]

    def __str__(self) -> str:
        return f"{self.person} {self.session} {self.indicator}={self.value}"


class Topic(models.Model):
    """一篇文章的政策領域，由 GPU 的 topic 工作分的（topics.classify_topics）。

    代碼的清單在 topics.TOPICS，這裡刻意不設 choices：topics 要讀這張表，表再回頭 import
    topics 就是循環；寫入只有 topics.parse_result 一個入口，它會擋掉清單外的代碼。
    文章重產（ingest.save_result）時刪掉，下一輪重新分類。
    """

    article = models.OneToOneField(Article, related_name="topic", on_delete=models.CASCADE)
    primary = models.CharField(max_length=16, db_index=True)
    # 空字串＝沒有次領域
    secondary = models.CharField(max_length=16, blank=True)
    # GPU 回來的那串：模型＋提示詞版本（「qwen3:14b#topic-v1」）。指標只認通過評估的那個版本
    classifier = models.CharField(max_length=200, db_index=True)
    labeled_at = models.DateTimeField()

    class Meta:
        ordering = ["-labeled_at", "-id"]
        verbose_name = verbose_name_plural = "議題分類（模型）"

    def __str__(self) -> str:
        return f"{self.article} → {self.primary}"


class TopicLabel(models.Model):
    """人工標註的主領域：議題分類器的標註集（topics.sample_labels 抽、admin 標、eval_topics 評）。

    文章重產時不刪：發言講的是什麼議題，不會因為摘要重寫而改變。
    """

    article = models.OneToOneField(Article, related_name="topic_label", on_delete=models.CASCADE)
    # 空字串＝還沒標
    primary = models.CharField(max_length=16, blank=True, db_index=True)
    note = models.CharField(max_length=300, blank=True)
    labeled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["article__source", "article_id"]
        verbose_name = verbose_name_plural = "議題標註"

    def __str__(self) -> str:
        return f"{self.article} 標註：{self.primary or '還沒標'}"


class TopicEvaluation(models.Model):
    """一次評估、一個來源的成績。某來源的議題指標只用最新一筆 passed 的 classifier。"""

    source = models.CharField(max_length=16, choices=ArticleSource.choices, db_index=True)
    classifier = models.CharField(max_length=200)
    labeled = models.PositiveIntegerField()
    correct = models.PositiveIntegerField()
    # 0～1
    accuracy = models.FloatField()
    passed = models.BooleanField(db_index=True)
    # 判錯的每一筆：[{article, slug, speaker, human, model, error?}]，model 為 null 是分類失敗
    mistakes = models.JSONField(default=list, blank=True)
    ran_at = models.DateTimeField()

    class Meta:
        ordering = ["-ran_at", "-id"]
        verbose_name = verbose_name_plural = "議題分類評估"

    def __str__(self) -> str:
        return (f"{self.get_source_display()} {self.classifier} "
                f"{self.correct}/{self.labeled}{'（通過）' if self.passed else ''}")


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


# --- 立法院的院內紀錄（出席、提案、表決）：ly_records.sync_ly_records 每週從 LYAPI 同步 ---


class LyMeetingKind(models.TextChoices):
    PLENARY = "plenary", "院會"
    # 聯席會議也存成委員會：出席率只問「單位裡有沒有他的委員會」
    COMMITTEE = "committee", "委員會"


class LyMeeting(models.Model):
    """一場院會或委員會會議（含聯席會議）與出席名單。

    以會議代碼為鍵而不是每一天一筆：LYAPI 的一場會議可以開好幾天，每天的簽到名單一樣，
    出席率的單位是「場」。
    """

    # LYAPI 的會議代碼（「院會-11-5-23」「聯席會議-11-3-20,36-1」）
    code = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=LyMeetingKind.choices, db_index=True)
    term = models.PositiveSmallIntegerField()
    session_number = models.PositiveSmallIntegerField()
    # 第一天：判斷「他那時在不在任」用
    date = models.DateField(null=True, blank=True)
    # 每一天（ISO 字串）：表決時間只有月日、沒有年，要靠它對回日期
    dates = models.JSONField(default=list, blank=True)
    name = models.CharField(max_length=300)
    # 會議單位：院會是「院會」，委員會是委員會名稱，聯席會議是全部的委員會
    units = models.JSONField(default=list, blank=True)
    # 出席委員（LYAPI 的原樣姓名）。null＝LYAPI 還沒有這場的出席紀錄（委員會的議事錄常常晚好幾週），
    # 不算進任何人的分母——不能當成「沒有人出席」
    attendees = models.JSONField(null=True, blank=True, default=None)
    # 議事網的會議頁
    url = models.URLField(max_length=500, blank=True)
    synced_at = models.DateTimeField()

    class Meta:
        ordering = ["-date", "code"]
        indexes = [models.Index(fields=["term", "session_number"])]
        verbose_name = verbose_name_plural = "立法院會議"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class LyBill(models.Model):
    """一件委員提案（主提案人、連署人、議案狀態）。"""

    # LYAPI 的議案編號
    bill_no = models.CharField(max_length=32, unique=True)
    term = models.PositiveSmallIntegerField()
    session_number = models.PositiveSmallIntegerField()
    # 公投案的主文整段都在名稱裡，可能好幾百字
    name = models.TextField()
    status = models.CharField(max_length=64, blank=True)
    # 姓名清單（原樣）。黨團提案的提案人裡會有「台灣民眾黨立法院黨團」，對不到任何人、不算給誰
    proposers = models.JSONField(default=list, blank=True)
    cosigners = models.JSONField(default=list, blank=True)
    # 議事網的議案頁
    url = models.URLField(max_length=500, blank=True)
    proposed_on = models.DateField(null=True, blank=True)
    synced_at = models.DateTimeField()

    class Meta:
        ordering = ["-proposed_on", "-bill_no"]
        indexes = [models.Index(fields=["term", "session_number"])]
        verbose_name = verbose_name_plural = "立法院委員提案"

    def __str__(self) -> str:
        return f"{self.bill_no} {self.name[:40]}"


class LyVote(models.Model):
    """一次記名表決：每位委員投了什麼。"""

    # LYAPI 的表決代碼
    code = models.CharField(max_length=64, unique=True)
    term = models.PositiveSmallIntegerField()
    session_number = models.PositiveSmallIntegerField()
    meeting_code = models.CharField(max_length=64, db_index=True)
    # 表決時間的原文（「中華民國115年3月20日 上午11時52分30秒」，偶爾沒有年）
    voted_at = models.CharField(max_length=100, blank=True)
    # 從原文與會議日期推出來的日期：判斷「他那時在不在任」用
    date = models.DateField(null=True, blank=True)
    topic = models.TextField(blank=True)
    yes = models.JSONField(default=list, blank=True)
    no = models.JSONField(default=list, blank=True)
    abstain = models.JSONField(default=list, blank=True)
    voters = models.JSONField(default=list, blank=True)
    synced_at = models.DateTimeField()

    class Meta:
        ordering = ["-date", "-code"]
        indexes = [models.Index(fields=["term", "session_number"])]
        verbose_name = verbose_name_plural = "立法院記名表決"

    def __str__(self) -> str:
        return f"{self.code} {self.topic[:40]}"
