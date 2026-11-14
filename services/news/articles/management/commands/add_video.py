"""手動加入 YouTube 影片：預檢 → 登記 → 送去 GPU 主機摘要 → 發佈到網站的「影片」頁。

    python manage.py add_video 'https://www.youtube.com/watch?v=jNQXAC9IVRw'
    python manage.py add_video 'https://youtu.be/jNQXAC9IVRw' --dry-run   # 只預檢，什麼都不寫
    python manage.py add_video '網址' --speakers 3        # 知道有幾個人講話就指定（2～20）；省略＝自動判斷
    python manage.py add_video '網址' --speakers 1        # 確定從頭到尾只有一個人（跳過說話者區分）
    python manage.py add_video '網址' --start 10:00 --end 58:00   # 只處理影片的一段（多人模式）；時間是原影片的時間
    python manage.py add_video '網址' --title '自己取的標題' --speaker '頻道的固定寫法' --date 2026-10-30
    python manage.py add_video '網址' --rerun             # 重做已發佈的影片（影片留在站上）
    python manage.py add_video '網址一' '網址二'
    python manage.py add_video '網址' --allow-unverified   # 影片頁讀不到、或缺了檢查要用的旗標時照樣處理（檢查不了的由你負責）

網址一定要加單引號：沒加的話，bash 會在 `&` 截斷，後面的參數變成背景工作。第一行永遠先印出解析出的
影片 id 與正規網址，截斷了一眼就看得出來。

預設會用聲音區分影片裡說話的人，每一句話、每個重點都記下是誰說的（編號 S1、S2…，網站上先顯示「發言者 A」
這種中性稱呼）。做完會印一張對照表，每位說話者附幾句代表的話和可以直接跳到那一秒的連結；聽一下確定是誰，
再用 label_video 取名字。誰說的絕對不能安錯人：GPU 的結果不合規格就整支影片當作失敗，不會發佈，更不會退回去
把全部的話安給同一位「講者」。GPU 主機還沒更新、或不支援區分時，在登記之前就停下來。

影片前後常有跟主題無關的訪談或廣告，聲音太多還會超過發佈上限（主要發言者 8 位、全部聲音 24 個）：用 --start 與 --end 只處理要的那一段。
時間寫秒數（600）、分:秒（10:00）或時:分:秒（1:02:03），只收整數秒，而且一律是**原影片的時間**：網站上的逐字稿、
時間連結（&t=）與截圖也是原影片的時間，不是剪出來那一段的。只給 --end 就從頭開始，只給 --start 就一直到影片結尾
（--start 0 就是整支影片，不算有範圍）。範圍只能用在多人模式、只能搭配一個網址；GPU 主機要先更新到認得範圍的版本
（/capabilities 的 video_range），沒更新的會悄悄處理整支影片，所以沒回報就在登記之前停下來。--max-minutes 與頁數都照
範圍的長度算。已經登記的影片要換範圍得加 --rerun；接回 GPU 上還在跑的工作時，範圍必須跟那個工作送出時的一樣。

影片不進每晚排程，只由這個指令處理。已經存在的影片會說清楚結果：已發佈、失敗（重設次數重跑）、
GPU 上還有做完或在跑的工作（接回去，並印出那個工作是什麼模式送出的；上次用 --speakers 1 送出的單人工作
不會被沒加 --speakers 的預設指令悄悄接回去，要接就明確加 --speakers 1）、別人正在處理（說還要幾分鐘）。等待時間用
GPU_JOB_TIMEOUT_SECONDS，不開放更長的 --timeout：夜間批次用同一個數字判斷「卡住」，這個指令等得比它久，
夜間會把還在跑的工作取消掉。長影片要在 api 與 scheduler 兩個容器都調大那個環境變數。

設計與每個決定的理由見 docs/superpowers/specs/2026-10-31-youtube-videos-design.md。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from articles.gpu_client import SPEAKER_CONTRACT, GpuApiClient, GpuApiError, JobFailed, JobField
from articles.ingest import (
    MAX_ATTEMPTS,
    RESUMABLE_STATUSES,
    STALE_FACTOR,
    process_articles,
    register_video,
)
from articles.models import Article, ArticleStatus
from articles.speaker_sheet import next_step_lines, rerun_loss_note, sheet_lines
from articles.speakers import conversation_hint
from articles.youtube_source import (
    MetadataUnavailable,
    PreflightOptions,
    UrlRejected,
    VideoMeta,
    VideoRef,
    VideoRefused,
    allow_dash_ids,
    clean_speaker,
    clean_text,
    fetch_metadata,
    parse_video_url,
    placeholder_title,
    preflight,
    slide_range,
)
from articles.video_range import (
    RangeInvalid,
    VideoRange,
    clock,
    fit_problem,
    normalize,
    range_length,
    range_of,
    resolve_range,
)

MAX_SPEAKERS_OPTION = 20
# --speakers 只收阿拉伯數字：int() 連 "１"（全形）、"٣"、"1_0"、"+3" 都吞，打錯的值會變成別的人數
_SPEAKERS_VALUE = re.compile(r"[0-9]{1,3}")

# 舊版的 GPU 不認得 range_start／range_end，會悄悄處理整支影片：沒有明說認得（video_range 剛好是 true）就不送
RANGE_NOT_SUPPORTED = (
    "沒有登記、也沒有送出：GPU 主機還不認得處理範圍（它的 /capabilities 沒有回報 video_range: true）。"
    "舊版的 GPU 會悄悄忽略 --start／--end、處理整支影片，那樣發佈出去的就是整支影片的結果，不是你要的那一段；"
    "請先把 GPU 主機更新到有範圍功能的版本（先更新 GPU 主機，再更新 Pi）。要處理整支影片的話，拿掉 --start／--end。"
)

REMOVED_WHILE_PROCESSING = (
    "這支影片在處理途中被下架了（remove_video）：GPU 做完的結果已經丟棄，沒有寫回資料庫，網站上不會出現。\n"
    "要它回來的話，再執行一次 add_video。"
)


@dataclass(frozen=True)
class Registration:
    """要登記的資料與它的來源（dry-run 印出來給人看）。"""
    title: str
    speaker: str
    day: date
    duration_seconds: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class GpuAbilities:
    """問 GPU 主機的結果。problem＝不能區分說話者的原因（能就是空字串）；ranges＝它明說認得處理範圍。

    ranged_problem 是同一個原因、給有範圍的請求看的版本：範圍不能搭配 --speakers 1，不建議那條退路。
    空字串＝跟 problem 一樣。
    """
    problem: str = ""
    ranges: bool = False
    ranged_problem: str = ""


class Command(BaseCommand):
    help = "手動加入 YouTube 影片：預檢、登記、送去 GPU 主機摘要，完成後出現在網站的「影片」頁"
    # 這次執行問過 GPU 的結果：None＝還沒問
    _abilities: GpuAbilities | None = None

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
        parser.add_argument("--allow-unverified", action="store_true",
                            help="影片頁讀不到，或讀到了卻缺了檢查要用的資料（播放器回應，或其中的 isUnlisted、"
                                 "isShortsEligible；被擋下、改版、被截斷）時照樣處理：那些檢查（不公開、直播中、Shorts、"
                                 "音樂、長度）做不了，由你自己負責。只給 --title、--speaker、--date 不能代替它")
        parser.add_argument("--max-minutes", type=int, default=180, help="長度上限（分鐘），預設 180")
        parser.add_argument("--min-slides", type=int, help="最少幾頁（預設依長度算，見設計文件）")
        parser.add_argument("--max-slides", type=int, help="最多幾頁（預設依長度算）")
        parser.add_argument("--speakers", help=f"說話者人數（1～{MAX_SPEAKERS_OPTION}）。省略＝自動區分說話的人、"
                                               "人數由 GPU 判斷（預設）；2 以上＝固定人數（連線進來的聲音也算一位）；"
                                               "1＝確定整支影片只有一個人在講話，跳過區分")
        parser.add_argument("--start", help="只處理影片的一段：從這個時間開始（秒數、分:秒 或 時:分:秒，例如 10:00；"
                                            "只收整數秒；原影片的時間）。省略＝從頭。只能搭配一個網址、"
                                            "只能用在多人模式；GPU 主機要更新到認得範圍的版本")
        parser.add_argument("--end", help="只處理影片的一段：到這個時間為止（寫法同 --start）。省略＝一直到影片結尾；"
                                          "超過影片長度的話處理到結尾。範圍至少 5 秒")

    def handle(self, *args, **options) -> None:
        refs = self._parse_urls(options["urls"])
        self._check_options(options, len(refs))
        if self._range_given(options) and self._video_range(options) is None:
            self.stdout.write("注意：--start 0 就是從頭開始、又沒有 --end：整支影片都會處理，不算指定範圍。")
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        # GPU 的能力（能不能區分說話者、認不認得範圍），整個指令只問一次（問不到的結果也記著，批次裡後面的影片不再各自
        # 等一次逾時）
        self._abilities = None
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
        if options["speakers"] is not None:
            self._speakers_option(options)
        if self._range_given(options):
            self._check_range_options(options, count)

    @staticmethod
    def _range_given(options) -> bool:
        """操作者有沒有寫 --start 或 --end。--start 0 也算：它的意思是整支影片，但寫了就是寫了，不悄悄當成沒寫。"""
        return options["start"] is not None or options["end"] is not None

    def _check_range_options(self, options, count: int) -> None:
        self._video_range(options)      # 時間看不懂、終點不在起點之後、太短：在動任何東西之前就停下來
        if count > 1:
            raise CommandError("--start、--end 只能搭配一個網址（每支影片要處理的一段不一樣）")
        if self._speaker_count(options) == 1:
            raise CommandError("--start、--end 只能用在多人模式：--speakers 1（確定只有一個人講話、跳過說話者區分）"
                               "沒有範圍可以指定。要處理一段就拿掉 --speakers 1，要處理整支影片就拿掉 --start／--end。")

    @staticmethod
    def _video_range(options) -> VideoRange | None:
        """這次指令要處理的範圍；沒有範圍（兩個都省略、或只有 --start 0）是 None。時間不合理就停下來。"""
        try:
            return resolve_range(options["start"], options["end"])
        except RangeInvalid as e:
            raise CommandError(str(e)) from e

    @staticmethod
    def _speakers_option(options) -> int:
        """--speakers 的整數（呼叫前它不是 None）。不是 1～20 的整數就拒絕，不猜。"""
        text = options["speakers"].strip()
        if not _SPEAKERS_VALUE.fullmatch(text) or not 1 <= int(text) <= MAX_SPEAKERS_OPTION:
            raise CommandError(f"--speakers 要是 1～{MAX_SPEAKERS_OPTION} 的整數（1＝只有一個人在講話，"
                               f"2 以上＝固定人數；省略就是自動判斷），不是 {text[:40]!r}")
        return int(text)

    @classmethod
    def _speaker_count(cls, options) -> int | None:
        """這次要用的說話者模式：None＝自動區分（省略 --speakers）、1＝單人、2 以上＝固定人數。"""
        return cls._speakers_option(options) if options["speakers"] is not None else None

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

    # --- GPU 能不能區分說話者、認不認得範圍 ---

    def _require_diarization(self, client, *, ranged: bool = False) -> None:
        """多人模式要送出之前，先確認 GPU 主機做得到；做不到就停下來，說明原因。ranged＝這次還帶了處理範圍。

        在登記之前、在重新送出（搶下處理權、耗掉一次重試）之前問：被擋下來時資料庫完全沒動。
        舊版 GPU 沒有這個端點、或能力不符，絕對不能把多人影片當單人送去——那會把每句話安到同一個人頭上。
        範圍也一樣：舊版 GPU 認不得 range_start／range_end、會悄悄處理整支影片，所以帶範圍的請求要它明說認得
        （video_range 剛好是 true）才送；沒帶範圍的指令一個字都不變，舊版 GPU 照樣可以用。
        """
        if self._abilities is None:
            self._abilities = self._probe(client)
        abilities = self._abilities
        if abilities.problem:
            raise CommandError(abilities.ranged_problem if ranged and abilities.ranged_problem else abilities.problem)
        if ranged and not abilities.ranges:
            raise CommandError(RANGE_NOT_SUPPORTED)

    @staticmethod
    def _probe(client) -> GpuAbilities:
        """問 GPU 主機的能力：不能區分說話者的原因（可以就是空字串），以及認不認得處理範圍。"""
        try:
            capabilities = client.capabilities()
        except (GpuApiError, JobFailed) as e:
            # JobFailed：問能力的請求被 GPU 以 422 拒絕時（RequestRejected）丟的是它，不是 GpuApiError
            return GpuAbilities(problem=f"沒有登記、也沒有送出：問不到 GPU 主機能不能區分說話者。{e}")
        if not isinstance(capabilities, dict):
            return GpuAbilities(problem="沒有登記、也沒有送出：GPU 主機回的能力資料不是預期的格式"
                                        "（它可能還沒更新到支援多人區分的版本）")
        if capabilities.get("speaker_diarization") is not True:
            reason = clean_text(str(capabilities.get("reason") or ""))[:200] or "GPU 沒有說明原因"
            stem = f"沒有登記、也沒有送出：GPU 主機現在不能區分說話者（{reason}）。等它準備好再試"
            return GpuAbilities(problem=stem + "；只有一個人講話的影片可以加 --speakers 1。",
                                ranged_problem=stem + "。")
        contract = capabilities.get("speaker_contract")
        if isinstance(contract, bool) or not isinstance(contract, int) or contract != SPEAKER_CONTRACT:
            # repr：文字 "1" 與數字 1 要看得出差別，不然訊息會變成「版本（1）和這一邊（1）不一樣」
            shown = clean_text(repr(contract))[:40] if contract is not None else "沒有回報"
            return GpuAbilities(problem=f"沒有登記、也沒有送出：GPU 主機的說話者區分介面版本（{shown}）和這一邊"
                                        f"（{SPEAKER_CONTRACT}）不一樣；請把 GPU 主機和 Pi 更新到同一版"
                                        "（先更新 GPU 主機，再更新 Pi）。")
        return GpuAbilities(ranges=capabilities.get("video_range") is True)

    def _settle(self, article: Article, options, client, *, resuming: bool) -> None:
        """要接回舊工作或送新工作之前，定下這支影片這次的說話者模式與處理範圍。

        接回 GPU 上還在跑（或已跑完）的工作時，模式與範圍都是那個工作送出時決定的，不能悄悄換掉：明確給了不同的
        --speakers 就拒絕。省略 --speakers 的意思是預設的自動區分，所以接回的如果是單人的工作（上次用
        --speakers 1 送出、還沒收）也要拒絕：否則「什麼都不加」會發佈出把全部的話安給同一位「講者」的文章，
        正是這個功能保證絕不發生的事；要接回單人的工作，得明確加 --speakers 1。範圍也是：必須跟這次指令的
        完全一樣（兩邊都是整支影片也算一樣），省略 --start／--end 的意思是整支影片，所以接回的如果是有範圍的
        工作也要拒絕，不然發佈出來的就不是操作者這次要的那一段。
        要送新工作時以這次的指令為準（省略＝自動區分、整支影片），印出跟之前不一樣的地方；多人模式要先確認
        GPU 做得到，帶範圍的還要確認它認得範圍。這裡任何一步擋下來，資料庫都還沒動過。
        """
        wanted = self._speaker_count(options)
        video_range = self._video_range(options)
        if resuming:
            self._check_resume(article, options, wanted, video_range)
            return
        if wanted != 1:
            self._require_diarization(client, ranged=video_range is not None)
        self._record(article, wanted, video_range)

    def _check_resume(self, article: Article, options, wanted: int | None, video_range: VideoRange | None) -> None:
        """這次的指令能不能接回 GPU 上那個工作：模式與範圍都要跟它送出時的一樣，不然就拒絕、說明怎麼辦。"""
        job = article.gpu_job_id
        sent = _mode_text(article.speaker_count)
        if options["speakers"] is not None and wanted != article.speaker_count:
            raise CommandError(
                f"GPU 工作 {job} 是以「{sent}」送出的，"
                f"不能用 --speakers {wanted} 接回去；等它完成之後再用 --rerun 改模式。")
        if options["speakers"] is None and article.speaker_count == 1:
            raise CommandError(
                f"GPU 工作 {job} 是以「{sent}」送出的（上一次加了 --speakers 1，還沒收），"
                "這次沒有指定 --speakers，預設是自動區分說話的人：照預設接回去，等於把全部的話安給同一位「講者」。\n"
                "  - 確定整支影片只有一個人、要收下那個單人的工作：加上 --speakers 1。\n"
                "  - 要用多人區分：等那個工作做完（再執行一次同樣的指令看結果），然後用 --rerun 重做。")
        stored = self._stored_range(article)
        if stored != video_range:
            how = ("不要加 --start／--end（整支影片）" if stored is None else f"加上 {_range_flags(stored)}")
            raise CommandError(
                f"GPU 工作 {job} 是以「處理範圍：{_range_text(stored)}」送出的，這次的指令是「{_range_text(video_range)}」，"
                "不能接回去：範圍不一樣的話，接回去發佈出來的就不是這次要的那一段。\n"
                f"  - 要收下那個工作：{how}，再執行一次。\n"
                "  - 要用這次的範圍：等那個工作做完（再執行一次同樣的指令看結果），然後用 --rerun 重做。")

    @staticmethod
    def _stored_range(article: Article) -> VideoRange | None:
        """資料列記著的範圍；記得不合理的（沒有人會寫出這種資料列）不猜，停下來說。"""
        try:
            return range_of(article)
        except RangeInvalid as e:
            raise CommandError(
                f"這支影片記著的處理範圍不合理（{article.range_start}～{article.range_end} 秒）：{e}\n"
                "GPU 上那個工作的範圍沒辦法確認，不接回去；等它結束之後用 --rerun 重做。") from e

    def _record(self, article: Article, wanted: int | None, video_range: VideoRange | None) -> None:
        """送新工作之前，把這次的模式與範圍寫進資料列，並印出跟之前不一樣的地方。

        資料列記的是「最近一次送去 GPU 的設定」：接回工作時拿它跟指令核對，結果也靠它核對（GPU 回報的範圍
        要跟送出的對得上）。
        """
        changes: dict[str, int | None] = {}
        if article.speaker_count != wanted:
            self.stdout.write(f"說話者模式：{_mode_text(wanted)}（之前是：{_mode_text(article.speaker_count)}）")
            changes["speaker_count"] = wanted
        self._say_range(article, video_range)
        start, end = (video_range.start, video_range.end) if video_range is not None else (None, None)
        if (article.range_start, article.range_end) != (start, end):
            changes.update(range_start=start, range_end=end)
        if changes:
            Article.objects.filter(pk=article.pk).update(**changes)
            for name, value in changes.items():
                setattr(article, name, value)

    def _say_range(self, article: Article, video_range: VideoRange | None) -> None:
        """這次的處理範圍：跟記著的不一樣就說之前是什麼；一樣的有範圍才說；兩邊都是整支影片就什麼都不說。"""
        before = _span_text(article.range_start, article.range_end)
        now = _range_text(video_range)
        if before != now:
            self.stdout.write(f"處理範圍：{now}（之前是：{before}）")
        elif video_range is not None:
            self.stdout.write(f"處理範圍：{now}")

    def _warn_single(self, options, title: str, channel: str) -> None:
        """明確用了 --speakers 1，標題或頻道名稱卻像訪談、對談：提醒一聲（照樣執行）。"""
        if options["speakers"] is None or self._speaker_count(options) != 1:
            return
        word = conversation_hint(title, channel)
        if word:
            self.stderr.write(
                f"  ! 標題或頻道名稱裡有「{word}」，這支影片多半不只一個人在講話；--speakers 1 會把整支影片的話"
                "都安給同一位講者。確定只有一個人的話可以不理這則；否則拿掉 --speakers 1，讓它自動區分說話的人。")

    # --- 新的影片 ---

    def _add_new(self, ref: VideoRef, options, client, timeout: float) -> None:
        meta = self._metadata(ref, options)
        video_range = self._video_range(options)
        try:
            warnings = preflight(meta, PreflightOptions(
                max_minutes=options["max_minutes"], force=options["force"],
                allow_unlisted=options["allow_unlisted"], allow_music=options["allow_music"],
                allow_unverified=options["allow_unverified"], video_range=video_range))
        except VideoRefused as e:
            # 讀不到影片頁的原因（429、同意頁、逾時…）要一起說：只說「沒有讀到」看不出該等多久、換不換網路
            why = f"\n讀取時遇到的問題：{'；'.join(meta.problems)}" if meta.problems else ""
            raise CommandError(f"預檢沒過，沒有登記：{e}{why}") from e
        registration = self._registration(ref, meta, options, warnings)
        # 頁數範圍在登記之前就算：選項互相矛盾要在寫進資料庫之前就說。頁數照要處理的那一段的長度算
        slides = self._slides(registration.duration_seconds, video_range, options)
        speakers = self._speaker_count(options)
        self._describe(registration, slides, speakers, video_range)
        for warning in registration.warnings:
            self.stderr.write(f"  ! {warning}")
        self._warn_single(options, registration.title, registration.speaker)
        if options["dry_run"]:
            self.stdout.write(f"會登記成 /article/{registration.day}-{ref.ivod_id}，再送去 GPU 摘要。\n"
                              "（--dry-run：沒有寫入資料庫，也沒有呼叫 GPU）")
            return

        # 多人模式：GPU 做不到（帶範圍的還要認得範圍）就在登記之前停下來（資料庫完全沒動）
        if speakers != 1:
            self._require_diarization(client, ranged=video_range is not None)
        article, _ = register_video(ref.video_id, title=registration.title, speaker=registration.speaker,
                                    day=registration.day, duration_seconds=registration.duration_seconds,
                                    speaker_count=speakers, video_range=video_range)
        self.stdout.write(f"已登記：/article/{article.slug}（處理完成才會出現在網站上）")
        self._run(article, client, timeout, slides)

    def _metadata(self, ref: VideoRef, options) -> VideoMeta:
        self.stdout.write("讀取影片資料…")
        try:
            return fetch_metadata(ref.video_id)
        except MetadataUnavailable as e:
            if options["title"] and options["speaker"] and options["date"]:
                # 標題、講者、日期都自己給了：登記不需要影片頁。但不公開、直播、Shorts、音樂與長度都檢查不了，
                # 所以預檢照樣拒絕，要明確加 --allow-unverified 才放行（三個覆寫不代替它）
                return VideoMeta(ref.video_id, problems=(str(e),))
            raise CommandError(
                f"讀不到這支影片的資料：{e}\n晚一點再試；或自己給 --title、--speaker、--date 三個（登記就不需要影片頁），"
                "再加上 --allow-unverified（不公開、直播中、Shorts、音樂與長度都檢查不了，由你自己負責）。"
            ) from e

    def _registration(self, ref: VideoRef, meta: VideoMeta, options, warnings: list[str]) -> Registration:
        notes = list(warnings) + list(meta.problems)
        day = self._override_date(options) if options["date"] else meta.date
        if day is None:
            day = timezone.localdate()
            notes.append(f"抓不到影片的日期，先用今天（台北時間）{day}；要指定請加 --date YYYY-MM-DD")
        # 標題與頻道名稱是上傳者寫的：先清掉控制字元與雙向覆寫（見 clean_text）再印、再存
        title = clean_text(options["title"]) or clean_text(meta.title) or placeholder_title(ref.video_id)
        speaker = clean_speaker(options["speaker"] or meta.channel) or "YouTube"
        return Registration(title, speaker, day, meta.duration_seconds, tuple(notes))

    def _describe(self, registration: Registration, slides: tuple[int | None, int | None],
                  speakers: int | None, video_range: VideoRange | None = None) -> None:
        self.stdout.write(f"  標題：{registration.title}")
        self.stdout.write(f"  頻道（講者）：{registration.speaker}")
        self.stdout.write(f"  日期：{registration.day}（台北時間）")
        self.stdout.write(f"  長度：{_clock(registration.duration_seconds)}")
        if video_range is not None:
            self.stdout.write(f"  處理範圍：{video_range.describe()}（原影片的時間；只處理這一段）")
        low, high = slides
        self.stdout.write("  頁數：" + (f"{low or '預設'}～{high or '預設'} 頁" if (low or high)
                                     else "GPU 的預設（8～15 頁）"))
        self.stdout.write(f"  說話者：{_mode_text(speakers)}")

    def _slides(self, duration_seconds: int, video_range: VideoRange | None, options) -> tuple[int | None, int | None]:
        """頁數範圍：照要處理的那一段的長度算（沒有範圍就是整支影片），--min-slides／--max-slides 蓋過它。"""
        try:
            return slide_range(range_length(duration_seconds, video_range), options["min_slides"],
                               options["max_slides"])
        except ValueError as e:
            raise CommandError(str(e)) from e

    # --- 已經存在的影片 ---

    def _add_existing(self, article: Article, options, client, timeout: float) -> None:
        path = f"/article/{article.slug}"
        if any(options[name] for name in ("title", "speaker", "date")):
            self.stderr.write("  ! 這支影片已經登記過了：標題、講者、日期不會改（要改請到 admin 的文章頁）")
        video_range = self._video_range(options)
        # 範圍要放得進登記時記下的長度（新影片在預檢檢查過；已登記的沒有影片頁可以再問，用記著的）
        problem = fit_problem(article.duration_seconds, video_range)
        if problem:
            raise CommandError(problem)
        slides = self._slides(article.duration_seconds, video_range, options)
        dry = options["dry_run"]
        # GPU 上留著的工作還能不能接回去：asked＝已經問過；status＝工作的狀態（接得回去），
        # None＝接不回去（沒有記著工作、GPU 已經不認得、失敗了或被取消）
        status: str | None = None
        asked = False

        if article.status == ArticleStatus.READY:
            if not options["rerun"]:
                self.stdout.write(f"已發佈在 {path}（影片頁 /videos）。要重做加上 --rerun；重做時影片會留在網站上。")
                shown = _span_text(article.range_start, article.range_end)
                if shown != _range_text(None):
                    self.stdout.write(f"處理範圍：{shown}")
                if options["speakers"] is not None and self._speaker_count(options) != article.speaker_count:
                    self.stderr.write(f"  ! 這支影片目前是「{_mode_text(article.speaker_count)}」；"
                                      "--speakers 要加 --rerun 重做才會生效")
                if self._range_given(options) and _range_text(video_range) != shown:
                    self.stderr.write(f"  ! 這支影片目前的處理範圍是「{shown}」；--start／--end 要加 --rerun 重做才會生效")
                return
            resend = f"已發佈在 {path}；--rerun：重新送去 GPU 摘要，過程中影片留在網站上。"
            if dry:
                self.stdout.write(resend)
                self._warn_single(options, article.title, article.speaker)
                self._dry_mode(article, options)
                self._dry_footer()
                return
            # 上次 --rerun 中斷的話，GPU 上可能還留著工作：先問，橫幅才不會說「重新送出」卻接回舊的
            status = self._job_status(article, client)
            self.stdout.write(resend if status is None else
                              f"已發佈在 {path}；--rerun：GPU 上還留著上次的工作，接回去而不重新送出，過程中影片留在網站上。")
            self._warn_single(options, article.title, article.speaker)
            published = (article.speaker_count, article.range_start, article.range_end)
            self._settle(article, options, client, resuming=status is not None)
            self._say_resume(article, status)
            self._announce_rerun(article)
            self._touch(article)
            try:
                self._run(article, client, timeout, slides, demote=False, claim=False)
            except CommandError:
                self._restore(article, published)
                raise
            return

        if article.status == ArticleStatus.PROCESSING:
            if article.gpu_job_id:
                self.stdout.write(f"上一輪還在處理中（GPU 工作 {article.gpu_job_id}）。")
                if dry:
                    self._dry_footer("（--dry-run 不問 GPU，所以不知道那個工作能不能接回去）")
                    return
                status, asked = self._job_status(article, client), True
                if status is not None:
                    self._warn_single(options, article.title, article.speaker)
                    self._settle(article, options, client, resuming=True)
                    self._say_resume(article, status)
                    self._touch(article)
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

        self._warn_single(options, article.title, article.speaker)
        if dry:
            self._dry_mode(article, options)
            self._dry_footer()
            return
        if not asked:
            status = self._job_status(article, client)
        # 說話者模式與處理範圍在動資料庫之前定下來（多人模式 GPU 做不到、不認得範圍、或接回的工作設定不同，都在這裡停）
        self._settle(article, options, client, resuming=status is not None)
        self._say_resume(article, status)
        # 人為重跑不是自動重試：MAX_ATTEMPTS 擋的是夜間排程的無限重試。gpu_job_id 留著，
        # 還沒死的 GPU 工作就接回去（管理頁的「重新排隊」會把它清掉，幾十分鐘的成品就丟了）
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.PENDING, error="", attempts=0)
        article.refresh_from_db()
        self._run(article, client, timeout, slides)

    def _restore(self, article: Article, published: tuple[int | None, int | None, int | None]) -> None:
        """已發佈的影片重做失敗、而且 GPU 上沒有留下新的工作：說話者模式與處理範圍還原成目前發佈的那一版的。

        published 是 (說話者人數, 範圍起點, 範圍終點)，在 _settle 動資料列之前記下的。_settle 在送出之前就把新的
        設定寫進資料列；送出就失敗（或工作自己失敗、工作 id 被清掉）的話，資料列上的設定會跟還掛在網站上的內容
        對不上（記著單人，網站上卻是三位說話者的版本；記著 20:00–35:00，網站上卻是 10:00–25:00 的那一版），
        之後每一句「目前是…」「之前是…」都說錯。GPU 上留著工作的（等太久、連線斷了）不還原：那個工作就是用新的
        設定送出的，下次接回去、以及結果的核對，都要靠資料列記得它。
        """
        row = Article.objects.filter(pk=article.pk).values(
            "status", "gpu_job_id", "speaker_count", "range_start", "range_end").first()
        if row is None or row["status"] != ArticleStatus.READY or row["gpu_job_id"]:
            return
        mode, start, end = published
        undone = []
        if row["speaker_count"] != mode:
            undone.append(f"說話者模式：{_mode_text(mode)}")
        if (row["range_start"], row["range_end"]) != (start, end):
            undone.append(f"處理範圍：{_span_text(start, end)}")
        if undone:
            Article.objects.filter(pk=article.pk).update(speaker_count=mode, range_start=start, range_end=end)
            self.stderr.write("  ! 這次沒有在 GPU 上留下新的工作，設定還原成目前發佈的　" + "；".join(undone))

    def _dry_mode(self, article: Article, options) -> None:
        """--dry-run：這次的說話者模式、處理範圍與重跑會丟掉的設定（不問 GPU，所以不知道它能不能區分、認不認得範圍）。"""
        wanted = self._speaker_count(options)
        change = "" if article.speaker_count == wanted else f"（之前是：{_mode_text(article.speaker_count)}）"
        self.stdout.write(f"說話者模式：{_mode_text(wanted)}{change}")
        self._say_range(article, self._video_range(options))
        if article.status == ArticleStatus.READY:
            self._announce_rerun(article)

    def _announce_rerun(self, article: Article) -> None:
        """重跑之前先說：操作者取的名字、併起來的編號不會保留（重跑後編號可能全變，舊名字掛上去會安錯人）。"""
        note = rerun_loss_note(article)
        if note:
            self.stdout.write(f"  注意：{note}")

    @staticmethod
    def _stale(article: Article, timeout: float) -> bool:
        return article.updated_at < timezone.now() - timedelta(seconds=STALE_FACTOR * timeout)

    @staticmethod
    def _touch(article: Article) -> None:
        """updated_at 更新成現在（QuerySet.update：只動這一個欄位，不 save）。

        remove_video 靠「有 gpu_job_id 而且 updated_at 還新」判斷有沒有指令正在等 GPU。--rerun 與接回去的
        工作不經過 _claim（那是把 updated_at 設成現在的地方），接回去的還不會重新送出；不自己更新的話，
        原本的 STALE_FACTOR × 逾時一過，守衛就放行，工作做完又把剛下架的影片寫回來。
        """
        Article.objects.filter(pk=article.pk).update(updated_at=timezone.now())

    @staticmethod
    def _job_status(article: Article, client) -> str | None:
        """文章記著的 GPU 工作做完了或還在跑嗎？可以接回去就回傳它的狀態，不行（沒記著工作、GPU 不認得、
        失敗了或被取消）就是 None。不必等到「卡住」的時間才接回去。不印東西：要不要接回去、說什麼，
        由呼叫端在確認模式沒有衝突之後才說。"""
        if not article.gpu_job_id:
            return None
        try:
            job = client.job(article.gpu_job_id)
        except GpuApiError as e:
            raise CommandError(f"問不到 GPU 主機上那個工作的狀態：{e}\nGPU 主機開著、摘要 API 在跑的時候再執行一次。") from e
        status = job.get(JobField.STATUS) if job is not None else None
        return str(status) if status in RESUMABLE_STATUSES else None

    def _say_resume(self, article: Article, status: str | None) -> None:
        """接回舊工作時，在等上幾十分鐘之前先說：哪一個工作、什麼狀態、是什麼模式與範圍送出的（沒有要接回的就不印）。"""
        if status is None:
            return
        shown = _span_text(article.range_start, article.range_end)
        where = "" if shown == _range_text(None) else f"；處理範圍：{shown}"
        self.stdout.write(f"GPU 上的工作 {article.gpu_job_id} 是「{status}」，模式：{_mode_text(article.speaker_count)}"
                          f"{where}；接回去等結果，不重新送出。")

    def _dry_footer(self, extra: str = "") -> None:
        self.stdout.write((extra + "\n" if extra else "") + "（--dry-run：沒有寫入資料庫，也沒有呼叫 GPU）")

    # --- 送去 GPU ---

    def _run(self, article: Article, client, timeout: float, slides, demote: bool = True,
             claim: bool = True) -> None:
        self.stdout.write(f"送去 GPU 主機摘要（最多等 {int(timeout)} 秒）…")
        report = process_articles(article, client, timeout, demote_on_failure=demote, claim=claim, slides=slides)
        if report.removed:
            raise CommandError(REMOVED_WHILE_PROCESSING)
        try:
            article.refresh_from_db()
        except Article.DoesNotExist:
            # 抓不到處理途中被下架的痕跡（例如接手之前就不見了）：一樣是被下架，不是「別人接手了」
            raise CommandError(REMOVED_WHILE_PROCESSING) from None
        if report.skipped:
            raise CommandError("另一個行程剛接手這支影片，這裡沒有處理；稍後再執行同一個指令看結果。")
        if report.failed or article.status != ArticleStatus.READY:
            reason = article.error or (report.errors[0] if report.errors else "原因不明")
            kept = "；影片仍然留在網站上" if article.status == ArticleStatus.READY else ""
            raise CommandError(f"處理失敗：{reason}{kept}\n再執行同一個指令就會重試（GPU 上還在跑的工作會接回去）。")
        self.stdout.write(f"完成：{clean_text(article.title)}")
        self.stdout.write(f"  文章 /article/{article.slug}（{article.slides.count()} 頁）")
        self.stdout.write("  影片頁 /videos")
        if article.source_note:
            self.stdout.write(f"  來源註記：{article.source_note}")
        self._speaker_sheet(article)

    def _speaker_sheet(self, article: Article) -> None:
        """多人影片做完：印說話者對照表（含 GPU 的警告）和下一步。單人影片沒有說話者，什麼都不印。"""
        lines = sheet_lines(article)
        if not lines:
            return
        self.stdout.write("")
        for line in lines:
            self.stdout.write(line)
        self.stdout.write("")
        for line in next_step_lines(article):
            self.stdout.write(line)


def _mode_text(count: int | None) -> str:
    """說話者模式的白話：None＝自動、1＝單人、其他＝固定人數。"""
    if count is None:
        return "自動區分說話的人（人數由 GPU 判斷）"
    if count == 1:
        return "只有一個人（--speakers 1，跳過說話者區分）"
    return f"固定 {count} 位說話者（--speakers {count}）"


def _range_text(video_range: VideoRange | None) -> str:
    """處理範圍的白話：沒有範圍就是整支影片。"""
    return "整支影片" if video_range is None else video_range.describe()


def _span_text(start: int | None, end: int | None) -> str:
    """資料列上記的（起點, 終點）→ 白話：兩個都空就是整支影片；記得不合理的照實說，不猜。"""
    try:
        return _range_text(normalize(start, end))
    except RangeInvalid:
        return f"不合理的記錄（{start}～{end} 秒）"


def _range_flags(video_range: VideoRange) -> str:
    """要得到這個範圍該加的旗標：10:00–25:00 → 「--start 10:00 --end 25:00」；從 0 開始的只要 --end。"""
    flags = []
    if video_range.start:
        flags.append(f"--start {clock(video_range.start)}")
    if video_range.end is not None:
        flags.append(f"--end {clock(video_range.end)}")
    return " ".join(flags)


def _clock(seconds: int) -> str:
    """5715 → 「1:35:15」；0 是讀不到。"""
    if not seconds:
        return "讀不到（頁數交給 GPU 的預設）"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
