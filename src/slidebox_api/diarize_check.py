"""在 GPU 主機上檢查說話者區分的好壞：只做「下載音訊 → 說話者區分 → 語音辨識 → 對齊」，
不呼叫語言模型、不寫任何成品、不碰新聞服務。

    python -m slidebox_api.diarize_check --sample
    python -m slidebox_api.diarize_check URL [--start 秒] [--end 秒] [--speakers N]

這是在相信多人文章之前先拿來聽的工具：它印出每一段話歸給誰、每個標籤各占多少時間、
每個標籤最長的三段話（附 &t= 連結，點開聽聽是不是同一個人），以及平均分布的幾個抽查位置。
正式工作用的是同一組對齊與分段的程式（usecases/speakers.py），所以這裡看到的標籤，就是
文章裡會出現的標籤。

--sample 用一段公開的四人中文對話（k2-fsa/sherpa-onnx 的範例音檔）當冒煙測試：
確認 token 有效、模型載得起來、整條流程跑得通，並量出耗時與記憶體；預期說話者區分器找到 4 個聲音。
結束碼 0 **不代表顯示卡能用**：用哪個裝置跑的，報告的「說話者區分」那一行會寫（cuda 或 cpu）；
cpu 的時候（沒有顯示卡、驅動對不上而退回 CPU，或根本沒設 GPU）只證明流程，要驗證顯示卡就在 GPU 主機上
設 SLIDEBOX_DIARIZATION_DEVICE=cuda 再跑一次：指定 cuda 時沒有可用的顯示卡會直接失敗，不會悄悄退回 CPU。
說話者區分（pyannote.audio、PyTorch）在另一個行程、另一個 Python 環境裡跑（見 infrastructure/pyannote_worker.py），
所以它的記憶體峰值由區分器回報，不是本行程的。
--start / --end 只檢查影片的一段（秒，或 分:秒、時:分:秒），省得為了調校等整支影片。

音訊只透過既有的 yt-dlp 音訊 gateway 下載，放在暫存資料夾，結束（包括失敗與 Ctrl-C）就刪掉。
結束碼：0 正常；1 跑完了，但正式工作會因為歸屬不可靠而拒絕這個結果（或樣本與預期不符）；
2 參數不對；3 沒辦法跑（說話者區分不能用、下載或語音辨識失敗…）；130 被中斷。
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from slidebox.domain.entities import (
    AudioClip,
    DiarizationReport,
    DiarizerOutput,
    SpeakerInfo,
    Turn,
    Word,
)
from slidebox.domain.errors import (
    AttributionUnreliable,
    DiarizationUnavailable,
    NoSubtitlesAvailable,
    OperationCancelled,
    SpeechRecognitionFailed,
)
from slidebox.domain.ports import AudioGateway, SpeechDiarizer, WordTranscriber
from slidebox.usecases.speakers import (
    WARN_COUNT_MISMATCH,
    WARN_HIGH_UNASSIGNED,
    WARN_MANY_MINOR,
    WARN_SINGLE_FOUND,
    assign_speakers,
    build_turns,
    clock,
    diarization_report,
    normalize_turns,
    summarize_speakers,
)

SAMPLE_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
              "speaker-segmentation-models/0-four-speakers-zh.wav")
SAMPLE_VOICES = 4
# 樣本的標題與代號寫死：樣本網址被 GitHub 轉到一串很長的簽章網址，yt-dlp 的一般解析器把它當影片 id 與標題，
# 照印會是一大段 sig=…&jwt=…
SAMPLE_TITLE = "內建樣本（四人中文對話）"
SAMPLE_ID = "0-four-speakers-zh"

EXIT_OK = 0
EXIT_NOT_RELIABLE = 1
EXIT_USAGE = 2
EXIT_FAILED = 3
EXIT_INTERRUPTED = 130

MAX_TURN_LINES = 400
SPOT_CHECKS = 10
TEXT_CHARS = 60
PROGRESS_EVERY_SECONDS = 2.0
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"})

_WARNING_TEXT = {
    WARN_COUNT_MISMATCH: "找到的主要發言者人數和指定的不同",
    WARN_HIGH_UNASSIGNED: "超過四分之一的說話時間無法判斷是誰說的",
    WARN_MANY_MINOR: "有四個以上只出現一下的聲音",
    WARN_SINGLE_FOUND: "自動判斷只找到 1 位主要發言者（可能把不同的人併成一個）",
}

# 用 CPU 跑的時候，報告要自己說清楚這次證明了什麼、沒證明什麼（結束碼 0 很容易被讀成「GPU 沒問題」）
_CPU_NOTICE = ("  注意：這次說話者區分是用 CPU 跑的。結束碼 0 只表示 token 有效、模型載得起來、流程跑得通，"
               "不表示顯示卡能用，耗時也不是顯示卡的耗時；要驗證顯示卡，設 SLIDEBOX_DIARIZATION_DEVICE=cuda 再跑一次"
               "（指定 cuda 時沒有可用的顯示卡會直接失敗，不會悄悄退回 CPU）。")


# --- 外面來的文字：標題、網址、逐字稿、別人的錯誤訊息，印到終端機之前一律先清過 ---

# 終端機的控制序列：ESC 開頭的（CSI、OSC、DCS…）與 8 位元的 CSI／OSC。要整段拿掉，不是只拿掉 ESC 字元：
# 否則參數變成看得到的亂碼，OSC 的內容（例如改視窗標題的字串）留在畫面上。
# 沒有結尾的序列也整段拿掉（一直吃到字串結束），不給它「等下一行來接」的機會。
_TERMINAL_SEQUENCE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"           # OSC：到 BEL 或 ST 為止
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"               # DCS／SOS／PM／APC：到 ST 為止
    r"|\x1b\[[0-?]*[ -/]*[@-~]?"                    # CSI
    r"|\x1b[ -/]*[0-~]?"                            # 其他 ESC 序列（ESC c、ESC ( B…）
    r"|\x9b[0-?]*[ -/]*[@-~]?"                      # 8 位元的 CSI
    r"|\x9d[^\x07\x9c]*(?:\x07|\x9c)?"              # 8 位元的 OSC
)
# 雙向文字的控制字元：可以把畫面上的字重新排序，讓人看到和實際不同的內容
_BIDI_CONTROLS = frozenset(chr(code) for code in (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def _strip_controls(text: str) -> str:
    """拿掉控制序列、雙向控制字元與其他控制字元（BEL、退格、DEL、C1…），不留空白；
    Tab、換行、行／段分隔符當成空白（標題不能自己多開一行來偽造報告）。"""
    text = _TERMINAL_SEQUENCE.sub("", text)
    out = []
    for c in text:
        category = unicodedata.category(c)
        if c in _BIDI_CONTROLS:
            continue
        if category in ("Zl", "Zp") or (category == "Cc" and c.isspace()):
            out.append(" ")
        elif category != "Cc":
            out.append(c)
    return "".join(out)


def plain(text: object) -> str:
    """可以安全印在報告同一行裡的文字：控制序列與控制字元拿掉，換行與連續空白壓成一個空白。"""
    return " ".join(_strip_controls(str(text)).split())


def _sweep(report: str) -> str:
    """整份報告印出前的最後一道：一行一行清，換行是報告自己的版面、留著。

    一行一行清是刻意的：沒有結尾的序列（OSC 一路吃到 BEL）只會吃到這一行的結尾，不會把後面的報告也吃掉。
    """
    return "\n".join(_strip_controls(line) for line in report.split("\n"))


class AudioCutFailed(Exception):
    """切出指定範圍的音訊失敗。"""


class BadRange(Exception):
    """--start／--end 和實際下載到的影片對不上（參數的語法在 parse_args 檢查，這裡是和影片長度比的結果）。"""


# --- 參數 ---


@dataclass(frozen=True)
class Options:
    url: str
    sample: bool = False
    start: float | None = None
    end: float | None = None
    speakers: int | None = None


def parse_time(text: str) -> float:
    """秒（90、90.5）、分:秒（1:30）或時:分:秒（1:02:03）；不合理的都丟 ValueError。"""
    pieces = text.strip().split(":")
    if not 1 <= len(pieces) <= 3:
        raise ValueError(f"看不懂的時間「{text}」")
    try:
        numbers = [float(piece) for piece in pieces]
    except ValueError:
        raise ValueError(f"看不懂的時間「{text}」") from None
    if any(not math.isfinite(n) or n < 0 for n in numbers):
        raise ValueError(f"時間不能是負的或無限大：「{text}」")
    if len(numbers) > 1 and any(n >= 60 for n in numbers[1:]):
        raise ValueError(f"分與秒要小於 60：「{text}」")
    total = 0.0
    for n in numbers:
        total = total * 60 + n
    return total


def _time_arg(text: str) -> float:
    try:
        return parse_time(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _count_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 2:
        raise argparse.ArgumentTypeError("要是 2 以上的整數（不指定就自動判斷人數；一個人就是單人模式，不需要這個工具）")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m slidebox_api.diarize_check",
        description="檢查說話者區分：印出每一段話歸給誰、每個標籤的統計與可以點開來聽的連結。不呼叫語言模型。")
    parser.add_argument("url", nargs="?", help="影片網址（YouTube）")
    parser.add_argument("--sample", action="store_true", help="用內建的四人中文對話樣本做冒煙測試")
    parser.add_argument("--start", type=_time_arg, metavar="時間", help="只檢查從這裡開始的一段（秒或 分:秒）")
    parser.add_argument("--end", type=_time_arg, metavar="時間", help="只檢查到這裡為止的一段（秒或 分:秒）")
    parser.add_argument("--speakers", type=_count_arg, metavar="N", help="指定人數（2 以上）；不指定就自動判斷")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """參數不對就印說明並以結束碼 2 離開（argparse 的 SystemExit）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.sample == bool(args.url):
        parser.error("要二選一：--sample，或是一個影片網址")
    if args.sample and (args.start is not None or args.end is not None or args.speakers is not None):
        parser.error("--sample 用固定的樣本，不能再指定 --start、--end 或 --speakers")
    if args.url and urlparse(args.url).scheme not in ("http", "https"):
        parser.error("網址必須是 http 或 https")
    if args.end is not None and args.end <= (args.start or 0.0):
        parser.error("--end 要大於 --start（沒指定 --start 就是大於 0）")
    return Options(url=SAMPLE_URL if args.sample else args.url, sample=args.sample,
                   start=args.start, end=args.end, speakers=args.speakers)


# --- 零件 ---


def peak_memory_bytes() -> int | None:
    """本行程（語音辨識在這裡）到目前為止的記憶體峰值，含載入的模型；這個平台量不到就是 None。

    說話者區分在另一個行程裡跑，它的記憶體峰值（含 PyTorch 在顯示卡上的）由區分器回報
    （worker_peak_bytes、gpu_peak_bytes），本行程這裡根本沒有 torch 可以問。
    """
    try:
        import resource
    except ImportError:                                  # Windows 沒有 resource
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def ffmpeg_cut(src: str, dest: str, start: float, length: float | None, runner=subprocess.run,
               ffmpeg_exe: str | None = None) -> None:
    """把 src 從 start 秒起的 length 秒（None 是切到結尾）切成單聲道 16 kHz 的 wav（說話者區分與 Whisper 都吃這個）。"""
    from slidebox.infrastructure.ffmpeg import ffmpeg_error, get_ffmpeg_exe

    span = [] if length is None else ["-t", f"{length:.3f}"]
    result = runner(
        [ffmpeg_exe or get_ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{start:.3f}", *span,
         "-i", src, "-vn", "-ac", "1", "-ar", "16000", dest],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not os.path.exists(dest):
        raise AudioCutFailed(f"切出指定範圍的音訊失敗：{ffmpeg_error(result.stderr) or '沒有輸出檔'}")


@dataclass
class Parts:
    audio: AudioGateway
    diarizer: SpeechDiarizer
    words: WordTranscriber
    whisper_model: str = ""
    cut_audio: Callable[[str, str, float, float | None], None] = ffmpeg_cut
    clock: Callable[[], float] = time.monotonic
    peak_memory: Callable[[], int | None] = peak_memory_bytes


def default_parts() -> Parts:
    """與正式服務同一組實作（組裝見 slidebox/composition.py）；建構時不載入任何模型。"""
    from slidebox.composition import build_diarizer
    from slidebox.domain.entities import Settings
    from slidebox.infrastructure.whisper_transcriber import FasterWhisperTranscriber
    from slidebox.infrastructure.ytdlp_audio import YtDlpAudioGateway

    model = Settings(output_dir=tempfile.gettempdir()).whisper_model       # 只為了取得預設的 Whisper 模型名稱
    return Parts(audio=YtDlpAudioGateway(), diarizer=build_diarizer(),
                 words=FasterWhisperTranscriber(model), whisper_model=model)


class _Progress:
    """把各階段的進度寫到 err：狀態有變、而且離上一行至少兩秒才印，免得洗版。"""

    def __init__(self, err, now: Callable[[], float]):
        self._err, self._now = err, now
        self._last_status = ""
        self._last_time: float | None = None

    def __call__(self, fraction: float | None, status: str) -> None:
        status = plain(status)
        moment = self._now()
        if status == self._last_status:
            return
        if self._last_time is not None and moment - self._last_time < PROGRESS_EVERY_SECONDS:
            return
        self._last_status, self._last_time = status, moment
        print(f"    {status}", file=self._err, flush=True)


# --- 聽：下載、切出範圍、說話者區分、語音辨識 ---


@dataclass(frozen=True)
class Heard:
    clip: AudioClip
    offset: float                  # 檢查範圍的起點（秒）；沒指定範圍就是 0
    length: float                  # 檢查範圍的長度；影片長度不明又沒指定範圍時是 0
    diarized: DiarizerOutput
    words: tuple[Word, ...]
    language: str


def _listen(options: Options, parts: Parts, err, base_dir: str | None, timings: dict[str, float]) -> Heard:
    """音訊放在暫存資料夾，無論成功、失敗或被中斷，離開時都刪掉。"""
    workdir = tempfile.mkdtemp(prefix="diarize_check_", dir=base_dir)
    audio_dir = os.path.join(workdir, "audio")
    progress = _Progress(err, parts.clock)

    def cancelled() -> bool:
        return False                                     # Ctrl-C 以 KeyboardInterrupt 中斷，不走旗標

    def stage(name: str, banner: str, action):
        print(f"[{name}] {banner}", file=err, flush=True)
        moment = parts.clock()
        result = action()
        timings[name] = parts.clock() - moment
        return result

    try:
        clip = stage("下載", "下載音訊…", lambda: parts.audio.download_audio(
            options.url, audio_dir, progress, cancelled))
        audio_path, length, offset = _scope(options, clip, parts, workdir, err, stage)
        diarized = stage("說話者區分", "說話者區分（第一次要載入模型，會比較久）…", lambda: parts.diarizer.diarize(
            audio_path, length, options.speakers, progress, cancelled))
        words, language = stage("語音辨識", "語音辨識…", lambda: parts.words.transcribe_words(
            audio_path, length, progress, cancelled, None))
        return Heard(clip, offset, length, diarized, tuple(words), language)
    finally:
        parts.audio.cleanup(audio_dir)
        shutil.rmtree(workdir, ignore_errors=True)


def _scope(options: Options, clip: AudioClip, parts: Parts, workdir: str, err, stage) -> tuple[str, float, float]:
    """要檢查的音訊檔、長度、起點。沒指定範圍就是整支；指定了就切出那一段。"""
    if options.start is None and options.end is None:
        return clip.path, clip.duration, 0.0
    start = options.start or 0.0
    if clip.duration and start >= clip.duration:
        raise BadRange(f"--start（{clock(start)}）已經超過影片長度（{clock(clip.duration)}）")
    end = options.end if options.end is not None else (clip.duration or None)    # None：長度不明，切到結尾
    if end is not None and clip.duration and end > clip.duration:
        print(f"--end（{clock(end)}）超過影片長度，改成影片結尾（{clock(clip.duration)}）", file=err)
        end = clip.duration
    length = None if end is None else end - start
    piece = os.path.join(workdir, "slice.wav")
    stage("切出", f"切出 {clock(start)}–{'結尾' if end is None else clock(end)}…",
          lambda: parts.cut_audio(clip.path, piece, start, length))
    return piece, length or 0.0, start


# --- 輸出 ---


def _short(text: str, limit: int = TEXT_CHARS) -> str:
    text = plain(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _gib(size: int) -> str:
    return f"{size / 2 ** 30:.1f} GiB"


def spot_indices(count: int, wanted: int = SPOT_CHECKS) -> list[int]:
    """平均分布在 0..count-1 的 wanted 個位置（包含頭尾）；不夠就全部。"""
    if count <= wanted:
        return list(range(count))
    return [round(i * (count - 1) / (wanted - 1)) for i in range(wanted)]


class _Where:
    """影片裡的某個時間點怎麼寫：mm:ss，YouTube 的影片再附 &t= 連結。offset 是檢查範圍的起點。"""

    def __init__(self, url: str, video_id: str, offset: float):
        on_youtube = (urlparse(url).hostname or "").lower() in _YOUTUBE_HOSTS
        self._link = f"https://www.youtube.com/watch?v={plain(video_id)}" if on_youtube and video_id else ""
        self._offset = offset

    def at(self, seconds: float) -> str:
        return clock(seconds + self._offset)

    def full(self, seconds: float) -> str:
        if not self._link:
            return self.at(seconds)
        return f"{self.at(seconds)}  {self._link}&t={int(seconds + self._offset)}s"


def _voice_lines(output: DiarizerOutput, where: _Where) -> tuple[list[str], int]:
    voices: dict[str, list] = {}
    for t in normalize_turns(output.turns):
        voices.setdefault(t.speaker, []).append(t)
    lines = [f"  {sid}  {len(ts)} 段  {sum(t.end - t.start for t in ts):.1f} 秒  "
             f"{where.at(ts[0].start)}–{where.at(max(t.end for t in ts))}" for sid, ts in voices.items()]
    return lines, len(voices)


def _turn_lines(turns: Sequence[Turn], labels: dict[str, str], where: _Where) -> list[str]:
    shown = turns[:MAX_TURN_LINES]
    lines = [f"  {where.at(t.start)}–{where.at(t.end)}  {t.speaker} {labels.get(t.speaker, '無法判斷')}  {_short(t.text)}"
             for t in shown]
    if len(turns) > len(shown):
        lines.append(f"  …共 {len(turns)} 段，只列前 {len(shown)} 段")
    return lines


def _speaker_lines(speakers: Sequence[SpeakerInfo], where: _Where) -> list[str]:
    kinds = {"participant": "主要發言者", "minor": "只出現一下的聲音"}
    return [f"  {s.id} {s.label}（{kinds.get(s.kind, s.kind)}）  {s.seconds:.1f} 秒  {s.turns} 段  "
            f"{where.at(s.first_seen)}–{where.at(s.last_seen)}" for s in speakers]


def _sheet_lines(speakers: Sequence[SpeakerInfo], where: _Where) -> list[str]:
    lines: list[str] = []
    for s in speakers:
        lines.append(f"  {s.label}（{s.id}）")
        lines.extend(f"    {where.full(sample.start)}  {_short(sample.text)}" for sample in s.samples)
    return lines


def _spot_lines(turns: Sequence[Turn], labels: dict[str, str], where: _Where) -> list[str]:
    return [f"  {where.full(turns[i].start)}  {turns[i].speaker} {labels.get(turns[i].speaker, '無法判斷')}  "
            f"{_short(turns[i].text)}" for i in spot_indices(len(turns))]


def _quality_lines(report: DiarizationReport | None, problem: str) -> list[str]:
    lines: list[str] = []
    if report is not None:
        lines.append(f"  狀態：{report.status}；主要發言者 {report.found_speakers} 位，"
                     f"只出現一下的聲音 {report.minor_speakers} 個")
        lines.append(f"  無法判斷是誰說的：{report.unassigned_ratio:.1%}；同時有人說話：{report.overlap_ratio:.1%}")
        lines.extend(f"  警告：{_WARNING_TEXT.get(code, code)}" for code in report.warnings)
    if problem:
        lines.append(f"  正式工作會在這裡失敗（AttributionUnreliable）：{problem}")
    return lines


def _describe(options: Options, parts: Parts, heard: Heard, timings: dict[str, float],
              total_seconds: float) -> tuple[str, int]:
    """把聽到的結果整理成給人看的報告，並決定結束碼。"""
    clip, where = heard.clip, _Where(options.url, heard.clip.video_id, heard.offset)
    title, video_id = (SAMPLE_TITLE, SAMPLE_ID) if options.sample else (clip.title, clip.video_id)
    # 標題、網址這些外面來的文字，一律先過 plain()：上傳者可以在標題裡放終端機的控制序列
    lines = ["=== 說話者區分檢查 ===", f"來源：{plain(options.url)}", f"影片：{plain(title)}（{plain(video_id)}）"]
    if heard.offset or heard.length != clip.duration:
        lines.append(f"範圍：{clock(heard.offset)}–{clock(heard.offset + heard.length)}（{heard.length:.0f} 秒）")
    else:
        lines.append("範圍：整支" + (f"（{heard.length:.0f} 秒）" if heard.length else ""))
    device = plain(getattr(parts.diarizer, "device", ""))
    lines.append(f"說話者區分：{parts.diarizer.engine} {parts.diarizer.version}（{parts.diarizer.model}"
                 + (f"，{device}" if device else "") + "）")
    if device == "cpu":
        lines.append(_CPU_NOTICE)
    lines.append(f"語音辨識：{plain(parts.whisper_model) or '預設模型'}（語言 {plain(heard.language)}）")
    lines.append(f"指定人數：{options.speakers if options.speakers else '自動判斷'}")
    voice_lines, voices = _voice_lines(heard.diarized, where)
    lines += ["", f"--- 說話者區分器找到 {voices} 個聲音 ---", *voice_lines]

    if not heard.words:
        lines += ["", "語音辨識沒有辨識出任何字（正式工作會以 SpeechRecognitionFailed 失敗）；沒有文字可以對齊。"]
        return "\n".join(lines), EXIT_FAILED

    moment = parts.clock()
    turns = build_turns(assign_speakers(heard.words, normalize_turns(heard.diarized.turns)))
    speakers = summarize_speakers(turns)
    report, problem, code = None, "", EXIT_OK
    try:
        report = diarization_report(
            turns, speakers, engine=parts.diarizer.engine, version=parts.diarizer.version,
            model=parts.diarizer.model, fingerprint=parts.diarizer.fingerprint,
            requested_speakers=options.speakers, overlap_seconds=heard.diarized.overlap_seconds)
    except AttributionUnreliable as e:
        problem, code = str(e), EXIT_NOT_RELIABLE
    timings["對齊"] = parts.clock() - moment

    labels = {s.id: s.label for s in speakers}
    lines += ["", f"--- 逐段（共 {len(turns)} 段；起訖、說話者、文字）---", *_turn_lines(turns, labels, where)]
    lines += ["", "--- 每位說話者 ---", *_speaker_lines(speakers, where)]
    lines += ["", "--- 說話者清單：點開連結聽聲音，確認每個標籤是同一個人（每位最長的三段話）---",
              *_sheet_lines(speakers, where)]
    lines += ["", f"--- 抽查（平均分布的 {min(SPOT_CHECKS, len(turns))} 個位置）---", *_spot_lines(turns, labels, where)]
    lines += ["", "--- 品質 ---", *_quality_lines(report, problem)]
    if options.sample:
        matched = voices == SAMPLE_VOICES
        lines.append(f"  樣本預期 {SAMPLE_VOICES} 個聲音，說話者區分器找到 {voices} 個："
                     + ("符合預期。" if matched else "與預期不符，請看上面的結果。"))
        if not matched:
            code = EXIT_NOT_RELIABLE

    steps = "；".join(f"{name} {seconds:.1f} 秒" for name, seconds in timings.items())
    # 說話者區分在另一個行程跑（pyannote_worker）：它的峰值是區分器回報的，本行程裡沒有 torch 可以量
    peak = parts.peak_memory()
    worker, gpu = (getattr(parts.diarizer, "worker_peak_bytes", None), getattr(parts.diarizer, "gpu_peak_bytes", None))
    lines += ["", "--- 耗時與記憶體 ---", f"  {steps}；合計 {total_seconds:.1f} 秒",
              f"  本行程（語音辨識）記憶體峰值：{_gib(peak) if peak else '這個平台量不到'}（含載入的模型）",
              f"  說話者區分的工作行程記憶體峰值：{_gib(worker) if worker else '這個平台量不到'}（含載入的模型）"
              + (f"；PyTorch 顯示卡記憶體峰值：{_gib(gpu)}（不含 Whisper，請另外看 nvidia-smi）" if gpu else "")]
    return "\n".join(lines), code


# --- 進入點 ---


def run(options: Options, parts: Parts, out, err, base_dir: str | None = None) -> int:
    began = parts.clock()
    usable, reason = parts.diarizer.available()
    if not usable:                                       # 不行就在下載任何東西之前停下來
        print(f"說話者區分目前無法使用：{plain(reason) or '沒有說明'}", file=err)
        return EXIT_FAILED
    timings: dict[str, float] = {}
    try:
        heard = _listen(options, parts, err, base_dir, timings)
    except (KeyboardInterrupt, OperationCancelled):
        print("已中斷。", file=err)
        return EXIT_INTERRUPTED
    except NoSubtitlesAvailable as e:                    # 音訊 gateway 下載失敗時丟的就是這個
        print(f"下載音訊失敗：{plain(e)}", file=err)     # yt-dlp 的錯誤常整段貼著頁面上的文字
        return EXIT_FAILED
    except (DiarizationUnavailable, SpeechRecognitionFailed, AudioCutFailed, BadRange) as e:
        print(plain(e), file=err)
        return EXIT_FAILED
    report, code = _describe(options, parts, heard, timings, parts.clock() - began)
    print(_sweep(report), file=out)                      # 最後一道：除了報告自己的換行，沒有任何控制字元
    return code


def main(argv: Sequence[str] | None = None, parts: Parts | None = None, out=None, err=None,
         base_dir: str | None = None) -> int:
    try:
        options = parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    return run(options, parts or default_parts(), out or sys.stdout, err or sys.stderr, base_dir)


def _use_utf8() -> None:
    """Windows 的主控台預設不是 UTF-8；輸出是中文，亂碼就看不出哪段話歸給誰。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _use_utf8()
    sys.exit(main())
