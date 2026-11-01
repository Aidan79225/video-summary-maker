"""下架一支 YouTube 影片：刪文章，並刪 MEDIA_ROOT/articles/<ivod_id>/ 底下的截圖。

    python manage.py remove_video 'https://www.youtube.com/watch?v=jNQXAC9IVRw' --dry-run   # 先看會刪什麼
    python manage.py remove_video 2026-10-31-yt-jNQXAC9IVRw       # 文章的 slug
    python manage.py remove_video yt-jNQXAC9IVRw

管理頁刪文章不會刪檔案（截圖網址還能用），把文章改回失敗或待處理也沒有用——夜間排程會把它重做回來。
所以要下架，用這個指令。跑兩次是安全的；上一次做到一半（文章刪了、檔案沒刪完）再跑一次就收尾。

只收 YouTube 影片：要刪的是哪一支，一律從網址、slug 或 yt-<影片 id> 解析出 11 碼的 id、確認格式之後才組
出路徑，不把使用者打的字直接拼進去。
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from articles.ingest import STALE_FACTOR, remove_video, video_media_folder
from articles.models import Article, ArticleStatus
from articles.youtube_source import (UrlRejected, allow_dash_ids, canonical_url, parse_video_url,
                                     video_id_from_ivod_id)

# 文章的 slug：<日期>-yt-<影片 id>
_SLUG = re.compile(r"\d{4}-\d{2}-\d{2}-(yt-[A-Za-z0-9_-]{11})")


class Command(BaseCommand):
    help = "下架一支 YouTube 影片：刪文章與它的截圖檔案（--dry-run 先看會刪什麼）"

    def create_parser(self, *args, **kwargs):
        # 開頭是「-」的單獨影片 id 不會被當成選項
        return allow_dash_ids(super().create_parser(*args, **kwargs))

    def add_arguments(self, parser) -> None:
        parser.add_argument("target", metavar="網址、slug 或 yt-id",
                            help="影片網址、文章的 slug（2026-10-31-yt-…）或 yt-<影片 id>")
        parser.add_argument("--dry-run", action="store_true", help="只列出會刪什麼，不刪")

    def handle(self, *args, **options) -> None:
        ivod_id = self._resolve(options["target"])
        video_id = video_id_from_ivod_id(ivod_id)
        self.stdout.write(f"影片 {video_id}　{canonical_url(video_id)}")

        article = Article.objects.filter(ivod_id=ivod_id).first()
        if article is not None:
            self._refuse_if_in_progress(article)
        try:
            report = remove_video(ivod_id, dry_run=options["dry_run"])
        except ValueError as e:
            raise CommandError(str(e)) from e

        what = "會刪除" if options["dry_run"] else "已刪除"
        if report.existed:
            self.stdout.write(f"{what}：文章 /article/{article.slug}（{article.get_status_display()}，{report.slides} 段）")
        else:
            self.stdout.write("資料庫裡沒有這支影片（沒加過，或已經下架了）。")
        if report.files:
            self.stdout.write(f"{what}：截圖等檔案 {len(report.files)} 個，與資料夾 {video_media_folder(ivod_id)}/")
        else:
            self.stdout.write(f"沒有截圖檔案要刪（{video_media_folder(ivod_id)}/ 是空的或不存在）。")
        for stray in report.strays:
            self.stderr.write(f"  ! 資料庫記著 {stray}，但它不在這支影片的資料夾底下，沒有刪")
        if report.errors:
            for error in report.errors:
                self.stderr.write(f"  ! {error}")
            raise CommandError("有檔案沒刪掉（上面列出）。排除原因之後再執行一次同樣的指令就會收尾。")
        if options["dry_run"]:
            self.stdout.write("（--dry-run：什麼都沒刪）")
        elif report.existed:
            self.stdout.write("截圖網址在 Cloudflare 的邊緣快取最多留一天（max-age=86400）；急的話到 Cloudflare 後台手動清快取。")

    def _resolve(self, text: str) -> str:
        """網址、slug 或 yt-<id> → ivod_id（yt-<11 碼影片 id>）。看不懂就說，不猜。"""
        raw = text.strip()
        matched = _SLUG.fullmatch(raw)
        if matched:
            return matched.group(1)
        if video_id_from_ivod_id(raw) is not None:
            return raw
        try:
            return parse_video_url(raw).ivod_id
        except UrlRejected as e:
            raise CommandError(f"看不出要下架哪一支影片：{e}\n可以給影片網址、文章的 slug（2026-10-31-yt-…），或 yt-<影片 id>") from e

    def _refuse_if_in_progress(self, article: Article) -> None:
        """正在被處理的影片現在不能下架：處理完成時 save_result 會把文章寫回來（文章已刪的話，它會重新建立）。"""
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        if article.status != ArticleStatus.PROCESSING:
            return
        if article.updated_at < timezone.now() - timedelta(seconds=STALE_FACTOR * timeout):
            return
        left = max(1, round((STALE_FACTOR * timeout - (timezone.now() - article.updated_at).total_seconds()) / 60))
        raise CommandError(
            "這支影片還在處理中（有指令正在等 GPU）：現在下架的話，處理完成時文章會被寫回來。\n"
            f"等它做完再下架；如果那個指令已經不在了，約 {left} 分鐘後它會被視為卡住，到時就能下架。")
