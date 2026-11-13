"""程式碼的指紋：把原始碼本身雜湊進存檔的指紋，讀不到原始碼就讓指紋永遠對不上舊存檔。

這裡只測性質，不釘任何一個雜湊值：改了程式，指紋一定跟著變；換行方式不同的同一份程式，指紋不變；
讀不到原始碼時不能 raise，而且不能對得上任何存檔（不管是這個行程還是別的行程留下的）。
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from slidebox.usecases import code_fingerprint as cf

SRC = str(Path(__file__).resolve().parents[2] / "src")
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _module(tmp_path, monkeypatch, name: str, text: str) -> str:
    """在 tmp_path 底下寫一個可以 import 的模組（不真的 import 它），回傳它的名稱。"""
    (tmp_path / f"{name}.py").write_bytes(text.encode("utf-8"))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return name


# --- 換行與開頭的 BOM ---


@pytest.mark.parametrize("variant", ["a = 1\r\nb = 2\r\n", "a = 1\rb = 2\r", "﻿a = 1\nb = 2\n",
                                     "﻿a = 1\r\nb = 2\r\n"], ids=["crlf", "cr", "bom", "bom+crlf"])
def test_line_endings_and_a_byte_order_mark_do_not_change_the_digest(variant):
    """攔的 bug：同一份程式在 Windows（CRLF）與 Linux 容器（LF）簽出來，指紋不一樣，
    換一台機器或重建映像，存檔就全部作廢。"""
    assert cf.source_digest(variant) == cf.source_digest("a = 1\nb = 2\n")


def test_any_other_edit_changes_the_digest():
    base = cf.source_digest("LIMIT = 3\n\ndef check(x):\n    return x < LIMIT\n")
    assert HEX16.match(base)
    assert cf.source_digest("LIMIT = 4\n\ndef check(x):\n    return x < LIMIT\n") != base
    assert cf.source_digest("LIMIT = 3\n\ndef check(x):\n    return x <= LIMIT\n") != base
    assert cf.source_digest("LIMIT = 3\n\ndef check(x):\n    return x < LIMIT\n\n") != base     # 多一個空行也算
    assert cf.source_digest("LIMIT = 3\n\ndef check(x):\n  return x < LIMIT\n") != base         # 縮排不同也算


# --- 一個模組、一個檔案 ---


def test_editing_a_constant_in_a_module_changes_its_fingerprint(tmp_path, monkeypatch):
    name = _module(tmp_path, monkeypatch, "fp_rules_a", "LIMIT = 3\n")
    before = cf.code_fingerprint([name])
    assert HEX16.match(before) and cf.code_fingerprint([name]) == before

    _module(tmp_path, monkeypatch, "fp_rules_a", "LIMIT = 4\n")

    assert cf.code_fingerprint([name]) != before


def test_editing_a_function_in_a_module_changes_its_fingerprint(tmp_path, monkeypatch):
    name = _module(tmp_path, monkeypatch, "fp_rules_b", "def ok(x):\n    return x > 1\n")
    before = cf.code_fingerprint([name])

    _module(tmp_path, monkeypatch, "fp_rules_b", "def ok(x):\n    return x >= 1\n")

    assert cf.code_fingerprint([name]) != before


def test_the_same_module_written_with_crlf_has_the_same_fingerprint(tmp_path, monkeypatch):
    text = "LIMIT = 3\n\n\ndef ok(x):\n    return x < LIMIT\n"
    name = _module(tmp_path, monkeypatch, "fp_rules_c", text)
    lf = cf.code_fingerprint([name])

    _module(tmp_path, monkeypatch, "fp_rules_c", text.replace("\n", "\r\n"))

    assert cf.code_fingerprint([name]) == lf


def test_a_fingerprint_covers_every_module_it_is_given_and_not_their_order(tmp_path, monkeypatch):
    first = _module(tmp_path, monkeypatch, "fp_rules_d", "A = 1\n")
    second = _module(tmp_path, monkeypatch, "fp_rules_e", "B = 1\n")
    both = cf.code_fingerprint([first, second])
    assert cf.code_fingerprint([second, first]) == both
    assert cf.code_fingerprint([first]) != both and cf.code_fingerprint([second]) != both

    _module(tmp_path, monkeypatch, "fp_rules_e", "B = 2\n")

    assert cf.code_fingerprint([first, second]) != both


def test_a_file_is_fingerprinted_by_its_text_and_not_by_where_it_lives(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    text = "print('worker')\n"
    (tmp_path / "one" / "worker.py").write_bytes(text.encode())
    (tmp_path / "two" / "worker.py").write_bytes(text.replace("\n", "\r\n").encode())

    one = cf.code_fingerprint(files=[str(tmp_path / "one" / "worker.py")])
    two = cf.code_fingerprint(files=[str(tmp_path / "two" / "worker.py")])

    assert HEX16.match(one) and one == two
    (tmp_path / "two" / "worker.py").write_bytes(b"print('another worker')\n")
    assert cf.code_fingerprint(files=[str(tmp_path / "two" / "worker.py")]) != one


def test_a_module_and_a_file_with_the_same_text_do_not_collide(tmp_path, monkeypatch):
    name = _module(tmp_path, monkeypatch, "fp_rules_f", "A = 1\n")
    assert cf.code_fingerprint([name]) != cf.code_fingerprint(files=[str(tmp_path / "fp_rules_f.py")])


# --- 讀不到原始碼：不 raise，而且對不上任何存檔 ---


def test_a_module_that_cannot_be_found_gives_a_fingerprint_instead_of_an_error():
    missing = "slidebox.usecases.module_that_does_not_exist"
    first = cf.code_fingerprint([missing])
    assert HEX16.match(first) and cf.code_fingerprint([missing]) == first
    assert first != cf.code_fingerprint(["slidebox.usecases.speakers"])


def test_a_file_that_cannot_be_read_gives_a_fingerprint_instead_of_an_error(tmp_path):
    missing = str(tmp_path / "nothing.py")
    assert HEX16.match(cf.code_fingerprint(files=[missing]))
    (tmp_path / "binary.py").write_bytes(b"\xff\xfe\x00\x81 not text")
    assert HEX16.match(cf.code_fingerprint(files=[str(tmp_path / "binary.py")]))
    assert HEX16.match(cf.code_fingerprint(files=[str(tmp_path)]))                 # 是資料夾，不是檔案
    assert cf.file_digest(missing).startswith("unreadable:")


@pytest.mark.parametrize("failure", [
    OSError("source is gone"), ImportError("no loader"), ValueError("bad spec"), UnicodeDecodeError(
        "utf-8", b"\xff", 0, 1, "bad"), RuntimeError("anything")],
    ids=lambda e: type(e).__name__)
def test_whatever_goes_wrong_while_reading_a_source_the_fingerprint_still_comes_out(monkeypatch, failure):
    readable = cf.code_fingerprint(["slidebox.usecases.speakers"])

    def broken(name):
        raise failure

    monkeypatch.setattr(cf, "read_module_source", broken)

    unreadable = cf.code_fingerprint(["slidebox.usecases.speakers"])

    assert HEX16.match(unreadable) and unreadable != readable
    assert cf.code_fingerprint(["slidebox.usecases.speakers"]) == unreadable      # 同一個行程裡是穩定的


def test_a_source_that_is_not_available_at_all_is_unreadable_too(monkeypatch):
    """只有 .pyc、沒有原始碼的安裝：loader 回 None。"""
    readable = cf.code_fingerprint(["slidebox.usecases.speakers"])
    monkeypatch.setattr(cf, "read_module_source", lambda name: None)
    assert cf.code_fingerprint(["slidebox.usecases.speakers"]) != readable
    assert cf.module_digest("slidebox.usecases.speakers").startswith("unreadable:")


def test_an_unreadable_source_never_looks_like_a_readable_one(monkeypatch):
    """讀不到的不是「某一份程式的雜湊」：不管原始碼是空的、是名字還是標記本身，都對不上。"""
    monkeypatch.setattr(cf, "read_module_source", lambda name: None)
    marker = cf.module_digest("anything")
    assert not HEX16.match(marker)
    for text in ("", "anything", marker, "unreadable"):
        assert cf.source_digest(text) != marker


def test_the_unreadable_marker_is_different_in_every_process():
    """攔的 bug：讀不到原始碼時用固定的字串，另一個同樣讀不到的行程（或之前留下的存檔）
    就會對上它——「讀不到」變成一個可以被沿用的版本。"""
    code = ("from slidebox.usecases import code_fingerprint as cf\n"
            "print(cf.unreadable_marker('x'))\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    runs = [subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                           timeout=60, check=True).stdout.strip() for _ in range(2)]
    assert runs[0] != runs[1]
    assert cf.unreadable_marker("x") not in runs
    assert all(run.startswith("unreadable:x:") for run in runs)


def test_the_marker_names_what_could_not_be_read_so_the_cause_can_be_found():
    assert "slidebox.usecases.names" in cf.unreadable_marker("slidebox.usecases.names")
