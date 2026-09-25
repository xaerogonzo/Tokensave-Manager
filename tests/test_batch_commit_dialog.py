"""tests/test_batch_commit_dialog.py — one dialog for a bulk action, honest per row.

The mixed case is the one that finds UI-state bugs: A ready, B blocked, C with
nothing to commit. A must be selectable, B visible with its reason, C not offered
as commit-worthy, and `Commit 1 project` must touch only A.
"""
from __future__ import annotations

import shutil
import subprocess
import types

import pytest

pytestmark = pytest.mark.tk

from dialogs import batch_commit as dlg
from helpers import batch_commit as bcm

GIT = shutil.which("git") or ""
needs_git = pytest.mark.skipif(not GIT, reason="git not installed")

FILES = {"project-baseline.md": "# b\n", "docs/gotchas/a.md": "a\n"}


def run(root, *args):
    return subprocess.run([GIT, "-C", str(root)] + list(args), capture_output=True,
                          text=True, check=True)


def repo(tmp_path, name, ignore=""):
    root = tmp_path / name
    root.mkdir()
    run(root, "init", "-q")
    run(root, "config", "user.email", "t@e")
    run(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    if ignore:
        (root / ".gitignore").write_text(ignore)
    run(root, "add", "-A")
    run(root, "commit", "-q", "-m", "init")
    return root


def item(root):
    dirty = bcm.capture_dirty(GIT, str(root))
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return bcm.BatchItem(str(root), root.name, tuple(FILES),
                         bcm.make_evidence(GIT, str(root), dirty, list(FILES)))


@pytest.fixture
def fleet(tmp_path):
    a = repo(tmp_path, "A")
    b = repo(tmp_path, "B")
    c = repo(tmp_path, "C", ignore="project-baseline.md\ndocs/\n")
    items = (item(a), item(b), item(c))
    (b / ".git" / "MERGE_HEAD").write_text(run(b, "rev-parse", "HEAD").stdout)
    return a, b, c, items


def open_dialog(tk_root, items, wait_for, **kw):
    cfg = types.SimpleNamespace(git_exe=GIT)
    d = dlg.BatchCommitDialog(tk_root, items, cfg, **kw)
    wait_for(lambda: d._inspections, timeout_s=10.0)
    return d


def rows(d):
    return list(d._tree.get_children())


@needs_git
def test_the_mixed_case_lists_a_and_b_and_never_offers_c(tk_root, wait_for, fleet):
    a, b, c, items = fleet
    d = open_dialog(tk_root, items, wait_for)
    try:
        assert rows(d) == [str(a), str(b)]                  # C is not listed
        assert d._tree.set(str(a), "tick") == "☑"
        assert d._tree.set(str(b), "tick") == "–"            # cannot be ticked
        assert "blocked" in d._tree.set(str(b), "status")
        assert str(d._go["text"]) == "Commit 1 project"
        d._tree.selection_set(str(b))
        d.update()
        assert "in progress" in d._files.get("1.0", "end")   # the reason is visible
    finally:
        d.destroy()


@needs_git
def test_commit_touches_only_the_ready_project_and_reports_per_row(
        tk_root, wait_for, fleet, mocker):
    a, b, c, items = fleet
    asked = mocker.patch("tkinter.messagebox.askyesno")
    synced = []
    d = open_dialog(tk_root, items, wait_for,
                    on_committed=lambda path, msg: synced.append(path))
    try:
        d._commit()
        wait_for(lambda: not d._busy and d._results, timeout_s=15.0)

        assert set(run(a, "show", "--name-only", "--format=", "HEAD").stdout.split()) \
            == set(FILES)
        assert run(b, "log", "--oneline").stdout.count("\n") == 1     # untouched
        assert run(c, "log", "--oneline").stdout.count("\n") == 1
        assert list(d._results) == [str(a)]
        assert d._results[str(a)].outcome == bcm.COMMITTED
        assert synced == [str(a)], "sync must follow THAT project's commit only"
        assert not asked.called, "a per-project popup appeared"
        assert "1 committed" in d._summary["text"]
        assert "Nothing was pushed" in d._summary["text"]
    finally:
        d.destroy()


@needs_git
def test_a_worker_exception_cannot_strand_the_button(tk_root, wait_for, fleet,
                                                     mocker):
    a, b, c, items = fleet
    mocker.patch.object(dlg.bcm, "commit_project", side_effect=RuntimeError("boom"))
    d = open_dialog(tk_root, items, wait_for)
    try:
        d._commit()
        wait_for(lambda: not d._busy, timeout_s=15.0)
        assert d._results[str(a)].outcome == bcm.FAILED
        assert "boom" in d._results[str(a)].detail
    finally:
        d.destroy()


@needs_git
def test_a_failed_project_does_not_trigger_a_sync_or_stop_the_next(tk_root,
                                                                    wait_for,
                                                                    tmp_path):
    bad, good = repo(tmp_path, "bad"), repo(tmp_path, "good")
    items = (item(bad), item(good))
    hook = bad / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    synced = []
    d = open_dialog(tk_root, items, wait_for,
                    on_committed=lambda path, msg: synced.append(path))
    try:
        d._commit()
        wait_for(lambda: not d._busy and len(d._results) == 2, timeout_s=20.0)
        assert d._results[str(bad)].outcome == bcm.FAILED
        assert d._results[str(good)].outcome == bcm.COMMITTED
        assert synced == [str(good)]
    finally:
        d.destroy()


@needs_git
def test_an_edited_message_is_kept_as_the_users_and_used(tk_root, wait_for, tmp_path):
    a = repo(tmp_path, "A")
    d = open_dialog(tk_root, (item(a),), wait_for)
    try:
        assert d._source[str(a)] == dlg.SOURCE_RULES
        assert d._msg.get("1.0", "end").startswith("chore(instructions):")
        d._msg.delete("1.0", "end")
        d._msg.insert("1.0", "my own words")
        d._on_edit()
        assert d._source[str(a)] == dlg.SOURCE_USER
        d._commit()
        wait_for(lambda: not d._busy and d._results, timeout_s=15.0)
        assert run(a, "log", "-1", "--format=%s").stdout.strip() == "my own words"
    finally:
        d.destroy()


@needs_git
def test_the_rule_built_message_counts_what_is_really_committed(tk_root, wait_for,
                                                                tmp_path):
    """Planned two lesson files; one is edited while the dialog is open."""
    a = repo(tmp_path, "A")
    d = open_dialog(tk_root, (item(a),), wait_for)
    try:
        (a / "docs" / "gotchas" / "a.md").write_text("edited after the scan\n")
        d._commit()
        wait_for(lambda: not d._busy and d._results, timeout_s=15.0)
        body = run(a, "log", "-1", "--format=%B").stdout
        assert body.startswith("chore(instructions): refresh project instructions")
        assert "shared lessons" not in body
        assert set(run(a, "show", "--name-only", "--format=", "HEAD").stdout.split()) \
            == {"project-baseline.md"}
    finally:
        d.destroy()


@needs_git
def test_nothing_to_commit_says_so_and_offers_nothing(tk_root, wait_for, tmp_path):
    c = repo(tmp_path, "C", ignore="project-baseline.md\ndocs/\n")
    d = open_dialog(tk_root, (item(c),), wait_for)
    try:
        assert rows(d) == []
        assert "Nothing to commit" in d._summary["text"]
        assert str(d._go["text"]) == "Commit 0 projects"
        assert "disabled" in d._go.state()
    finally:
        d.destroy()


def test_use_for_all_makes_every_ticked_message_the_users_own(tk_root, wait_for,
                                                             tmp_path):
    if not GIT:
        pytest.skip("git not installed")
    a, b = repo(tmp_path, "A"), repo(tmp_path, "B")
    d = open_dialog(tk_root, (item(a), item(b)), wait_for)
    try:
        d._msg.delete("1.0", "end")
        d._msg.insert("1.0", "one message for all")
        d._use_for_all()
        assert set(d._messages.values()) == {"one message for all"}
        assert set(d._source.values()) == {dlg.SOURCE_USER}
    finally:
        d.destroy()
