"""同步議員名單（政黨、選區、任期）。

    python manage.py sync_members                 # 立法院 + 臺中市議會
    python manage.py sync_members --source tccc
    python manage.py sync_members --relink        # 同步後把所有文章重新標政黨
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from articles.members_sync import (LyMemberSource, MembersUnavailable, TcccMemberSource,
                                   relink_all, sync)


class Command(BaseCommand):
    help = "從 LYAPI 與臺中市議會官網同步議員名單，維護人物／任期／政黨"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--source", choices=["ly", "tccc"],
                            help="只同步這個來源；不給就全部")
        parser.add_argument("--relink", action="store_true",
                            help="同步後把所有文章依任期重新填政黨（回填既有文章用）")

    def handle(self, *args, **options) -> None:
        for source in self._sources(options.get("source")):
            try:
                records = source.fetch()
            except MembersUnavailable as e:
                self.stderr.write(f"  ! {source.name}：{e}")
                continue
            report = sync(records)
            self.stdout.write(f"{source.name}：{report}")
        if options["relink"]:
            self.stdout.write(f"重新連結 {relink_all()} 篇文章")

    def _sources(self, only: str | None) -> list:
        sources = []
        if only in (None, "ly"):
            sources.append(LyMemberSource(settings.LYAPI_BASE, settings.LY_TERM))
        if only in (None, "tccc"):
            sources.append(TcccMemberSource(settings.TCCC_WEB_BASE))
        return sources
