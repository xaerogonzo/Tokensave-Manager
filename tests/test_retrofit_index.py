"""tests/test_retrofit_index.py — Retrofit initialises an index, or says why not.

The defect these pin: ``_retrofit_add_tokensave`` only ever edited CLAUDE.md,
and the whole tokensave option short-circuited on "the @include is already
there". Every project the Manager scaffolds *starts* with that @include, so
those projects could never obtain an index through Retrofit. The button
reported "Everything was already up to date — nothing changed" and the Projects
tab answered by telling the user to run Retrofit. A closed loop, escapable only
by dropping to the CLI.

The property that fixes it is not "call init more often". It is that Retrofit
is idempotent with respect to **project state** rather than with respect to
whether some other file happened to change. Two independent steps, each asking
its own question.

The second property is the safety one: ``tokensave init`` discards any index
already present, so only a genuinely absent index may be created without
asking. An index that exists but cannot be read is reported and left alone —
"repairing" it would destroy the thing the user actually wanted back.
"""
from __future__ import annotations

import sqlite3

import pytest

from controllers.scaffold_ctrl import ScaffoldRetrofitController
from helpers.sync_service import InitResult


TOKENSAVE_FLAGS = {
    "tokensave": True,
    "basic_instructions": False,
    "nuitka": False,
    "shadow_links": False,
    "shadow_ext_map": {},
    "git_hook": False,
}


class _Cfg:
    tokensave_exe = "tokensave"
    template_dir = ""
    basic_instructions_template = ""
    baseline_include_line = "@D:/templates/project-baseline.md"


@pytest.fixture
def logged():
    """Captures the (message, colour) pairs the controller emits."""
    return []


@pytest.fixture
def ctrl(logged, mocker):
    """The controller with every Tk dependency replaced.

    ``__init__`` is pure assignment and ``_retrofit_init_index`` touches no
    widget, so a real toplevel would only make this slower and flakier.
    """
    return ScaffoldRetrofitController(
        tab=mocker.MagicMock(),
        cfg=_Cfg(),
        on_log=lambda msg, colour="": logged.append((msg, colour)),
        on_set_running=mocker.MagicMock(),
        on_set_proc=mocker.MagicMock(),
        on_refresh=mocker.MagicMock(),
        on_commit_offer=mocker.MagicMock(),
        on_insert_pending=mocker.MagicMock(),
    )


def _project(tmp_path, *, integrated: bool):
    if integrated:
        (tmp_path / "CLAUDE.md").write_text(
            "@D:/templates/project-baseline.md\n\n# Notes\n", encoding="utf-8")
    return str(tmp_path)


def _index(tmp_path, kind: str):
    d = tmp_path / ".tokensave"
    d.mkdir(exist_ok=True)
    db = str(d / "tokensave.db")
    if kind == "usable":
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE nodes (id TEXT, file_path TEXT, name TEXT)")
        conn.execute("CREATE TABLE edges (source TEXT, target TEXT, kind TEXT)")
        conn.commit()
        conn.close()
    elif kind == "unopenable":
        with open(db, "w", encoding="utf-8") as fh:
            fh.write("not a database")
    elif kind == "drift":
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE nodes (id TEXT)")
        conn.commit()
        conn.close()
    else:                                    # pragma: no cover - guard
        raise ValueError(kind)


def _text(logged):
    return "\n".join(m for m, _ in logged)


# ── The trap ────────────────────────────────────────────────────────────────

def test_index_is_created_even_when_claude_md_is_already_integrated(
        tmp_path, ctrl, mocker):
    """The exact loop. An integrated CLAUDE.md must not suppress the index."""
    init = mocker.patch("controllers.scaffold_ctrl.run_index_init",
                        return_value=InitResult(True, 0, "Initialized", []))
    path = _project(tmp_path, integrated=True)

    actions = ctrl._run_retrofit_steps(path, "Proj", dict(TOKENSAVE_FLAGS))

    init.assert_called_once()
    assert init.call_args.args[0] == path
    assert "Initialised tokensave index" in actions


def test_that_run_reports_an_action_so_the_summary_is_not_nothing_changed(
        tmp_path, ctrl, mocker):
    """``_build_retrofit_summary`` says "nothing changed" for an empty action
    list, which is exactly what the user saw. A real action must reach it."""
    mocker.patch("controllers.scaffold_ctrl.run_index_init",
                 return_value=InitResult(True, 0, "", []))
    actions = ctrl._run_retrofit_steps(_project(tmp_path, integrated=True),
                                       "Proj", dict(TOKENSAVE_FLAGS))
    summary = ScaffoldRetrofitController._build_retrofit_summary("Proj", actions)
    assert "nothing changed" not in summary
    assert "Initialised tokensave index" in summary


def test_a_fresh_project_gets_both_the_include_and_the_index(
        tmp_path, ctrl, mocker):
    init = mocker.patch("controllers.scaffold_ctrl.run_index_init",
                        return_value=InitResult(True, 0, "", []))
    actions = ctrl._run_retrofit_steps(_project(tmp_path, integrated=False),
                                       "Proj", dict(TOKENSAVE_FLAGS))
    init.assert_called_once()
    assert any("CLAUDE.md" in a for a in actions)
    assert "Initialised tokensave index" in actions


# ── Idempotence is about project state, not about file diffs ────────────────

def test_running_retrofit_twice_initialises_exactly_once(tmp_path, ctrl, mocker):
    def fake_init(path, exe, **kw):
        _index(tmp_path, "usable")           # what the real command would do
        return InitResult(True, 0, "", [])

    init = mocker.patch("controllers.scaffold_ctrl.run_index_init",
                        side_effect=fake_init)
    path = _project(tmp_path, integrated=False)

    first = ctrl._run_retrofit_steps(path, "Proj", dict(TOKENSAVE_FLAGS))
    second = ctrl._run_retrofit_steps(path, "Proj", dict(TOKENSAVE_FLAGS))

    assert init.call_count == 1
    assert "Initialised tokensave index" in first
    assert "Initialised tokensave index" not in second


# ── Never destroy an index we merely failed to understand ───────────────────

def test_a_usable_index_is_left_alone(tmp_path, ctrl, logged, mocker):
    init = mocker.patch("controllers.scaffold_ctrl.run_index_init")
    _index(tmp_path, "usable")
    actions = ctrl._retrofit_init_index(_project(tmp_path, integrated=True))
    init.assert_not_called()
    assert actions == []
    assert "already present" in _text(logged)


@pytest.mark.parametrize("kind", ["unopenable", "drift"])
def test_a_broken_index_is_reported_never_silently_rebuilt(
        tmp_path, ctrl, logged, mocker, kind):
    """Re-indexing discards the existing index. A user whose index is corrupt
    may still want it back; a schema we do not recognise is usually a version
    mismatch rather than damage. Both are the user's call, not ours."""
    init = mocker.patch("controllers.scaffold_ctrl.run_index_init")
    _index(tmp_path, kind)
    actions = ctrl._retrofit_init_index(_project(tmp_path, integrated=True))
    init.assert_not_called()
    assert actions == []
    text = _text(logged)
    assert "unusable" in text
    assert "discard" in text, "the user must be told why it was left alone"


# ── Failures are reported, never raised into the retrofit worker ────────────

def test_a_missing_executable_is_reported_not_raised(tmp_path, ctrl, logged, mocker):
    ctrl._cfg = type("C", (), {"tokensave_exe": ""})()
    init = mocker.patch("controllers.scaffold_ctrl.run_index_init")
    actions = ctrl._retrofit_init_index(_project(tmp_path, integrated=True))
    init.assert_not_called()
    assert actions == []
    assert "No tokensave executable" in _text(logged)


def test_a_failed_init_adds_no_action(tmp_path, ctrl, logged, mocker):
    """An action string would put a tick in the summary for something that did
    not happen, and would then offer to commit it."""
    mocker.patch("controllers.scaffold_ctrl.run_index_init",
                 return_value=InitResult(False, 1, "", [], error="exited 1"))
    actions = ctrl._retrofit_init_index(_project(tmp_path, integrated=True))
    assert actions == []
    assert "failed" in _text(logged).lower()


def test_init_is_bounded_so_a_stuck_index_cannot_hang_the_retrofit(
        tmp_path, ctrl, mocker):
    init = mocker.patch("controllers.scaffold_ctrl.run_index_init",
                        return_value=InitResult(True, 0, "", []))
    ctrl._retrofit_init_index(_project(tmp_path, integrated=True))
    assert init.call_args.kwargs.get("timeout"), \
        "an unbounded init can hang the retrofit worker for the session"
