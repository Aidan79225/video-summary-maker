"""替多人影片的說話者取名字、併人、抽查有沒有安對人。不碰 GPU：只改資料庫裡的說話者清單。

    python manage.py label_video 影片                      # 印對照表（預設；每位說話者附幾句話和可以跳到那一秒的連結）
    python manage.py label_video 影片 --name S1=黃國昌 --name S2=王小明 --role S1=主持人
    python manage.py label_video 影片 --merge S3=S1       # S3 和 S1 其實是同一個人（例如連線進來的聲音被分開了）
    python manage.py label_video 影片 --clear S1          # 取消 S1 的名字、角色與併法
    python manage.py label_video 影片 --audit 5 [--seed 7]  # 抽 5 個重點：誰說的、依據是哪句話、連結跳到那一秒

影片可以是 slug（2026-10-30-yt-xxxxxxxxxxx）、11 碼影片 id，或網址（網址要加單引號）。

名字只由你設定，程式不猜：GPU 只區分「這是同一個聲音」，不知道那個人是誰。聽過、確定了才取名字；沒取名字的說話者
在網站上顯示成「發言者 A」這種中性稱呼。改了馬上生效（網站讀取時才換名字），不必重跑 GPU。每一項設定都先驗證，
有一項不對就整批都不套用。

重做影片（add_video --rerun）之後說話者編號可能全部重新排，這裡設的名字與併法不會保留。
"""
from __future__ import annotations

import random
import re
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from articles.models import Article, ArticleSource, ArticleStatus
from articles.speaker_labels import Labels, LabelError, apply_labels, changed, split_pair
from articles.speaker_sheet import clock, describe, next_step_lines, plain, sheet_lines, video_id_of, watch_link
from articles.speakers import display_of, has_structured_speakers
from articles.youtube_source import UrlRejected, allow_dash_ids, clean_text, parse_video_url

MAX_AUDIT = 100
_NUMBER = re.compile(r"[0-9]{1,9}")


class Command(BaseCommand):
    help = "替多人影片的說話者取名字、併人、抽查有沒有安對人（不碰 GPU）"

    def create_parser(self, *args, **kwargs):
        # 開頭是「-」的單獨影片 id 不會被當成選項（跟 add_video 一樣）
        return allow_dash_ids(super().create_parser(*args, **kwargs))

    def add_arguments(self, parser) -> None:
        parser.add_argument("video", metavar="影片", help="slug、11 碼影片 id 或網址（網址要加單引號）")
        parser.add_argument("--sheet", action="store_true", help="印說話者對照表（沒有其他選項時預設就印）")
        parser.add_argument("--name", action="append", default=[], metavar="S1=名字",
                            help="替說話者取名字（最多 40 字；不能含 [[ ]]、冒號、換行）。可以重複")
        parser.add_argument("--role", action="append", default=[], metavar="S1=角色",
                            help="說話者的角色或職稱，例如 主持人（最多 20 字）。可以重複")
        parser.add_argument("--merge", action="append", default=[], metavar="S3=S1",
                            help="S3 和 S1 是同一個人：S3 的話之後顯示成 S1。只併一層，可以重複")
        parser.add_argument("--clear", action="append", default=[], metavar="S1",
                            help="取消 S1 的名字、角色與併法。可以重複")
        parser.add_argument("--audit", metavar="N", help=f"抽 N 個重點（1～{MAX_AUDIT}）：誰說的、依據、連結")
        parser.add_argument("--seed", metavar="N", help="--audit 的抽樣種子；同一個種子抽到同一批")

    def handle(self, *args, **options) -> None:
        audit, seed = self._audit_options(options)
        labels = self._labels(options)
        article = self._find(options["video"])
        self.stdout.write(f"影片 {video_id_of(article) or article.ivod_id}　/article/{article.slug}　"
                          f"{clean_text(article.title)}")

        if not labels.empty:
            article = self._apply(article, labels)
        if options["sheet"] or (labels.empty and audit is None):
            self._sheet(article)
        if audit is not None:
            self._audit(article, audit, seed)

    # --- 參數 ---

    @staticmethod
    def _number(options, name: str, low: int, high: int) -> int | None:
        text = options[name]
        if text is None:
            return None
        text = text.strip()
        if not _NUMBER.fullmatch(text) or not low <= int(text) <= high:
            raise CommandError(f"--{name} 要是 {low}～{high} 的整數，不是 {clean_text(text)[:40]!r}")
        return int(text)

    def _audit_options(self, options) -> tuple[int | None, int | None]:
        audit = self._number(options, "audit", 1, MAX_AUDIT)
        seed = self._number(options, "seed", 0, 2**31 - 1)
        if seed is not None and audit is None:
            raise CommandError("--seed 只搭配 --audit 用（它是抽樣的種子）")
        return audit, seed

    @staticmethod
    def _labels(options) -> Labels:
        """命令列的設定 → Labels。格式不對的一次全部說出來。"""
        labels = Labels(clears=[text.strip() for text in options["clear"]])
        problems = []
        for flag, target in (("--name", labels.names), ("--role", labels.roles), ("--merge", labels.merges)):
            for raw in options[flag.lstrip("-")]:
                try:
                    speaker_id, value = split_pair(raw, flag)
                except LabelError as e:
                    problems.append(str(e))
                    continue
                target.append((speaker_id, value.strip() if flag == "--merge" else value))
        if problems:
            raise CommandError("設定不對，一項都沒有套用：\n  " + "\n  ".join(problems))
        return labels

    # --- 找影片 ---

    @staticmethod
    def _find(text: str) -> Article:
        """slug、影片 id 或網址 → 有說話者清單的 YouTube 影片。不是的話說清楚為什麼。"""
        text = (text or "").strip()
        article = Article.objects.filter(slug=text).first()
        if article is None:
            try:
                article = Article.objects.filter(ivod_id=parse_video_url(text).ivod_id).first()
            except UrlRejected:
                article = None
        shown = clean_text(text)[:80]
        if article is None:
            raise CommandError(f"找不到這支影片：{shown!r}。先用 add_video 加入，或檢查 slug、影片 id、網址。")
        if article.source != ArticleSource.YOUTUBE:
            raise CommandError("這不是 YouTube 影片：議會的質詢只有一位講者，沒有說話者要標。")
        if not has_structured_speakers(article.speakers):
            if article.status != ArticleStatus.READY:
                raise CommandError(f"這支影片還沒處理完（狀態：{article.get_status_display()}），做完才有說話者可以標。")
            raise CommandError(
                "這支影片沒有說話者的資料：它是用單人模式（--speakers 1）或第一部分的流程處理的，沒有區分說話的人。\n"
                f"要區分的話：python manage.py add_video {article.ivod_url!r} --rerun"
                "（不加 --speakers 就是自動區分）。")
        return article

    # --- 套用 ---

    def _apply(self, article: Article, labels: Labels) -> Article:
        """驗證通過才寫，一次寫完。

        在交易裡重讀這一列再算：算新清單到寫進去之間，add_video --rerun 可能剛好做完、把說話者換了一批
        （編號全變）；用舊清單算的結果寫回去，名字就掛到別人身上了。SQLite 的 IMMEDIATE 交易一開始就拿寫入鎖。
        """
        with transaction.atomic():
            fresh = Article.objects.filter(pk=article.pk).first()
            if fresh is None:
                raise CommandError("這支影片剛剛被下架了（remove_video），什麼都沒有改。")
            if not has_structured_speakers(fresh.speakers):
                raise CommandError("這支影片的說話者資料剛剛被換掉了（正在重做？），什麼都沒有改；稍後再試。")
            try:
                updated = apply_labels(fresh.speakers, labels)
            except LabelError as e:
                raise CommandError("設定不對，一項都沒有套用：\n  " + str(e).replace("\n", "\n  ")) from e
            touched = changed(fresh.speakers, updated)
            if touched:
                fresh.speakers = updated
                fresh.save(update_fields=["speakers", "updated_at"])
        self._report(fresh, touched)
        return fresh

    def _report(self, article: Article, touched: list[str]) -> None:
        if not touched:
            self.stdout.write("沒有任何變動（設定和現在的一樣）。")
            return
        self.stdout.write(f"已更新 {len(touched)} 位說話者（網站上馬上會換，不必重跑 GPU）：")
        by_id = {s["id"]: s for s in article.speakers}
        for speaker_id in touched:
            speaker = by_id[speaker_id]
            self.stdout.write(f"  {speaker_id}　{plain(speaker.get('label'))}　→　{describe(article.speakers, speaker)}")
            samples = speaker.get("samples") if isinstance(speaker.get("samples"), list) else []
            sample = next((x for x in samples if isinstance(x, dict)), None)
            if sample:
                self.stdout.write(f"      例：「{plain(sample.get('text'))}」{clock(sample.get('start'))}")

    # --- 印出來給人看的 ---

    def _sheet(self, article: Article) -> None:
        for line in sheet_lines(article):
            self.stdout.write(line)
        self.stdout.write("")
        for line in next_step_lines(article):
            self.stdout.write(line)

    def _audit(self, article: Article, count: int, seed: int | None) -> None:
        """隨機抽 count 個重點，一個一個印出：哪一頁、誰說的、說了什麼、依據的原話、連結跳到那一秒。

        抽到的人要自己點進去聽：這是驗證「有沒有安對人」唯一可靠的辦法。種子印出來，同一批可以再抽一次。
        """
        entries = [(slide.index, point) for slide in article.slides.all() for point in slide.points or []
                   if isinstance(point, dict) and isinstance(point.get("speaker"), str)]
        if not entries:
            self.stdout.write("這支影片沒有可以抽查的重點。")
            return
        seed = secrets.randbelow(1_000_000) if seed is None else seed
        chosen = sorted(random.Random(seed).sample(range(len(entries)), min(count, len(entries))))
        self.stdout.write(f"抽查 {len(chosen)} 個重點（共 {len(entries)} 個；抽樣種子 {seed}，"
                          f"--seed {seed} 可以抽到同一批）。點連結聽一下，確認真的是那個人說的：")
        video_id = video_id_of(article)
        by_id = {s["id"]: s for s in article.speakers}
        for number, index in enumerate(chosen, start=1):
            slide_index, point = entries[index]
            speaker_id = point["speaker"]
            label = plain((by_id.get(speaker_id) or {}).get("label")) or plain(speaker_id)
            shown = plain(display_of(article.speakers, speaker_id))
            who = f"{plain(speaker_id)} {label}" + (f"／{shown}" if shown != label else "")
            self.stdout.write(f"{number}. 第 {slide_index} 頁　[{who}] {plain(point.get('text'))}")
            self.stdout.write(f"     依據：「{plain(point.get('evidence'))}」")
            if video_id:
                self.stdout.write(f"     {watch_link(video_id, point.get('start'), lead=3)}")
