"""逐字稿裡「用來稱呼人的名字」：重點文字裡不可以出現（V3）。

故意抓得保守：誤抓的代價是丟掉一個重點，漏抓的代價是顯示一個稱呼。
"""
from __future__ import annotations

import random

import pytest

from slidebox.usecases.names import detect_names

TITLES = ["委員", "教授", "博士", "部長", "局長", "署長", "處長", "理事長", "董事長", "院長", "議員", "主席", "老師",
          "先生", "小姐", "女士", "主任", "校長", "醫師", "經理", "執行長", "科長", "組長", "市長", "縣長", "區長", "里長"]


@pytest.mark.parametrize("title", TITLES)
def test_a_surname_and_a_title_is_a_name(title):
    assert detect_names(f"今天請王{title}說明。") == {f"王{title}"}


@pytest.mark.parametrize("text, expected", [
    ("請問邱教授怎麼看這件事", {"邱教授"}),
    ("湯委員提到預算不夠", {"湯委員"}),
    ("謝謝褚理事長的說明", {"褚理事長"}),
    ("林先生，你好", {"林先生"}),
    ("謝謝陳教授，也謝謝林委員", {"陳教授", "林委員"}),
    ("卞教授也提醒過這件事", {"卞教授"}),
])
def test_titled_names_are_found_wherever_they_stand(text, expected):
    assert detect_names(text) == expected


@pytest.mark.parametrize("text", [
    "各位先生，大家好", "總經理表示會檢討", "這位老師說了重點", "他是我的教授", "副教授發言", "我們請教授說明",
    "各位委員", "本部長期推動", "每位博士都同意", "跟老師討論過", "讓主任決定",
])
def test_a_title_without_a_surname_is_not_a_name(text):
    assert detect_names(text) == frozenset()


@pytest.mark.parametrize("text, expected", [
    ("阿翔，你怎麼看", {"阿翔"}),
    ("好的，小安你說", {"小安"}),
    ("老張好，今天很準時", {"老張"}),
    ("謝謝阿翔", {"阿翔"}),
    ("請小安說明", {"小安"}),
    ("感謝老王的協助", {"老王"}),
    ("阿翔大哥，請說", {"阿翔"}),
    ("對，小林！你再說一次", {"小林"}),
])
def test_a_nickname_used_as_address_is_a_name(text, expected):
    assert detect_names(text) == expected


@pytest.mark.parametrize("text", [
    "請小心路面", "小孩好可愛", "老人家，你好", "阿公，你好", "小時候的事", "我跟阿翔去了", "他說阿翔很準時", "老闆好",
    "小店的生意不好", "老師好",
])
def test_an_ordinary_word_or_a_nickname_that_is_only_mentioned_is_not_a_name(text):
    assert detect_names(text) == frozenset()


# 職稱前面這些字不是姓氏；清單故意在測試裡再寫一次（從 names 匯入的話，被改掉的清單會連測試的範圍一起縮小）
NOT_SURNAMES = "的是這那個位請謝跟和與及讓給對向被把將於在到從由為各本前副總該每某老小阿一二兩三四五六七八九十"


@pytest.mark.parametrize("char", list(NOT_SURNAMES))
def test_a_title_after_a_word_that_is_not_a_surname_is_not_a_name(char):
    assert detect_names(f"今天{char}委員說明預算。") == frozenset()
    assert detect_names(f"今天{char}教授說明預算。") == frozenset()


# 動詞、名詞的最後一個字剛好接上職稱：看醫生、找老師、報告局長、前任局長、學校老師（真實逐字稿裡抓到過「看醫生」）
@pytest.mark.parametrize("text", [
    "有症狀的就請去看醫生", "我想找醫師談一談", "要問醫生才知道", "他想當老師", "我們要叫醫生過來",
    "前任局長的做法", "現任局長還沒有回應", "歷任局長都說過", "新任局長今天上任", "學校老師都知道", "今天局長不在",
    "覺得局長講得有道理", "要約律師談一談", "去見醫師", "這個我們要聘律師處理",
])
def test_a_verb_or_noun_that_happens_to_end_before_a_title_is_not_a_surname(text):
    assert detect_names(text) == frozenset()


@pytest.mark.parametrize("text, expected", [
    ("請任局長說明", {"任局長"}),                  # 任是姓氏
    ("謝謝任委員", {"任委員"}),
    ("看看王醫生怎麼說", {"王醫生"}),
    ("報告局長，任局長剛剛說的", {"報告局長", "任局長"}),
])
def test_a_real_surname_next_to_those_words_is_still_a_name(text, expected):
    assert detect_names(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("報告委員，已經修復四站", {"報告委員"}),
    ("報告局長，我想請教", {"報告局長"}),
    ("報告主席，各位委員好", {"報告主席"}),
])
def test_reporting_to_a_title_is_an_address_and_stays_out_of_the_text(text, expected):
    """「報告委員，…」是對人說話的稱呼（模型常把它抄進重點的開頭）；整個稱呼算名字，不是「告委員」。"""
    assert detect_names(text) == expected


def test_nothing_in_nothing_out():
    assert detect_names("") == frozenset() and detect_names("今天討論預算與期程。") == frozenset()


def test_the_result_is_an_immutable_set_and_the_same_every_time():
    text = "謝謝陳教授，阿翔你好，也請林委員補充。"

    first = detect_names(text)

    assert isinstance(first, frozenset) and first == detect_names(text) == {"陳教授", "阿翔", "林委員"}


def test_every_name_found_is_written_in_the_text():
    rng = random.Random(5)
    pool = "謝請好陳林阿小老翔安教授委員先生，。！？ 今天預算討論的了我你他"
    for _ in range(500):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 40)))
        for name in detect_names(text):
            assert name in text


# --- 審查補的：名字不只有「姓＋職稱」與「阿X」 ---


@pytest.mark.parametrize("title", ["顧問", "律師", "法官", "總監", "副總", "秘書", "助理", "主播", "教練", "醫生", "檢察官"])
def test_more_titles_make_a_surname_and_a_title_a_name(title):
    assert detect_names(f"今天請李{title}說明。") == {f"李{title}"}


@pytest.mark.parametrize("text, expected", [
    ("志豪，你怎麼看明年的預算？", {"志豪"}),
    ("好，怡君，您覺得這個方案可行嗎？", {"怡君"}),
    ("Kevin，你怎麼看明年的預算？", {"Kevin"}),
    ("謝謝。志豪，你先說。", {"志豪"}),
])
def test_a_given_name_used_as_address_before_you_is_a_name(text, expected):
    assert detect_names(text) == expected


@pytest.mark.parametrize("text", [
    "所以，你覺得呢？", "但是，你要知道這件事", "我覺得，你說得對", "首先，你要了解背景", "那麼，您的看法是什麼",
    "如果，你同意的話", "其實，你也知道", "不過，你可以再想想", "我們來談預算，你要先看這份資料",
])
def test_a_connector_or_a_phrase_before_you_is_not_a_name(text):
    assert detect_names(text) == frozenset()
