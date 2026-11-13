"""逐字稿裡「用來稱呼人的名字」是哪些（純邏輯）：重點的文字裡不可以出現這些名字。

為什麼要從逐字稿本身找：提示裡不能放任何姓名（見 infrastructure/ollama_conversation.py），
所以模型看得到的名字只有逐字稿裡出現的。主持人問「卞教授，請問…」、對談的人叫「阿翔」「小安」，
模型就會把這個稱呼抄進重點，讀起來像是替那個人說了話（歸屬沒錯，但「誰是卞教授」是程式不知道的事）。
原本的檢查只看文字的開頭（姓＋職稱）與少數角色字眼，抓不到中間的稱呼，所以由程式從這個窗的逐字稿
找出稱呼用的名字，重點文字（「」以外）裡有就丟掉。找出來的名字不會給模型看。

只找三種樣子，故意抓得保守（誤抓的代價是丟掉一個重點，漏抓的代價是顯示一個稱呼）：
- 一個字加上人的職稱：邱教授、湯委員、褚理事長、林先生；
- 阿X／小X／老X 當作稱呼用：句首或標點之後、後面接「你、您、好、標點」，或前面是「謝謝、感謝、請」；
- 名字當作稱呼用：句首或標點之後的二、三個字（或英文名字），後面緊接逗號與「你、您」：
  「志豪，你怎麼看」「Kevin，您覺得呢」。連接詞與句子（「所以，你…」「我覺得，你…」）不是名字。
"""
from __future__ import annotations

import re

_PERSON_TITLES = (
    "委員|教授|博士|部長|局長|署長|處長|理事長|董事長|院長|議員|主席|老師|先生|小姐|女士|主任|校長|醫師|"
    "經理|執行長|科長|組長|市長|縣長|區長|里長|顧問|律師|法官|總監|副總|秘書|助理|主播|教練|醫生|檢察官"
)
_TITLED_RE = re.compile(rf"([一-鿿])({_PERSON_TITLES})")
_ENDS_WITH_TITLE_RE = re.compile(rf"(?:{_PERSON_TITLES})$")
# 職稱前面的這些字不是姓氏：「的教授」「這位老師」「總經理」「各位先生」…
_NOT_A_NAME_CHAR = frozenset("的是這那個位請謝跟和與及讓給對向被把將於在到從由為各本前副總該每某老小阿一二兩三四五六七八九十")
_NICKNAME_RE = re.compile(
    r"(?:^|[\s，。！？、：「])((?:阿|小|老)[一-鿿])(?=你|您|妳|好|，|。|！|？|\s|$|大哥|大姊|哥|姐)")
_THANKS_RE = re.compile(r"(?:謝謝|感謝|謝|請)((?:阿|小|老)[一-鿿])")
_VOCATIVE_RE = re.compile(
    r"(?:^|[\s，。！？、：「])([一-鿿]{2,3}|[A-Z][A-Za-z]{2,11})(?=[，,]\s*(?:你|您|妳))")
# 名字裡不會有的字（連接詞、語助詞、代名詞…）；句首常見的連接詞與慣用語
_NOT_IN_A_NAME = _NOT_A_NAME_CHAR | frozenset("所但因然而如其或問覺認不有沒還就都也很我她他它這那的了是在和與對")
_NOT_A_VOCATIVE = frozenset([
    "首先", "另外", "最後", "現在", "今天", "同時", "此外", "總之", "當然", "謝謝", "感謝", "抱歉", "好的", "請問",
    "簡單", "老實", "坦白", "一般", "比如", "例如", "舉例", "換言", "否則", "至於", "畢竟", "反正", "再來",
])
# 阿X／小X／老X 的普通詞，不是稱呼
_NOT_A_NICKNAME = frozenset([
    "小店", "小孩", "小朋友", "小心", "小時", "小學", "小型", "小錢", "小事", "小客", "小量", "小的", "小幅",
    "小家", "老人", "老師", "老闆", "老大", "老家", "老街", "老舊", "老婆", "老公", "老實", "老是", "老年",
    "老化", "老爸", "老媽", "阿嬤", "阿公", "阿姨", "阿伯",
])


def detect_names(text: str) -> frozenset[str]:
    """這段逐字稿裡用來稱呼人的名字與稱謂（見模組說明）。"""
    found: set[str] = set()
    for match in _TITLED_RE.finditer(text):
        if match.group(1) not in _NOT_A_NAME_CHAR:
            found.add(match.group(0))
    for pattern in (_NICKNAME_RE, _THANKS_RE):
        for match in pattern.finditer(text):
            if match.group(1) not in _NOT_A_NICKNAME:
                found.add(match.group(1))
    for match in _VOCATIVE_RE.finditer(text):
        name = match.group(1)
        if _ENDS_WITH_TITLE_RE.search(name):
            continue                                     # 只是職稱（「部長，您…」）不是名字
        if name.isascii() or (name not in _NOT_A_VOCATIVE and not any(ch in _NOT_IN_A_NAME for ch in name)):
            found.add(name)
    return frozenset(found)
