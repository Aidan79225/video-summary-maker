"""事實查核：把已完成摘要的文章送去 GPU 主機查核。

    python manage.py factcheck_articles                 # 查核還沒查過的文章
    python manage.py factcheck_articles --article 171140
    python manage.py factcheck_articles --article 171140 --force   # 連人工審核過的也重跑
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.gpu_client import GpuApiClient
from articles.models import Article
from factchecks.runner import check_article, check_articles


class Command(BaseCommand):
    help = "把已完成摘要的文章送去 GPU 主機做事實查核"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=settings.FACTCHECK_DAILY_LIMIT)
        parser.add_argument("--article", help="只查核這一篇（IVOD id）")
        parser.add_argument("--force", action="store_true",
                            help="連已經有人工審核的也重跑（審核紀錄會被換掉）")

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        if options["article"]:
            article = Article.objects.filter(ivod_id=options["article"]).first()
            if article is None:
                raise CommandError(f"沒有這篇文章：{options['article']}")
            report = check_article(article, client, timeout, force=options["force"])
        else:
            report = check_articles(client, limit=options["limit"], timeout=timeout)
        self.stdout.write(str(report))
        for error in report.errors[:10]:
            self.stderr.write(f"  ! {error}")
