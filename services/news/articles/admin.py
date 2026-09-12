from __future__ import annotations

from django.contrib import admin

from .models import Article, Slide


class SlideInline(admin.TabularInline):
    model = Slide
    extra = 0


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("date", "speaker", "meeting", "status", "updated_at")
    list_filter = ("status", "date", "speaker")
    search_fields = ("ivod_id", "speaker", "title", "transcript_text")
    inlines = [SlideInline]
