"""下架一支 YouTube 影片：刪文章，並刪 MEDIA_ROOT/articles/<ivod_id>/ 底下的截圖。

    python manage.py remove_video 'https://www.youtube.com/watch?v=jNQXAC9IVRw' --dry-run   # 先看會刪什麼
    python manage.py remove_video 2026-10-31-yt-jNQXAC9IVRw       # 文章的 slug
    python manage.py remove_video yt-jNQXAC9IVRw
    python manage.py remove_video yt-jNQXAC9IVRw --force          # 緊急下架：不等進行中的 GPU 工作

管理頁刪文章不會刪檔案（截圖網址還能用），把文章改回失敗或待處理也沒有用——夜間排程會把它重做回來。
所以要下架，用這個指令。跑兩次是安全的；上一次做到一半（文章刪了、檔案沒刪完）再跑一次就收尾。

影片正在被處理（有指令在等 GPU）時預設拒絕：現在刪，GPU 做的工作會白費。但這個守衛最久擋 2 × GPU_JOB_TIMEOUT_SECONDS
（預設一小時），緊急下架（版權、隱私、放錯影片）不能等：--force 跳過它，印出警告，其餘照做。GPU 上的工作不會被取消，
做完的結果會被丟棄（save_result 發現文章不在，丟 ArticleDeleted），不會把影片寫回來；等著它的 add_video 以失敗結束。

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

# --force 跳過「有 GPU 工作在進行」的守衛時印的警告（--dry-run 也印：真的做的話就是這樣）
IN_PROGRESS_FORCED = (
    "警告：這支影片還在處理中（有指令正在等 GPU），而 --force 不等它。\n"
    "  GPU 上的工作不會被取消，它做完的結果會被丟棄，不會把影片寫回資料庫或網站（處理完成時發現文章不在，"
    "管線不會重建已下架的影片）；那個 add_video 指令會以失敗結束，說這支影片「處理途中被下架」。")


class Command(BaseCommand):
    help = "下架一支 YouTube 影片：刪文章與它的截圖檔案（--dry-run 先看會刪什麼）"

    def create_parser(self, *args, **kwargs):
        # 開頭是「-」的單獨影片 id 不會被當成選項
        return allow_dash_ids(super().create_parser(*args, **kwargs))

    def add_arguments(self, parser) -> None:
        parser.add_argument("target", metavar="網址、slug 或 yt-id",
                            help="影片網址、文章的 slug（2026-10-31-yt-…）或 yt-<影片 id>")
        parser.add_argument("--dry-run", action="store_true", help="只列出會刪什麼，不刪")
        parser.add_argument("--force", action="store_true",
                            help="影片還在處理中（有指令在等 GPU）也照樣下架，緊急下架用：GPU 上的工作不會被取消，"
                                 "做完的結果會被丟棄，不會把影片寫回來")

    def handle(self, *args, **options) -> None:
        ivod_id = self._resolve(options["target"])
        video_id = video_id_from_ivod_id(ivod_id)
        self.stdout.write(f"影片 {video_id}　{canonical_url(video_id)}")

        article = Article.objects.filter(ivod_id=ivod_id).first()
        left = self._minutes_in_progress(article) if article is not None else None
        if left is not None:
            if not options["force"]:
                self._refuse_in_progress(left)
            # --force：不等，但那個工作怎麼了要說清楚
            self.stderr.write(IN_PROGRESS_FORCED)
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

    def _minutes_in_progress(self, article: Article) -> int | None:
        """影片正在被處理（有指令在等 GPU）的話，回傳約還要幾分鐘它才會被視為卡住；沒有在處理是 None。

        「正在處理」有兩種樣子，而且 updated_at 都還在 STALE_FACTOR × 逾時之內才算：狀態是處理中（有指令
        claim 了它）；或有 gpu_job_id——不管狀態。--rerun 重做的影片一直是已完成（才會留在站上），接回去的舊工作
        也不經過 claim，只看狀態的話，這兩種都看不出有指令正在等 GPU。
        """
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        if article.status != ArticleStatus.PROCESSING and not article.gpu_job_id:
            return None
        if article.updated_at < timezone.now() - timedelta(seconds=STALE_FACTOR * timeout):
            return None
        return max(1, round((STALE_FACTOR * timeout - (timezone.now() - article.updated_at).total_seconds()) / 60))

    def _refuse_in_progress(self, left: int) -> None:
        """正在被處理的影片預設不下架：現在刪的話，GPU 的工作白做（處理完成時 save_result 發現文章不在，
        會把結果丟掉）。緊急的話用 --force。"""
        raise CommandError(
            "這支影片還在處理中（有指令正在等 GPU）：現在下架的話，GPU 做的工作會白費（做完時發現文章不在，"
            "結果會被丟掉，那個指令也會以失敗結束）。\n"
            f"等它做完再下架；如果那個指令已經不在了，約 {left} 分鐘後它會被視為卡住，到時就能下架。\n"
            "要現在就下架（例如要立刻讓它消失），加上 --force：GPU 上的工作不會被取消，做完的結果會被丟棄，影片不會回來。")
