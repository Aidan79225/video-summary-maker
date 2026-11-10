"""桌面版 HTML 在多人模式下把 [[Sn]] 代號換成標籤；單人模式一個字都不動。

API 的成品（payload）保留代號，由新聞服務決定怎麼顯示；桌面程式沒有「操作者填的名字」這回事，
所以這裡只用程式給的標籤（發言者 A…）。
"""
from __future__ import annotations

import hashlib

from slidebox.domain.entities import Ask, Brief, Deck, KeyNumber, Point, Slide, SpeakerInfo
from slidebox.infrastructure.html_renderer import HtmlDeckRenderer
from slidebox.usecases.conversation import render_slide

SPEAKERS = (
    SpeakerInfo("S1", "發言者 A", "participant", 300.0, 40, 5.0, 700.0),
    SpeakerInfo("S2", "發言者 B", "participant", 390.0, 39, 14.0, 710.0),
    SpeakerInfo("S3", "其他聲音 1", "minor", 22.0, 2, 62.0, 410.0),
)
POINTS = (
    Point("S1", 0, 5.0, "詢問目前補助政策的成效", "先請教您怎麼看目前的補助政策"),
    Point("S2", 1, 14.0, "現金補助有幫助，但托育資源更重要", "現金補助有幫助"),
    Point("S2", 3, 38.0, "公共托育名額不夠", "名額不夠"),
)
TRANSCRIPT = (
    "00:05 [[S1]]：今天我們來談少子化\n"
    "00:14 [[S2]]：現金補助有幫助\n"
    "00:20 [[S0]]：嗯嗯\n"
    "01:02 [[S3]]：我想補充"
)


def diarized_deck(slides=None, speakers=SPEAKERS, transcript=TRANSCRIPT) -> Deck:
    return Deck(
        "https://www.youtube.com/watch?v=abcdefghijk", "少子化對談",
        tuple(slides) if slides is not None else (render_slide(1, "補助政策與托育名額", POINTS),),
        source_note="由語音辨識產生，說話者由聲音自動區分；標籤可能有誤，未經人工確認",
        transcript_text=transcript, brief=None,
        speaker_mode="diarize", speakers=speakers,
    )


def _render(tmp_path, deck) -> str:
    dest = tmp_path / "out" / "slides.html"
    HtmlDeckRenderer().render(deck, str(dest))
    return dest.read_text(encoding="utf-8")


def test_a_diarized_deck_shows_labels_instead_of_codes(tmp_path):
    html = _render(tmp_path, diarized_deck())
    assert "<li>發言者 A：詢問目前補助政策的成效</li>" in html
    assert "<li>發言者 B：現金補助有幫助，但托育資源更重要</li>" in html
    assert "<li>發言者 B：公共托育名額不夠</li>" in html
    assert "[[" not in html and "]]" not in html


def test_the_detail_paragraph_shows_labels_too(tmp_path):
    html = _render(tmp_path, diarized_deck())
    assert ('<p class="detail">發言者 A：詢問目前補助政策的成效。 '
            '發言者 B：現金補助有幫助，但托育資源更重要；公共托育名額不夠。</p>') in html


def test_the_transcript_shows_labels_and_marks_the_lines_nobody_could_be_assigned(tmp_path):
    html = _render(tmp_path, diarized_deck())
    assert "00:05 發言者 A：今天我們來談少子化" in html
    assert "00:14 發言者 B：現金補助有幫助" in html
    assert "00:20 無法判斷：嗯嗯" in html
    assert "01:02 其他聲音 1：我想補充" in html


def test_a_code_nobody_listed_never_leaks_through_as_a_raw_code(tmp_path):
    deck = diarized_deck(transcript="00:05 [[S9]]：不知道是誰", speakers=SPEAKERS[:1])
    html = _render(tmp_path, deck)
    assert "未知說話者：不知道是誰" in html and "[[S9]]" not in html


def test_substitution_happens_before_escaping_so_nothing_slips_through_unescaped(tmp_path):
    slide = Slide(1, "標題", ("[[S1]]：<script>alert(1)</script>",), 5.0,
                  detail="[[S1]]：<img onerror=x>。", points=POINTS[:1])
    risky = (SpeakerInfo("S1", "A&B<i>", "participant", 300.0, 40, 5.0, 700.0),)
    html = _render(tmp_path, diarized_deck(slides=(slide,), speakers=risky,
                                           transcript="00:05 [[S1]]：<b>x</b>"))
    assert "<script>alert(1)</script>" not in html and "<img onerror=x>" not in html
    assert "<b>x</b>" not in html and "<i>" not in html
    assert "A&amp;B&lt;i&gt;：&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A&amp;B&lt;i&gt;：&lt;img onerror=x&gt;。" in html
    assert "00:05 A&amp;B&lt;i&gt;：&lt;b&gt;x&lt;/b&gt;" in html


def test_the_transcript_heading_of_a_diarized_deck_does_not_claim_to_be_subtitles(tmp_path):
    html = _render(tmp_path, diarized_deck())
    assert "字幕原文" not in html
    assert "完整逐字稿（語音辨識，依說話者分段）" in html


def test_a_single_speaker_deck_keeps_a_literal_code_in_its_text(tmp_path):
    """單人影片的字幕或標題裡可能剛好有 [[S1]]（例如在講維基語法）：那是內容，不是代號。"""
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("[[S1]]：甲",), 0.0, detail="[[S2]]：乙。"),),
                transcript_text="00:00 [[S1]]：丙")
    html = _render(tmp_path, deck)
    assert "<li>[[S1]]：甲</li>" in html
    assert '<p class="detail">[[S2]]：乙。</p>' in html
    assert "00:00 [[S1]]：丙" in html
    assert "（字幕原文）" in html


def test_a_deck_that_claims_no_mode_at_all_is_rendered_as_single(tmp_path):
    """Deck 的預設就是 single：舊的呼叫端（桌面程式的單人與議會）完全不用知道多人模式。"""
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("[[S1]]：甲",), 0.0),), speakers=SPEAKERS)
    assert "<li>[[S1]]：甲</li>" in _render(tmp_path, deck)


# 與 origin/master 的 html_renderer.py 在同一份 Deck 上產生的 HTML 的 sha256：
# 這個值是用 `git show origin/master:src/slidebox/infrastructure/html_renderer.py` 的原樣內容算出來的，
# 不是用改過的檔案。單人與議會的 HTML 不能因為多人模式而多一個字。
# 以文字（換行一律當 \n）計算，免得 Windows 寫檔時把 \n 變成 \r\n 就對不上。
SINGLE_DECK_HTML_SHA256 = "25931a23adf56909b80114d7e4d0d6d477e3c4640f8dd03a3fc8f3940fb9135b"


def single_golden_deck(image_path: str) -> Deck:
    return Deck(
        "https://www.youtube.com/watch?v=abcdefghijk&t=1s", "答詢 <質詢> & 備詢",
        (
            Slide(1, "預算怎麼編", ("重點一", "重點 <二>", "含 [[S1]] 的重點"), 5.0, image_path,
                  detail="完整敘述 & 更多。"),
            Slide(2, "沒圖的一頁", ("只有一點",), 3725.0),
        ),
        source_note="由語音辨識產生", transcript_text="00:05 第一行\n01:02 第二行 <b>",
        brief=Brief("一句話", (KeyNumber("82.4", "%", "占比", "占 82.4%", "預算法", "第5條"),),
                    (Ask("提高補助", "三個月內", "已回覆"),)),
    )


def test_single_mode_output_is_byte_identical_to_master(tmp_path):
    image = tmp_path / "1.webp"
    image.write_bytes(b"\x00\x01fake-webp\xff")
    dest = tmp_path / "slides.html"
    HtmlDeckRenderer().render(single_golden_deck(str(image)), str(dest))
    text = dest.read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == SINGLE_DECK_HTML_SHA256
