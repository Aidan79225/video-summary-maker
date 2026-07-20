"""FileSystemGateway 的檔案系統實作。"""
from __future__ import annotations

import os


class FilesystemGateway:
    def list_files(self, folder: str) -> list[str]:
        if not os.path.isdir(folder):
            return []
        return [
            f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
        ]

    def rename(self, folder: str, src_name: str, dst_name: str) -> None:
        os.rename(os.path.join(folder, src_name), os.path.join(folder, dst_name))
