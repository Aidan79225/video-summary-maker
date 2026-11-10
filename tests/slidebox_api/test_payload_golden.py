"""單人結果的黃金測試：`deck_payload()` 的輸出必須與 origin/master 逐鍵相同。

議會的 client（新聞服務）與 `--speakers 1` 都讀這個形狀。多人模式才會多出
`speaker_mode`、`speakers`、`diarization`、`slides[].points`；單人結果不能帶任何
新的鍵（連空的也不行）。

這個檔案在 origin/master 的乾淨 checkout 上也必須全過。
"""
from __future__ import annotations

import base64
import json

from slidebox.domain.entities import Ask, Brief, Deck, KeyNumber, Slide
from slidebox_api.payload import deck_payload

IMAGE = b"\x00\x01fake-webp\xff"


def _deck(tmp_path) -> Deck:
    image = tmp_path / "01.webp"
    image.write_bytes(IMAGE)
    return Deck(
        source_url="https://ivod.ly.gov.tw/Play/Clip/1M/171180",
        video_title="2026-08-27 洪毓祥－第11屆第5會期第23次會議",
        slides=(
            Slide(1, "章節標題", ("重點一", "重點二"), 12.5, str(image), "這一段的完整敘述"),
            Slide(2, "沒有截圖的一頁", ("重點三",), 90.0),
        ),
        source_note="逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        transcript_text="00:00 主席 各位同仁\n01:30 洪委員 謝謝主席",
        brief=Brief(
            "國防部三年編 82.4 億買無人機，交到部隊的不到一半",
            key_numbers=(KeyNumber("82.4", "億元", "三年累計編列", "累計編列八十二點四億元",
                                   law="預算法", article="第1條"),),
            asks=(Ask("提出交機時程清冊", "一個月內", "部長允諾"),),
        ),
    )


def test_a_single_mode_payload_is_key_for_key_what_master_produces(tmp_path):
    payload = deck_payload(_deck(tmp_path), video_id="171180")

    assert payload == {
        "video_id": "171180",
        "source_url": "https://ivod.ly.gov.tw/Play/Clip/1M/171180",
        "title": "2026-08-27 洪毓祥－第11屆第5會期第23次會議",
        "source_note": "逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        "transcript_text": "00:00 主席 各位同仁\n01:30 洪委員 謝謝主席",
        "slides": [
            {
                "index": 1,
                "title": "章節標題",
                "bullets": ["重點一", "重點二"],
                "detail": "這一段的完整敘述",
                "timestamp": 12.5,
                "image_base64": base64.b64encode(IMAGE).decode("ascii"),
                "image_media_type": "image/webp",
            },
            {
                "index": 2,
                "title": "沒有截圖的一頁",
                "bullets": ["重點三"],
                "detail": "",
                "timestamp": 90.0,
                "image_base64": None,
                "image_media_type": None,
            },
        ],
        "brief": {
            "one_liner": "國防部三年編 82.4 億買無人機，交到部隊的不到一半",
            "key_numbers": [
                {"value": "82.4", "unit": "億元", "label": "三年累計編列",
                 "quote": "累計編列八十二點四億元", "law": "預算法", "article": "第1條"},
            ],
            "asks": [{"request": "提出交機時程清冊", "deadline": "一個月內", "response": "部長允諾"}],
        },
    }


def test_a_single_mode_payload_has_exactly_the_master_key_sets(tmp_path):
    """上一個測試比的是值；這個比的是鍵，讓失敗訊息直接指出多出來的鍵。"""
    payload = deck_payload(_deck(tmp_path), video_id="171180")

    assert sorted(payload) == ["brief", "slides", "source_note", "source_url", "title",
                               "transcript_text", "video_id"]
    assert sorted(payload["slides"][0]) == ["bullets", "detail", "image_base64",
                                            "image_media_type", "index", "timestamp", "title"]
    for forbidden in ("speaker_mode", "speakers", "diarization"):
        assert forbidden not in payload
    assert "points" not in payload["slides"][0]


def test_a_single_mode_payload_without_a_brief_is_still_the_same_shape(tmp_path):
    deck = _deck(tmp_path)
    plain = Deck(source_url=deck.source_url, video_title=deck.video_title, slides=deck.slides,
                 source_note=deck.source_note, transcript_text=deck.transcript_text)

    payload = deck_payload(plain, video_id="171180")

    assert payload["brief"] is None
    assert json.loads(json.dumps(payload)) == payload
    assert "speakers" not in payload
