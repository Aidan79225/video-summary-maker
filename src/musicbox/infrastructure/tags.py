"""AudioTagGateway 的 mutagen 實作：寫 MP3 的 ID3 標題（不重編碼音訊）。"""
from __future__ import annotations

from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3


class MutagenTagGateway:
    def write_title(self, path: str, title: str) -> None:
        audio = MP3(path, ID3=EasyID3)
        if audio.tags is None:
            audio.add_tags()
        audio["title"] = title
        audio.save()
