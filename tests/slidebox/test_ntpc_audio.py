"""新北市議會影片的音訊：假的 client 與 ffmpeg runner，不碰網路。"""
from __future__ import annotations

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable
from slidebox.infrastructure.ntpc_api import NtpcRecord
from slidebox.infrastructure.ntpc_audio import NtpcAudioGateway
from slidebox.usecases.sources import NtpcRef

GUID = "ebc80ece-7491-4288-be73-7c59f6b4815c"
URL = f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={GUID.upper()}"
VIDEO = "https://vodwms.ntp.gov.tw:443/NTP/x.mp4/playlist.m3u8?device=PC&kind=Guest"
AUDIO = VIDEO + "&wowzaaudioonly=true"
RECORD = NtpcRecord(asset_id=GUID, session="第4屆第8次定期會", agenda="市政總質詢",
                    speakers=("周雅玲", "林裔綺"), date="2026-09-16", duration_seconds=7166)


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def record(self, ref):
        self.calls.append(("record", ref))
        if self.error:
            raise self.error
        return RECORD

    def video_url(self, ref):
        self.calls.append(("video_url", ref))
        return VIDEO

    def audio_url(self, ref):
        self.calls.append(("audio_url", ref))
        if self.error:
            raise self.error
        return AUDIO


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        with open(command[-1], "wb") as f:
            f.write(b"\0" * 16000 * 2)

        class Result:
            returncode = 0
            stderr = ""
        return Result()


def _noop(frac, status):
    pass


def test_downloads_the_audio_only_stream(tmp_path):
    """語音辨識用不到影像：整支 SD 影片約 1.6 Mbps，純音訊約 64 kbps。"""
    client, runner = FakeClient(), FakeRunner()
    gateway = NtpcAudioGateway(client, runner=runner, ffmpeg_exe="ffmpeg")
    clip = gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)
    command = runner.commands[0]
    assert command[command.index("-i") + 1] == AUDIO
    assert ("video_url", NtpcRef(GUID)) not in client.calls
    assert clip.video_id == f"ntpc-{GUID}"
    assert clip.path.endswith(f"ntpc-{GUID}.wav")
    assert clip.title == RECORD.title
    assert clip.duration == pytest.approx(1.0)


def test_a_non_new_taipei_url_is_rejected_without_touching_the_network(tmp_path):
    client = FakeClient(error=AssertionError("不該打到"))
    gateway = NtpcAudioGateway(client, runner=FakeRunner(), ffmpeg_exe="ffmpeg")
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會"):
        gateway.download_audio("https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833",
                               str(tmp_path / "a"), _noop, lambda: False)
    assert client.calls == []


def test_a_client_failure_is_reported_as_no_subtitles(tmp_path):
    client = FakeClient(error=NoSubtitlesAvailable("新北市議會的播放器頁抓不到：timed out"))
    runner = FakeRunner()
    gateway = NtpcAudioGateway(client, runner=runner, ffmpeg_exe="ffmpeg")
    with pytest.raises(NoSubtitlesAvailable, match="播放器頁"):
        gateway.download_audio(URL, str(tmp_path / "a"), _noop, lambda: False)
    assert runner.commands == []
