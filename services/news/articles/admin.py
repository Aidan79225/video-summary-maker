from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from . import topics
from .models import (Article, ArticleStatus, Membership, Person, ProfileStat, Session, Slide, Topic,
                     TopicEvaluation, TopicLabel)
from .profiles import compute_profiles


class SlideInline(admin.TabularInline):
    model = Slide
    extra = 0


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("date", "speaker", "party", "source", "meeting", "status", "attempts",
                    "updated_at")
    list_filter = ("status", "source", "session", "party", "date", "speaker")
    search_fields = ("ivod_id", "speaker", "title", "transcript_text", "error")
    # 失敗原因與 GPU 工作編號是給人看的診斷資訊，不該在 admin 裡被改掉
    readonly_fields = ("error", "gpu_job_id", "attempts", "created_at", "updated_at",
                       "published_at")
    # 會期可以手動改：每晚的重算只替「還沒掛會期」的文章掛，不會蓋掉人改過的
    autocomplete_fields = ("membership", "session")
    fieldsets = (
        (None, {"fields": ("source", "ivod_id", "slug", "title", "speaker", "party", "membership",
                           "meeting", "session", "date", "duration_seconds", "ivod_url")}),
        ("處理狀態", {"fields": ("status", "attempts", "error", "gpu_job_id",
                                 "created_at", "updated_at", "published_at")}),
        ("內容", {"classes": ("collapse",),
                  "fields": ("source_note", "brief", "transcript_text")}),
    )
    inlines = [SlideInline]
    actions = ["requeue"]

    @admin.action(description="打回待處理（清掉錯誤與重試次數，下次執行會重做）")
    def requeue(self, request, queryset):
        """失敗或卡住的文章勾一勾就能重送，不必下 shell。

        已完成的刻意不收：打回 pending 會讓它從網站上消失直到重做完成；
        要重產已完成的文章用 `ingest_ivod --rerun-ready`，那條路不會下架。
        """
        ready = queryset.filter(status=ArticleStatus.READY).count()
        updated = (queryset.exclude(status=ArticleStatus.READY)
                   .update(status=ArticleStatus.PENDING, error="", attempts=0, gpu_job_id=""))
        self.message_user(request, f"已打回待處理 {updated} 篇。", messages.SUCCESS)
        if ready:
            self.message_user(
                request,
                f"略過 {ready} 篇已完成的文章：要重產請用 ingest_ivod --rerun-ready，"
                "打回待處理會讓它們先從網站上消失。",
                messages.WARNING)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("source", "name", "party", "district", "term", "role", "caucus", "start_date",
              "end_date", "needs_review")


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("name", "memberships_summary", "needs_review", "updated_at")
    list_filter = ("needs_review",)
    search_fields = ("name", "aliases")
    inlines = [MembershipInline]
    actions = ["merge"]

    @admin.display(description="任期")
    def memberships_summary(self, person):
        return "；".join(f"{m.get_source_display()} {m.term} {m.party}".strip()
                         for m in person.memberships.all()) or "—"

    @admin.action(description="合併成同一個人（保留最早建立的，其餘的任期與文章搬過去）")
    def merge(self, request, queryset):
        """同步時認人只靠姓名，跨議會或改了寫法的同一個人會被建成兩個；在這裡合併。"""
        people = list(queryset.order_by("id"))
        if len(people) < 2:
            self.message_user(request, "要勾選兩個以上的人物才能合併。", messages.WARNING)
            return
        keep, rest = people[0], people[1:]
        aliases = list(keep.aliases or [])
        for p in rest:
            for name in [p.name, *(p.aliases or [])]:
                if name != keep.name and name not in aliases:
                    aliases.append(name)
            p.memberships.update(person=keep)
            p.delete()
        keep.aliases = aliases
        keep.needs_review = False
        keep.save()
        self.message_user(request, f"已把 {len(rest)} 個人物併入「{keep.name}」。", messages.SUCCESS)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("name", "source", "party", "district", "term", "role", "caucus",
                    "start_date", "end_date", "needs_review", "synced_at")
    list_filter = ("source", "party", "needs_review", "term", "role", "caucus")
    search_fields = ("name", "external_id", "person__name")
    autocomplete_fields = ("person",)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    """會期是從會議名稱解析出來的，涵蓋範圍每晚重算：在這裡改了也會被蓋掉，所以只給看。

    刪除照常可用：文章的會期會變成空的，下次 compute_profiles 再依會議名稱掛回去。
    """

    list_display = ("name", "source", "term", "start_date", "end_date")
    list_filter = ("source", "term")
    search_fields = ("name",)
    readonly_fields = ("source", "term", "name", "start_date", "end_date")

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(ProfileStat)
class ProfileStatAdmin(admin.ModelAdmin):
    """側寫快取：每晚整批重算，手改會被下一次重算蓋掉，所以只給看。要重算跑 compute_profiles。"""

    list_display = ("person", "session", "indicator", "value", "n", "percentile", "peers",
                    "computed_at")
    list_filter = ("session__source", "indicator", "session")
    search_fields = ("person__name",)
    list_select_related = ("person", "session")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


# --- 議題分布 ---


class TopicLabelForm(forms.ModelForm):
    """主領域用下拉選單選，選項來自 topics.TOPICS（唯一的來源）；空的就是還沒標。"""

    primary = forms.ChoiceField(label="主領域", required=False,
                                choices=[("", "（還沒標）"), *topics.TOPIC_CHOICES])

    class Meta:
        model = TopicLabel
        fields = ("primary", "note")


class LabelledFilter(admin.SimpleListFilter):
    title = "標註狀態"
    parameter_name = "labelled"

    def lookups(self, request, model_admin):
        return (("no", "還沒標"), ("yes", "已標"))

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(primary="")
        if self.value() == "yes":
            return queryset.exclude(primary="")
        return queryset


@admin.register(TopicLabel)
class TopicLabelAdmin(admin.ModelAdmin):
    """議題標註：盲標。清單上直接顯示分類器讀到的那段文字，主領域在清單上直接選。

    **不要在這裡（或 ArticleAdmin）加任何 Topic 的欄位、篩選或搜尋**：看得到模型的答案，人就會
    跟著它標，評估量到的就變成「模型跟自己有多像」。

    不能在這裡新增：標註集要用 sample_topic_labels 隨機抽，人手挑的文章會偏向好分的。
    """

    form = TopicLabelForm
    list_display = ("article_link", "source", "classifier_text", "primary", "note", "labeled_at")
    # 文章欄是連到文章本身的連結，不是這一筆的編輯頁：標註就在清單上做
    list_display_links = None
    list_editable = ("primary", "note")
    list_filter = (LabelledFilter, "article__source")
    search_fields = ("article__speaker", "article__ivod_id")
    list_per_page = 20
    fields = ("article_link", "classifier_text", "primary", "note", "labeled_at")
    readonly_fields = ("article_link", "classifier_text", "labeled_at")

    def get_queryset(self, request):
        return (super().get_queryset(request).select_related("article")
                .prefetch_related("article__slides"))

    def get_changelist_form(self, request, **kwargs):
        # 清單上的表單預設不用 self.form；不指定的話主領域會變成沒有選項的文字框
        kwargs.setdefault("form", TopicLabelForm)
        return super().get_changelist_form(request, **kwargs)

    def has_add_permission(self, request) -> bool:
        return False

    def save_model(self, request, obj, form, change):
        if "primary" in form.changed_data:
            obj.labeled_at = timezone.now() if obj.primary else None
        super().save_model(request, obj, form, change)

    @admin.display(description="文章")
    def article_link(self, label: TopicLabel):
        article = label.article
        return format_html('<a href="{}">{} {}</a><br><a href="{}" target="_blank" rel="noopener">原始影片</a>',
                           reverse("admin:articles_article_change", args=[article.pk]),
                           article.date, article.speaker, article.ivod_url)

    @admin.display(description="來源", ordering="article__source")
    def source(self, label: TopicLabel) -> str:
        return label.article.get_source_display()

    @admin.display(description="分類器讀到的文字")
    def classifier_text(self, label: TopicLabel):
        # 跟送給 GPU 的是同一個函式：標的人跟模型讀的是同一段文字
        return format_html('<div style="white-space: pre-line; max-width: 40em">{}</div>',
                           topics.classifier_input(label.article))


class _ReadOnlyAdmin(admin.ModelAdmin):
    """算出來的資料：手改會跟產生它的流程對不上，只給看（要改就重跑指令）。刪除照常可用。"""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Topic)
class TopicAdmin(_ReadOnlyAdmin):
    """模型分的領域。刪掉一筆，下一輪 classify_topics 會重分那篇。"""

    list_display = ("article", "primary_label", "secondary_label", "classifier", "labeled_at")
    list_filter = ("classifier", "primary", "article__source")
    search_fields = ("article__speaker", "article__ivod_id")
    list_select_related = ("article",)

    @admin.display(description="主領域", ordering="primary")
    def primary_label(self, topic: Topic) -> str:
        area = topics.TOPIC_BY_KEY.get(topic.primary)
        return area.label if area else topic.primary

    @admin.display(description="次領域")
    def secondary_label(self, topic: Topic) -> str:
        area = topics.TOPIC_BY_KEY.get(topic.secondary)
        return area.label if area else (topic.secondary or "—")


@admin.register(TopicEvaluation)
class TopicEvaluationAdmin(_ReadOnlyAdmin):
    """評估紀錄。刪掉通過的評估會改變上線條件：那個來源退回較舊的通過版本，或整個下架。

    刪完之後上線的分類器變了，就當場重算人物側寫（同 eval_topics）。API 每次都看最新的評估，
    側寫的數字卻是上一次重算時的分類器算的：不重算的話，區塊上寫的是舊版本的名稱與準確率、
    數字卻是剛刪掉的那個版本分的，證據清單的篇數也兜不攏，直到明早排程跑完。
    """

    list_display = ("source", "classifier", "labeled", "correct", "accuracy_percent", "passed",
                    "ran_at")
    list_filter = ("source", "passed", "classifier")

    @admin.display(description="準確率", ordering="accuracy")
    def accuracy_percent(self, evaluation: TopicEvaluation) -> str:
        return f"{evaluation.accuracy * 100:.1f}%"

    def delete_model(self, request, obj) -> None:
        before = topics.passing_classifiers()
        super().delete_model(request, obj)
        self._recompute_if_the_gate_moved(request, before)

    def delete_queryset(self, request, queryset) -> None:
        before = topics.passing_classifiers()
        super().delete_queryset(request, queryset)
        self._recompute_if_the_gate_moved(request, before)

    def _recompute_if_the_gate_moved(self, request, before: dict[str, str]) -> None:
        if topics.passing_classifiers() == before:
            return
        compute_profiles()
        self.message_user(request, "上線的分類器變了，已經重算人物側寫", messages.INFO)
