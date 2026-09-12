"""立法院 IVOD 的資料來源：開放 API ly.govapi.tw。

立法院自己已經用 WhisperX 產好逐字稿（含時間戳），所以這條路不需要本機
語音辨識——直接把 API 的段落轉成 Cue 就好。
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Sequence

from ..domain.entities import Cue, Transcript
from ..domain.errors import NoSubtitlesAvailable, SubtitleDownloadFailed
from ..usecases.sources import ivod_id

_BASE = "https://ly.govapi.tw/v2/ivods"

# 逐字稿由立法院以 AI 產生，實測看得到錯字（「朝野黨壇協商」應為「黨團」），
# 台語發言更差。成品必須說出來源，否則會被當成官方紀錄。
_SOURCE_NOTE = "逐字稿由立法院 AI 自動產生，可能有辨識錯誤"


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _seconds(text: str) -> float:
    """把 "00:03:17" 轉成秒。格式不對時回 0，由呼叫端決定怎麼補。"""
    parts = (text or "").split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


class IvodClient:
    """取一筆 IVOD record，並快取最後一筆。

    字幕 gateway 要逐字稿、片段 gateway 要 video_url，兩者要的是同一筆資料。
    不共用就會對同一個 id 打兩次 API。只留最後一筆——app 一次只跑一個生成。
    """

    def __init__(self, fetch=_http_get, base: str = _BASE):
        self._fetch = fetch
        self._base = base
        self._cached_id: str | None = None
        self._cached: dict = {}

    def record(self, video_id: str) -> dict:
        if video_id == self._cached_id:
            return self._cached
        try:
            payload = json.loads(self._fetch(f"{self._base}/{video_id}"))
        except Exception as e:  # noqa: BLE001 網路或格式問題都是同一種結局
            raise SubtitleDownloadFailed(
                f"無法取得 IVOD {video_id} 的資料：{str(e)[:160]}"
            ) from e
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SubtitleDownloadFailed(f"IVOD {video_id} 的回應格式不符預期")
        self._cached_id, self._cached = video_id, data
        return data

    def video_url(self, video_id: str) -> str:
        url = self.record(video_id).get("video_url")
        if not isinstance(url, str) or not url:
            raise SubtitleDownloadFailed(f"IVOD {video_id} 沒有提供影片網址")
        return url


class IvodSubtitleGateway:
    def __init__(self, client: IvodClient | None = None):
        self._client = client or IvodClient()

    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        """langs 用不到：IVOD 只有一種語言的逐字稿，沒有挑軌這回事。"""
        video_id = ivod_id(url)
        if video_id is None:
            raise SubtitleDownloadFailed(f"這不是 IVOD 的播放網址：{url}")
        record = self._client.record(video_id)

        segments = ((record.get("transcript") or {}).get("whisperx")) or []
        cues = tuple(
            Cue(start=float(s.get("start") or 0.0),
                end=float(s.get("end") or 0.0),
                text=" ".join(str(s.get("text") or "").split()))
            for s in segments
            if isinstance(s, dict) and str(s.get("text") or "").strip()
        )
        if not cues:
            # 刻意用 SubtitleDownloadFailed：一般的 NoSubtitlesAvailable 會啟動
            # 語音備援，而備援用的是 yt-dlp，它不認得 IVOD 網址——使用者最後
            # 看到的會是無關的「無法下載音訊」，真正的原因被蓋掉。
            raise SubtitleDownloadFailed(
                f"這段 IVOD（{video_id}）還沒有 AI 逐字稿，立法院產生後才能摘要"
            )

        duration = _seconds(str(record.get("影片長度") or ""))
        return Transcript(
            video_id=video_id,
            title=_title(record, video_id),
            # 宣告長度拿不到時退回最後一段的結束時間：duration 只用來
            # 提示模型與夾取時間戳，寧可近似也不要 0。
            duration=duration or cues[-1].end,
            cues=cues,
            language="zh",
            # WhisperX 是一句一句的獨立段落，不是 YouTube 的滾動字幕；
            # 套用滾動去重只會誤刪內容。
            is_automatic=False,
            source_note=_SOURCE_NOTE,
        )


def _title(record: dict, video_id: str) -> str:
    """`日期 委員－會議`。資料夾名稱用的就是它，要能一眼分辨是哪一段發言。"""
    meeting = (record.get("會議資料") or {}).get("標題") or ""
    speaker = record.get("委員名稱") or ""
    date = record.get("日期") or ""
    parts = [p for p in (date, speaker) if p]
    head = " ".join(parts)
    if head and meeting:
        return f"{head}－{meeting}"
    return head or meeting or f"IVOD {video_id}"


__all__ = ["IvodClient", "IvodSubtitleGateway", "NoSubtitlesAvailable"]
