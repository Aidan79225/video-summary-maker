"""落地檢查：模型說的話必須在原文裡找得到。"""
from __future__ import annotations

from factcheck.usecases.grounding import is_grounded, locate, normalize, transcript_lines

TRANSCRIPT = (
    "00:16 那這幾年有逐漸攀升的一個趨勢\n"
    "00:32 臺灣民眾黨對醫療法24條跟106條提出修正條文草案 首先我們對於滋擾醫院秩序之人 "
    "他的行政罰鍰從現行的3萬到5萬提升到5到25萬\n"
    "00:48 希望能夠提升賀主力\n"
)


def test_normalize_drops_spaces_and_widths():
    assert normalize("３萬 到 ５萬\n") == "3萬到5萬"


def test_a_verbatim_quote_is_grounded():
    assert is_grounded("他的行政罰鍰從現行的3萬到5萬", TRANSCRIPT)


def test_spacing_differences_do_not_matter():
    assert is_grounded("滋擾醫院秩序之人他的行政罰鍰", TRANSCRIPT)


def test_a_rewritten_quote_is_not_grounded():
    """攔的行為：模型把「3萬到5萬」改寫成「三萬至五萬元」。原文不是那樣說的。"""
    assert not is_grounded("行政罰鍰從現行的三萬至五萬元", TRANSCRIPT)


def test_an_empty_fragment_is_never_grounded():
    assert not is_grounded("", TRANSCRIPT)
    assert not is_grounded("   ", TRANSCRIPT)


def test_lines_carry_their_seconds():
    lines = transcript_lines(TRANSCRIPT)
    assert [at for at, _ in lines] == [16.0, 32.0, 48.0]
    assert lines[0][1] == "那這幾年有逐漸攀升的一個趨勢"


def test_hour_timestamps():
    assert transcript_lines("1:02:03 很長的會議")[0][0] == 3723.0


def test_locate_finds_the_line_where_the_quote_starts():
    assert locate("從現行的3萬到5萬", TRANSCRIPT) == 32.0


def test_locate_across_a_line_break():
    """quote 跨兩行時，時間取開頭那一行。"""
    assert locate("提升到5到25萬希望能夠", TRANSCRIPT) == 32.0


def test_timestamps_are_not_part_of_the_spoken_text():
    """攔的 bug：沒去掉時間標記的話，跨行的 quote 中間會夾著「00:48」而永遠找不到。"""
    assert locate("25萬 希望", TRANSCRIPT) == 32.0


def test_locate_returns_none_when_missing():
    assert locate("完全不存在的句子", TRANSCRIPT) is None
    assert locate("", TRANSCRIPT) is None
