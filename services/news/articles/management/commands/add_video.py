"""手動加入 YouTube 影片：預檢 → 登記 → 送去 GPU 主機摘要 → 發佈到網站的「影片」頁。

    python manage.py add_video 'https://www.youtube.com/watch?v=jNQXAC9IVRw' --speakers 1
    python manage.py add_video 'https://youtu.be/jNQXAC9IVRw' --speakers 1 --dry-run   # 只預檢，什麼都不寫
    python manage.py add_video '網址' --speakers 1 --title '自己取的標題' --speaker '頻道的固定寫法' --date 2026-10-30
    python manage.py add_video '網址' --speakers 1 --rerun          # 重做已發佈的影片（影片留在站上）
    python manage.py add_video '網址一' '網址二' --speakers 1

網址一定要加單引號：沒加的話，bash 會在 `&` 截斷，後面的參數變成背景工作。第一行永遠先印出解析出的
影片 id 與正規網址，截斷了一眼就看得出來。

影片不進每晚排程，只由這個指令處理。已經存在的影片會說清楚結果：已發佈、失敗（重設次數重跑）、
GPU 上還有做完或在跑的工作（接回去）、別人正在處理（說還要幾分鐘）。等待時間用
GPU_JOB_TIMEOUT_SECONDS，不開放更長的 --timeout：夜間批次用同一個數字判斷「卡住」，這個指令等得比它久，
夜間會把還在跑的工作取消掉。長影片要在 api 與 scheduler 兩個容器都調大那個環境變數。

設計與每個決定的理由見 docs/superpowers/specs/2026-10-31-youtube-videos-design.md。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from articles.gpu_client import GpuApiClient, GpuApiError, JobField
from articles.ingest import (
    MAX_ATTEMPTS,
    RESUMABLE_STATUSES,
    STALE_FACTOR,
    process_articles,
    register_video,
)
from articles.models import Article, ArticleStatus
from articles.youtube_source import (
    MetadataUnavailable,
    PreflightOptions,
    UrlRejected,
    VideoMeta,
    VideoRef,
    VideoRefused,
    allow_dash_ids,
    clean_speaker,
    fetch_metadata,
    parse_video_url,
    placeholder_title,
    preflight,
    slide_range,
)

SPEAKERS_VALVE = (
    "影片裡常常不只一個人在講話，指令沒辦法事先知道；而現在的摘要流程會把全部的話都寫成同一個「講者」說的，"
    "訪談、對談、連線的影片會把話安到錯的人頭上。\n"
    "確定這支影片從頭到尾只有一個人在講話的話，加上 --speakers 1 再執行一次。\n"
    "有多個人講話的影片先不要加：用聲音區分說話的人的功能還在做。"
)


@dataclass(frozen=True)
class Registration:
    """要登記的資料與它的來源（dry-run 印出來給人看）。"""
    title: str
    speaker: str
    day: date
    duration_seconds: int
    warnings: tuple[str, ...]


class Command(BaseCommand):
    help = "手動加入 YouTube 影片：預檢、登記、送去 GPU 主機摘要，完成後出現在網站的「影片」頁"

    def create_parser(self, *args, **kwargs):
        # 開頭是「-」的單獨影片 id 不會被當成選項
        return allow_dash_ids(super().create_parser(*args, **kwargs))

    def add_arguments(self, parser) -> None:
        parser.add_argument("urls", nargs="+", metavar="網址",
                            help="影片網址（要加單引號）；watch?v=、youtu.be/、shorts/、live/、embed/ 或單獨的 11 碼 id")
        parser.add_argument("--title", help="標題（蓋過讀到的；只能搭配一個網址）。處理完成後也不會被 GPU 的標題換掉")
        parser.add_argument("--speaker", help="講者（頻道名稱的固定寫法；只能搭配一個網址）。"
                                              "頻道改名時用它讓新舊影片維持同一個名字")
        parser.add_argument("--date", help="YYYY-MM-DD（蓋過讀到的；只能搭配一個網址）")
        parser.add_argument("--dry-run", action="store_true",
                            help="只做預檢並印出會登記的資料，不寫資料庫、不呼叫 GPU")
        parser.add_argument("--rerun", action="store_true", help="重做已發佈的影片（重做時影片留在站上）")
        parser.add_argument("--force", action="store_true", help="允許剛結束、YouTube 還在轉檔的直播")
        parser.add_argument("--allow-unlisted", action="store_true",
                            help="允許「不公開」的影片（摘要、逐字稿與截圖會公開在網站上）")
        parser.add_argument("--allow-music", action="store_true",
                            help="允許音樂類別的影片（整首歌詞會被當成逐字稿公開）")
        parser.add_argument("--max-minutes", type=int, default=180, help="長度上限（分鐘），預設 180")
        parser.add_argument("--min-slides", type=int, help="最少幾頁（預設依長度算，見設計文件）")
        parser.add_argument("--max-slides", type=int, help="最多幾頁（預設依長度算）")
        parser.add_argument("--speakers", help="聲明「這支影片只有一個人講話」：現在只接受 1。"
                                               "沒有它，指令會拒絕登記與送出（區分講者的功能還在做）")

    def handle(self, *args, **options) -> None:
        refs = self._parse_urls(options["urls"])
        self._check_options(options, len(refs))
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        if not options["dry_run"] and options["speakers"] is None:
            # 在登記、送出任何東西之前：dry-run 與「已發佈」這種什麼都不會送的結果不受影響
            if any(self._would_process(ref, options, timeout) for ref in refs):
                raise CommandError(SPEAKERS_VALVE)

        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        failures = []
        for ref in refs:
            if len(refs) > 1:
                self.stdout.write(f"\n=== {ref.video_id} ===")
            try:
                self._add_one(ref, options, client, timeout)
            except CommandError as e:
                if len(refs) == 1:
                    raise
                self.stderr.write(f"  ! {ref.video_id}：{e}")
                failures.append(ref.video_id)
        if failures:
            raise CommandError(f"{len(failures)} 支影片沒有成功：{'、'.join(failures)}")

    # --- 參數 ---

    def _parse_urls(self, texts: list[str]) -> list[VideoRef]:
        """全部先解析完才動手：有一個網址不能用就整個指令不做。第一行永遠是解析出的 id 與正規網址。"""
        refs: list[VideoRef] = []
        rejected = []
        for text in texts:
            try:
                ref = parse_video_url(text)
            except UrlRejected as e:
                rejected.append(f"{text.strip()[:80]!r}：{e}")
                continue
            if any(ref.video_id == seen.video_id for seen in refs):
                self.stdout.write(f"影片 {ref.video_id}　（和前面的網址是同一支影片，只處理一次）")
                continue
            refs.append(ref)
            self.stdout.write(f"影片 {ref.video_id}　{ref.url}")
            for note in ref.notes:
                self.stdout.write(f"  注意：{note}")
        if rejected:
            raise CommandError("網址不能用：\n  " + "\n  ".join(rejected))
        return refs

    def _check_options(self, options, count: int) -> None:
        if count > 1 and any(options[name] for name in ("title", "speaker", "date")):
            raise CommandError("--title、--speaker、--date 只能搭配一個網址（每支影片各自的標題、講者、日期不一樣）")
        if options["date"]:
            self._override_date(options)
        if options["max_minutes"] < 1:
            raise CommandError("--max-minutes 至少要 1")
        try:
            slide_range(0, options["min_slides"], options["max_slides"])
        except ValueError as e:
            raise CommandError(str(e)) from e
        if options["speakers"] is not None and options["speakers"].strip() != "1":
            raise CommandError("--speakers 現在只接受 1（聲明這支影片只有一個人在講話）。"
                               "有多個人講話的影片先不要加：用聲音區分說話的人的功能還在做。")

    @staticmethod
    def _override_date(options) -> date:
        try:
            return date.fromisoformat(options["date"])
        except ValueError as e:
            raise CommandError("--date 要是 YYYY-MM-DD 的格式") from e

    # --- 一支影片 ---

    def _add_one(self, ref: VideoRef, options, client, timeout: float) -> None:
        article = Article.objects.filter(ivod_id=ref.ivod_id).first()
        if article is None:
            self._add_new(ref, options, client, timeout)
        else:
            self._add_existing(article, options, client, timeout)

    def _would_process(self, ref: VideoRef, options, timeout: float) -> bool:
        """這個網址會不會登記或送出東西（--speakers 1 的安全閥只擋這種）。"""
        article = Article.objects.filter(ivod_id=ref.ivod_id).first()
        if article is None:
            return True
        if article.status == ArticleStatus.READY:
            return bool(options["rerun"])
        if article.status == ArticleStatus.PROCESSING and not article.gpu_job_id:
            return self._stale(article, timeout)
        return True

    # --- 新的影片 ---

    def _add_new(self, ref: VideoRef, options, client, timeout: float) -> None:
        meta = self._metadata(ref, options)
        try:
            warnings = preflight(meta, PreflightOptions(
                max_minutes=options["max_minutes"], force=options["force"],
                allow_unlisted=options["allow_unlisted"], allow_music=options["allow_music"]))
        except VideoRefused as e:
            raise CommandError(f"預檢沒過，沒有登記：{e}") from e
        registration = self._registration(ref, meta, options, warnings)
        # 頁數範圍在登記之前就算：選項互相矛盾要在寫進資料庫之前就說
        slides = self._slides(registration.duration_seconds, options)
        self._describe(registration, slides)
        for warning in registration.warnings:
            self.stderr.write(f"  ! {warning}")
        if options["dry_run"]:
            self.stdout.write(f"會登記成 /article/{registration.day}-{ref.ivod_id}，再送去 GPU 摘要。\n"
                              "（--dry-run：沒有寫入資料庫，也沒有呼叫 GPU）")
            return

        article, _ = register_video(ref.video_id, title=registration.title, speaker=registration.speaker,
                                    day=registration.day, duration_seconds=registration.duration_seconds)
        self.stdout.write(f"已登記：/article/{article.slug}（處理完成才會出現在網站上）")
        self._run(article, client, timeout, slides)

    def _metadata(self, ref: VideoRef, options) -> VideoMeta:
        self.stdout.write("讀取影片資料…")
        try:
            return fetch_metadata(ref.video_id)
        except MetadataUnavailable as e:
            if options["title"] and options["speaker"] and options["date"]:
                # 標題、講者、日期都自己給了：不需要影片頁。其餘的預檢做不到，會在警告裡說
                return VideoMeta(ref.video_id, problems=(str(e),))
            raise CommandError(
                f"讀不到這支影片的資料：{e}\n晚一點再試，或自己給 --title、--speaker、--date 三個（就不必讀影片頁了）。"
            ) from e

    def _registration(self, ref: VideoRef, meta: VideoMeta, options, warnings: list[str]) -> Registration:
        notes = list(warnings) + list(meta.problems)
        day = self._override_date(options) if options["date"] else meta.date
        if day is None:
            day = timezone.localdate()
            notes.append(f"抓不到影片的日期，先用今天（台北時間）{day}；要指定請加 --date YYYY-MM-DD")
        title = (options["title"] or "").strip() or meta.title or placeholder_title(ref.video_id)
        speaker = clean_speaker(options["speaker"] or meta.channel) or "YouTube"
        return Registration(title, speaker, day, meta.duration_seconds, tuple(notes))

    def _describe(self, registration: Registration, slides: tuple[int | None, int | None]) -> None:
        self.stdout.write(f"  標題：{registration.title}")
        self.stdout.write(f"  頻道（講者）：{registration.speaker}")
        self.stdout.write(f"  日期：{registration.day}（台北時間）")
        self.stdout.write(f"  長度：{_clock(registration.duration_seconds)}")
        low, high = slides
        self.stdout.write("  頁數：" + (f"{low or '預設'}～{high or '預設'} 頁" if (low or high)
                                     else "GPU 的預設（8～15 頁）"))

    def _slides(self, duration_seconds: int, options) -> tuple[int | None, int | None]:
        try:
            return slide_range(duration_seconds, options["min_slides"], options["max_slides"])
        except ValueError as e:
            raise CommandError(str(e)) from e

    # --- 已經存在的影片 ---

    def _add_existing(self, article: Article, options, client, timeout: float) -> None:
        path = f"/article/{article.slug}"
        if any(options[name] for name in ("title", "speaker", "date")):
            self.stderr.write("  ! 這支影片已經登記過了：標題、講者、日期不會改（要改請到 admin 的文章頁）")
        slides = self._slides(article.duration_seconds, options)
        dry = options["dry_run"]

        if article.status == ArticleStatus.READY:
            if not options["rerun"]:
                self.stdout.write(f"已發佈在 {path}（影片頁 /videos）。要重做加上 --rerun；重做時影片會留在網站上。")
                return
            self.stdout.write(f"已發佈在 {path}；--rerun：重新送去 GPU 摘要，過程中影片留在網站上。")
            if dry:
                self._dry_footer()
                return
            self._run(article, client, timeout, slides, demote=False, claim=False)
            return

        if article.status == ArticleStatus.PROCESSING:
            if article.gpu_job_id:
                self.stdout.write(f"上一輪還在處理中（GPU 工作 {article.gpu_job_id}）。")
                if dry:
                    self._dry_footer("（--dry-run 不問 GPU，所以不知道那個工作能不能接回去）")
                    return
                if self._resumable(article, client):
                    self._run(article, client, timeout, slides, demote=True, claim=False)
                    return
                self.stdout.write("GPU 已經不認得那個工作（重開過、失敗了或被取消），重新送出。")
            elif not self._stale(article, timeout):
                age = (timezone.now() - article.updated_at).total_seconds()
                left = max(1, round((STALE_FACTOR * timeout - age) / 60))
                self.stdout.write(f"另一個行程正在處理這支影片（{round(age / 60)} 分鐘前開始）。"
                                  f"約還要 {left} 分鐘才會被視為卡住；到時再執行同一個指令。")
                return
            else:
                self.stdout.write(f"上一輪處理中斷了（超過 {round(STALE_FACTOR * timeout / 60)} 分鐘沒有動靜），重新處理。")
        elif article.status == ArticleStatus.FAILED:
            reason = (article.error or "原因不明").splitlines()[0][:200]
            if article.attempts >= MAX_ATTEMPTS:
                self.stdout.write(f"上一次處理失敗，而且自動重試的 {MAX_ATTEMPTS} 次都用完了：{reason}\n"
                                  "人為重跑，重設重試次數後重新處理。")
            else:
                self.stdout.write(f"上一次處理失敗（第 {article.attempts} 次）：{reason}\n人為重跑，重設重試次數後重新處理。")
        else:
            self.stdout.write("已登記但還沒處理完，重新處理。")

        if dry:
            self._dry_footer()
            return
        # 人為重跑不是自動重試：MAX_ATTEMPTS 擋的是夜間排程的無限重試。gpu_job_id 留著，
        # 還沒死的 GPU 工作就接回去（管理頁的「重新排隊」會把它清掉，幾十分鐘的成品就丟了）
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.PENDING, error="", attempts=0)
        article.refresh_from_db()
        self._run(article, client, timeout, slides)

    @staticmethod
    def _stale(article: Article, timeout: float) -> bool:
        return article.updated_at < timezone.now() - timedelta(seconds=STALE_FACTOR * timeout)

    def _resumable(self, article: Article, client) -> bool:
        """GPU 上那個工作做完了或還在跑嗎？不必等到「卡住」的時間才接回去。"""
        try:
            job = client.job(article.gpu_job_id)
        except GpuApiError as e:
            raise CommandError(f"問不到 GPU 主機上那個工作的狀態：{e}\nGPU 主機開著、摘要 API 在跑的時候再執行一次。") from e
        if job is None or job.get(JobField.STATUS) not in RESUMABLE_STATUSES:
            return False
        self.stdout.write(f"GPU 上的工作是「{job.get(JobField.STATUS)}」，接回去等結果。")
        return True

    def _dry_footer(self, extra: str = "") -> None:
        self.stdout.write((extra + "\n" if extra else "") + "（--dry-run：沒有寫入資料庫，也沒有呼叫 GPU）")

    # --- 送去 GPU ---

    def _run(self, article: Article, client, timeout: float, slides, demote: bool = True,
             claim: bool = True) -> None:
        self.stdout.write(f"送去 GPU 主機摘要（最多等 {int(timeout)} 秒）…")
        report = process_articles(article, client, timeout, demote_on_failure=demote, claim=claim, slides=slides)
        article.refresh_from_db()
        if report.skipped:
            raise CommandError("另一個行程剛接手這支影片，這裡沒有處理；稍後再執行同一個指令看結果。")
        if report.failed or article.status != ArticleStatus.READY:
            reason = article.error or (report.errors[0] if report.errors else "原因不明")
            kept = "；影片仍然留在網站上" if article.status == ArticleStatus.READY else ""
            raise CommandError(f"處理失敗：{reason}{kept}\n再執行同一個指令就會重試（GPU 上還在跑的工作會接回去）。")
        self.stdout.write(f"完成：{article.title}")
        self.stdout.write(f"  文章 /article/{article.slug}（{article.slides.count()} 頁）")
        self.stdout.write("  影片頁 /videos")
        if article.source_note:
            self.stdout.write(f"  來源註記：{article.source_note}")


def _clock(seconds: int) -> str:
    """5715 → 「1:35:15」；0 是讀不到。"""
    if not seconds:
        return "讀不到（頁數交給 GPU 的預設）"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
