from __future__ import annotations

from django.contrib import admin, messages

from .models import Article, ArticleStatus, Slide


class SlideInline(admin.TabularInline):
    model = Slide
    extra = 0


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("date", "speaker", "source", "meeting", "status", "attempts", "updated_at")
    list_filter = ("status", "source", "date", "speaker")
    search_fields = ("ivod_id", "speaker", "title", "transcript_text", "error")
    # 失敗原因與 GPU 工作編號是給人看的診斷資訊，不該在 admin 裡被改掉
    readonly_fields = ("error", "gpu_job_id", "attempts", "created_at", "updated_at",
                       "published_at")
    fieldsets = (
        (None, {"fields": ("source", "ivod_id", "slug", "title", "speaker", "meeting", "date",
                           "duration_seconds", "ivod_url")}),
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
