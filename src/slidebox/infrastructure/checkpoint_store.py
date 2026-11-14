"""Checkpoints 的 JSON 實作：存在輸出資料夾底下的 .slidebox_cache/<網址雜湊>/。

一個網址一個資料夾，一步一個檔案（transcript.json、slides.json、brief.json），每個檔案是
{"fingerprint": ..., "data": ...}。寫入先寫暫存檔再改名，當機也不會留下半個檔案被當成存檔；
真的讀到壞檔（或任何讀寫錯誤）一律當作沒有存檔——存檔是加值，不能讓工作因為它失敗。

多人模式另外有三個階段（diarization.json、words.json、conversation.json）；單人模式的三個階段與
檔案一個字都沒動。多人的階段**只存原始的、完整的產出**（說話者區段、語音辨識的字、通過檢查的摘要），
空的、沒有說話者的、或時間不是有限數字的一律不存：存下去就等於下次接著用一份不可信的結果。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
from collections.abc import Sequence
from dataclasses import asdict

from ..domain.entities import (
    Ask,
    Brief,
    Cue,
    DiarizerOutput,
    KeyNumber,
    Point,
    Slide,
    SpeakerTurn,
    Transcript,
    Word,
    WordTranscript,
)

# 名稱刻意取成明顯屬於本程式的樣子：clear 會整個刪掉其中一個子資料夾
_CACHE_DIR = ".slidebox_cache"


class JsonCheckpointStore:
    def load_transcript(self, output_dir: str, url: str,
                        fingerprint: str) -> tuple[Transcript, str] | None:
        data = self._read(output_dir, url, "transcript", fingerprint)
        if data is None:
            return None
        try:
            t = data["transcript"]
            transcript = Transcript(
                video_id=t["video_id"], title=t["title"], duration=float(t["duration"]),
                cues=tuple(Cue(float(c["start"]), float(c["end"]), c["text"]) for c in t["cues"]),
                language=t["language"], is_automatic=bool(t["is_automatic"]),
                source_note=t.get("source_note", ""))
            return transcript, data["note"]
        except (KeyError, TypeError, ValueError):
            return None

    def save_transcript(self, output_dir: str, url: str, fingerprint: str,
                        transcript: Transcript, note: str) -> None:
        self._write(output_dir, url, "transcript", fingerprint,
                    {"transcript": asdict(transcript), "note": note})

    def load_slides(self, output_dir: str, url: str,
                    fingerprint: str) -> tuple[tuple[Slide, ...], str] | None:
        data = self._read(output_dir, url, "slides", fingerprint)
        if data is None:
            return None
        try:
            # image_path 不存：截圖每次都重抽，存了也是指向上一次的暫存位置
            slides = tuple(
                Slide(index=int(x["index"]), title=x["title"], bullets=tuple(x["bullets"]),
                      timestamp=float(x["timestamp"]), detail=x.get("detail", ""))
                for x in data["slides"])
            return slides, data["note"]
        except (KeyError, TypeError, ValueError):
            return None

    def save_slides(self, output_dir: str, url: str, fingerprint: str,
                    slides: Sequence[Slide], note: str) -> None:
        self._write(output_dir, url, "slides", fingerprint, {
            "slides": [{"index": x.index, "title": x.title, "bullets": list(x.bullets),
                        "timestamp": x.timestamp, "detail": x.detail} for x in slides],
            "note": note,
        })

    def load_brief(self, output_dir: str, url: str, fingerprint: str) -> Brief | None:
        data = self._read(output_dir, url, "brief", fingerprint)
        if data is None:
            return None
        try:
            return Brief(
                one_liner=data["one_liner"],
                key_numbers=tuple(KeyNumber(**k) for k in data["key_numbers"]),
                asks=tuple(Ask(**a) for a in data["asks"]))
        except (KeyError, TypeError, ValueError):
            return None

    def save_brief(self, output_dir: str, url: str, fingerprint: str, brief: Brief) -> None:
        self._write(output_dir, url, "brief", fingerprint, asdict(brief))

    # --- 多人模式 ---

    def load_diarization(self, output_dir: str, url: str,
                         fingerprint: str) -> DiarizerOutput | None:
        data = self._read(output_dir, url, "diarization", fingerprint)
        if data is None:
            return None
        try:
            turns = tuple(SpeakerTurn(float(start), float(end), _text(speaker))
                          for start, end, speaker in data["turns"])
            output = DiarizerOutput(turns, float(data["overlap_seconds"]))
        except (KeyError, TypeError, ValueError):
            return None
        return output if _usable_diarization(output) else None

    def save_diarization(self, output_dir: str, url: str, fingerprint: str,
                         output: DiarizerOutput) -> None:
        if not _usable_diarization(output):
            return                                  # 沒有說話者區段的結果不存，下次重做
        self._write(output_dir, url, "diarization", fingerprint, {
            "turns": [[t.start, t.end, t.speaker] for t in output.turns],
            "overlap_seconds": output.overlap_seconds,
        })

    def load_words(self, output_dir: str, url: str, fingerprint: str) -> WordTranscript | None:
        data = self._read(output_dir, url, "words", fingerprint)
        if data is None:
            return None
        try:
            words = tuple(Word(float(start), float(end), _text(text), int(segment), float(probability))
                          for start, end, text, segment, probability in data["words"])
            transcript = WordTranscript(
                video_id=_text(data["video_id"]), title=_text(data["title"]),
                duration=float(data["duration"]), language=_text(data["language"]), words=words)
        except (KeyError, TypeError, ValueError):
            return None
        return transcript if _usable_words(transcript) else None

    def save_words(self, output_dir: str, url: str, fingerprint: str,
                   words: WordTranscript) -> None:
        if not _usable_words(words):
            return                                  # 沒有字的語音辨識不存，下次重做
        self._write(output_dir, url, "words", fingerprint, {
            "video_id": words.video_id, "title": words.title, "duration": words.duration,
            "language": words.language,
            "words": [[w.start, w.end, w.text, w.segment, w.probability] for w in words.words],
        })

    def load_conversation(self, output_dir: str, url: str,
                          fingerprint: str) -> tuple[tuple[Slide, ...], int] | None:
        data = self._read(output_dir, url, "conversation", fingerprint)
        if data is None:
            return None
        try:
            # image_path 不存：截圖每次都重抽，存了也是指向上一次的暫存位置
            slides = tuple(
                Slide(index=int(x["index"]), title=_text(x["title"]),
                      bullets=tuple(_text(b) for b in x["bullets"]),
                      timestamp=float(x["timestamp"]), detail=_text(x.get("detail", "")),
                      points=tuple(Point(speaker=_text(p["speaker"]), turn=int(p["turn"]),
                                         start=float(p["start"]), text=_text(p["text"]),
                                         evidence=_text(p["evidence"]))
                                   for p in x["points"]))
                for x in data["slides"])
            proposed = int(data["proposed"])
        except (KeyError, TypeError, ValueError):
            return None
        return (slides, proposed) if _usable_conversation(slides, proposed) else None

    def save_conversation(self, output_dir: str, url: str, fingerprint: str,
                          slides: Sequence[Slide], proposed: int) -> None:
        slides = tuple(slides)
        if not _usable_conversation(slides, proposed):
            return                                  # 有一頁沒有重點（沒有說話者）就整份不存
        self._write(output_dir, url, "conversation", fingerprint, {
            "slides": [{
                "index": x.index, "title": x.title, "bullets": list(x.bullets),
                "timestamp": x.timestamp, "detail": x.detail,
                "points": [{"speaker": p.speaker, "turn": p.turn, "start": p.start,
                            "text": p.text, "evidence": p.evidence} for p in x.points],
            } for x in slides],
            "proposed": proposed,
        })

    def clear(self, output_dir: str, url: str) -> None:
        shutil.rmtree(_folder(output_dir, url), ignore_errors=True)

    # --- 內部 ---

    def _read(self, output_dir: str, url: str, stage: str, fingerprint: str):
        try:
            with open(_path(output_dir, url, stage), encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(stored, dict) or stored.get("fingerprint") != fingerprint:
            return None
        return stored.get("data")

    def _write(self, output_dir: str, url: str, stage: str, fingerprint: str, data) -> None:
        path = _path(output_dir, url, stage)
        temp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp, "w", encoding="utf-8") as f:
                json.dump({"url": url, "fingerprint": fingerprint, "data": data}, f,
                          ensure_ascii=False)
            os.replace(temp, path)
        except (OSError, TypeError, ValueError):
            with contextlib.suppress(OSError):
                os.remove(temp)


def _text(value: object) -> str:
    """存檔裡的字串欄位：不是字串就當壞檔（TypeError 由呼叫端轉成「沒有存檔」）。"""
    if not isinstance(value, str):
        raise TypeError("不是字串")
    return value


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _usable_diarization(output: DiarizerOutput) -> bool:
    return (bool(output.turns) and _finite(output.overlap_seconds)
            and all(t.speaker and _finite(t.start, t.end) and t.end > t.start for t in output.turns))


def _usable_words(transcript: WordTranscript) -> bool:
    return (bool(transcript.words) and _finite(transcript.duration)
            and all(_finite(w.start, w.end, w.probability) for w in transcript.words))


def _usable_conversation(slides: Sequence[Slide], proposed: int) -> bool:
    return (bool(slides) and proposed >= sum(len(s.points) for s in slides)
            and all(s.points and all(p.speaker and _finite(p.start) for p in s.points) for s in slides)
            and all(_finite(s.timestamp) for s in slides))


def _folder(output_dir: str, url: str) -> str:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(output_dir, _CACHE_DIR, key)


def _path(output_dir: str, url: str, stage: str) -> str:
    return os.path.join(_folder(output_dir, url), f"{stage}.json")
