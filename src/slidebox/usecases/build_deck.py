"""把整條 pipeline 串起來的 use case。

只做路徑字串運算，不碰檔案系統——建立目錄是各 adapter 的責任，
這樣本 use case 才能完全用記憶體假物件測試。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace

from ..domain.entities import (
    AudioClip,
    Brief,
    Deck,
    DeckResult,
    DiarizerOutput,
    SectionClips,
    Settings,
    Slide,
    TimeRange,
    Transcript,
    WordTranscript,
)
from ..domain.errors import (
    DiarizationUnavailable,
    NoSubtitlesAvailable,
    OperationCancelled,
    SpeechRecognitionFailed,
    SubtitleDownloadFailed,
    SubtitleTrackFailed,
    SummarizerOutputInvalid,
    SummarizerUnavailable,
)
from ..domain.ports import (
    AudioCutter,
    AudioGateway,
    BriefWriter,
    CancelCheck,
    Checkpoints,
    ConversationSummarizer,
    DeckRenderer,
    FrameExtractor,
    ProgressCallback,
    SpeechTranscriber,
    SpeechDiarizer,
    SubtitleGateway,
    SlideRewriter,
    Summarizer,
    VideoSectionGateway,
    WordTranscriber,
)
from .brief import ground_brief, validate_brief
from .chapters import (
    clamp_timestamps,
    first_person_problems,
    compress_cues,
    full_transcript,
    validate_slides,
)
from .conversation import DEFAULT_CHUNK_CHARS, allocate_slides, chunk_turns, format_window
from .diarized_pipeline import (
    adapter_fingerprint,
    assemble,
    attribution_counts,
    check_reliability,
    conversation_fingerprint,
    diarization_fingerprint,
    source_note,
    summarize_windows,
    summary_line,
    timeline_fingerprint,
    words_fingerprint,
)
from .naming import deck_folder_name
from .speakers import (
    assign_speakers,
    build_turns,
    clock,
    diarization_report,
    normalize_turns,
    summarize_speakers,
    transcript_lines,
)
from .time_range import effective_range, normalize_range, reported_range, shift_turns, shift_words

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
        diarizer: SpeechDiarizer | None = None,
        brief_writer: BriefWriter | None = None,
        rewriter: SlideRewriter | None = None,
        checkpoints: Checkpoints | None = None,
        word_transcriber: WordTranscriber | None = None,
        conversation: ConversationSummarizer | None = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        quote_only: bool = False,
        audio_cutter: AudioCutter | None = None,
    ):
        # 做完的貴步驟存起來，停了可以接著做；None 時每次都從頭來（測試多半這樣用）
        self._checkpoints = checkpoints
        # 多人模式（speaker_mode="diarize"）才用到這三個；單人與議會的路徑碰都不碰
        self._word_transcriber = word_transcriber
        self._conversation = conversation
        self._chunk_chars = chunk_chars
        # 安全模式（SLIDEBOX_QUOTE_ONLY）：多人模式留下的每個重點都顯示被核對過的原話
        self._quote_only = quote_only
        # 只處理影片的一段區間（add_video --start/--end）才用到；沒有區間的工作碰都不碰
        self._audio_cutter = audio_cutter
        self._audio = audio
        self._transcriber = transcriber
        self._diarizer = diarizer
        self._brief_writer = brief_writer
        self._rewriter = rewriter
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
        speech_hint: str | None = None,
        speaker_mode: str = "single",
        speaker_count: int | None = None,
        time_range: TimeRange | None = None,
    ) -> DeckResult:
        """speech_hint：走語音辨識時給辨識器的專有名詞提示（講者姓名、機關名）；
        有字幕的來源用不到。

        speaker_mode："single"（預設，議會與 --speakers 1）走下面這條字幕／語音摘要的路徑，
        行為與多人模式加進來之前完全相同；"diarize" 走 _execute_diarized，不經過下面的單人路徑。
        speaker_count：只有 diarize 用得到——None 是自動判斷人數，否則是固定的人數（2 以上）。
        time_range：只處理影片的這一段（原影片的秒數；add_video --start/--end）。只有 diarize 用得到：
        單人路徑吃整支影片的字幕，沒辦法切，所以 single 帶了區間就拒絕，絕不默默忽略。
        """
        if time_range is not None and not isinstance(time_range, TimeRange):
            raise ValueError(f"time_range 要是 TimeRange（或不指定），不是 {type(time_range).__name__}")
        if speaker_mode == "diarize":
            return self._execute_diarized(
                url, settings, progress, is_cancelled, speech_hint, speaker_count, time_range)
        if speaker_mode != "single":
            # 不認得的模式一律拒絕：絕不能默默走下面的單人路徑，那會把多位說話者全寫成「講者」——
            # 寧可失敗，也不能寫錯是誰說的。
            raise DiarizationUnavailable(
                f"不支援的說話者模式「{speaker_mode}」，只有 single 與 diarize；不會退回單人的寫法，工作中止")
        if normalize_range(time_range) is not None:
            raise ValueError(
                "只處理影片的一段區間只能用在多人模式（speaker_mode=\"diarize\"）：單人模式吃整支影片的字幕，"
                "沒辦法切；不會默默忽略區間，工作中止")
        cb: ProgressCallback = progress or (lambda frac, status: None)
        cancelled: CancelCheck = is_cancelled or (lambda: False)

        def check() -> None:
            if cancelled():
                raise OperationCancelled()

        ck = self._checkpoints
        transcript_fp = _fingerprint(settings.subtitle_langs, settings.whisper_model, speech_hint or "")
        saved = ck.load_transcript(settings.output_dir, url, transcript_fp) if ck else None
        if saved is not None:
            transcript, note = saved
            cb(_P_SUBTITLES, "沿用上次取得的逐字稿")
        else:
            transcript, note = self._fetch_transcript(url, settings, cb, check, cancelled, speech_hint)
            if ck:
                ck.save_transcript(settings.output_dir, url, transcript_fp, transcript, note)

        check()
        cb(_P_SUBTITLES, "整理字幕…" + (f"（{note}）" if note else ""))
        compressed = compress_cues(
            transcript.cues, _budget(settings), is_automatic=transcript.is_automatic
        )

        # 指紋含餵給模型的那份字幕：字元預算、字幕語言變了，摘要就該重做
        slides_fp = _fingerprint(settings.model, settings.min_slides, settings.max_slides,
                                 settings.detailed, settings.num_ctx, compressed)
        saved_slides = ck.load_slides(settings.output_dir, url, slides_fp) if ck else None
        if saved_slides is not None:
            slides, leftover = saved_slides
            cb(_P_SUMMARY, "沿用上次整理好的摘要")
        else:
            slides, leftover = self._summarize(
                compressed, transcript, settings, cb, check, cancelled)
            if ck:
                ck.save_slides(settings.output_dir, url, slides_fp, slides, leftover)
        # 改不掉的第一人稱照樣出片，但寫進來源註記：讀的人要知道那幾頁的「我們」是講者
        note = "；".join(n for n in (note, leftover) if n)

        # 逐字稿在摘要之後才算：取消發生在摘要階段的話，這幾十毫秒的字串
        # 處理就白做了。內容無損，字元預算只約束餵給模型的那一份。
        transcript_text = (
            full_transcript(transcript.cues, transcript.is_automatic)
            if settings.detailed else ""
        )
        # 質詢卡只在詳細模式做：它讀的是各段的 detail，一般模式沒有那一段。
        brief = None
        if settings.detailed and self._brief_writer is not None:
            # 卡片是從這份摘要寫出來的：摘要重產了，舊卡片的指紋就對不上
            brief_fp = _fingerprint(settings.model, [(x.title, x.bullets, x.detail) for x in slides],
                                    transcript_text)
            brief = ck.load_brief(settings.output_dir, url, brief_fp) if ck else None
            if brief is not None:
                cb(None, "沿用上次的摘要卡")
            else:
                brief = self._brief(slides, transcript_text, cb, check, cancelled)
                if brief is not None and ck:
                    # 放棄的卡片（None）不存：下次再試一次
                    ck.save_brief(settings.output_dir, url, brief_fp, brief)

        return self._publish(
            url, settings, cb, check, cancelled, video_title=transcript.title,
            video_id=transcript.video_id, slides=slides, note=note,
            transcript_text=transcript_text, brief=brief)

    def _execute_diarized(
        self,
        url: str,
        settings: Settings,
        progress: ProgressCallback | None,
        is_cancelled: CancelCheck | None,
        speech_hint: str | None,
        speaker_count: int | None,
        time_range: TimeRange | None = None,
    ) -> DeckResult:
        """多人模式（speaker_mode="diarize"）：只靠聲音，不看字幕。

        下載音訊（一次）→（只處理一段區間時：切出那一段）→ 說話者區分 → 語音辨識（每個字帶時間）→
        把字對到說話者 → 分段 →
        一個窗一個窗地請模型寫「引用第幾段＋沒有主詞的重點」→ 程式逐點檢查、說話者由被引用的
        那一段查出來 → 排成投影片 → 與單人模式共用的收尾（片段、截圖、HTML）。

        鐵則：模型不決定誰說了什麼，也看不到姓名；不可靠就失敗（AttributionUnreliable、
        DiarizationUnavailable…），**絕不退回單人的「講者」寫法**，也絕不發佈。不寫質詢卡
        （brief=None）、不用第一人稱改寫（檢查已經禁止第一人稱）。
        """
        cb: ProgressCallback = progress or (lambda frac, status: None)
        cancelled: CancelCheck = is_cancelled or (lambda: False)

        def check() -> None:
            if cancelled():
                raise OperationCancelled()

        if speaker_count is not None and (not isinstance(speaker_count, int) or speaker_count < 2):
            raise ValueError("多人模式的 speaker_count 要不指定（自動判斷人數），或是 2 以上的整數")
        # 沒給、或只是「從頭到尾」的區間就是沒有區間：指紋與存檔都照整支影片
        time_range = normalize_range(time_range)
        parts = (self._audio, self._diarizer, self._word_transcriber, self._conversation)
        if any(part is None for part in parts):
            raise DiarizationUnavailable(
                "多人模式需要音訊下載、說話者區分、語音辨識與對談摘要，這個組裝沒有提供其中一項；工作中止")
        if time_range is not None and self._audio_cutter is None:
            # 沒有切割器卻照樣處理整支影片，等於默默忽略區間：在下載任何東西之前就失敗
            raise DiarizationUnavailable("只處理影片的一段區間需要音訊切割器，這個組裝沒有提供；工作中止")
        # 先問可不可以（便宜，不載入模型）：不行就在下載任何東西之前失敗
        usable, reason = self._diarizer.available()
        if not usable:
            raise DiarizationUnavailable(reason or "說話者區分目前無法使用")

        cb(0.0, "多人模式：說話者由聲音區分…")
        ck = self._checkpoints
        diarization_fp = diarization_fingerprint(self._diarizer.fingerprint, speaker_count, time_range)
        words_fp = words_fingerprint(settings.whisper_model, speech_hint,
                                     adapter_fingerprint(self._word_transcriber), time_range)
        diarized, words = self._listen(
            url, settings, cb, check, cancelled, speech_hint, speaker_count, time_range,
            diarization_fp, words_fp,
            ck.load_diarization(settings.output_dir, url, diarization_fp) if ck else None,
            ck.load_words(settings.output_dir, url, words_fp) if ck else None)

        check()
        cb(_P_SUBTITLES, "對齊說話者…")
        turns = build_turns(assign_speakers(words.words, normalize_turns(diarized.turns)))
        speakers = summarize_speakers(turns)
        # 明顯不可靠（要求 2 人卻只找到 1 人、發言者超過 8 人、沒有任何發言者）就在這裡 raise：
        # 還沒花任何一次模型呼叫
        report = diarization_report(
            turns, speakers, engine=self._diarizer.engine, version=self._diarizer.version,
            model=self._diarizer.model, fingerprint=self._diarizer.fingerprint,
            requested_speakers=speaker_count, overlap_seconds=diarized.overlap_seconds)
        # 實際處理的區間（夾在影片長度內）寫進報告。新做的與接著做的都走同一條路，兩邊回報的一定一樣
        shown = reported_range(time_range, words.duration, words.words)
        if shown is not None:
            report = replace(report, range_start=shown.start, range_end=shown.end)

        allocations = allocate_slides(
            chunk_turns(turns, speakers, self._chunk_chars), settings.min_slides, settings.max_slides)
        plan = [(a.min_slides, a.max_slides, format_window(a.window, speakers)) for a in allocations]
        # 摘要的指紋除了設定與餵給模型的窗，還有驗證規則與摘要器的程式版本、每段話的時間：
        # 規則或提示詞改了卻忘了升版本，舊規則放行的摘要也不會被沿用
        conversation_fp = conversation_fingerprint(
            settings, self._chunk_chars, plan, self._quote_only,
            adapter=adapter_fingerprint(self._conversation), timeline=timeline_fingerprint(turns))
        saved = ck.load_conversation(settings.output_dir, url, conversation_fp) if ck else None
        counts = None
        if saved is not None:
            slides, proposed = saved
            cb(_P_SUMMARY, "沿用上次整理好的對談重點")
        else:
            outcomes = summarize_windows(
                self._conversation, allocations, turns, speakers,
                self._scaled(cb, _P_SUBTITLES, _P_SUMMARY), check, cancelled, self._quote_only)
            counts = attribution_counts(outcomes)
            cb(None, summary_line(counts))               # 提出／留下／原話／丟掉的分類，記在工作的進度紀錄裡
            check_reliability(outcomes)
            slides = assemble(outcomes)
            proposed = counts.proposed
            trimmed = sum(o.trimmed_pages for o in outcomes)
            if trimmed:
                cb(None, f"有 {trimmed} 頁超出頁數上限，只留前面的頁")
            if ck:
                ck.save_conversation(settings.output_dir, url, conversation_fp, slides, proposed)
        report = replace(report, proposed_points=proposed,
                         kept_points=sum(len(s.points) for s in slides))
        if counts is not None:                           # 沿用存檔時沒有重新檢查，這幾個數字留空
            report = replace(report, rescued_points=counts.rescued, contradicted_points=counts.contradicted,
                             unverifiable_points=counts.unverifiable, anchor_conflicts=counts.anchor_conflicts)

        return self._publish(
            url, settings, cb, check, cancelled, video_title=words.title, video_id=words.video_id,
            slides=slides, note=source_note(report), transcript_text=transcript_lines(turns),
            brief=None, extra={"speaker_mode": "diarize", "speakers": speakers, "diarization": report})

    def _publish(
        self,
        url: str,
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
        *,
        video_title: str,
        video_id: str,
        slides: tuple[Slide, ...],
        note: str,
        transcript_text: str,
        brief: Brief | None,
        extra: dict | None = None,
    ) -> DeckResult:
        """單人與多人共用的收尾：下載片段 → 抽幀 → 組成 Deck → 輸出 HTML → 清存檔 → 完成訊息。

        extra 是多人模式才有的 Deck 欄位（speaker_mode、speakers、diarization）；單人不傳，
        Deck 就維持預設值，與多人模式加進來之前完全相同。
        """
        out_dir = os.path.join(settings.output_dir, deck_folder_name(video_title, video_id))
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
            slides, screenshot_problem = self._attach_images(slides, clips, out_dir, settings, cb, check)
        finally:
            self._sections.cleanup(clips_dir)

        check()
        cb(_P_FRAMES, "產生 HTML…")
        deck = Deck(source_url=url, video_title=video_title, slides=slides,
                    source_note=note, transcript_text=transcript_text, brief=brief, **(extra or {}))
        html_path = os.path.join(out_dir, "slides.html")
        self._renderer.render(deck, html_path)

        result = DeckResult(deck=deck, html_path=html_path, screenshot_problem=screenshot_problem)
        if self._checkpoints:
            # 只在整份成功之後清：失敗或取消時留著，下次接著做
            self._checkpoints.clear(settings.output_dir, url)
        # 截圖失敗的原因與來源註記都放進最後一則狀態：中途的提示會被後續狀態蓋掉
        done = f"完成，共 {len(slides)} 頁{result.missing_images_note}"
        cb(1.0, done + (f"（{note}）" if note else ""))
        return result

    # --- 內部 ---

    def _listen(
        self,
        url: str,
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
        speech_hint: str | None,
        speaker_count: int | None,
        time_range: TimeRange | None,
        diarization_fp: str,
        words_fp: str,
        diarized: DiarizerOutput | None,
        words: WordTranscript | None,
    ) -> tuple[DiarizerOutput, WordTranscript]:
        """多人模式的「聽」：說話者區分與語音辨識（每個字帶時間），共用同一份音訊、只下載一次。

        diarized／words 是上一次存下來的（沒有就是 None）；兩份都有就完全不下載音訊。做完的
        每一份各自立刻存檔：下一步失敗或被取消時，這一份不必再做。音訊在 finally 裡清掉。
        沒偵測到語音是失敗，不存、不發佈。

        只處理一段區間時，兩個模型只聽切出來的那一段，量到的時間在**存檔之前**就加回區間的起點：
        存下來、往後傳的都是原影片的時間（設計文件 #32）。
        """
        if diarized is not None:
            cb(None, "沿用上次的說話者區分")
        if words is not None:
            cb(None, "沿用上次的語音辨識")
        if diarized is not None and words is not None:
            return diarized, words

        ck = self._checkpoints
        audio_dir = os.path.join(settings.output_dir, ".slidebox_audio_tmp")

        def speech_cb(frac: float | None, status: str) -> None:
            # 同單人的語音路徑：進度條顯示忙碌，百分比寫進文字，不讓條子倒退
            cb(None, status if frac is None else f"{status} {frac:.0%}")

        try:
            check()
            cb(None, "下載音訊…")
            # 先清空：上一次若在 finally 之前就中斷，留下的 .part 會被 yt-dlp 續傳
            self._audio.cleanup(audio_dir)
            try:
                clip = self._audio.download_audio(url, audio_dir, speech_cb, cancelled)
            except NoSubtitlesAvailable as e:
                # 音訊 gateway 的失敗訊息是為「沒有字幕之後」寫的；多人模式根本沒找過字幕
                raise SpeechRecognitionFailed(_speech_reason(e)) from e
            check()
            heard = self._cut(clip, time_range, audio_dir, cb, check)
            if diarized is None:
                diarized = shift_turns(self._diarizer.diarize(
                    heard.path, heard.duration, speaker_count, speech_cb, cancelled), heard.offset)
                if ck:
                    ck.save_diarization(settings.output_dir, url, diarization_fp, diarized)
                check()
            if words is None:
                spoken, language = self._word_transcriber.transcribe_words(
                    heard.path, heard.duration, speech_cb, cancelled, speech_hint)
                if not spoken:
                    raise SpeechRecognitionFailed("沒有偵測到語音，無法區分誰說了什麼")
                # duration 是整支影片的長度（不是切出來的那一段）：回報區間時要拿它來夾終點
                words = WordTranscript(video_id=clip.video_id, title=clip.title,
                                       duration=clip.duration, language=language,
                                       words=shift_words(spoken, heard.offset))
                if ck:
                    ck.save_words(settings.output_dir, url, words_fp, words)
        finally:
            self._audio.cleanup(audio_dir)
        return diarized, words

    def _cut(self, clip: AudioClip, time_range: TimeRange | None, audio_dir: str,
             cb: ProgressCallback, check) -> AudioClip:
        """只處理一段區間時，把下載好的音訊切成那一段；沒有區間（或區間就是整支影片）原樣回傳。

        影片的實際長度要下載之後才知道，所以區間在這裡才夾、才檢查（起點超過影片長度、夾完太短就失敗，
        在花任何一次模型的時間之前）。切出來的檔放在下載音訊的同一個資料夾，一起被清掉。
        """
        wanted = effective_range(time_range, clip.duration)
        if wanted is None:
            return clip
        cb(None, f"切出指定區間（{clock(wanted.start)}–{'結尾' if wanted.end is None else clock(wanted.end)}）…")
        cut = self._audio_cutter.cut(clip, wanted.start, wanted.end, audio_dir)
        check()
        return cut

    def _fetch_transcript(
        self,
        url: str,
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
        speech_hint: str | None,
    ) -> tuple[Transcript, str]:
        """取得字幕；沒有字幕（或字幕軌下載失敗）就改走語音辨識。回傳 (逐字稿, 來源註記)。"""
        check()
        cb(0.0, "取得字幕…")
        try:
            transcript = self._subtitles.fetch(url, settings.subtitle_langs)
            # 來源自己講的優先；沒講就用「是不是 YouTube 自動字幕」判斷
            note = transcript.source_note or (
                "使用 YouTube 自動字幕，品質可能較差" if transcript.is_automatic else "")
        except SubtitleDownloadFailed:
            # 來源明確說「改走語音也幫不上忙」（例如 IVOD 還沒有逐字稿，備援的 yt-dlp 不認得
            # IVOD 網址）：原樣回報，真正的原因才不會被一個無關的音訊錯誤蓋掉
            raise
        except NoSubtitlesAvailable as missing:
            # 沒有字幕、或字幕軌下載失敗（SubtitleTrackFailed，例如 YouTube 限流）：改用語音辨識。
            # 下載失敗時把原因寫進來源註記——讀的人要知道這份投影片不是從字幕來的、為什麼
            if self._audio is None or self._transcriber is None:
                raise
            transcript = self._transcribe(url, settings, cb, check, cancelled, speech_hint, missing)
            note = (f"字幕下載失敗（{missing.reason}），改由語音辨識產生，可能有辨識錯誤"
                    if isinstance(missing, SubtitleTrackFailed)
                    else "由語音辨識產生，可能有辨識錯誤")
        return transcript, note

    def _transcribe(
        self,
        url: str,
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
        speech_hint: str | None = None,
        missing: NoSubtitlesAvailable | None = None,
    ) -> Transcript:
        """沒有字幕（或字幕軌下載失敗）時：下載音訊 → 語音辨識 → 組成 Transcript。

        missing 是字幕那一步的錯誤。字幕軌下載失敗（SubtitleTrackFailed）而語音備援也失敗時，
        最後的錯誤要同時講兩件事：只講音訊的錯，使用者不會知道字幕其實存在、只是被限流。

        音訊放在固定的暫存資料夾：此時還不知道 video_id，無法放進影片自己的
        資料夾。app 一次只跑一個生成，不會撞名。資料夾名稱刻意取成明顯屬於
        本程式的樣子——cleanup 會整個刪除它，不能跟使用者自己的資料夾同名。
        """
        audio_dir = os.path.join(settings.output_dir, ".slidebox_audio_tmp")

        # 語音路徑只回報文字、進度條顯示忙碌：音訊下載的 50% 若直接進度條，
        # 接著摘要從 5% 開始，進度條會倒退。百分比改寫進文字，不丟掉。
        def speech_cb(frac: float | None, status: str) -> None:
            cb(None, status if frac is None else f"{status} {frac:.0%}")

        track_failed = isinstance(missing, SubtitleTrackFailed)
        try:
            check()
            cb(None, "字幕下載失敗，改用語音辨識…" if track_failed else "無法取得字幕，改用語音辨識…")
            # 先清空：上一次若在 finally 之前就中斷（例如轉錄時關掉視窗），
            # 留下的 .part 會被 yt-dlp 續傳，把兩支影片的位元組拼在一起。
            self._audio.cleanup(audio_dir)
            clip = self._audio.download_audio(url, audio_dir, speech_cb, cancelled)
            check()
            cues, language = self._transcriber.transcribe(
                clip.path, clip.duration, speech_cb, cancelled, speech_hint
            )
        except OperationCancelled:
            raise
        except Exception as e:
            if not track_failed:
                raise
            raise SubtitleDownloadFailed(
                _both_failed(missing, f"改用語音辨識也失敗：{_speech_reason(e)}")) from e
        finally:
            self._audio.cleanup(audio_dir)

        if not cues:
            if track_failed:
                raise SubtitleDownloadFailed(_both_failed(missing, "改用語音辨識也沒有偵測到語音"))
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
    ) -> tuple[tuple[Slide, ...], str]:
        """摘要並驗證；不合格時把錯誤回饋給模型，最多重試一次。

        第二個回傳值是改不掉的第一人稱註記（都改好時是空字串）：重試後剩下的問題只有
        第一人稱時照樣出片——那是小瑕疵，為它把幾分鐘的摘要（還可能有語音辨識）整份作廢不划算。
        頁數不對、空白頁這種是真的壞了，照舊失敗。
        """
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
            if problems and self._rewriter is not None and _only_first_person(slides, problems):
                # 只有第一人稱的問題：整份重產修不好（修好這頁別頁又中），
                # 改成只把有問題的頁送回去改寫。實測整份重產的失敗率兩成。
                slides, problems = self._repair_first_person(
                    slides, settings, cb, check, cancelled)
            if not problems:
                return slides, ""
            hint = "上一次的輸出有這些問題，請修正後重新產出：" + "；".join(problems)
        if _only_first_person(slides, problems):
            pages = "、".join(str(s.index) for s in slides if first_person_problems(s))
            cb(None, f"第 {pages} 頁仍有第一人稱，照樣出片")
            return slides, f"第 {pages} 頁仍有第一人稱的敘述（模型改寫不掉）"
        raise SummarizerOutputInvalid(
            "模型輸出重試後仍不符合要求（" + "；".join(problems) + "）。可以試試換一個模型。"
        )

    def _repair_first_person(
        self,
        slides: tuple[Slide, ...],
        settings: Settings,
        cb: ProgressCallback,
        check,
        cancelled: CancelCheck,
    ) -> tuple[tuple[Slide, ...], list[str]]:
        """逐頁改寫第一人稱，最多兩輪。回傳 (slides, 剩下的問題)。

        每一輪只碰還有問題的頁；改寫後重跑完整驗證，改寫若弄壞了別的規則
        （例如 detail 變太短），那些問題會留在回傳值裡，交給外層決定重產。
        """
        problems: list[str] = []
        for round_no in (1, 2):
            fixed = list(slides)
            offenders = [(i, s, first_person_problems(s)) for i, s in enumerate(slides)
                         if first_person_problems(s)]
            for n, (i, slide, found) in enumerate(offenders, start=1):
                check()
                cb(None, f"第 {slide.index} 頁改寫成第三人稱…（{n}/{len(offenders)}）")
                try:
                    fixed[i] = self._rewriter.rewrite(slide, found[0], cb, cancelled)
                except (SummarizerOutputInvalid, SummarizerUnavailable) as e:
                    # 改寫失敗（實測模型偶爾回空字串）就當這頁沒改掉：交給下一輪或整份重產，
                    # 不讓一頁的改寫把整個工作停掉
                    cb(None, f"第 {slide.index} 頁改寫失敗（{_short(e)}），先保留原樣")
            slides = tuple(fixed)
            problems = validate_slides(
                slides, settings.min_slides, settings.max_slides, settings.detailed)
            if not problems or not _only_first_person(slides, problems):
                break
        return slides, problems

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
    ) -> tuple[tuple[Slide, ...], str]:
        """逐頁抽幀。單頁失敗不中斷——13/15 頁有圖的成品仍然有用。

        第二個回傳值是第一張沒截到的圖的原因（下載片段或抽幀），給完成訊息用；都有圖時是空字串。
        """
        filled: list[Slide] = []
        problem = ""
        # gateway 沒說原因（只回 list）時至少講出是哪一步
        download_failure = (clips.failure if isinstance(clips, SectionClips) else "") or "片段下載失敗"
        total = max(1, len(slides))
        for i, slide in enumerate(slides):
            check()
            clip = clips[i] if i < len(clips) else None
            if clip is None:
                filled.append(slide)
                problem = problem or _short_text(download_failure)
            else:
                # 檔名取自迴圈位置，不是 slide.index——index 來自模型輸出，
                # 不保證唯一或連續，不能拿來決定檔案系統路徑。
                dest = os.path.join(out_dir, f"{i + 1:02d}.webp")
                try:
                    self._frames.extract(clip, dest, settings.image_width)
                    filled.append(replace(slide, image_path=dest))
                except Exception as e:  # noqa: BLE001 該頁沒圖，其餘照做；原因留給完成訊息
                    filled.append(slide)
                    problem = problem or _short(e)
            span = _P_FRAMES - _P_DOWNLOAD
            cb(_P_DOWNLOAD + span * (i + 1) / total, f"處理截圖 {i + 1}/{total}")
        return tuple(filled), problem

    @staticmethod
    def _scaled(cb: ProgressCallback, lo: float, hi: float) -> ProgressCallback:
        """把子步驟的 0..1 進度映射到整條 pipeline 的 lo..hi 區間。"""
        def inner(frac: float | None, status: str) -> None:
            cb(None if frac is None else lo + (hi - lo) * frac, status)
        return inner


def _only_first_person(slides, problems: list[str]) -> bool:
    """驗證失敗是否全是第一人稱。其他問題（頁數、空白、太短）要整份重產才修得好。"""
    first_person = {p for s in slides for p in first_person_problems(s)}
    return bool(problems) and all(p in first_person for p in problems)


def _short(error: Exception) -> str:
    """錯誤訊息給畫面用：去掉 yt-dlp 的「ERROR: 」前綴、截短。"""
    return _short_text(str(error)) or type(error).__name__


# yt-dlp 在終端機裡會替「ERROR:」上色
_ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


def _short_text(text: str) -> str:
    """去掉顏色碼與「ERROR: 」前綴，壓成一行（狀態列放不下換行），超過 160 字截短。"""
    text = " ".join(_ANSI_COLOUR.sub("", text).split()).removeprefix("ERROR: ")
    return text if len(text) <= 160 else text[:159] + "…"


# 音訊與語音辨識的 adapter 預設自己是在「沒有字幕」之後才被叫到，訊息都以這句開頭
_NO_SUBTITLES_PREFIX = re.compile(r"^這部影片沒有字幕[，；]\s*")
# 拿掉前綴之後，「音訊也下載失敗」「語音辨識模型…也無法載入」的「也」沒有東西可以接了
_DANGLING_ALSO = re.compile(r"^([^：（]*?)也")


def _speech_reason(error: Exception) -> str:
    """語音備援失敗的原因，接在「字幕軌下載失敗」後面。

    adapter 丟的 NoSubtitlesAvailable 已經是給人看的句子（含「若未安裝請執行 uv sync」這類提示），
    只拿掉「這部影片沒有字幕」——字幕其實存在，只是下載失敗——不截短；其他例外照 _short 處理。
    """
    if not isinstance(error, NoSubtitlesAvailable):
        return _short(error)
    text = _NO_SUBTITLES_PREFIX.sub("", str(error).strip())
    return _DANGLING_ALSO.sub(r"\1", text, count=1) if text != str(error).strip() else text


def _both_failed(missing: SubtitleTrackFailed, speech: str) -> str:
    """字幕軌下載失敗、語音備援也失敗：兩個原因都講；限流的話提醒稍後重試。"""
    return f"{missing.reason}；{speech}" + ("，請過幾分鐘再試。" if missing.retry_later else "")


def _fingerprint(*parts) -> str:
    """產生某一步時用到的設定與輸入。存檔的指紋對不上就重做那一步，不拿舊設定的結果冒充。"""
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
