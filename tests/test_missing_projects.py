"""A repo that leaves every search root must be reported, not dropped."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from helpers import missing_projects as mp


def _p(path, name=None, root="D:/r"):
    return {"path": path, "name": name or os.path.basename(path), "root": root,
            "root_label": "R"}


def test_remember_reports_change_once():
    raw = {}
    assert mp.remember(raw, [_p("D:/r/a")]) is True
    assert mp.remember(raw, [_p("D:/r/a")]) is False


def test_moved_repo_is_listed_as_gone():
    raw = {}
    mp.remember(raw, [_p("D:/r/a"), _p("D:/r/b")])
    rows = mp.find_missing(raw, [_p("D:/r/b")], isdir=lambda p: p == "D:/r")
    assert [(r["name"], r["reason"]) for r in rows] == [("a", mp.GONE)]


def test_unplugged_root_is_not_called_deleted():
    raw = {}
    mp.remember(raw, [_p("E:/r/a", root="E:/r")])
    rows = mp.find_missing(raw, [], isdir=lambda p: False)
    assert rows[0]["reason"] == mp.OFFLINE


def test_existing_folder_outside_roots_is_unlisted():
    raw = {}
    mp.remember(raw, [_p("D:/r/a")])
    assert mp.find_missing(raw, [], isdir=lambda p: True)[0]["reason"] == mp.UNLISTED


def test_spelling_differences_are_one_project():
    raw = {}
    mp.remember(raw, [_p(r"D:\r\a")])
    assert mp.find_missing(raw, [_p("d:/r/a")]) == []


def test_nothing_forgets_except_forget():
    raw = {}
    mp.remember(raw, [_p("D:/r/a")])
    mp.find_missing(raw, [])
    assert mp.forget(raw, "D:/r/a") is True
    assert mp.find_missing(raw, []) == []
    assert mp.forget(raw, "D:/r/a") is False


def test_garbage_memory_is_ignored():
    assert mp.find_missing({"known_projects": "x"}, []) == []
