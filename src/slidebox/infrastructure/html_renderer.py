"""DeckRenderer 的 HTML 實作：自包含的單一檔案，圖片以 base64 內嵌。"""
from __future__ import annotations

import base64
import html
import os

from ..domain.entities import Brief, Deck, Slide

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 16px;
  background: #f5f6f8; color: #1c1f26;
  font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif;
  line-height: 1.7;
}
header { max-width: 900px; margin: 0 auto 32px; }
header h1 { font-size: 26px; margin: 0 0 6px; }
header a { color: #4a6fa5; font-size: 13px; word-break: break-all; }
header .source { font-size: 13px; color: #8a6d3b; margin: 8px 0 0; }
.slide {
  max-width: 900px; margin: 0 auto 28px; padding: 24px;
  background: #fff; border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
}
.meta { font-size: 12px; color: #8a90a0; letter-spacing: .04em; }
.slide h2 { font-size: 20px; margin: 4px 0 16px; }
.slide img { width: 100%; border-radius: 8px; display: block; margin-bottom: 16px; }
.slide ul { margin: 0; padding-left: 20px; }
.slide li { margin-bottom: 6px; }
.noimg { font-size: 12px; color: #a8adba; margin-bottom: 12px; }
.slide .detail { margin: 14px 0 0; padding-top: 12px; border-top: 1px solid #ecedf1; }
.brief { margin: 0 0 28px; padding: 18px 22px; border: 1px solid #e3e5ea; border-radius: 12px; }
.brief .one { margin: 0; font-size: 1.15em; font-weight: 600; line-height: 1.6; }
.brief .numbers { display: flex; flex-wrap: wrap; gap: 16px 28px; margin-top: 14px; }
.brief .kn b { font-size: 1.5em; }
.brief .kn small { margin-left: 2px; color: #6b7280; }
.brief .kn span { display: block; font-size: .85em; color: #6b7280; }
.brief .asks { margin: 14px 0 0; padding-left: 18px; }
.brief .asks em { font-style: normal; font-size: .85em; color: #6b7280; }
.brief .asks .reply { display: block; font-size: .9em; color: #6b7280; }
details.transcript {
  max-width: 900px; margin: 8px auto 40px; padding: 16px 24px;
  background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
}
details.transcript summary { cursor: pointer; font-weight: 600; }
details.transcript pre {
  white-space: pre-wrap; word-break: break-word;
  font-family: inherit; font-size: 14px; color: #4a4f5c; margin: 16px 0 0;
}
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #e3e6ec; }
  .slide { background: #1e2128; box-shadow: none; }
  .slide .detail { border-top-color: #2c303a; }
  .brief { border-color: #2c303a; }
  details.transcript { background: #1e2128; box-shadow: none; }
  details.transcript pre { color: #b6bcc9; }
  header a { color: #7aa2d8; }
}
"""


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _data_uri(path: str | None) -> str | None:
    """讀圖轉成 data URI；檔案不存在或讀不到就回 None（該頁降級成無圖）。"""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None
    return f"data:image/webp;base64,{payload}"


def _render_slide(slide: Slide) -> str:
    # 標題與條列來自 LLM，而 LLM 讀的是任何人都能上傳的 YouTube 字幕。
    # 不跳脫等於把第三方內容當程式碼執行。
    title = html.escape(slide.title)
    bullets = "".join(f"<li>{html.escape(b)}</li>" for b in slide.bullets)
    # detail 同樣來自模型讀的第三方字幕，一樣要跳脫
    detail = (f'<p class="detail">{html.escape(slide.detail)}</p>'
              if slide.detail.strip() else "")
    uri = _data_uri(slide.image_path)
    image = (
        f'<img src="{uri}" alt="{title}">' if uri
        else '<p class="noimg">（這一頁沒有截圖）</p>'
    )
    return (
        '<section class="slide">'
        f'<div class="meta">{slide.index:02d} · {_mmss(slide.timestamp)}</div>'
        f"<h2>{title}</h2>{image}<ul>{bullets}</ul>{detail}"
        "</section>"
    )


def _render_brief(brief: Brief | None) -> str:
    """摘要卡放在所有投影片之前：它就是為了「先看這個」而存在的。"""
    if brief is None:
        return ""
    numbers = "".join(
        f'<div class="kn"><b>{html.escape(n.value)}</b>'
        f'<small>{html.escape(n.unit)}</small><span>{html.escape(n.label)}</span></div>'
        for n in brief.key_numbers
    )
    asks = ""
    for a in brief.asks:
        when = f"<em>{html.escape(a.deadline)}</em> " if a.deadline else ""
        reply = f'<span class="reply">{html.escape(a.response)}</span>' if a.response else ""
        asks += f"<li>{when}{html.escape(a.request)}{reply}</li>"
    return (
        '<section class="brief">'
        f"<p class=\"one\">{html.escape(brief.one_liner)}</p>"
        + (f'<div class="numbers">{numbers}</div>' if numbers else "")
        + (f"<ul class=\"asks\">{asks}</ul>" if asks else "")
        + "</section>"
    )


class HtmlDeckRenderer:
    def render(self, deck: Deck, dest_path: str) -> None:
        title = html.escape(deck.video_title)
        url = html.escape(deck.source_url, quote=True)
        # 內容來源（例如「由語音辨識產生」）跟著成品走——HTML 會被分享與重看
        source = (f'<p class="source">{html.escape(deck.source_note)}</p>'
                  if deck.source_note else "")
        body = _render_brief(deck.brief) + "".join(_render_slide(s) for s in deck.slides)
        # 逐字稿預設收合：它比投影片長一個數量級，攤開就把成品推到螢幕外。
        # 用 <pre> 保留換行——每行開頭的時間是使用者找位置的唯一線索。
        transcript = (
            '<details class="transcript"><summary>完整逐字稿（字幕原文）</summary>'
            f"<pre>{html.escape(deck.transcript_text)}</pre></details>"
            if deck.transcript_text.strip() else ""
        )
        document = (
            '<!DOCTYPE html>\n<html lang="zh-Hant">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{title}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
            f'<header><h1>{title}</h1><a href="{url}">{url}</a>{source}</header>\n'
            f"{body}\n{transcript}\n</body>\n</html>\n"
        )
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(document)
