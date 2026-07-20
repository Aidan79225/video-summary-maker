"""設定持久化的單元測試。"""
from __future__ import annotations

from musicbox.domain.entities import DownloadFormat, RenameMode, Settings
from musicbox.infrastructure.settings_repository import JsonSettingsRepository


def _default():
    return Settings(output_dir="/music", rename_folder="/music")


def test_round_trip(tmp_path):
    path = str(tmp_path / "settings.json")
    repo = JsonSettingsRepository(path)
    saved = Settings(
        output_dir="/dl",
        rename_folder="/songs",
        fmt=DownloadFormat.MP3,
        max_height=1080,
        separator=" ",
        mode=RenameMode.KEEP,
        padding=3,
    )
    repo.save(saved)
    loaded = repo.load(_default())
    assert loaded == saved


def test_missing_file_returns_default(tmp_path):
    repo = JsonSettingsRepository(str(tmp_path / "nope.json"))
    assert repo.load(_default()) == _default()


def test_corrupt_file_returns_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")
    repo = JsonSettingsRepository(str(path))
    assert repo.load(_default()) == _default()


def test_partial_file_falls_back_per_field(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"padding": 4}', encoding="utf-8")
    repo = JsonSettingsRepository(str(path))
    loaded = repo.load(_default())
    assert loaded.padding == 4
    assert loaded.output_dir == "/music"  # 其餘沿用預設
    assert loaded.fmt == DownloadFormat.MP4
