from __future__ import annotations

from django.contrib import admin

from .models import Claim, Evidence, FactCheckRun, ReviewStatus
from .review import review


class EvidenceInline(admin.TabularInline):
    model = Evidence
    extra = 0


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("article_date", "speaker", "verdict", "review_status", "statement")
    list_filter = ("review_status", "verdict", "kind")
    search_fields = ("statement", "quote", "article__speaker")
    # 判定與審核只能走下面的核准／駁回：在編輯頁直接改，會跳過 review()，
    # reviewed_at 不會記錄，也能把自動判定改成任何結果
    readonly_fields = ("verdict", "review_status", "reviewed_at", "method", "kind", "quote")
    # 清單的日期、委員欄位都讀 article；不先 join 會每列多一次查詢
    list_select_related = ("article",)
    inlines = [EvidenceInline]
    actions = ["approve", "reject"]

    @admin.display(description="日期", ordering="article__date")
    def article_date(self, obj: Claim):
        return obj.article.date

    @admin.display(description="委員", ordering="article__speaker")
    def speaker(self, obj: Claim) -> str:
        return obj.article.speaker

    @admin.action(description="核准（公開並計入查證相符率）")
    def approve(self, request, queryset) -> None:
        self.message_user(request, f"已核准 {review(queryset, ReviewStatus.APPROVED)} 則")

    @admin.action(description="駁回（不公開、不計分）")
    def reject(self, request, queryset) -> None:
        self.message_user(request, f"已駁回 {review(queryset, ReviewStatus.REJECTED)} 則")


@admin.register(FactCheckRun)
class FactCheckRunAdmin(admin.ModelAdmin):
    list_display = ("article", "status", "attempts", "model", "checked_at")
    list_filter = ("status",)
    search_fields = ("article__ivod_id", "article__speaker")
