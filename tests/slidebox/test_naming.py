"""輸出資料夾命名：純字串處理，不碰檔案系統。"""
from __future__ import annotations

from slidebox.usecases.naming import deck_folder_name


def test_the_title_leads_and_the_id_follows():
    """攔的 bug：資料夾只用影片 id 命名，一堆 dQw4w9WgXcQ 根本分不出是哪支。"""
    assert deck_folder_name("如何做好簡報", "abc123XYZ_-") == "如何做好簡報 [abc123XYZ_-]"


def test_windows_illegal_characters_are_removed():
    """攔的 bug：標題含 ? : / 等字元時建資料夾直接 OSError。YouTube 標題很常有
    「問題？」「A/B 測試」「重點：三件事」這種寫法。"""
    bad = chr(92) + '/:*?"<>|'          # Windows 不允許的全部字元
    name = deck_folder_name('這樣對嗎？A/B 測試：重點<三>件*事' + bad, "vid")
    assert not any(c in name for c in bad)
    assert name.endswith(" [vid]")


def test_control_characters_and_newlines_collapse():
    """攔的 bug：標題裡的換行讓資料夾名稱變成怪東西。"""
    assert deck_folder_name("第一行\n\t第二行   第三行", "vid") == "第一行 第二行 第三行 [vid]"


def test_trailing_dots_and_spaces_are_stripped():
    """攔的 bug：Windows 不允許資料夾名結尾是點或空白，會被靜默去掉或建立失敗，
    之後用同一個路徑去寫檔就找不到。"""
    name = deck_folder_name("未完待續...  ", "vid")
    assert name == "未完待續 [vid]"


def test_a_very_long_title_is_truncated():
    """攔的 bug：超長標題撞到 Windows 的路徑長度上限，寫檔失敗。"""
    name = deck_folder_name("長" * 300, "vid")
    assert len(name) <= 100
    assert name.endswith(" [vid]")


def test_a_title_that_survives_as_nothing_falls_back_to_the_id():
    """攔的 bug：標題整個由非法字元組成時，資料夾名變成「 [vid]」或空字串。"""
    assert deck_folder_name('///???', "vid") == "vid"
    assert deck_folder_name("   ", "vid") == "vid"


def test_two_videos_sharing_a_title_do_not_collide():
    """攔的 bug：只用標題命名，同名影片會蓋掉彼此的成品。同名影片是真的存在的。"""
    a = deck_folder_name("每日新聞", "aaa")
    b = deck_folder_name("每日新聞", "bbb")
    assert a != b


def test_the_same_video_always_maps_to_the_same_folder():
    """重跑同一支影片要覆寫自己的舊成品，而不是每次都長出新資料夾。"""
    assert deck_folder_name("標題", "vid") == deck_folder_name("標題", "vid")
