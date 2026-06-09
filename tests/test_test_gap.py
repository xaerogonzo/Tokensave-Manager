"""Tests for controllers/test_gap_ctrl.py — _GapPanelCtx, TestGapCtrl helpers.

Complements ``tests/test_pr_gap_body_refresh.py`` (which covers the panel render,
``_apply_gap_progress_to_body``, ``_gap_copy_claude_prompt``, ``_show_ai_failures``).
Here we cover the transient state container ``_GapPanelCtx`` and the pure-ish
``_gap_reverify_and_rollback`` extraction (Phase B6) — driven unbound with stub
``self`` objects so no real ``reverify_against_suite`` / pytest run happens.

Tk-marked to match the project convention for the Tk-importing source module.
"""

import pytest
from types import SimpleNamespace

tk = pytest.importorskip("tkinter")

pytestmark = pytest.mark.tk

from controllers.test_gap_ctrl import (
    TestGapCtrl,
    _GapPanelCtx,
    _apply_gap_progress_body,
)


# ── _GapPanelCtx ──────────────────────────────────────────────────────────────

def _make_ctx():
    return _GapPanelCtx(panel="P", dlg="D", path="/proj", base="master",
                        on_tests_written=None)


def test_gap_panel_ctx_initial_state():
    ctx = _make_ctx()
    assert ctx.panel == "P"
    assert ctx.dlg == "D"
    assert ctx.path == "/proj"
    assert ctx.base == "master"
    assert ctx.on_tests_written is None
    assert ctx.check_vars == []
    assert ctx.status_vars == []
    assert ctx.panel_suggestions == []
    # Default True so a panel with no AI backend still builds (see header logic).
    assert ctx._ai_available is True


def test_gap_panel_ctx_ai_available_is_a_real_slot():
    """Regression: `_ai_available` must be assignable (it was missing from
    __slots__, which crashed the header build mid-render)."""
    ctx = _make_ctx()
    ctx._ai_available = False          # must NOT raise AttributeError
    assert ctx._ai_available is False


def test_gap_panel_ctx_has_no_dict():
    """__slots__ class → undeclared attributes are rejected (no __dict__)."""
    ctx = _make_ctx()
    assert not hasattr(ctx, "__dict__")
    with pytest.raises(AttributeError):
        ctx.some_undeclared_attribute = 1


def test_gap_panel_ctx_reset_for_rebuild_clears_state():
    ctx = _make_ctx()
    ctx.check_vars.append("v")
    ctx.status_vars.append("s")
    ctx.panel_suggestions.append("sg")
    ctx.stub_btn = "btn"
    ctx.ai_btn = "btn2"
    ctx.status_var = "sv"

    ctx.reset_for_rebuild()

    assert ctx.check_vars == []
    assert ctx.status_vars == []
    assert ctx.panel_suggestions == []
    assert ctx.stub_btn is None
    assert ctx.ai_btn is None
    assert ctx.status_var is None
    # Immutable wiring is preserved across a rebuild.
    assert ctx.path == "/proj"
    assert ctx.base == "master"


# ── _apply_gap_progress_body (module-level free function) ─────────────────────

def test_apply_gap_progress_body_noop_when_txt_none():
    # A destroyed/closed dialog (txt is None) must be a silent no-op, not a crash.
    _apply_gap_progress_body({"txt": None, "prog": [False]}, ["src/x.py"])


def test_apply_gap_progress_body_noop_on_missing_key():
    # Missing "txt" key → still a silent no-op.
    _apply_gap_progress_body({"prog": [False]}, ["src/x.py"])


# ── _gap_reverify_and_rollback (Phase B6 extraction) ─────────────────────────

def _sugg(rel_path):
    sg = SimpleNamespace(rel_path=rel_path, test_exists=True,
                         test_path=f"tests/test_{rel_path}.py")
    return sg


def test_reverify_no_rollbacks_returns_zero(monkeypatch):
    """All verdicts pass → no deltas, no mutations."""
    monkeypatch.setattr(
        "helpers.test_gen_llm.reverify_against_suite",
        lambda root, paths: {p: "kept" for p in paths})

    logs = []
    stub = SimpleNamespace(_on_log=lambda *a, **k: logs.append(a))
    sg = _sugg("foo")
    gate_relevant = [(0, sg, "tests/test_foo.py", False)]
    written_paths = ["foo"]
    partials, failures = {"foo": "partial"}, {}
    set_calls = []

    dp, df = TestGapCtrl._gap_reverify_and_rollback(
        stub, gate_relevant, cancel_event=None, captured_root="/proj",
        written_paths=written_paths, partials=partials, failures=failures,
        set_row_cb=lambda idx, glyph: set_calls.append((idx, glyph)))

    assert (dp, df) == (0, 0)
    assert written_paths == ["foo"]    # untouched
    assert partials == {"foo": "partial"}
    assert failures == {}
    assert set_calls == []


def test_reverify_rolls_back_failed_test(monkeypatch):
    """A 'rolled_back' verdict → -1 passed / +1 failed and all mutations applied."""
    monkeypatch.setattr(
        "helpers.test_gen_llm.reverify_against_suite",
        lambda root, paths: {"tests/test_foo.py": "rolled_back"})

    logs = []
    stub = SimpleNamespace(_on_log=lambda *a, **k: logs.append(a))
    sg = _sugg("foo")
    gate_relevant = [(3, sg, "tests/test_foo.py", False)]
    written_paths = ["foo", "bar"]
    partials = {"foo": "was partial"}
    failures = {}
    set_calls = []

    dp, df = TestGapCtrl._gap_reverify_and_rollback(
        stub, gate_relevant, cancel_event=None, captured_root="/proj",
        written_paths=written_paths, partials=partials, failures=failures,
        set_row_cb=lambda idx, glyph: set_calls.append((idx, glyph)))

    assert (dp, df) == (-1, 1)
    assert set_calls == [(3, "✗")]          # row marked failed
    assert "foo" not in written_paths       # removed from written
    assert "bar" in written_paths           # other entry kept
    assert "foo" not in partials            # partial report cleared
    assert "foo" in failures                # failure report added
    assert "FAILED in the full" in failures["foo"]
    # Row model reset so a re-scan doesn't point at the deleted test file.
    assert sg.test_exists is False
    assert sg.test_path == ""
    assert logs                             # a rollback was logged


def test_reverify_mixed_verdicts(monkeypatch):
    """Only the rolled-back entry is mutated; the kept one is untouched."""
    monkeypatch.setattr(
        "helpers.test_gen_llm.reverify_against_suite",
        lambda root, paths: {
            "tests/test_foo.py": "rolled_back",
            "tests/test_bar.py": "kept",
        })

    stub = SimpleNamespace(_on_log=lambda *a, **k: None)
    foo, bar = _sugg("foo"), _sugg("bar")
    gate_relevant = [
        (0, foo, "tests/test_foo.py", False),
        (1, bar, "tests/test_bar.py", False),
    ]
    written_paths = ["foo", "bar"]
    failures = {}

    dp, df = TestGapCtrl._gap_reverify_and_rollback(
        stub, gate_relevant, cancel_event=None, captured_root="/proj",
        written_paths=written_paths, partials={}, failures=failures,
        set_row_cb=lambda idx, glyph: None)

    assert (dp, df) == (-1, 1)
    assert written_paths == ["bar"]
    assert list(failures.keys()) == ["foo"]
    assert bar.test_exists is True          # kept entry untouched


# ── TestGapCtrl construction + callback properties ───────────────────────────

def test_ctrl_properties_mirror_callbacks():
    ctrl = TestGapCtrl(
        tab=None, cfg=None, on_log=lambda *a: None,
        get_path=lambda: "/live/path",
        get_test_manager_ref=lambda: "TM")
    assert ctrl._git_path == "/live/path"
    assert ctrl._test_manager_ref == "TM"
    assert ctrl._last_ai_partials == {}
    assert ctrl._last_ai_fail_reports == {}


def test_ctrl_disconnect_clears_callbacks():
    ctrl = TestGapCtrl(
        tab=None, cfg=None, on_log=lambda *a: None,
        get_path=lambda: "/p", get_test_manager_ref=lambda: None)
    ctrl.disconnect()
    assert ctrl.get_path is None
    assert ctrl.get_test_manager_ref is None
