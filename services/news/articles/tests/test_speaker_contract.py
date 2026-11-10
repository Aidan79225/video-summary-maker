"""契約檔（GPU 與 Pi 共用的範例）不能悄悄分歧。

Pi 這邊的 articles/tests/fixtures/speaker_contract_v1.json，與 GPU 那邊 <repo>/tests/slidebox_api/fixtures/ 的同名檔案
必須是同一份：兩邊的測試各自拿它當「對方會送來／會收到的樣子」，任何一邊自己改了，契約就名存實亡。

GPU 那邊的檔案不在這個 checkout 裡（兩條線分開開發、還沒合併）就跳過；合併之後這個測試自動開始比對。
換行符號不算分歧：Windows 的 git（core.autocrlf）會把工作目錄的檔案換成 CRLF，內容並沒有不同。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

PI_COPY = Path(__file__).parent / "fixtures" / "speaker_contract_v1.json"
# tests/ → articles/ → news/ → services/ → 專案根目錄
REPO_ROOT = Path(__file__).resolve().parents[4]
GPU_COPY = REPO_ROOT / "tests" / "slidebox_api" / "fixtures" / "speaker_contract_v1.json"


def drift(pi: Path, gpu: Path) -> str | None:
    """兩份契約檔有沒有分歧：沒有是 None，有就回傳一句說明哪裡不同。只有換行符號不同不算。"""
    mine = pi.read_bytes().replace(b"\r\n", b"\n")
    theirs = gpu.read_bytes().replace(b"\r\n", b"\n")
    if mine == theirs:
        return None
    for offset, (a, b) in enumerate(zip(mine, theirs, strict=False)):
        if a != b:
            return f"第 {offset} 個位元組開始不同"
    return f"長度不同（{len(mine)} 對 {len(theirs)} 位元組）"


class DriftHelperTests(SimpleTestCase):
    """比對的函式本身：沒有它們，上面那個「跳過」的測試就算壞了也沒人知道。"""

    def write(self, name: str, data: bytes) -> Path:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="news_contract_")))
        path = folder / name
        path.write_bytes(data)
        return path

    def test_identical_files_do_not_drift(self):
        self.assertIsNone(drift(self.write("a", b'{"a": 1}\n'), self.write("b", b'{"a": 1}\n')))

    def test_line_endings_alone_are_not_drift(self):
        self.assertIsNone(drift(self.write("a", b'{\r\n"a": 1\r\n}\r\n'), self.write("b", b'{\n"a": 1\n}\n')))

    def test_one_changed_byte_is_drift(self):
        self.assertIn("不同", drift(self.write("a", b'{"a": 1}\n'), self.write("b", b'{"a": 2}\n')))

    def test_an_added_line_is_drift(self):
        self.assertIn("長度不同", drift(self.write("a", b'{"a": 1}\n'), self.write("b", b'{"a": 1}\n\n')))

    def test_a_trailing_space_is_drift(self):
        """只放寬換行符號，不放寬別的：多一個空白也是另一份檔案。"""
        self.assertIsNotNone(drift(self.write("a", b'{"a": 1}\n'), self.write("b", b'{"a": 1} \n')))


class ContractCopyTests(SimpleTestCase):
    def test_the_pi_copy_is_valid_json_with_the_four_parts_both_sides_use(self):
        contract = json.loads(PI_COPY.read_text("utf-8"))
        self.assertEqual(sorted(contract), ["_comment", "capabilities", "request", "result"])

    def test_the_path_to_the_gpu_copy_starts_at_the_real_repository_root(self):
        """路徑算錯的話，下面那個測試會永遠「跳過」而沒人發現。"""
        self.assertTrue((REPO_ROOT / "services" / "news" / "manage.py").is_file())
        self.assertEqual(GPU_COPY.relative_to(REPO_ROOT).as_posix(),
                         "tests/slidebox_api/fixtures/speaker_contract_v1.json")

    def test_the_pi_copy_is_where_the_other_tests_look_for_it(self):
        from articles.tests.speaker_support import CONTRACT

        self.assertEqual(CONTRACT, json.loads(PI_COPY.read_text("utf-8")))

    @unittest.skipUnless(GPU_COPY.exists(), "GPU 那邊的契約檔不在這個 checkout 裡（兩條線還沒合併）")
    def test_the_gpu_copy_is_the_same_file(self):
        self.assertIsNone(drift(PI_COPY, GPU_COPY),
                          "Pi 與 GPU 的契約檔分歧了：兩邊要一起改、一起 review，不能只改一邊")
