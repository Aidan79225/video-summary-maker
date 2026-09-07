"""把整條 pipeline 串起來的 use case。

只做路徑字串運算，不碰檔案系統——建立目錄是各 adapter 的責任，
這樣本 use case 才能完全用記憶體假物件測試。
"""
from __future__ import annotations

import os
from dataclasses import replace

from ..domain.entities import Deck, DeckResult, Settings, Slide, Transcript
from ..domain.errors import OperationCancelled, SummarizerOutputInvalid
from ..domain.ports import (
    CancelCheck,
    DeckRenderer,
    FrameExtractor,
    ProgressCallback,
    SubtitleGateway,
    Summarizer,
    VideoSectionGateway,
)
from .chapters import clamp_timestamps, compress_cues, validate_slides

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
    ):
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
        transcript = self._subtitles.fetch(url, settings.subtitle_langs)

        check()
        cb(_P_SUBTITLES, "整理字幕…")
        compressed = compress_cues(transcript.cues, settings.char_budget)

        slides = self._summarize(compressed, transcript, settings, cb, check)

        out_dir = os.path.join(settings.output_dir, transcript.video_id)
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
        deck = Deck(source_url=url, video_title=transcript.title, slides=slides)
        html_path = os.path.join(out_dir, "slides.html")
        self._renderer.render(deck, html_path)

        missing = deck.missing_images
        done = f"完成，共 {len(slides)} 頁"
        cb(1.0, done + (f"，其中 {missing} 頁沒有截圖" if missing else ""))
        return DeckResult(deck=deck, html_path=html_path)

    # --- 內部 ---

    def _summarize(
        self,
        compressed: str,
        transcript: Transcript,
        settings: Settings,
        cb: ProgressCallback,
        check,
    ) -> tuple[Slide, ...]:
        """摘要並驗證；不合格時把錯誤回饋給模型，最多重試一次。"""
        hint = ""
        problems: list[str] = []
        for attempt in (1, 2):
            check()
            cb(_P_SUBTITLES, "產生摘要…" if attempt == 1 else "摘要不合要求，重試一次…")
            slides = self._summarizer.summarize(
                compressed,
                transcript.duration,
                settings.min_slides,
                settings.max_slides,
                hint,
                self._scaled(cb, _P_SUBTITLES, _P_SUMMARY),
            )
            # 時間戳越界是小毛病，夾回去即可，不算驗證失敗
            slides = clamp_timestamps(slides, transcript.duration)
            problems = validate_slides(slides, settings.min_slides, settings.max_slides)
            if not problems:
                return slides
            hint = "上一次的輸出有這些問題，請修正後重新產出：" + "；".join(problems)
        raise SummarizerOutputInvalid(
            "模型輸出重試後仍不符合要求（" + "；".join(problems) + "）。可以試試換一個模型。"
        )

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
                dest = os.path.join(out_dir, f"{slide.index:02d}.webp")
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
