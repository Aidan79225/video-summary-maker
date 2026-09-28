from __future__ import annotations

from django.contrib import admin, messages

from .models import Article, ArticleStatus, Membership, Person, Slide


class SlideInline(admin.TabularInline):
    model = Slide
    extra = 0


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("date", "speaker", "party", "source", "meeting", "status", "attempts",
                    "updated_at")
    list_filter = ("status", "source", "party", "date", "speaker")
    search_fields = ("ivod_id", "speaker", "title", "transcript_text", "error")
    # 失敗原因與 GPU 工作編號是給人看的診斷資訊，不該在 admin 裡被改掉
    readonly_fields = ("error", "gpu_job_id", "attempts", "created_at", "updated_at",
                       "published_at")
    autocomplete_fields = ("membership",)
    fieldsets = (
        (None, {"fields": ("source", "ivod_id", "slug", "title", "speaker", "party", "membership",
                           "meeting", "date", "duration_seconds", "ivod_url")}),
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
    fields = ("source", "name", "party", "district", "term", "start_date", "end_date",
              "needs_review")


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
    list_display = ("name", "source", "party", "district", "term", "start_date", "end_date",
                    "needs_review", "synced_at")
    list_filter = ("source", "party", "needs_review", "term")
    search_fields = ("name", "external_id", "person__name")
    autocomplete_fields = ("person",)
