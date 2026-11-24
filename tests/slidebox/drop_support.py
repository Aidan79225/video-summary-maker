"""「被丟掉的重點」診斷（drop_analysis、drop_report）的測試共用的小對談：每一份都是真的檢查丟得掉的情況。

C_*：模型的 quote 前面多了一段自己編的話，但後面 23 個字逐字在逐字稿裡（C：只取最長一段逐字吻合的）。
D_*：同一位說話者的話被 diarizer 切成相鄰的兩輪、模型的 quote 接了兩輪（D：同一位說話者相鄰的輪併起來）。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases import conversation as c
from slidebox.usecases import diarized_pipeline as dp
from slidebox.usecases import drop_analysis as a
from slidebox.usecases.conversation import Allocation, Window, chunk_turns

from .diarized_support import FakeConversation
from .speaker_fixtures import make_speakers, make_turns

SPEAKERS = make_speakers("participant", "participant", "minor")          # S1、S2 主要發言者；S3 其他聲音
CITABLE = frozenset({"S1", "S2"})

# --- C：quote 前面多了一段模型自己編的話 ---
BODY = "正民主法治的國家這個時候你要去用人家的私有權"                      # 逐字稿裡有的（23 個字）
HEAD = "我覺得啊其實"                                                     # 模型自己加的、逐字稿裡沒有
C_TURNS = make_turns(("S2", "請問您怎麼看都更的問題"), ("S1", BODY + "來做這件事情"), ("S2", "好的謝謝您的說明"))
C_TEXT = "主張民主法治的國家需要尊重人家的私有權"
C_POINT = RawPoint(1, C_TEXT, quote=HEAD + BODY)

# --- D：同一位說話者的話被切成相鄰的兩輪（T1、T2），模型的 quote 接了兩輪 ---
D_TURNS = make_turns(
    ("S2", "請問您怎麼看今年交通建設的規劃"),
    ("S1", "優先處理淡水捷運延伸線"),
    ("S1", "預計三年內完成規劃並且開始動工興建"),
    ("S2", "那公車路網呢要不要一起調整"),
    ("S1", "公車路網會同步調整並且增加偏鄉班次"),
    ("S2", "好的謝謝您的說明"),
)
D_TEXT = "優先處理淡水捷運延伸線並在三年內完成規劃"
D_QUOTE = "優先處理淡水捷運延伸線預計三年內完成規劃"                           # 接了第 1、2 輪
D_POINT = RawPoint(1, D_TEXT, quote=D_QUOTE)
D_OTHER = RawPoint(4, "同步調整公車路網並增加偏鄉班次", quote="公車路網會同步調整並且增加偏鄉班次")        # 通過的點

# --- D 的另一種：一個字的「X 嗯」夾在同一位說話者的話中間 ---
NOISE_TURNS = make_turns(
    ("S2", "請問您怎麼看今年交通建設的規劃"),
    ("S1", "優先處理淡水捷運延伸線預計"),
    ("S3", "嗯"),
    ("S1", "三年內完成規劃並且開始動工興建"),
    ("S2", "好的謝謝您的說明"),
)

# --- 不同說話者的話拼在一起：怎麼併都不能歸給任何一個人 ---
CROSS_TURNS = make_turns(
    ("S2", "請問您怎麼看今年交通建設的規劃"),
    ("S1", "優先處理淡水捷運延伸線"),
    ("S2", "預計三年內完成規劃並且開始動工興建"),
    ("S1", "好的謝謝您的說明"),
)


def dropped(turns, *points, window=None, speakers=SPEAKERS, quote_only=False):
    """真的 validate_points 丟掉的點（DroppedPoint）。"""
    window = window or Window(0, tuple(turns))
    return c.validate_points([RawSlide("主題", tuple(points))], turns, speakers, window, quote_only).dropped


def contexts(turns, windows=None, quote_only=False):
    return a.window_contexts(windows or [Window(0, tuple(turns))], CITABLE, quote_only)


def outcomes_for(turns, *points, speakers=SPEAKERS, budget=10**6, quote_only=False):
    """用真的檢查跑一遍：每個窗每次問模型都得到同一份回應（一頁、這些點）。"""
    reply = (RawSlide("主題", tuple(points)),)
    allocations = tuple(Allocation(w, 1, 3) for w in chunk_turns(turns, speakers, budget))
    return dp.summarize_windows(FakeConversation((), default=lambda call: reply), allocations, turns, speakers,
                                lambda frac, status: None, lambda: None, lambda: False, quote_only)

# 模型的 quote 有空白（逐字稿是語音辨識，段與段之間常有空白）：夾著一個字的雜音時，quote 的片段跨過它仍然依序出現
NOISE_POINT = RawPoint(1, D_TEXT, quote="優先處理淡水捷運延伸線預計 三年內完成規劃")

# --- 問句：逐字稿的一輪裡有問句字眼，重點卻寫成陳述 ---
Q_SHORT_TURNS = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "那明年的預算打算怎麼編列呢"), ("S2", "好的謝謝您的說明"))
Q_SHORT_POINT = RawPoint(1, "明年的預算打算要編列", quote="那明年的預算打算怎麼編列呢")

Q_LONG = ("我們的高地有了自來水阿伯打開水龍頭流出水的時候他掉眼淚了我也哭了這種等待一件微小的事"
          "但是卻對你日常生活影響這麼重大的事過去為什麼沒有人做但沒有人去做")         # 沒有標點的一整輪
Q_NEAR_TURNS = make_turns(("S2", "請問您怎麼看高地自來水的事"), ("S1", Q_LONG), ("S2", "好的謝謝您的說明"))
Q_NEAR_POINT = RawPoint(1, "指出過去沒有人去做這種事", quote="過去為什麼沒有人做但沒有人去做")

# --- 問句：重點的數字剛被別人說過，所以不看字眼離重點多遠（strict）---
Q_STRICT_TURNS = make_turns(
    ("S2", "我們的月費是2980元"),
    ("S1", "月費2980喔這個價格比一般的方案高出很多但是你不怕沒有人來用嗎我們現在要的是真的有人在使用"),
    ("S2", "好的謝謝您的說明"),
)
Q_STRICT_POINT = RawPoint(1, "指出月費2980元的價格比一般方案高", quote="月費2980喔這個價格比一般的方案高出很多")

# --- C 與 D 都救得了：一位說話者的話被切成兩輪、quote 接了兩輪，而第一輪那一段本身就夠長、也撐得起這個重點 ---
BOTH_TURNS = make_turns(
    ("S2", "請問您怎麼看今年交通建設的規劃"),
    ("S1", "我們今年優先處理淡水捷運延伸線的規劃工作"),
    ("S1", "預計三年內完成並且開始動工興建"),
    ("S2", "好的謝謝您的說明"),
)
BOTH_POINT = RawPoint(1, "優先處理淡水捷運延伸線的規劃工作", quote="優先處理淡水捷運延伸線的規劃工作預計三年內完成")


@pytest.fixture
def without_anchoring(monkeypatch):
    """診斷的「再算一次」比的是「沒有保護的 C、D」會多救幾個：這幾份測試要的是產品自己救不了的點，
    所以把產品裡有保護的 C、D（anchoring）關掉，才造得出被丟掉的點。"""
    monkeypatch.setattr(c, "join_for_quote", lambda *args, **kwargs: None)
    monkeypatch.setattr(c, "longest_run", lambda *args, **kwargs: None)
