"""多人模式的 API 測試共用的東西：契約範例、從範例建回 Deck、契約的不變式檢查。

契約範例（fixtures/speaker_contract_v1.json）與新聞服務那邊的副本逐位元組相同；
兩邊各自用它驗證自己的那一半，所以改欄位要兩邊一起改、一起升 speaker_contract 的版本。
"""
from __future__ import annotations

import json
import math
import os
import re

from slidebox.domain.entities import (
    Deck,
    DiarizationReport,
    Point,
    Slide,
    SpeakerInfo,
    SpeakerSample,
)

CONTRACT_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "speaker_contract_v1.json")
SPEAKER_ID = re.compile(r"^S[1-9][0-9]{0,2}$")
CLOCK_LINE = re.compile(r"^\d+:\d\d \[\[S\d+\]\]：")
CODE = re.compile(r"\[\[(S\d+)\]\]")


def load_contract() -> dict:
    with open(CONTRACT_PATH, encoding="utf-8") as f:
        return json.load(f)


def deck_from_result(result: dict) -> Deck:
    """契約範例裡的成品，反過來建成 Deck（圖片一律沒有）。"""
    speakers = tuple(
        SpeakerInfo(
            id=s["id"], label=s["label"], kind=s["kind"], seconds=s["seconds"], turns=s["turns"],
            first_seen=s["first_seen"], last_seen=s["last_seen"],
            samples=tuple(SpeakerSample(x["start"], x["text"]) for x in s["samples"]),
            flags=tuple(s["flags"]))
        for s in result["speakers"])
    d = result["diarization"]
    report = DiarizationReport(
        engine=d["engine"], version=d["version"], model=d["model"], fingerprint=d["fingerprint"],
        status=d["status"], warnings=tuple(d["warnings"]), requested_speakers=d["requested_speakers"],
        found_speakers=d["found_speakers"], minor_speakers=d["minor_speakers"],
        speech_seconds=d["speech_seconds"], unassigned_ratio=d["unassigned_ratio"],
        overlap_ratio=d["overlap_ratio"], proposed_points=d["proposed_points"], kept_points=d["kept_points"])
    slides = tuple(
        Slide(index=s["index"], title=s["title"], bullets=tuple(s["bullets"]), timestamp=s["timestamp"],
              image_path=None, detail=s["detail"],
              points=tuple(Point(p["speaker"], p["turn"], p["start"], p["text"], p["evidence"])
                           for p in s["points"]))
        for s in result["slides"])
    return Deck(
        source_url=result["source_url"], video_title=result["title"], slides=slides,
        source_note=result["source_note"], transcript_text=result["transcript_text"], brief=None,
        speaker_mode="diarize", speakers=speakers, diarization=report)


def key_paths(value, prefix: str = "") -> set[str]:
    """成品裡所有「鍵的路徑」，例如 slides[].points[].evidence：比較兩份成品的形狀，不比值。"""
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= key_paths(inner, path)
    elif isinstance(value, list):
        for inner in value:
            paths |= key_paths(inner, prefix + "[]")
    return paths


def _finite(number) -> bool:
    return isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number)


def violations(payload: dict, requested_speakers: int | None = None) -> list[str]:
    """新聞服務存檔之前會檢查的那些條件（brief_pi 的 check_speaker_result），回傳違反了哪幾條。"""
    problems: list[str] = []
    if payload.get("speaker_mode") != "diarize":
        problems.append("speaker_mode 不是 diarize")
    if payload.get("brief") is not None:
        problems.append("brief 不是 null")
    report = payload.get("diarization")
    if not isinstance(report, dict):
        problems.append("diarization 不是物件")
        report = {}
    if report.get("status") not in ("ok", "warn"):
        problems.append("diarization.status 不是 ok 或 warn")
    if not (isinstance(report.get("found_speakers"), int) and report["found_speakers"] >= 1):
        problems.append("diarization.found_speakers 小於 1")
    if report.get("requested_speakers") != requested_speakers:
        problems.append("diarization.requested_speakers 與要求的人數不同")
    speakers = payload.get("speakers")
    if not isinstance(speakers, list) or not 1 <= len(speakers) <= 12:
        problems.append("speakers 不是 1 到 12 個")
        speakers = []
    ids = [s.get("id") for s in speakers]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and SPEAKER_ID.match(i) for i in ids):
        problems.append("speakers 的 id 重複或格式不對")
    if any(s.get("kind") not in ("participant", "minor") for s in speakers):
        problems.append("speakers 有不認得的 kind")
    participants = {s["id"] for s in speakers if s.get("kind") == "participant"}
    if not participants:
        problems.append("沒有任何主要發言者")
    listed = {s["id"] for s in speakers}
    slides = payload.get("slides")
    if not isinstance(slides, list) or not slides:
        problems.append("沒有任何投影片")
        slides = []
    for slide in slides:
        points = slide.get("points")
        if not isinstance(points, list) or not points:
            problems.append(f"第 {slide.get('index')} 頁沒有任何重點的出處")
            continue
        for point in points:
            where = f"第 {slide.get('index')} 頁"
            if point.get("speaker") not in participants:
                problems.append(f"{where}的重點歸給了不是主要發言者的 {point.get('speaker')}")
            if not (isinstance(point.get("turn"), int) and point["turn"] >= 0):
                problems.append(f"{where}的 turn 不是非負整數")
            if not (_finite(point.get("start")) and point["start"] >= 0):
                problems.append(f"{where}的 start 不是有限的非負數")
            text = point.get("text")
            if not (isinstance(text, str) and 1 <= len(text) <= 120) or "[[" in text:
                problems.append(f"{where}的重點文字長度不對，或含有代號")
            evidence = point.get("evidence")
            if not isinstance(evidence, str) or len(evidence) > 120:
                problems.append(f"{where}的 evidence 太長")
        # 條列與詳述只能出現有列在 speakers 的代號
        for text in [*slide.get("bullets", []), slide.get("detail", "")]:
            for code in CODE.findall(text):
                if code not in listed:
                    problems.append(f"第 {slide.get('index')} 頁的文字出現沒有列出的代號 {code}")
    transcript = payload.get("transcript_text", "")
    if not transcript or not all(CLOCK_LINE.match(line) for line in transcript.split("\n")):
        problems.append("transcript_text 有不是「mm:ss [[Sn]]：文字」的行")
    return problems
