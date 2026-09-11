"""設定持久化：round-trip 與各種壞檔的降級。"""
from __future__ import annotations

import json

from slidebox.domain.entities import Settings
from slidebox.infrastructure.settings_repository import JsonSettingsRepository


def _default() -> Settings:
    return Settings(output_dir="C:/default")


def test_round_trip(tmp_path):
    repo = JsonSettingsRepository(str(tmp_path / "s.json"))
    s = Settings(
        output_dir="D:/out",
        model="gemma4:latest",
        ollama_host="http://127.0.0.1:9999",
        max_height=720,
        min_slides=5,
        max_slides=9,
        image_width=960,
        num_ctx=8192,
        char_budget=5000,
        subtitle_langs=("en", "zh"),
        whisper_model="small",
    )
    repo.save(s)
    assert repo.load(_default()) == s


def test_subtitle_langs_comes_back_as_tuple(tmp_path):
    """JSON 只有 list，載回來必須還原成 tuple，否則與預設值比較會不相等。"""
    repo = JsonSettingsRepository(str(tmp_path / "s.json"))
    repo.save(Settings(output_dir="D:/out", subtitle_langs=("zh-TW", "en")))
    assert repo.load(_default()).subtitle_langs == ("zh-TW", "en")


def test_missing_file_returns_default(tmp_path):
    repo = JsonSettingsRepository(str(tmp_path / "nope.json"))
    assert repo.load(_default()) == _default()


def test_corrupt_file_returns_default(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ not json", encoding="utf-8")
    assert JsonSettingsRepository(str(p)).load(_default()) == _default()


def test_partial_file_falls_back_per_field(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"output_dir": "E:/only"}), encoding="utf-8")
    loaded = JsonSettingsRepository(str(p)).load(_default())
    assert loaded.output_dir == "E:/only"
    assert loaded.model == _default().model
    assert loaded.num_ctx == _default().num_ctx


def test_empty_subtitle_langs_falls_back_to_default(tmp_path):
    """空的偏好語言清單會讓字幕挑軌永遠失敗，退回預設值才有用。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"subtitle_langs": []}), encoding="utf-8")
    assert JsonSettingsRepository(str(p)).load(_default()).subtitle_langs == (
        _default().subtitle_langs
    )


def test_non_list_subtitle_langs_falls_back_to_default(tmp_path):
    """手改壞的設定檔不該讓 app 拿到怪型別。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"subtitle_langs": "zh-TW"}), encoding="utf-8")
    assert JsonSettingsRepository(str(p)).load(_default()).subtitle_langs == (
        _default().subtitle_langs
    )


def test_top_level_non_dict_returns_default(tmp_path):
    """JSON 合法但不是物件（例如一個陣列）時整份退回預設值。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert JsonSettingsRepository(str(p)).load(_default()) == _default()
