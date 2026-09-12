"""HTML 產出：base64 內嵌、跳脫、無圖降級。"""
from __future__ import annotations

import base64

from slidebox.domain.entities import Deck, Slide
from slidebox.infrastructure.html_renderer import HtmlDeckRenderer

IMAGE_BYTES = b"\x00\x01fake-webp\xff"


def _write_image(tmp_path, name="1.webp"):
    p = tmp_path / name
    p.write_bytes(IMAGE_BYTES)
    return str(p)


def _render(tmp_path, deck) -> str:
    dest = tmp_path / "out" / "slides.html"
    HtmlDeckRenderer().render(deck, str(dest))
    return dest.read_text(encoding="utf-8")


def test_embeds_the_image_as_base64_data_uri(tmp_path):
    deck = Deck("https://x", "影片", (
        Slide(1, "標題", ("重點",), 0.0, _write_image(tmp_path)),
    ))
    html = _render(tmp_path, deck)
    assert "data:image/webp;base64," in html
    assert base64.b64encode(IMAGE_BYTES).decode("ascii") in html


def test_creates_parent_directory(tmp_path):
    """use case 只做路徑運算，建立目錄是 adapter 的責任。"""
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("重點",), 0.0),))
    dest = tmp_path / "deep" / "nested" / "slides.html"
    HtmlDeckRenderer().render(deck, str(dest))
    assert dest.exists()


def test_escapes_script_tags_from_the_model(tmp_path):
    """標題來自 LLM，LLM 讀的是任何人都能上傳的字幕。"""
    deck = Deck("https://x", "影片", (
        Slide(1, "<script>alert(1)</script>", ("<img onerror=x>",), 0.0),
    ))
    html = _render(tmp_path, deck)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img onerror=x>" not in html


def test_escapes_the_video_title(tmp_path):
    deck = Deck("https://x", "<b>粗體</b>", (Slide(1, "T", ("b",), 0.0),))
    html = _render(tmp_path, deck)
    assert "<b>粗體</b>" not in html
    assert "&lt;b&gt;" in html


def test_escapes_the_source_url_in_the_href(tmp_path):
    deck = Deck('https://x/"onmouseover="evil()', "影片", (Slide(1, "T", ("b",), 0.0),))
    html = _render(tmp_path, deck)
    assert 'onmouseover="evil()"' not in html
    assert "&quot;" in html


def test_renders_a_slide_without_an_image(tmp_path):
    deck = Deck("https://x", "影片", (Slide(1, "沒圖的一頁", ("重點",), 0.0),))
    html = _render(tmp_path, deck)
    assert "沒圖的一頁" in html
    assert "data:image/webp" not in html


def test_missing_image_file_degrades_instead_of_raising(tmp_path):
    """image_path 指向不存在的檔案時不該炸掉整份成品。"""
    deck = Deck("https://x", "影片", (
        Slide(1, "標題", ("重點",), 0.0, str(tmp_path / "gone.webp")),
    ))
    html = _render(tmp_path, deck)
    assert "標題" in html
    assert "data:image/webp" not in html


def test_includes_every_slide_and_bullet(tmp_path):
    deck = Deck("https://x", "影片", (
        Slide(1, "第一頁", ("甲", "乙"), 0.0),
        Slide(2, "第二頁", ("丙",), 30.0),
    ))
    html = _render(tmp_path, deck)
    for text in ("第一頁", "第二頁", "甲", "乙", "丙"):
        assert text in html


def test_shows_a_readable_timestamp(tmp_path):
    deck = Deck("https://x", "影片", (Slide(1, "T", ("b",), 125.0),))
    assert "02:05" in _render(tmp_path, deck)


def test_is_self_contained(tmp_path):
    """單一 HTML 檔不該引用任何外部資源。"""
    deck = Deck("https://x", "影片", (
        Slide(1, "標題", ("重點",), 0.0, _write_image(tmp_path)),
    ))
    html = _render(tmp_path, deck)
    assert "<link" not in html
    assert "<script" not in html


def test_escapes_a_hostile_title_inside_the_image_alt_attribute(tmp_path):
    """有圖時標題會進 alt="..."，屬性脈絡的跳脫必須有測試釘住。

    目前的安全性只來自 html.escape 的 quote 預設值；沒有這個測試，
    把它改成 quote=False 的重構不會被任何測試攔下來。
    """
    deck = Deck("https://x", "影片", (
        Slide(1, 'x" onmouseover="evil()', ("重點",), 0.0, _write_image(tmp_path)),
    ))
    html = _render(tmp_path, deck)
    assert 'onmouseover="evil()"' not in html
    assert "&quot;" in html
    assert "data:image/webp;base64," in html      # 確實走到了有圖的分支



def test_the_source_note_travels_with_the_deck(tmp_path):
    """攔的 bug：成品完全沒提內容來源。HTML 會被分享、會被日後重看，那時已經
    沒有狀態列可看——「由語音辨識產生」必須寫在成品裡。"""
    deck = Deck("https://x", "影片", (Slide(1, "T", ("b",), 0.0),),
                source_note="由語音辨識產生，可能有辨識錯誤")
    assert "由語音辨識產生，可能有辨識錯誤" in _render(tmp_path, deck)


def test_the_source_note_is_escaped(tmp_path):
    deck = Deck("https://x", "影片", (Slide(1, "T", ("b",), 0.0),), source_note="<b>x</b>")
    html = _render(tmp_path, deck)
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;" in html


# --- 詳細模式 ---


def test_detail_paragraph_is_rendered_when_present(tmp_path):
    deck = Deck("https://x", "影片", (
        Slide(1, "標題", ("重點",), 0.0, None, "這一段的完整敘述。"),
    ))
    assert "這一段的完整敘述。" in _render(tmp_path, deck)


def test_detail_is_escaped_like_everything_else(tmp_path):
    """detail 跟標題條列一樣來自 LLM 讀的第三方字幕，不跳脫等於執行別人的程式碼。"""
    deck = Deck("https://x", "影片", (
        Slide(1, "標題", ("重點",), 0.0, None, "<script>alert(1)</script>"),
    ))
    html = _render(tmp_path, deck)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_no_empty_paragraph_in_normal_mode(tmp_path):
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("重點",), 0.0),))
    assert '<p class="detail">' not in _render(tmp_path, deck)


def test_transcript_appendix_is_collapsed_so_it_does_not_bury_the_slides(tmp_path):
    """逐字稿動輒數萬字。直接攤平會把投影片推到螢幕外，等於毀掉原本的用途。"""
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("重點",), 0.0),),
                transcript_text="00:00 逐字稿內容")
    html = _render(tmp_path, deck)
    assert "<details" in html
    assert "逐字稿內容" in html


def test_transcript_keeps_its_line_breaks(tmp_path):
    """逐字稿靠每行開頭的時間找位置；折成一整段就失去可讀性。"""
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("重點",), 0.0),),
                transcript_text="00:00 第一行\n00:15 第二行")
    html = _render(tmp_path, deck)
    assert "<pre" in html or "white-space" in html


def test_no_appendix_when_there_is_no_transcript(tmp_path):
    deck = Deck("https://x", "影片", (Slide(1, "標題", ("重點",), 0.0),))
    assert "<details" not in _render(tmp_path, deck)
