"""歌名正規化純函式的單元測試。"""
from __future__ import annotations

from musicbox.usecases import rename_songs as rs
from musicbox.usecases.rename_songs import derive_title, normalize_title


def test_removes_known_noise_brackets():
    assert normalize_title("告白氣球 (Official Music Video)") == "告白氣球"
    assert normalize_title("Shape of You [Official Audio]") == "Shape of You"
    assert normalize_title("演唱會 【MV】") == "演唱會"
    assert normalize_title("某歌 (Lyric Video)") == "某歌"


def test_keeps_meaningful_brackets():
    assert normalize_title("Hotel California (Live)") == "Hotel California (Live)"
    assert normalize_title("某歌 (Remix)") == "某歌 (Remix)"
    assert normalize_title("某歌 (feat. ABC)") == "某歌 (feat. ABC)"


def test_fullwidth_to_halfwidth():
    assert normalize_title("ＡＢＣ１２３") == "ABC123"


def test_removes_illegal_filename_chars():
    assert normalize_title('a: b / c') == "a b c"


def test_collapses_whitespace_and_underscores():
    assert normalize_title("a__b   c") == "a b c"


def test_simplified_to_traditional():
    normalize_title("觸發初始化")          # 讓 OpenCC 延遲載入
    assert rs.OPENCC_AVAILABLE is True     # 本專案已把 opencc 列為硬依賴
    assert normalize_title("简体字") == "簡體字"


def test_traditional_fallback_when_opencc_absent(monkeypatch):
    # 模擬「已嘗試載入但失敗」：簡轉繁略過，其餘規則仍生效
    monkeypatch.setattr(rs, "_opencc_tried", True)
    monkeypatch.setattr(rs, "_opencc_converter", None)
    assert normalize_title("简体 (Official Video)") == "简体"


def test_derive_title_off_vs_on():
    name = "01-告白氣球 (Official MV).mp3"
    assert derive_title(name, normalize=False) == "告白氣球 (Official MV)"
    assert derive_title(name, normalize=True) == "告白氣球"
