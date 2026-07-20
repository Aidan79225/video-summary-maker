"""重新編號 use case 的單元測試。"""
from __future__ import annotations

import os

from musicbox.domain.entities import RenameMode, RenameOptions
from musicbox.usecases.rename_songs import (
    ApplyRenamePlanUseCase,
    BuildRenamePlanUseCase,
    build_undo_plan,
)

from .fakes import FakeGateway, FakeTagGateway

FOLDER = "/music"


def _mapping(plan):
    return {it.old_name: it.new_name for it in plan.items}


def test_renumber_fills_gaps_and_normalizes_separator():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3", "10  c.mp3", "note.txt"]})
    plan = BuildRenamePlanUseCase(gw).execute(FOLDER, RenameOptions())
    # note.txt 被過濾；依數字排序後連續重編
    assert _mapping(plan) == {
        "02-a.mp3": "01-a.mp3",
        "06-b.mp3": "02-b.mp3",
        "10  c.mp3": "03-c.mp3",
    }


def test_files_without_number_sort_last():
    gw = FakeGateway({FOLDER: ["x.mp3", "01-a.mp3"]})
    plan = BuildRenamePlanUseCase(gw).execute(FOLDER, RenameOptions())
    assert [it.old_name for it in plan.items] == ["01-a.mp3", "x.mp3"]
    assert _mapping(plan)["x.mp3"] == "02-x.mp3"


def test_keep_mode_preserves_numbers_unifies_format():
    gw = FakeGateway({FOLDER: ["05-a.mp3", "02-b.mp3"]})
    opts = RenameOptions(separator=" ", mode=RenameMode.KEEP)
    plan = BuildRenamePlanUseCase(gw).execute(FOLDER, opts)
    assert _mapping(plan) == {"02-b.mp3": "02 b.mp3", "05-a.mp3": "05 a.mp3"}


def test_padding_widens_past_capacity():
    gw = FakeGateway({FOLDER: []})
    uc = BuildRenamePlanUseCase(gw)
    order = [f"song{i}.mp3" for i in range(100)]  # 100 個 → 需 3 位數
    plan = uc.plan_from_order(FOLDER, order, RenameOptions())
    assert plan.items[0].new_name == "001-song0.mp3"
    assert plan.items[99].new_name == "100-song99.mp3"


def test_plan_from_order_respects_manual_order():
    gw = FakeGateway({FOLDER: []})
    uc = BuildRenamePlanUseCase(gw)
    order = ["b.mp3", "a.mp3"]  # 手動順序：b 在前
    plan = uc.plan_from_order(FOLDER, order, RenameOptions())
    assert _mapping(plan) == {"b.mp3": "01-b.mp3", "a.mp3": "02-a.mp3"}


def test_apply_returns_zero_when_nothing_changes():
    gw = FakeGateway({FOLDER: ["01-a.mp3", "02-b.mp3"]})
    build = BuildRenamePlanUseCase(gw)
    plan = build.execute(FOLDER, RenameOptions())
    count = ApplyRenamePlanUseCase(gw).execute(plan)
    assert count == 0
    assert gw.names(FOLDER) == {"01-a.mp3", "02-b.mp3"}


def test_apply_handles_swap_without_collision():
    # 交換兩檔的編號：天真的直接改名會撞名，兩階段改名應安全完成
    gw = FakeGateway({FOLDER: ["02-a.mp3", "01-a.mp3"]})
    build = BuildRenamePlanUseCase(gw)
    # 手動把順序倒過來，逼出「01<->02」的交換
    plan = build.plan_from_order(FOLDER, ["02-a.mp3", "01-a.mp3"], RenameOptions())
    count = ApplyRenamePlanUseCase(gw).execute(plan)
    assert count == 2
    assert gw.names(FOLDER) == {"01-a.mp3", "02-a.mp3"}
    assert not any(n.startswith(".__rename_tmp_") for n in gw.names(FOLDER))


def test_apply_then_undo_restores_original_names():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw)

    plan = build.execute(FOLDER, RenameOptions())
    apply.execute(plan)
    assert gw.names(FOLDER) == {"01-a.mp3", "02-b.mp3"}

    undo = build_undo_plan(plan)
    apply.execute(undo)
    assert gw.names(FOLDER) == {"06-b.mp3", "02-a.mp3"}


def test_plan_from_titles_numbers_and_assembles():
    gw = FakeGateway({FOLDER: []})
    uc = BuildRenamePlanUseCase(gw)
    ordered = [("x.mp3", "告白氣球"), ("y.mp3", "晴天")]
    plan = uc.plan_from_titles(FOLDER, ordered, RenameOptions())
    assert _mapping(plan) == {"x.mp3": "01-告白氣球.mp3", "y.mp3": "02-晴天.mp3"}


def test_plan_from_titles_keep_mode_uses_original_number():
    gw = FakeGateway({FOLDER: []})
    uc = BuildRenamePlanUseCase(gw)
    ordered = [("05-a.mp3", "Alpha")]
    plan = uc.plan_from_titles(FOLDER, ordered, RenameOptions(mode=RenameMode.KEEP))
    assert _mapping(plan) == {"05-a.mp3": "05-Alpha.mp3"}


def test_duplicate_new_names_detects_collisions():
    from musicbox.domain.entities import RenameItem, RenamePlan
    from musicbox.usecases.rename_songs import duplicate_new_names
    plan = RenamePlan(folder=FOLDER, items=(
        RenameItem("a.mp3", "01-x.mp3"),
        RenameItem("b.mp3", "01-x.mp3"),
        RenameItem("c.mp3", "02-y.mp3"),
    ))
    assert duplicate_new_names(plan) == {"01-x.mp3"}


def test_has_illegal_chars():
    from musicbox.usecases.rename_songs import has_illegal_chars
    assert has_illegal_chars("a:b") is True
    assert has_illegal_chars("正常名稱") is False


def test_apply_writes_tags_for_all_items_when_enabled():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())      # 兩檔都會改名
    count = apply.execute(plan, write_tags=True)
    assert count == 2
    assert set(tags.writes) == {
        (os.path.join(FOLDER, "01-a.mp3"), "01-a"),
        (os.path.join(FOLDER, "02-b.mp3"), "02-b"),
    }


def test_apply_writes_tags_without_renaming():
    gw = FakeGateway({FOLDER: ["01-a.mp3", "02-b.mp3"]})   # 已是正確編號
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())
    count = apply.execute(plan, write_tags=True)
    assert count == 0
    assert gw.names(FOLDER) == {"01-a.mp3", "02-b.mp3"}    # 沒改名
    assert set(tags.writes) == {
        (os.path.join(FOLDER, "01-a.mp3"), "01-a"),
        (os.path.join(FOLDER, "02-b.mp3"), "02-b"),
    }


def test_apply_does_not_write_tags_when_disabled():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())
    apply.execute(plan, write_tags=False)
    assert tags.writes == []


def test_undo_writes_tags_with_old_names():
    gw = FakeGateway({FOLDER: ["06-b.mp3", "02-a.mp3"]})
    tags = FakeTagGateway()
    build = BuildRenamePlanUseCase(gw)
    apply = ApplyRenamePlanUseCase(gw, tags)
    plan = build.execute(FOLDER, RenameOptions())
    apply.execute(plan, write_tags=True)
    tags.writes.clear()
    apply.execute(build_undo_plan(plan), write_tags=True)  # 復原
    assert gw.names(FOLDER) == {"06-b.mp3", "02-a.mp3"}
    assert set(tags.writes) == {
        (os.path.join(FOLDER, "06-b.mp3"), "06-b"),
        (os.path.join(FOLDER, "02-a.mp3"), "02-a"),
    }
