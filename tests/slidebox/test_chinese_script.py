"""語音辨識簡體漂移的修正：只轉漂掉的段落，正體的字一個都不動。

幾乎每個例子都來自兩輪 review 實際跑出來的錯誤，每一個都曾經被改壞過。
"""
from __future__ import annotations

import os

import pytest

from slidebox.infrastructure.chinese_script import (
    _tables,
    fix_transcript,
    keep_terms_from,
    looks_simplified,
    to_traditional,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
DRIFT = "我们来讲一下这个问题"   # 一段確定漂掉的開場，後面的段落在漂移區段裡
TRAD = "市長 我請教一下這個問題"  # 一段確定是正體的開場


def _lines(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def _after(lead, text, keep=()):
    return fix_transcript([lead, text], keep)[1]


# --- 正體不能碰 ---

@pytest.mark.parametrize("text", [
    # 整份丟 OpenCC 時被改壞的真實逐字稿
    "是不是可以請去公所請里長", "不是只有永和", "就是細紙淡水萬里共聊跟三支", "了解 了解",
    "所以他便需要分區", "要分布很多點齁",
    # 臺灣標準字被當成簡體
    "有一群里民", "全里的族群", "郁慕明一群人", "游淑慧的秘密", "于美人一群人", "余家的秘方",
    "病床的核准", "排泄的病床", "占床率", "台南的病床", "舞台上一群人", "台北的尖峰",
    "主秘也是只有", "不是只有族群", "于美人主秘",
    "雇主的責任", "就業服務法規定雇主", "雇員", "牆壁都發霉了", "霉運", "倒霉", "苧麻",
    "我是家裡的老么", "庄跤", "台南的虱目魚", "虱目仔粥", "虱目魚養殖",
    "這個都市計畫", "我們盡量要求", "我們瞭解",
    "",
])
def test_traditional_text_is_left_alone(text):
    assert to_traditional(text) == text
    assert _after(TRAD, text) == text


def test_one_stray_simplified_character_does_not_flip_the_following_lines():
    lines = ["市長 我請教一下", "对", "那一台", "你不准停", "占用人行道", "于先生的那一台",
             "余先生也有", "范先生", "好 謝謝"]
    assert fix_transcript(lines) == ["市長 我請教一下", "對", *lines[2:]]


def test_a_stray_milkfish_line_does_not_start_a_drift():
    lines = ["台南的虱目魚", "虱目仔粥", "你不准去", "余先生"]
    assert fix_transcript(lines) == lines


@pytest.mark.parametrize("text,expected", [
    # 正體段落夾一個簡體字：只換那個字，正體專用字（裡）與共用字（里、只、表）都不動
    ("市長你知道吗", "市長你知道嗎"),
    ("議員還有五分钟", "議員還有五分鐘"),
    ("請議員签名", "請議員簽名"),
    ("這個問題很复雜", "這個問題很複雜"),
    ("議員的头发", "議員的頭髮"),
    ("今天是農历初一", "今天是農曆初一"),
    ("預算編在这里", "預算編在這裡"),
    ("在議會裡长期以來", "在議會裡長期以來"),
    ("市府說每个里都有經費", "市府說每個里都有經費"),
    ("我們這個里开會", "我們這個里開會"),
    ("這邊的里办公室", "這邊的里辦公室"),
    ("議員這個問題这只能靠中央", "議員這個問題這只能靠中央"),
    ("這個都市計畫书", "這個都市計畫書"),
    ("這是計畫经費", "這是計畫經費"),
    ("記名表决", "記名表決"),
])
def test_a_few_simplified_characters_in_traditional_text(text, expected):
    assert _after(TRAD, text) == expected


# --- 漂移的段落要轉 ---

@pytest.mark.parametrize("text,expected", [
    ("我这样是不对的", "我這樣是不對的"),
    ("为什么你知道吗", "為什麼你知道嗎"),
    ("法治单也会支援", "法治單也會支援"),
    ("请里長帮忙", "請里長幫忙"),
    ("请公所的里長来", "請公所的里長來"),
    ("淡水萬里这边", "淡水萬里這邊"),
    ("不是只有永和的问题", "不是只有永和的問題"),
    ("我们不是只有这样", "我們不是只有這樣"),
    ("就是只有这个", "就是只有這個"),
    ("他就是只要钱", "他就是只要錢"),
    ("那只能说很遗憾", "那只能說很遺憾"),
    ("这只能靠中央", "這只能靠中央"),
    ("第一只是开始", "第一只是開始"),
    ("我们这里有两只狗", "我們這裡有兩隻狗"),
    ("流浪犬只的绝育", "流浪犬隻的絕育"),
    ("非洲猪瘟的猪只", "非洲豬瘟的豬隻"),
    ("渔港的船只", "漁港的船隻"),
    ("整只鸡", "整隻雞"),
    ("总统咨文", "總統咨文"),
    ("一群里民说", "一群里民說"),
    ("外籍移工的雇主说", "外籍移工的雇主說"),
    ("你在干什么", "你在幹什麼"),
    ("这里长期以来", "這裡長期以來"),
    ("这里民众很多", "這裡民眾很多"),
    ("这边不准停车", "這邊不准停車"),
    ("现在进行记名表决", "現在進行記名表決"),
    ("这三千万里面有多少是人事费", "這三千萬裡面有多少是人事費"),
    ("在农村里面", "在農村裡面"),
    ("他整台车都已经停到车格里面了", "他整台車都已經停到車格裡面了"),
    ("签这种合约", "簽這種合約"),
    ("但是听说签八成", "但是聽說簽八成"),
    ("去庙里抽签", "去廟裡抽籤"),
    ("游局长你好", "游局長你好"),
    ("范局长说", "范局長說"),
    ("余局长说", "余局長說"),
    ("于委员说", "于委員說"),
    ("观光旅游局长说", "觀光旅遊局長說"),
    ("关于委员会的决议", "關於委員會的決議"),
    ("其余的部分", "其餘的部分"),
    ("我们托婴中心", "我們托嬰中心"),
    ("拜托一下", "拜託一下"),
    ("台南的虱目鱼很好吃", "台南的虱目魚很好吃"),
])
def test_a_drifted_segment_becomes_traditional(text, expected):
    assert _after(DRIFT, text) == expected


def test_speaker_names_from_the_hint_are_protected():
    keep = keep_terms_from("新北市議會 市政總質詢。發言者：游淑慧、范雲")
    assert _after(DRIFT, "游淑慧说的没有错", keep) == "游淑慧說的沒有錯"
    assert _after(DRIFT, "范云说的没有错", keep) == "范雲說的沒有錯"


def test_a_hint_name_inside_a_drift_does_not_end_it():
    """講者姓名是正體，出現在漂移段落裡不能被當成「回到正體」的證據。"""
    keep = keep_terms_from("臺中市議會 市政總質詢。發言者：黃守達")
    lines = [DRIFT, "黃守達说", "然后后面的里面"]
    assert fix_transcript(lines, keep) == ["我們來講一下這個問題", "黃守達說", "然後後面的裡面"]


# --- 漂移區段的判斷 ---

def test_shared_only_segments_inside_a_drift_are_converted():
    out = fix_transcript([DRIFT, "然后", "拜托一下", "新北市里面", "土地征收"])
    assert out[1:] == ["然後", "拜託一下", "新北市裡面", "土地徵收"]


def test_a_lone_traditional_word_does_not_end_a_drift():
    lines = ["给我看这儿", "市長", "然后呢", "拜托一下", "后面的人"]
    assert fix_transcript(lines) == ["給我看這兒", "市長", "然後呢", "拜託一下", "後面的人"]


def test_a_tie_inside_a_drift_gets_the_full_conversion():
    assert fix_transcript([DRIFT, "然后市長说"])[1] == "然後市長說"


def test_leading_shared_only_segments_are_decided_by_what_follows():
    assert fix_transcript(["然后呢", "里面的问题我们来看一下"]) == ["然後呢", "裡面的問題我們來看一下"]


def test_the_drift_ends_when_traditional_comes_back():
    lines = [DRIFT, "我們現在請教局長", "謝謝議員的指教", "然后"]
    assert fix_transcript(lines)[1:] == ["我們現在請教局長", "謝謝議員的指教", "然后"]


# --- 真實逐字稿回歸 ---

def test_real_traditional_transcript_is_untouched():
    lines = _lines("ntpc_whisper_traditional.txt")
    _, traditional_only, _, _ = _tables()
    out = fix_transcript(lines)
    for before, after in zip(lines, out):
        # 正體專用字一個都不能被換掉
        assert all(a == b for a, b in zip(before, after) if a in traditional_only), (before, after)


def test_real_drifted_transcript_leaves_no_simplified_text():
    simplified_only, _, _, _ = _tables()
    out = fix_transcript(_lines("ntpc_whisper_drifted.txt"))
    assert [o for o in out if any(ch in simplified_only for ch in o)] == []
    assert [o for o in out if looks_simplified(o)] == []


# --- 第三輪 review ---

@pytest.mark.parametrize("text", [
    # 正體段落裡的「里」一律不動（行政區、地名、面臨／面積）
    "八里面臨的問題其實不只是臭味", "我們鄰里面臨很大的壓力", "上個月的豪雨 大里面臨嚴重的積淹水",
    "后里面臨的是農損的問題", "每一個里面對的問題都不一樣", "我們這個里面積很大 人口又少",
    "萬里面臨海岸侵蝕的問題", "萬里邊坡", "淡水萬里頭城",
    "那里長也跟我反映很多次了", "那里民都很生氣 說市府都沒有回應", "這里程碑對我們新北市很重要",
    "那里辦公處的經費",
    "剛才顏寬恒委員也有提到這個問題",
])
def test_round3_traditional_text_is_left_alone(text):
    assert _after(TRAD, text) == text


def test_a_traditional_only_line_before_a_drift_is_not_pulled_in():
    lines = ["汽車呢要停在格線裡面", "如果不依格線停止", "那個", "我們呢", "每個里都有",
             "时代背景改变", "车子越变越长", "我们的格子越变越大"]
    assert fix_transcript(lines)[4] == "每個里都有"


@pytest.mark.parametrize("text,expected", [
    ("新北市里长的事务补助费", "新北市里長的事務補助費"),
    ("新北市里民都很关心", "新北市里民都很關心"),
    ("各县市里长", "各縣市里長"),
    ("在城市里长大", "在城市裡長大"),
    ("全里停电", "全里停電"),
    ("该里的里长", "該里的里長"),
    ("万里面临海岸侵蚀的问题", "萬里面臨海岸侵蝕的問題"),
    ("会后里长都跑来找我", "會後里長都跑來找我"),
    ("要不要延后里民大会", "要不要延後里民大會"),
    ("我们不准备这样做", "我們不準備這樣做"),
    ("这个数据不准确", "這個數據不準確"),
    ("公车都不准时", "公車都不準時"),
    ("审核准则要改", "審核準則要改"),
    ("这属于局长的权限", "這屬於局長的權限"),
    ("于是市长就说", "於是市長就說"),
    ("这取决于市长", "這取決於市長"),
    ("业者一直游说立委", "業者一直遊說立委"),
    ("我们的模范市长", "我們的模範市長"),
    ("我对范局长的回答不满意", "我對范局長的回答不滿意"),
    ("由游局长来说明", "由游局長來說明"),
    ("我对余局长很失望", "我對余局長很失望"),
    ("这只代表一件事", "這只代表一件事"),
    ("这只针对低收入户", "這只針對低收入戶"),
    ("是只补助一半", "是只補助一半"),
    ("这些猪只在运送的过程", "這些豬隻在運送的過程"),
    ("流浪犬只不能进入", "流浪犬隻不能進入"),
    ("台风天的时候", "颱風天的時候"),
    ("抽到上上签", "抽到上上籤"),
    ("那个签诗怎么说", "那個籤詩怎麼說"),
    ("用竹签", "用竹籤"),
    ("其中签了三份", "其中簽了三份"),
    ("市府委托儿福联盟办理", "市府委託兒福聯盟辦理"),
    ("我拜托儿福联盟", "我拜託兒福聯盟"),
    ("我们盡量", "我們盡量"),
    ("这个計畫", "這個計畫"),
    ("占用人行道的问题", "占用人行道的問題"),
    ("批准的案子", "批准的案子"),
    ("核准的案子", "核准的案子"),
    ("里头的东西", "裡頭的東西"),
])
def test_round3_drifted_segments(text, expected):
    assert _after(DRIFT, text) == expected


def test_the_keep_term_mask_keeps_a_name_from_ending_a_drift():
    keep = keep_terms_from("發言者：黃守達")
    assert fix_transcript([DRIFT, "黃守達 然后呢", "我们再来看"], keep)[1] == "黃守達 然後呢"


def test_a_strong_isolated_drift_line_between_traditional_lines_is_converted():
    lines = ["市長 我請教一下這個問題", "这个预算为什么会这样", "謝謝議員的指教"]
    assert fix_transcript(lines) == ["市長 我請教一下這個問題", "這個預算為什麼會這樣", "謝謝議員的指教"]
