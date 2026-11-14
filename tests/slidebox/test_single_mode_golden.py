"""單人路徑的黃金測試：把今天（origin/master）的行為一個字一個字釘住。

議會（立法院、臺中、新北）與 `--speakers 1` 走的都是這條路徑。多人模式
（speaker_mode="diarize"）是另一條路徑，加進來之後這裡的值不能有任何變動。

這個檔案在 origin/master 的乾淨 checkout 上也必須全過：它只用 master 就有的名稱，
期望值也是在 master 上跑出來的。看到這裡的測試失敗，代表單人路徑的行為被改了——
先確認那是不是你要的，別直接改期望值。
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from slidebox.domain.entities import Brief, Cue, Settings, Slide, Transcript
from slidebox.infrastructure import ollama_summarizer as summarizer
from slidebox.infrastructure.checkpoint_store import JsonCheckpointStore
from slidebox.usecases.build_deck import BuildDeckUseCase
from slidebox.usecases.chapters import compress_cues, full_transcript

from .fakes import (
    FakeAudioGateway,
    FakeBriefWriter,
    FakeCheckpoints,
    FakeFrameExtractor,
    FakeRenderer,
    FakeSectionGateway,
    FakeSubtitleGateway,
    FakeSummarizer,
    FakeTranscriber,
    make_slides,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- 提示詞：單人摘要的每一段文字都不能動 ---


def prompt_digests() -> dict[str, str]:
    slide = Slide(1, "標題", ("我們要求部會回覆",), 5.0, detail="我們認為應該改善")
    texts = {
        "_SYSTEM": summarizer._SYSTEM,
        "_DETAIL_FIELD": summarizer._DETAIL_FIELD,
        "_TAIL": summarizer._TAIL,
        "_REWRITE_SYSTEM": summarizer._REWRITE_SYSTEM,
        "_BRIEF_SYSTEM": summarizer._BRIEF_SYSTEM,
        "_system(False)": summarizer._system(False),
        "_system(True)": summarizer._system(True),
        "user_prompt": summarizer._build_user_prompt("[0] 內容", 600.0, 8, 15, "", False),
        "user_prompt_detailed_hint": summarizer._build_user_prompt(
            "[0] 內容", 600.0, 8, 15, "請修正", True),
        "slides_schema": json.dumps(summarizer.slides_schema(False), sort_keys=True),
        "slides_schema_detailed": json.dumps(summarizer.slides_schema(True), sort_keys=True),
        "brief_schema": json.dumps(summarizer.brief_schema(), sort_keys=True),
        "rewrite_schema": json.dumps(summarizer.rewrite_schema(), sort_keys=True),
        "rewrite_prompt": summarizer._build_rewrite_prompt(slide, "第 1 頁用了第一人稱"),
        "brief_prompt": summarizer._build_brief_prompt("## 標題\n- 重點", "請修正"),
    }
    return {name: _sha(text) for name, text in texts.items()}


# 以下的值是在 origin/master（42afb0e）上跑出來的
EXPECTED_PROMPT_DIGESTS: dict[str, str] = {
    "_SYSTEM": "50d269237bed109aff2d6f9f0ef32632315f1a2405e48fcc699deda103ad8a17",
    "_DETAIL_FIELD": "55540632a00bd6b4af42f502ec63730f0aec56607641817350aac70f3bf9b1b4",
    "_TAIL": "38cb44dd5eb4c2a375d6671b0f86f366a5821a4a57e554ac721ec6f97a682a3c",
    "_REWRITE_SYSTEM": "d8adc7e0a1aa1775949b864e8a895e4faa48f4d636d319d7b460a9faabe8a152",
    "_BRIEF_SYSTEM": "4486d1fbc954898e4c368c222e6e9a031060ab976af5dd84eb6a26634da5201e",
    "_system(False)": "f9da174c47e475e8c8cb11fe8e449bd1003a1c402c42b222bca25dec2825e969",
    "_system(True)": "0a674a6a1734281235ceb0183fd8a1406aa80ec551f6a487972b73e2e7c30bc9",
    "user_prompt": "92853bed311f8aab4648e12c4160b4ed8aa25ea3376f6e248b92679747d24137",
    "user_prompt_detailed_hint": "baefee261efa059e0bc1c57e65c08e169c9277836cad1b4142289599f461de44",
    "slides_schema": "5382d90f40d5ab8fe17bb20d41a2bf9f7782db2c1b2e9ded7a1babf7a1dd7183",
    "slides_schema_detailed": "ad0f20946aa9e56e01374c4dcc3b9892cdd9e69f5267f80074add12fab3808e2",
    "brief_schema": "fbcdb5abf0e5465ac1054233d66bd76129ddc724d538ab67679a735c63800929",
    "rewrite_schema": "72f962eb8efbcad6e63190119c8502e6fb404f128ba7d8d96fec439e2cb2094c",
    "rewrite_prompt": "ac937ff86c05043d2d19efc1934f8054ac5c9035cd37b001a8cd10c24b722e70",
    "brief_prompt": "34b37a2a8c967ff99d0947b0966a2167fa88a8331720559110931f70c6b82660",
}


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPT_DIGESTS))
def test_single_mode_prompts_are_byte_identical_to_master(name):
    assert prompt_digests()[name] == EXPECTED_PROMPT_DIGESTS[name], f"{name} 的內容變了"


def test_every_single_mode_prompt_is_pinned():
    assert set(prompt_digests()) == set(EXPECTED_PROMPT_DIGESTS)


# --- 字幕壓縮與完整逐字稿：給模型與給讀者的文字 ---

CUES = (
    Cue(0.0, 3.0, "今天我們來談少子化"),
    Cue(2.0, 5.0, "今天我們來談少子化的補助"),   # 滾動字幕：前一句的延伸
    Cue(5.0, 7.0, "對。"),
    Cue(7.5, 9.0, "對。"),                      # 短句重複：本身就是內容
    Cue(16.0, 19.0, "預算一共三千億"),
    Cue(20.0, 22.0, "預算一共三千億"),           # 逐字重複
    Cue(31.0, 33.0, "好的謝謝"),
    Cue(3725.0, 3730.0, "一小時以後的一句話"),    # 分鐘數超過 60 不換成 hh:mm:ss
)


def compression_outputs() -> dict[str, str]:
    return {
        "compress_auto": compress_cues(CUES, 1000),
        "compress_manual": compress_cues(CUES, 1000, is_automatic=False),
        "compress_tight_budget": compress_cues(CUES, 40),
        "full_auto": full_transcript(CUES),
        "full_manual": full_transcript(CUES, False),
    }


EXPECTED_COMPRESSION: dict[str, str] = {
    "compress_auto": "[0] 今天我們來談少子化 的補助 對。 對。\n[16] 預算一共三千億\n[31] 好的謝謝\n[3725] 一小時以後的一句話",
    "compress_manual": ("[0] 今天我們來談少子化 今天我們來談少子化的補助 對。 對。\n"
                        "[16] 預算一共三千億 預算一共三千億\n[31] 好的謝謝\n[3725] 一小時以後的一句話"),
    "compress_tight_budget": "[0] 今天我們來談少子\n[16] 預算一\n[31] 好\n[3725] 一小時以",
    "full_auto": "00:00 今天我們來談少子化 的補助 對。 對。\n00:16 預算一共三千億\n00:31 好的謝謝\n62:05 一小時以後的一句話",
    "full_manual": ("00:00 今天我們來談少子化 今天我們來談少子化的補助 對。 對。\n"
                    "00:16 預算一共三千億 預算一共三千億\n00:31 好的謝謝\n62:05 一小時以後的一句話"),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_COMPRESSION))
def test_compress_cues_and_full_transcript_are_unchanged(name):
    assert compression_outputs()[name] == EXPECTED_COMPRESSION[name]


def test_every_compression_output_is_pinned():
    assert set(compression_outputs()) == set(EXPECTED_COMPRESSION)


# --- 存檔的指紋：單人模式的指紋一個字都不能變，否則升級後會重做所有人的語音辨識 ---


class RecordingCheckpoints(FakeCheckpoints):
    """記下每次讀寫用的 (動作, 階段, 指紋)。"""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []

    def _load(self, output_dir, url, stage, fingerprint):
        self.calls.append(("load", stage, fingerprint))
        return super()._load(output_dir, url, stage, fingerprint)

    def _save(self, output_dir, url, stage, fingerprint, value):
        self.calls.append(("save", stage, fingerprint))
        super()._save(output_dir, url, stage, fingerprint, value)


def checkpoint_runs() -> dict[str, object]:
    settings = Settings(output_dir="OUT", min_slides=2, max_slides=5, detailed=True)

    subtitles_ck = RecordingCheckpoints()
    summ = FakeSummarizer([make_slides(3, detail=True)])
    BuildDeckUseCase(
        FakeSubtitleGateway(), summ, FakeSectionGateway(), FakeFrameExtractor(), FakeRenderer(),
        brief_writer=FakeBriefWriter([Brief("講者要求一個月內提出改善計畫")]),
        checkpoints=subtitles_ck,
    ).execute("URL", settings)

    speech_ck = RecordingCheckpoints()
    speech_summ = FakeSummarizer([make_slides(3, detail=True)])
    BuildDeckUseCase(
        FakeSubtitleGateway(fail=True), speech_summ, FakeSectionGateway(), FakeFrameExtractor(),
        FakeRenderer(), audio=FakeAudioGateway(), transcriber=FakeTranscriber(),
        brief_writer=FakeBriefWriter([Brief("講者要求一個月內提出改善計畫")]),
        checkpoints=speech_ck,
    ).execute("URL", settings, speech_hint="示範頻道 少子化對談")

    return {
        "subtitles_calls": [list(c) for c in subtitles_ck.calls],
        "subtitles_compressed": summ.compressed,
        "speech_calls": [list(c) for c in speech_ck.calls],
        "speech_compressed": speech_summ.compressed,
    }


EXPECTED_CHECKPOINT_RUNS: dict[str, object] = {
    "subtitles_calls": [
        ["load", "transcript", "691582521577211a"], ["save", "transcript", "691582521577211a"],
        ["load", "slides", "331f8e79e4031469"], ["save", "slides", "331f8e79e4031469"],
        ["load", "brief", "3040b197a672795c"], ["save", "brief", "3040b197a672795c"],
    ],
    "subtitles_compressed": ["[0] 第一句\n[30] 第二句"],
    "speech_calls": [
        ["load", "transcript", "df811799a9fadc62"], ["save", "transcript", "df811799a9fadc62"],
        ["load", "slides", "c96d3bed47781ebc"], ["save", "slides", "c96d3bed47781ebc"],
        ["load", "brief", "d4ce0be3bb52a3ee"], ["save", "brief", "d4ce0be3bb52a3ee"],
    ],
    "speech_compressed": ["[0] こんにちは\n[30] よろしくお願いします"],
}


@pytest.mark.parametrize("name", sorted(EXPECTED_CHECKPOINT_RUNS))
def test_single_mode_checkpoint_fingerprints_are_unchanged(name):
    assert checkpoint_runs()[name] == EXPECTED_CHECKPOINT_RUNS[name]


def test_every_checkpoint_run_value_is_pinned():
    assert set(checkpoint_runs()) == set(EXPECTED_CHECKPOINT_RUNS)


# --- 存檔的檔案：階段與內容 ---

URL = "https://www.youtube.com/watch?v=f4r2yWk4LV0"
TRANSCRIPT = Transcript("vid1", "標題", 600.0,
                        (Cue(0.0, 3.0, "第一句"), Cue(30.0, 33.5, "第二句")),
                        "zh-TW", is_automatic=True, source_note="來源註記")
SLIDES = (Slide(1, "第一段", ("講者指出", "第二點"), 12.5, detail="完整敘述"),
          Slide(2, "第二段", ("講者要求",), 40.0))
BRIEF = Brief("一句話")


def checkpoint_files(root: str) -> dict[str, object]:
    store = JsonCheckpointStore()
    store.save_transcript(root, URL, "fp1", TRANSCRIPT, "由語音辨識產生")
    store.save_slides(root, URL, "fp2", SLIDES, "第 1 頁仍有第一人稱")
    store.save_brief(root, URL, "fp3", BRIEF)
    files: dict[str, object] = {}
    for current, _, names in os.walk(root):
        for name in sorted(names):
            with open(os.path.join(current, name), encoding="utf-8") as f:
                files[name] = json.load(f)
    return files


EXPECTED_CHECKPOINT_FILES: dict[str, object] = {
    "brief.json": {
        "url": URL, "fingerprint": "fp3",
        "data": {"one_liner": "一句話", "key_numbers": [], "asks": []},
    },
    "slides.json": {
        "url": URL, "fingerprint": "fp2",
        "data": {
            "slides": [
                {"index": 1, "title": "第一段", "bullets": ["講者指出", "第二點"],
                 "timestamp": 12.5, "detail": "完整敘述"},
                {"index": 2, "title": "第二段", "bullets": ["講者要求"],
                 "timestamp": 40.0, "detail": ""},
            ],
            "note": "第 1 頁仍有第一人稱",
        },
    },
    "transcript.json": {
        "url": URL, "fingerprint": "fp1",
        "data": {
            "transcript": {
                "video_id": "vid1", "title": "標題", "duration": 600.0,
                "cues": [{"start": 0.0, "end": 3.0, "text": "第一句"},
                         {"start": 30.0, "end": 33.5, "text": "第二句"}],
                "language": "zh-TW", "is_automatic": True, "source_note": "來源註記",
            },
            "note": "由語音辨識產生",
        },
    },
}


def test_json_checkpoint_files_keep_the_single_mode_layout(tmp_path):
    assert checkpoint_files(str(tmp_path)) == EXPECTED_CHECKPOINT_FILES
