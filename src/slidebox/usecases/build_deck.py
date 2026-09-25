"""把整條 pipeline 串起來的 use case。

只做路徑字串運算，不碰檔案系統——建立目錄是各 adapter 的責任，
這樣本 use case 才能完全用記憶體假物件測試。
"""
from __future__ import annotations

import os
from dataclasses import replace

from ..domain.entities import Brief, Deck, DeckResult, Settings, Slide, Transcript
from ..domain.errors import (
    NoSubtitlesAvailable,
    OperationCancelled,
    SubtitleDownloadFailed,
    SummarizerOutputInvalid,
    SummarizerUnavailable,
)
from ..domain.ports import (
    AudioGateway,
    BriefWriter,
    CancelCheck,
    DeckRenderer,
    FrameExtractor,
    ProgressCallback,
    SpeechTranscriber,
    SubtitleGateway,
    Summarizer,
    VideoSectionGateway,
)
from .brief import ground_brief, validate_brief
from .chapters import (
    clamp_timestamps,
    compress_cues,
    full_transcript,
    validate_slides,
)
from .naming import deck_folder_name

# 詳細模式下每頁輸出約略會用掉的字元（150～300 字的敘述加上標題條列，
# 取上緣）。輸出與提示共用 num_ctx，所以要從預算裡先扣掉。
_DETAIL_CHARS_PER_SLIDE = 400
# 字幕再怎麼被擠壓也要留下的量，否則摘要會失去全片涵蓋
_MIN_BUDGET = 4000


def _budget(settings: Settings) -> int:
    """這次要餵給模型的字幕字元上限。

    一般模式就是設定值。詳細模式的輸出量是一般模式的近十倍，而 Ollama 的
    num_ctx 是提示與生成共用的——頁數上限拉到 50 時，20000 字的字幕加上
    50 × 400 字的輸出會超出 32768，此時 Ollama 會從頭靜默截斷提示，症狀是
    「摘要只涵蓋影片後半段」而且沒有任何錯誤訊息。
    """
    if not settings.detailed:
        return settings.char_budget
    room = settings.num_ctx - settings.max_slides * _DETAIL_CHARS_PER_SLIDE
    return max(_MIN_BUDGET, min(settings.char_budget, room))


# 各階段的進度界線
_P_SUBTITLES = 0.05
_P_SUMMARY = 0.40
_P_DOWNLOAD = 0.75
_P_FRAMES = 0.90


class BuildDeckUseCase:
    def __init__(
        self,
        subtitles: SubtitleGateway,
        summarizer: Summarizer,
        sections: VideoSectionGateway,
        frames: FrameExtractor,
        renderer: DeckRenderer,
        audio: AudioGateway | None = None,
        transcriber: SpeechTranscriber | None = None,
        brief_writer: BriefWriter | None = None,
    ):
        self._audio = audio
        self._transcriber = transcriber
        self._brief_writer = brief_writer
        self._subtitles = subtitles
        self._summarizer = summarizer
        self._sections = sections
        self._frames = frames
        self._renderer = renderer

    def execute(
        self,
        url: str,
        settings: Settings,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCheck | None = None,
    ) -> DeckResult:
        cb: ProgressCallback = progress or (lambda frac, status: None)
        cancelled: CancelCheck = is_cancelled or (lambda: False)

        def check() -> None:
            if cancelled():
                raise OperationCancelled()

        check()
        cb(0.0, "取得字幕…")
        try:
            transcript = self._subtitles.fetch(url, settings.subtitle_langs)
            # 來源自己講的優先；沒講就用「是不是 YouTube 自動字幕」判斷
            note = transcript.source_note or (
                "使用 YouTube 自動字幕，品質可能較差" if transcript.is_automatic else "")
        except SubtitleDownloadFailed:
            # 字幕存在但這次下載失敗（例如 HTTP 429）：這是暫時性問題，原樣
            # 告知使用者稍後重試。改走語音會把品質最好的人工字幕無聲地換成
            # 較差的語音辨識，而且把「請過幾分鐘再試」的提示吞掉。
            raise
        except NoSubtitlesAvailable:
            # 影片真的沒有可用字幕，才改用語音辨識
            if self._audio is None or self._transcriber is None:
                raise
            transcript = self._transcribe(url, settings, cb, check, cancelled)
            note = "由語音辨識產生，可能有辨識錯誤"

        check()
        cb(_P_SUBTITLES, "整理字幕…" + (f"（{note}）" if note else ""))
        compressed = compress_cues(
            transcript.cues, _budget(settings), is_automatic=transcript.is_automatic
        )

        slides = self._summarize(
            compressed, transcript, settings, cb, check, cancelled)

        # 逐字稿在摘要之後才算：取消發生在摘要階段的話，這幾十毫秒的字串
        # 處理就白做了。內容無損，字元預算只約束餵給模型的那一份。
        transcript_text = (
            full_transcript(transcript.cues, transcript.is_automatic)
            if settings.detailed else ""
        )
        # 質詢卡只在詳細模式做：它讀的是各段的 detail，一般模式沒有那一段。
        brief = (
            self._brief(slides, transcript_text, cb, check, cancelled)
            if settings.detailed and self._brief_writer is not None else None
        )

        out_dir = os.path.join(
            settings.output_dir, deck_folder_name(transcript.title, transcript.video_id)
        )
        clips_dir = os.path.join(out_dir, "_clips")
        try:
            check()
            cb(_P_SUMMARY, "下載影片片段…")
            clips = self._sections.download_sections(
                url,
                [s.timestamp for s in slides],
                settings.max_height,
                clips_dir,
                self._scaled(cb, _P_SUMMARY, _P_DOWNLOAD),
                cancelled,
            )
            slides = self._attach_images(slides, clips, out_dir, settings, cb, check)
        finally:
            self._sections.cleanup(clips_dir)

        check()
        cb(_P_FRAMES, "產生 HTML…")
        deck = Deck(source_url=url, video_title=transcript.title, slides=slides,
                    source_note=note, transcript_text=transcript_text, brief=brief)
        html_path = os.path.join(out_dir, "slides.html")
        self._renderer.render(deck, html_path)

        missing = deck.missing_images
        done = f"完成，共 {len(slides)} 頁"
        # 來源註記放進最後一則狀態：中途的提示會被後續狀態蓋掉
        done += f"，其中 {missing} 頁沒有截圖" if missing else ""
        cb(1.0, done + (f"（{note}）" if note else ""))
        return DeckResult(deck=deck, html_path=html_path)

    # --- 內部 ---

    def _transcribe(
        self,
        url: str,
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
    ) -> Transcript:
        """沒有字幕時：下載音訊 → 語音辨識 → 組成 Transcript。

        音訊放在固定的暫存資料夾：此時還不知道 video_id，無法放進影片自己的
        資料夾。app 一次只跑一個生成，不會撞名。資料夾名稱刻意取成明顯屬於
        本程式的樣子——cleanup 會整個刪除它，不能跟使用者自己的資料夾同名。
        """
        audio_dir = os.path.join(settings.output_dir, ".slidebox_audio_tmp")

        # 語音路徑只回報文字、進度條顯示忙碌：音訊下載的 50% 若直接進度條，
        # 接著摘要從 5% 開始，進度條會倒退。百分比改寫進文字，不丟掉。
        def speech_cb(frac: float | None, status: str) -> None:
            cb(None, status if frac is None else f"{status} {frac:.0%}")

        try:
            check()
            cb(None, "無法取得字幕，改用語音辨識…")
            # 先清空：上一次若在 finally 之前就中斷（例如轉錄時關掉視窗），
            # 留下的 .part 會被 yt-dlp 續傳，把兩支影片的位元組拼在一起。
            self._audio.cleanup(audio_dir)
            clip = self._audio.download_audio(url, audio_dir, speech_cb, cancelled)
            check()
            cues, language = self._transcriber.transcribe(
                clip.path, clip.duration, speech_cb, cancelled
            )
        finally:
            self._audio.cleanup(audio_dir)

        if not cues:
            raise NoSubtitlesAvailable("這部影片沒有字幕，也沒有偵測到語音")
        # is_automatic=False：Whisper 的輸出是一句一句的獨立段落，不是 YouTube
        # 的滾動字幕，套用滾動去重只會誤刪內容。
        return Transcript(
            video_id=clip.video_id,
            title=clip.title,
            duration=clip.duration,
            cues=cues,
            language=language,
            is_automatic=False,
        )

    def _summarize(
        self,
        compressed: str,
        transcript: Transcript,
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
    ) -> tuple[Slide, ...]:
        """摘要並驗證；不合格時把錯誤回饋給模型，最多重試一次。"""
        hint = ""
        problems: list[str] = []
        for attempt in (1, 2):
            check()
            cb(_P_SUBTITLES, "產生摘要…" if attempt == 1 else "摘要不合要求，重試一次…")
            try:
                slides = self._summarizer.summarize(
                    compressed,
                    transcript.duration,
                    settings.min_slides,
                    settings.max_slides,
                    hint,
                    self._scaled(cb, _P_SUBTITLES, _P_SUMMARY),
                    settings.detailed,
                    cancelled,
                )
            except SummarizerOutputInvalid as e:
                # 完全不是 JSON 的回應和「JSON 但欄位不合格」一樣值得重試一次；
                # 把錯誤內容當成 hint 回饋給模型，第二次仍失敗才真的放棄。
                if attempt == 2:
                    raise SummarizerOutputInvalid(
                        f"模型輸出重試後仍不符合要求（{e}）。可以試試換一個模型。"
                    ) from e
                hint = f"上一次的輸出有這個問題，請修正後重新產出：{e}"
                continue
            # 時間戳越界是小毛病，夾回去即可，不算驗證失敗
            slides = clamp_timestamps(slides, transcript.duration)
            problems = validate_slides(
                slides, settings.min_slides, settings.max_slides, settings.detailed)
            if not problems:
                return slides
            hint = "上一次的輸出有這些問題，請修正後重新產出：" + "；".join(problems)
        raise SummarizerOutputInvalid(
            "模型輸出重試後仍不符合要求（" + "；".join(problems) + "）。可以試試換一個模型。"
        )

    def _brief(
        self,
        slides: tuple[Slide, ...],
        transcript_text: str,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
    ) -> Brief | None:
        """寫質詢卡並做落地檢查；不合格重試一次，仍不行就沒有卡片。

        卡片是加值不是主體：分段摘要已經花了幾分鐘的 GPU，為了卡片把整支
        影片判成失敗不划算。放棄時寫進狀態列，後端與前端都會退回沒有卡片
        的樣子。
        """
        hint = ""
        for attempt in (1, 2):
            check()
            cb(None, "整理摘要卡…" if attempt == 1 else "摘要卡不合要求，重試一次…")
            try:
                raw = self._brief_writer.write(slides, cb, cancelled, hint)
            except SummarizerOutputInvalid as e:
                hint = f"上一次的輸出有這個問題，請修正後重新產出：{e}"
                continue
            except SummarizerUnavailable as e:
                cb(None, f"摘要卡略過（{e}）")
                return None
            brief = ground_brief(raw, transcript_text)
            problems = validate_brief(brief)
            if not problems:
                return brief
            hint = "上一次的輸出有這些問題，請修正後重新產出：" + "；".join(problems)
        cb(None, "摘要卡重試後仍不合要求，略過")
        return None

    def _attach_images(
        self,
        slides: tuple[Slide, ...],
        clips: list[str | None],
        out_dir: str,
        settings: Settings,
        cb: ProgressCallback,
        check,
    ) -> tuple[Slide, ...]:
        """逐頁抽幀。單頁失敗不中斷——13/15 頁有圖的成品仍然有用。"""
        filled: list[Slide] = []
        total = max(1, len(slides))
        for i, slide in enumerate(slides):
            check()
            clip = clips[i] if i < len(clips) else None
            if clip is None:
                filled.append(slide)
            else:
                # 檔名取自迴圈位置，不是 slide.index——index 來自模型輸出，
                # 不保證唯一或連續，不能拿來決定檔案系統路徑。
                dest = os.path.join(out_dir, f"{i + 1:02d}.webp")
                try:
                    self._frames.extract(clip, dest, settings.image_width)
                    filled.append(replace(slide, image_path=dest))
                except Exception:  # noqa: BLE001 該頁沒圖，其餘照做
                    filled.append(slide)
            span = _P_FRAMES - _P_DOWNLOAD
            cb(_P_DOWNLOAD + span * (i + 1) / total, f"處理截圖 {i + 1}/{total}")
        return tuple(filled)

    @staticmethod
    def _scaled(cb: ProgressCallback, lo: float, hi: float) -> ProgressCallback:
        """把子步驟的 0..1 進度映射到整條 pipeline 的 lo..hi 區間。"""
        def inner(frac: float | None, status: str) -> None:
            cb(None if frac is None else lo + (hi - lo) * frac, status)
        return inner
