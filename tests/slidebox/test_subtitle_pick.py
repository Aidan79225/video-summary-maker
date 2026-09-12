"""字幕挑軌：手動優先於自動，語言依偏好序，支援語言前綴退讓。"""
from __future__ import annotations

import pytest

from slidebox.domain.errors import NoSubtitlesAvailable
from slidebox.usecases.chapters import pick_subtitle_track

PREFER = ("zh-TW", "zh-Hant", "zh-HK", "zh", "en")


def test_manual_beats_automatic_even_for_less_preferred_language():
    """手動字幕優先於自動字幕，即使自動字幕的語言排序更前面。"""
    lang, auto = pick_subtitle_track({"en": []}, {"zh-TW": []}, PREFER)
    assert (lang, auto) == ("en", False)


def test_language_preference_order_within_manual():
    lang, auto = pick_subtitle_track({"en": [], "zh-TW": []}, {}, PREFER)
    assert (lang, auto) == ("zh-TW", False)


def test_falls_back_to_automatic_when_no_manual():
    lang, auto = pick_subtitle_track({}, {"zh-Hant": []}, PREFER)
    assert (lang, auto) == ("zh-Hant", True)


def test_prefix_match_when_no_exact_key():
    """YouTube 常給 en-US / zh-Hant-TW 這類鍵，精確比對會落空。"""
    lang, auto = pick_subtitle_track({}, {"en-US": []}, PREFER)
    assert (lang, auto) == ("en-US", True)


def test_exact_match_wins_over_prefix_match():
    lang, _ = pick_subtitle_track({"en-US": [], "en": []}, {}, PREFER)
    assert lang == "en"


def test_raises_when_nothing_available():
    with pytest.raises(NoSubtitlesAvailable):
        pick_subtitle_track({}, {}, PREFER)


def test_none_inputs_are_treated_as_empty():
    """yt-dlp 的 info dict 可能整個缺這兩個鍵。"""
    with pytest.raises(NoSubtitlesAvailable):
        pick_subtitle_track(None, None, PREFER)


def test_unrelated_language_is_not_picked():
    with pytest.raises(NoSubtitlesAvailable):
        pick_subtitle_track({"ko": []}, {"ja": []}, PREFER)


# --- 自動字幕優先選影片原始語言 ---
#
# 英文影片的自動字幕裡，zh-TW／zh-Hant 是 YouTube 對英文語音辨識結果再做的
# 機器翻譯（URL 帶 tlang=）；en-orig／en 才是原文。餵原文給模型、讓它邊摘要
# 邊翻譯，比餵兩層損失的機器翻譯好。實測翻譯端點還會被 HTTP 429 限流。


def test_automatic_original_language_beats_automatic_translation():
    """攔的 bug：沒有原始語言這一層時，偏好序會先挑到機器翻譯的 zh-TW。"""
    auto = {"zh-TW": [], "zh-Hant": [], "en-orig": []}
    assert pick_subtitle_track({}, auto, PREFER, "en") == ("en-orig", True)


def test_bare_original_language_key_also_counts():
    """攔的 bug：只認 -orig 字尾，沒有 -orig 的影片就退回機器翻譯。"""
    auto = {"zh-TW": [], "zh-Hant": [], "en": []}
    assert pick_subtitle_track({}, auto, PREFER, "en") == ("en", True)


def test_manual_chinese_still_beats_automatic_original():
    """攔的 bug：原始語言層被放到人工字幕前面。人工翻譯永遠最好。"""
    manual = {"zh-TW": []}
    auto = {"zh-Hant": [], "en-orig": []}
    assert pick_subtitle_track(manual, auto, PREFER, "en") == ("zh-TW", False)


def test_unknown_original_language_keeps_the_previous_order():
    """yt-dlp 常給不出 language（實測 3 支中文影片有 2 支是 None）。

    攔的 bug：None 沒被處理而崩潰，或被誤當成某個語言。
    """
    auto = {"zh-Hant": [], "en-orig": []}
    assert pick_subtitle_track({}, auto, PREFER, None) == ("zh-Hant", True)


def test_region_tagged_original_language_matches_its_base():
    """language 可能帶區域或字體標記（實測見過 zh-Hant）。

    攔的 bug：只做精確比對，en-US 找不到 en-orig 就退回機器翻譯。
    """
    auto = {"zh-Hant": [], "en-orig": []}
    assert pick_subtitle_track({}, auto, PREFER, "en-US") == ("en-orig", True)


def test_original_language_without_a_matching_track_falls_back():
    """攔的 bug：原始語言層找不到時沒有往下退，而是回傳錯誤的值或拋例外。"""
    auto = {"zh-Hant": [], "en": []}
    assert pick_subtitle_track({}, auto, PREFER, "ja") == ("zh-Hant", True)


# --- 預設偏好序：輸出是繁體中文 ---

from slidebox.domain.entities import Settings  # noqa: E402

DEFAULT = Settings(output_dir="X").subtitle_langs


def test_default_prefers_human_simplified_chinese_over_human_english():
    """攔的 bug：人工簡中字幕排不進偏好序，輸給人工英文。

    精確比對會先把整份偏好序跑完才做前綴比對，所以 zh-CN 只能靠前綴命中，
    而 en 是精確命中——不列入偏好序的話，已經是中文的人工字幕反而落選，
    模型得從英文重新翻譯。
    """
    assert pick_subtitle_track({"zh-CN": [], "en": []}, {}, DEFAULT) == ("zh-CN", False)


def test_default_still_prefers_traditional_over_simplified():
    """攔的 bug：為了讓簡中勝過英文，把簡中排到繁中前面。輸出是繁體。"""
    manual = {"zh-CN": [], "zh-Hant": []}
    assert pick_subtitle_track(manual, {}, DEFAULT) == ("zh-Hant", False)
