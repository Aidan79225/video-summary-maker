"""片段下載 adapter 的契約性質：不碰網路，以 monkeypatch 取代 yt-dlp。"""
from __future__ import annotations

import pytest

from slidebox.domain.errors import OperationCancelled
from slidebox.infrastructure import ytdlp_sections
from slidebox.infrastructure.ytdlp_sections import YtDlpSectionGateway


def _noop_progress(frac, status):
    pass


def test_cleanup_tolerates_a_missing_directory(tmp_path):
    """BuildDeckUseCase 從 finally 呼叫 cleanup；取消於摘要階段時該目錄根本不存在。

    若這裡拋例外，會從 finally 取代掉正在傳播的 OperationCancelled，
    把使用者的「取消」變成檔案系統錯誤。
    """
    YtDlpSectionGateway(ffmpeg_dir=str(tmp_path)).cleanup(str(tmp_path / "never" / "created"))


def test_returns_one_entry_per_timestamp_even_when_every_download_fails(tmp_path, monkeypatch):
    """回傳長度必須恆等於 timestamps 長度——BuildDeckUseCase 以位置配對
    clips[i] 與 slides[i]，短一格就會把圖配到錯的頁。"""
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    monkeypatch.setattr(gw, "_one", lambda *a, **kw: None)
    out = gw.download_sections(
        "URL", [10.0, 20.0, 30.0], 1080, str(tmp_path), _noop_progress, lambda: False
    )
    assert out == [None, None, None]


def test_returns_one_entry_per_timestamp_on_mixed_success(tmp_path, monkeypatch):
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    seen: list[int] = []

    def fake_one(url, start, max_height, dest_dir, index):
        seen.append(index)
        return None if index == 1 else f"clip{index}.mp4"

    monkeypatch.setattr(gw, "_one", fake_one)
    out = gw.download_sections(
        "URL", [10.0, 20.0, 30.0], 1080, str(tmp_path), _noop_progress, lambda: False
    )
    assert len(out) == 3
    assert out[1] is None
    assert out[0] is not None and out[2] is not None
    assert seen == [0, 1, 2]


def test_cancellation_propagates_and_is_not_swallowed(tmp_path, monkeypatch):
    """每個時間點的 except Exception 不得吞掉 OperationCancelled。"""
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    monkeypatch.setattr(gw, "_one", lambda *a, **kw: "clip.mp4")
    with pytest.raises(OperationCancelled):
        gw.download_sections(
            "URL", [10.0, 20.0], 1080, str(tmp_path), _noop_progress, lambda: True
        )


def test_a_failing_download_does_not_abort_the_batch(tmp_path, monkeypatch):
    """單點失敗只讓該頁沒圖，其餘照做——這是整條 pipeline 的降級策略。"""
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))

    def exploding_download(self, *a, **kw):
        raise RuntimeError("yt-dlp 爆了")

    monkeypatch.setattr(ytdlp_sections.yt_dlp.YoutubeDL, "download", exploding_download)
    out = gw.download_sections(
        "URL", [10.0, 20.0], 1080, str(tmp_path), _noop_progress, lambda: False
    )
    assert out == [None, None]


class _CapturingYDL:
    """攔下 yt-dlp 收到的 opts，不做任何實際下載。"""

    captured: list[dict] = []

    def __init__(self, params):
        _CapturingYDL.captured.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        pass


def _capture_opts(monkeypatch, tmp_path, max_height):
    _CapturingYDL.captured = []
    monkeypatch.setattr(ytdlp_sections.yt_dlp, "YoutubeDL", _CapturingYDL)
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    gw.download_sections(
        "URL", [10.0], max_height, str(tmp_path), _noop_progress, lambda: False
    )
    # 先查格式的那一次不下載；要驗的是下載片段那一次的 opts
    return next(o for o in _CapturingYDL.captured if "download_ranges" in o)


def test_format_excludes_hls_streams(tmp_path, monkeypatch):
    """格式選擇必須限定 http(s) 協定，排除 HLS。

    HLS 不支援任意位置的分段抽取：選到它時下載會回報成功，卻產出一個
    約 250 bytes、沒有影像的容器，抽幀必然失敗且症狀難以追查。這個測試
    擋的是「日後有人把選擇字串簡化回去」的回歸。
    """
    assert "protocol^=http" in _capture_opts(monkeypatch, tmp_path, 1080)["format"]


def test_format_excludes_hls_streams_without_a_height_cap(tmp_path, monkeypatch):
    """未設畫質上限時同樣要限定協定。"""
    assert "protocol^=http" in _capture_opts(monkeypatch, tmp_path, None)["format"]


# --- 直播剛結束、YouTube 還沒轉檔完的影片（post-live） ---


class _Response:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _SegmentedYDL:
    """仿 yt-dlp 對 post-live 影片的回應：選到的格式切成每 5 秒一段，網址帶 &sq=N。"""

    info: dict = {}
    fetched: list[str] = []
    ranged_downloads = 0
    failing_sq: set[int] = set()

    def __init__(self, params):
        self.params = params

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        assert download is False
        return _SegmentedYDL.info

    def urlopen(self, request):
        url = request.url
        _SegmentedYDL.fetched.append(url)
        if int(url.rsplit("sq=", 1)[1]) in _SegmentedYDL.failing_sq:
            raise OSError("HTTP Error 403: Forbidden")
        return _Response(f"segment {url}".encode())

    def download(self, urls):
        _SegmentedYDL.ranged_downloads += 1


def _post_live(monkeypatch, fragments=10, failing=()):
    _SegmentedYDL.info = {
        "format_id": "137", "protocol": "http_dash_segments", "target_duration": 5,
        "fragments": [{"url": f"https://x/videoplayback?itag=137&sq={i}"} for i in range(fragments)],
        "http_headers": {"User-Agent": "UA"},
    }
    _SegmentedYDL.fetched = []
    _SegmentedYDL.ranged_downloads = 0
    _SegmentedYDL.failing_sq = set(failing)
    monkeypatch.setattr(ytdlp_sections.yt_dlp, "YoutubeDL", _SegmentedYDL)


def test_post_live_videos_fetch_the_segment_that_holds_each_timestamp(tmp_path, monkeypatch):
    """攔的 bug：post-live 影片只有切段格式，protocol^=http 也會選到；交給 download_ranges 時
    yt-dlp 讓 ffmpeg 在直播串流網址上 -ss 跳秒，跳不過去，下載「成功」卻是沒有畫格的空殼，
    整份投影片一張截圖都沒有。每一段本身解得開，直接抓包含那一秒的那一段。"""
    _post_live(monkeypatch)
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    out = gw.download_sections(
        "URL", [0.0, 12.0, 49.9], 1080, str(tmp_path / "clips"), _noop_progress, lambda: False
    )
    assert [u.rsplit("sq=", 1)[1] for u in _SegmentedYDL.fetched] == ["0", "2", "9"]
    assert _SegmentedYDL.ranged_downloads == 0
    assert len(out) == 3
    with open(out[1], "rb") as f:
        assert f.read() == b"segment https://x/videoplayback?itag=137&sq=2"


def test_a_timestamp_past_the_last_segment_uses_the_last_one(tmp_path, monkeypatch):
    """投影片的時間點來自字幕，可能比最後一段的起點還晚一點：用最後一段，不要整頁沒圖。"""
    _post_live(monkeypatch, fragments=4)
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    out = gw.download_sections(
        "URL", [21.0], 1080, str(tmp_path / "clips"), _noop_progress, lambda: False
    )
    assert out[0] is not None
    assert _SegmentedYDL.fetched[0].endswith("sq=3")


def test_a_failing_segment_only_loses_that_page_and_says_why(tmp_path, monkeypatch):
    _post_live(monkeypatch, failing={2})
    statuses: list[str] = []
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    out = gw.download_sections(
        "URL", [0.0, 10.0, 20.0], 1080, str(tmp_path / "clips"),
        lambda frac, status: statuses.append(status), lambda: False
    )
    assert out[0] is not None and out[1] is None and out[2] is not None
    assert "403" in statuses[-1]


def test_ordinary_videos_still_download_the_time_range(tmp_path, monkeypatch):
    """一般影片（https 格式、沒有 target_duration）照舊走 download_ranges。"""
    _post_live(monkeypatch)
    _SegmentedYDL.info = {"format_id": "137", "protocol": "https", "url": "https://x/v"}
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    gw.download_sections(
        "URL", [10.0, 20.0], 1080, str(tmp_path / "clips"), _noop_progress, lambda: False
    )
    assert _SegmentedYDL.ranged_downloads == 2
    assert _SegmentedYDL.fetched == []


def test_an_empty_segment_list_is_reported_in_words(tmp_path, monkeypatch):
    """yt-dlp 查片段編號的那一次 HEAD 失敗時，post-live 影片的片段清單是空的。每頁都會失敗，
    但原因要看得懂，不能是「list index out of range」；也不能退回 download_ranges——那條路
    會「成功」下載出沒有畫格的空殼，連失敗原因都不留。"""
    _post_live(monkeypatch, fragments=0)
    statuses: list[str] = []
    gw = YtDlpSectionGateway(ffmpeg_dir=str(tmp_path))
    out = gw.download_sections(
        "URL", [0.0, 10.0], 1080, str(tmp_path / "clips"),
        lambda frac, status: statuses.append(status), lambda: False
    )
    assert out == [None, None]
    assert _SegmentedYDL.ranged_downloads == 0
    assert "片段清單" in statuses[-1]
    assert "index" not in statuses[-1]
