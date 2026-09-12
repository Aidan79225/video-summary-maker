"""字幕 gateway 的接線：只換掉 yt-dlp 的網路那一層，挑軌與解析走真實程式碼。"""
from __future__ import annotations

import os

import pytest
import yt_dlp.utils

from slidebox.domain.errors import SubtitleDownloadFailed
from slidebox.infrastructure import ytdlp_subtitles
from slidebox.infrastructure.ytdlp_subtitles import YtDlpSubtitleGateway

PREFER = ("zh-TW", "zh-Hant", "zh-HK", "zh", "en")

VTT = """WEBVTT

00:00:19.000 --> 00:00:22.000
Hear that? That's nothing.

00:00:22.000 --> 00:00:25.000
Which is what I have for you all.
"""

# 形狀取自實測的英文 TEDx（8S0FDjFBj8o）：沒有人工中文字幕，自動字幕同時
# 有原文辨識 en-orig 與兩軌機器翻譯。
ENGLISH_INFO = {
    "id": "8S0FDjFBj8o",
    "title": "How to sound smart in your TEDx Talk",
    "duration": 356,
    "language": "en",
    "subtitles": {},
    "automatic_captions": {"zh-TW": [], "zh-Hant": [], "en-orig": []},
}


def _fake_ydl(info, download_error=None):
    """回傳一個假的 YoutubeDL 類別：extract_info 給 info，download 寫出 VTT。"""

    class FakeYDL:
        def __init__(self, params):
            self.params = params

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return info

        def download(self, urls):
            if download_error is not None:
                raise download_error
            lang = self.params["subtitleslangs"][0]
            folder = os.path.dirname(self.params["outtmpl"])
            with open(os.path.join(folder, f"{info['id']}.{lang}.vtt"), "w",
                      encoding="utf-8") as f:
                f.write(VTT)

    return FakeYDL


def test_fetch_uses_the_untranslated_original_language_track(monkeypatch):
    """攔的 bug：gateway 沒把 info['language'] 傳給挑軌，於是挑到機器翻譯的 zh-TW。

    挑軌的單元測試全都會過，但實際執行時新行為根本不會發生——只有走過
    gateway 的接線才看得出來。
    """
    monkeypatch.setattr(ytdlp_subtitles.yt_dlp, "YoutubeDL", _fake_ydl(ENGLISH_INFO))
    t = YtDlpSubtitleGateway().fetch("URL", PREFER)
    assert (t.language, t.is_automatic) == ("en-orig", True)
    assert t.cues[0].text == "Hear that? That's nothing."


def test_a_subtitle_download_error_becomes_a_domain_error(monkeypatch):
    """攔的 bug：yt-dlp 的 DownloadError 原封不動冒到 UI，使用者看到一串英文錯誤。

    實測 YouTube 的翻譯端點會回 HTTP 429；原因要保留，使用者才知道是被
    限流而不是影片沒字幕。
    """
    # 錯誤原文刻意不含字幕軌名稱，下面「訊息點名字幕軌」的斷言才只能靠
    # 我們的程式碼成立。只留一軌，讓挑軌結果與語言接線無關。
    err = yt_dlp.utils.DownloadError("ERROR: HTTP Error 429: Too Many Requests")
    info = {**ENGLISH_INFO, "automatic_captions": {"en-orig": []}}
    monkeypatch.setattr(
        ytdlp_subtitles.yt_dlp, "YoutubeDL", _fake_ydl(info, download_error=err)
    )
    # 必須是 SubtitleDownloadFailed 而非一般的 NoSubtitlesAvailable：否則 use
    # case 會把暫時性的限流當成「沒有字幕」，改走較差的語音辨識。
    with pytest.raises(SubtitleDownloadFailed) as exc:
        YtDlpSubtitleGateway().fetch("URL", PREFER)
    assert "429" in str(exc.value)
    assert "en-orig" in str(exc.value)
