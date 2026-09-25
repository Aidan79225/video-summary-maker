"""Deck → 新聞服務要的 JSON。"""
from __future__ import annotations

import base64

from slidebox.domain.entities import Ask, Brief, Deck, KeyNumber, Slide
from slidebox_api.payload import deck_payload

IMAGE = b"\x00\x01fake-webp\xff"


def _deck(tmp_path, with_image=True, detail="這一段的完整敘述"):
    path = None
    if with_image:
        path = tmp_path / "01.webp"
        path.write_bytes(IMAGE)
        path = str(path)
    return Deck(
        source_url="https://ivod.ly.gov.tw/Play/Clip/1M/171180",
        video_title="2026-08-27 洪毓祥－第11屆第5會期第23次會議",
        slides=(Slide(1, "章節標題", ("重點一", "重點二"), 12.5, path, detail),),
        source_note="逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        transcript_text="00:00 主席 各位同仁",
    )


def test_every_field_the_news_service_needs_is_present(tmp_path):
    data = deck_payload(_deck(tmp_path), video_id="171180")
    assert data["video_id"] == "171180"
    assert data["title"].startswith("2026-08-27")
    assert data["source_url"].endswith("171180")
    assert data["source_note"].startswith("逐字稿")
    assert data["transcript_text"] == "00:00 主席 各位同仁"
    slide = data["slides"][0]
    assert slide["index"] == 1
    assert slide["title"] == "章節標題"
    assert slide["bullets"] == ["重點一", "重點二"]
    assert slide["detail"] == "這一段的完整敘述"
    assert slide["timestamp"] == 12.5


def test_the_image_travels_as_base64_so_the_pi_can_store_it(tmp_path):
    """Pi 上沒有這台機器的檔案系統。分開做圖片端點要處理保存期限、清理與
    授權，為了幾百 KB 不值得。"""
    data = deck_payload(_deck(tmp_path), video_id="171180")
    slide = data["slides"][0]
    assert base64.b64decode(slide["image_base64"]) == IMAGE
    assert slide["image_media_type"] == "image/webp"


def test_a_page_without_a_screenshot_is_still_delivered(tmp_path):
    """既有策略就是「部分截圖失敗仍然出片」，API 不該推翻它。"""
    data = deck_payload(_deck(tmp_path, with_image=False), video_id="171180")
    assert data["slides"][0]["image_base64"] is None


def test_an_image_that_disappeared_degrades_instead_of_raising(tmp_path):
    deck = _deck(tmp_path)
    (tmp_path / "01.webp").unlink()
    assert deck_payload(deck, video_id="171180")["slides"][0]["image_base64"] is None


def test_the_payload_is_json_serialisable(tmp_path):
    import json
    json.dumps(deck_payload(_deck(tmp_path), video_id="171180"))


def test_the_brief_travels_with_the_deck(tmp_path):
    deck = _deck(tmp_path)
    deck = Deck(**{**deck.__dict__, "brief": Brief(
        "國防部三年編 82.4 億買無人機，交到部隊的不到一半",
        key_numbers=(KeyNumber("82.4", "億元", "三年累計編列", "累計編列八十二點四億元"),),
        asks=(Ask("提出交機時程清冊", "一個月內", "部長允諾"),),
    )})
    brief = deck_payload(deck, video_id="171180")["brief"]
    assert brief["one_liner"].startswith("國防部")
    assert brief["key_numbers"] == [
        {"value": "82.4", "unit": "億元", "label": "三年累計編列", "quote": "累計編列八十二點四億元"}]
    assert brief["asks"] == [{"request": "提出交機時程清冊", "deadline": "一個月內", "response": "部長允諾"}]


def test_no_brief_is_null_not_an_empty_object(tmp_path):
    """Pi 那邊要分得出「沒產出」與「產出但空」。"""
    assert deck_payload(_deck(tmp_path), video_id="171180")["brief"] is None
