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
