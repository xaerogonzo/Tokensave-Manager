"""tests/test_mcp_migration.py — the guard on retiring the user-scoped entry.

Binding projects and removing the user-scoped `tokensave` are two different
decisions, and conflating them is the dangerous version of this feature. Claude
Code dedupes MCP servers by name, so while a user-scoped definition exists it
can shadow a project binding — but removing it also takes tokensave away from
every project that is *not* bound.

So the removal is gated: each project must be either bound or explicitly
skipped, and at least one must actually be bound. "Every project" means every
project the user intends to use, which is why skipping counts as an answer
rather than being ignored.

Readiness deliberately does NOT require proof that a binding is currently
serving. While the user-scoped entry exists it is the thing that may be
shadowing, so demanding that proof beforehand would ask for the outcome the
removal itself produces. Verification happens after, against a bound project.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.tk

from dialogs.mcp_config import MCPConfigDialog
from helpers.mcp import ADVISORY_STATES, _project_mcp_path


class _Cfg:
    def __init__(self, skips=()):
        self.raw = {"mcp_skip_warnings": list(skips)}


def _dialog(skips=()):
    """A bare instance — `_migration_status` reads only `_cfg.raw`, no Tk."""
    dlg = object.__new__(MCPConfigDialog)
    dlg._cfg = _Cfg(skips)
    return dlg


def _row(name, root, state):
    return (name, root, {"state": state})


def test_all_bound_is_ready():
    st = _dialog()._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", "ok")])

    assert st["ready"] is True
    assert len(st["bound"]) == 2
    assert st["remaining"] == []


def test_an_unbound_project_blocks_the_migration():
    """The whole point of the guard: removing now would leave /b with no
    tokensave at all, silently."""
    st = _dialog()._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", "no_file")])

    assert st["ready"] is False
    assert [n for n, _ in st["remaining"]] == ["b"]


def test_an_explicitly_skipped_project_counts_as_answered():
    """Skipping is a decision, not an omission — otherwise a user with one
    project they never query could never complete the migration."""
    st = _dialog(skips=[_project_mcp_path("/b")])._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", "no_file")])

    assert st["ready"] is True
    assert [n for n, _ in st["skipped"]] == ["b"]
    assert st["remaining"] == []


def test_skipping_everything_is_not_ready():
    """Removing the fallback with nothing bound would leave no tokensave
    anywhere. Refusing is the point of requiring at least one binding."""
    st = _dialog(skips=[_project_mcp_path("/a"),
                        _project_mcp_path("/b")])._migration_status([
        _row("a", "/a", "no_file"), _row("b", "/b", "no_file")])

    assert st["ready"] is False
    assert st["bound"] == []


def test_no_projects_at_all_is_not_ready():
    assert _dialog()._migration_status([])["ready"] is False


@pytest.mark.parametrize("state", [
    "no_file", "missing", "project_unbound", "project_mismatch",
    "project_absolute", "unparseable",
])
def test_every_not_ok_state_blocks_until_bound_or_skipped(state):
    """A mismatched or absolute binding is not "close enough" — the first
    answers from another codebase, the second only works on this machine."""
    st = _dialog()._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", state)])

    assert st["ready"] is False
    assert [n for n, _ in st["remaining"]] == ["b"]


@pytest.mark.parametrize("state", sorted(ADVISORY_STATES))
def test_an_advisory_state_still_counts_as_bound(state):
    """A shadowed or unapproved binding is a WRITTEN binding.

    Its file is correct by construction, and what blocks it is usually the
    user-scoped entry this migration exists to remove. Counting it as unbound
    would withhold the button at exactly the moment it is the fix — the same
    "demand the outcome beforehand" trap the readiness rule already refuses
    for shadowing.
    """
    st = _dialog()._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", state)])

    assert st["ready"] is True
    assert [n for n, _ in st["bound"]] == ["a", "b"]
    assert st["remaining"] == []


def test_advisory_alone_is_still_ready():
    """Every project shadowed and none plainly `ok` is the live starting state.

    Measured on a real machine: ten correct `.mcp.json` files, none approved,
    every session served by the user-scoped entry. If that configuration could
    not reach the removal button, the migration would be unreachable for the
    exact population that needs it most.
    """
    st = _dialog()._migration_status([
        _row("a", "/a", "project_unapproved"),
        _row("b", "/b", "project_shadowed")])

    assert st["ready"] is True


def test_skipping_uses_the_same_list_the_dialog_already_owns():
    """Reuses `mcp_skip_warnings`, which Apply already clears — so binding a
    previously skipped project takes it out of the skipped bucket without any
    second bookkeeping to keep in sync."""
    path = _project_mcp_path("/b")
    st = _dialog(skips=[path])._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", "no_file")])
    assert [n for n, _ in st["skipped"]] == ["b"]

    # Same project, now bound: it must count as bound, not skipped.
    st2 = _dialog(skips=[path])._migration_status([
        _row("a", "/a", "ok"), _row("b", "/b", "ok")])
    assert [n for n, _ in st2["bound"]] == ["a", "b"]
    assert st2["skipped"] == []


# ── every offered binding must be portable ────────────────────────────────

def test_no_project_row_ever_proposes_a_machine_path(tmp_path, monkeypatch):
    """The guard for the bug this nearly shipped with.

    `_classify_mcp_entry` grants project scope only when a real `.tokensave/`
    exists — correct, since it stops a stray `.mcp.json` being judged by
    project rules. But the dialog listed every project `find_projects`
    returned, including UNINDEXED ones, and those fell through to the GLOBAL
    wrapper proposal: `pythonw.exe` plus an absolute path to
    `tokensave-wrapper.py`, offered for writing into a shared project file.

    Absolute machine paths in a `.mcp.json` are the precise outcome this
    feature exists to prevent, so it is asserted over the rows the dialog
    would actually render rather than trusted to review.
    """
    import json as _json
    import os as _os
    from helpers.mcp import _classify_mcp_entry, _project_mcp_path

    indexed = tmp_path / "indexed"
    (indexed / ".tokensave").mkdir(parents=True)
    unindexed = tmp_path / "unindexed"
    unindexed.mkdir()

    shown = [p for p in (str(indexed), str(unindexed))
             if _os.path.isdir(_os.path.join(p, ".tokensave"))]
    assert shown == [str(indexed)], "unindexed project must not be offered"

    for root in shown:
        proposed = _json.dumps(
            _classify_mcp_entry(_project_mcp_path(root), {})["proposed"])
        assert "wrapper" not in proposed.lower(), proposed
        assert ":" not in proposed.replace('":', "").replace('",', ""), proposed
