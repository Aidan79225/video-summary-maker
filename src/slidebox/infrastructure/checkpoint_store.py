"""Checkpoints 的 JSON 實作：存在輸出資料夾底下的 .slidebox_cache/<網址雜湊>/。

一個網址一個資料夾，一步一個檔案（transcript.json、slides.json、brief.json），每個檔案是
{"fingerprint": ..., "data": ...}。寫入先寫暫存檔再改名，當機也不會留下半個檔案被當成存檔；
真的讀到壞檔（或任何讀寫錯誤）一律當作沒有存檔——存檔是加值，不能讓工作因為它失敗。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import asdict

from ..domain.entities import Ask, Brief, Cue, KeyNumber, Slide, Transcript

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


def _folder(output_dir: str, url: str) -> str:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(output_dir, _CACHE_DIR, key)


def _path(output_dir: str, url: str, stage: str) -> str:
    return os.path.join(_folder(output_dir, url), f"{stage}.json")
