"""tests/test_dialog_test_manager.py — TestManagerDialog (Tk-marked).

Lightweight construction + tab-switching tests. Heavy mocking of the
underlying helpers (test_discovery, test_scaffold, pr_checklist,
smoke_runner) so we exercise UI orchestration without spawning real
pytest subprocesses.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.test_manager import TestManagerDialog
from helpers.gh_ci_status import (
    NO_RESULT, SUCCESS, UNAVAILABLE, CIStatus)

pytestmark = pytest.mark.tk


# ── Helper: build a dialog with everything mocked to defaults ────────────

def _build_dialog(tk_root, mock_config, tmp_path, mocker,
                  branch=None, ci_status=None):
    """Construct a TestManagerDialog with discovery helpers stubbed.

    The real helpers return live data from the project root; we
    substitute empty-ish lists so the dialog opens cleanly even when
    the test runs from anywhere.
    """
    mocker.patch("dialogs.test_manager.list_test_files",   return_value=[])
    mocker.patch("dialogs.test_manager.scan_coverage_gaps", return_value=[])
    mocker.patch("dialogs.test_manager.detect_stale_tests", return_value=[])
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value={})
    mocker.patch("dialogs.test_manager.load_stale_allowlist",
                 return_value=set())
    # The CI badge shells out to `gh` on a worker thread. tmp_path is not a
    # git repo, so the real _current_branch already returns None and no
    # thread starts — but pin that explicitly so a future change to branch
    # resolution cannot silently start spawning `gh` calls (and leaking
    # worker threads) from every construction test. Badge tests that need a
    # branch re-patch this themselves and join the worker via wait_for.
    mocker.patch("dialogs.test_manager._current_branch", return_value=branch)
    ci_spy = mocker.patch(
        "dialogs.test_manager.get_latest_run_status",
        return_value=ci_status or CIStatus(SUCCESS, branch=branch or "main",
                                           url="https://example/runs/1"))
    dlg = TestManagerDialog(tk_root, str(tmp_path), mock_config)
    dlg._ci_spy = ci_spy          # for badge tests to assert against
    return dlg


# ── Construction + initial state ─────────────────────────────────────────

def test_dialog_constructs_without_error(tk_root, mock_config, tmp_path, mocker):
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog.title().startswith("🧪 Test Manager")


def test_dialog_has_four_tabs(tk_root, mock_config, tmp_path, mocker):
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    # The notebook's tab() count is the public API.
    assert len(dialog._nb.tabs()) == 4


def test_dialog_tab_titles(tk_root, mock_config, tmp_path, mocker):
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    tab_titles = [dialog._nb.tab(i, "text") for i in dialog._nb.tabs()]
    assert any("Run" in t for t in tab_titles)
    assert any("Coverage" in t for t in tab_titles)
    assert any("Stale" in t for t in tab_titles)
    assert any("Scaffold" in t for t in tab_titles)


def test_stop_button_disabled_initially(tk_root, mock_config, tmp_path, mocker):
    """V-G: Stop button starts disabled because no run is in flight."""
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    assert str(dialog._stop_btn.cget("state")) == "disabled"


# ── Tab 1 — Run + View handlers ──────────────────────────────────────────

def test_run_selected_with_no_selection_shows_info(
    tk_root, mock_config, tmp_path, mocker
):
    """Clicking Run Selected with nothing chosen → showinfo, no work."""
    mock_info = mocker.patch("dialogs.test_manager.messagebox.showinfo")
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_run_selected()
    mock_info.assert_called_once()


def test_run_all_invokes_background_helper(
    tk_root, mock_config, tmp_path, mocker
):
    """V-E: Tab 1's Run All delegates to run_pytest_in_background."""
    mock_run = mocker.patch(
        "helpers.smoke_runner.run_pytest_in_background",
        return_value=type("H", (), {"is_alive": lambda self: True,
                                       "cancel": lambda self: None})(),
    )
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_run_all()
    mock_run.assert_called_once()
    # Target was tests/ (whole-suite).
    _args, kwargs = mock_run.call_args
    assert kwargs.get("target") == "tests/"


def test_stop_button_calls_cancel_on_handle(
    tk_root, mock_config, tmp_path, mocker
):
    """V-G: clicking Stop calls .cancel() on the PytestRun handle."""
    fake_handle = mocker.MagicMock()
    fake_handle.is_alive.return_value = True
    mocker.patch("helpers.smoke_runner.run_pytest_in_background",
                 return_value=fake_handle)
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_run_all()
    dialog._on_stop()
    fake_handle.cancel.assert_called_once()


# ── Tab 4 — Scaffold handlers ────────────────────────────────────────────

def test_scaffold_generate_without_source_shows_info(
    tk_root, mock_config, tmp_path, mocker
):
    mock_info = mocker.patch("dialogs.test_manager.messagebox.showinfo")
    mocker.patch("dialogs.test_manager.generate_test_file")
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._scaffold_source_var.set("")     # nothing picked
    dialog._on_scaffold_generate()
    mock_info.assert_called_once()


def test_scaffold_generate_calls_helper(
    tk_root, mock_config, tmp_path, mocker
):
    # Seed a real source file the picker can point at.
    src_dir = tmp_path / "src" / "helpers"
    src_dir.mkdir(parents=True)
    src_file = src_dir / "foo.py"
    src_file.write_text("def foo(): pass\n")

    mock_gen = mocker.patch(
        "dialogs.test_manager.generate_test_file",
        return_value=(True, str(tmp_path / "tests" / "test_foo.py")),
    )
    mocker.patch("dialogs.test_manager.messagebox.showinfo")

    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._scaffold_source_var.set(str(src_file))
    dialog._scaffold_template_var.set("pure_helper")
    dialog._on_scaffold_generate()

    mock_gen.assert_called_once_with(
        str(tmp_path), str(src_file), "pure_helper")


# ── Tab 1 — Sync PR Checklist handler ────────────────────────────────────

def test_sync_pr_checklist_when_no_cache_shows_info(
    tk_root, mock_config, tmp_path, mocker
):
    """No last-run cache → showinfo prompting user to run tests first."""
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value={})
    mock_info = mocker.patch("dialogs.test_manager.messagebox.showinfo")
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_sync_pr_checklist()
    mock_info.assert_called_once()


def test_sync_pr_checklist_delegates_to_helper(
    tk_root, mock_config, tmp_path, mocker
):
    """When cache exists, sync_pr_checklist helper is called."""
    cache = {
        "ran_at": "2026-05-27",
        "results": {
            "tests/test_foo.py": {"passed": 5, "total": 5, "status": "pass"},
        },
    }
    # Build the dialog FIRST (its internal load_last_run_results mock
    # would otherwise override ours). Then re-mock for this specific call.
    mock_sync = mocker.patch(
        "helpers.pr_checklist.sync_pr_checklist",
        return_value=(True, "synced"),
    )
    mocker.patch("dialogs.test_manager.messagebox.showinfo")
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value=cache)
    dialog._on_sync_pr_checklist()
    mock_sync.assert_called_once()


def test_sync_pr_checklist_prefers_summary_over_sum(
    tk_root, mock_config, tmp_path, mocker
):
    """Run All stamps every row with the suite totals; summing those rows
    multiplies the real count by the file count (the 5648/5648 bug). The
    summary block, when present, must win."""
    cache = {
        "ran_at": "2026-05-27",
        "summary": {"passed": 353, "total": 353, "ran_at": "2026-05-27"},
        "results": {
            f"tests/test_{name}.py": {"passed": 353, "total": 353,
                                       "status": "pass"}
            for name in ("foo", "bar", "baz")
        },
    }
    mock_sync = mocker.patch(
        "helpers.pr_checklist.sync_pr_checklist",
        return_value=(True, "synced"),
    )
    mocker.patch("dialogs.test_manager.messagebox.showinfo")
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value=cache)
    dialog._on_sync_pr_checklist()
    _args, _kwargs = mock_sync.call_args
    payload = _args[2]
    assert payload["passed"] == 353      # not 3 × 353 = 1059
    assert payload["total"] == 353


def test_sync_pr_checklist_legacy_cache_sums_per_file_rows(
    tk_root, mock_config, tmp_path, mocker
):
    """Caches without a summary block (single-file runs / pre-fix caches)
    still aggregate by summing the per-file rows."""
    cache = {
        "ran_at": "2026-05-27",
        "results": {
            "tests/test_foo.py": {"passed": 5, "total": 5, "status": "pass"},
            "tests/test_bar.py": {"passed": 2, "total": 3, "status": "fail"},
        },
    }
    mock_sync = mocker.patch(
        "helpers.pr_checklist.sync_pr_checklist",
        return_value=(True, "synced"),
    )
    mocker.patch("dialogs.test_manager.messagebox.showinfo")
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value=cache)
    dialog._on_sync_pr_checklist()
    _args, _kwargs = mock_sync.call_args
    payload = _args[2]
    assert payload["passed"] == 7
    assert payload["total"] == 8


# ── Tab 1 — last-run cache summary block ─────────────────────────────────

def test_run_all_done_writes_suite_summary(
    tk_root, mock_config, tmp_path, mocker
):
    """A whole-suite run records its true totals in cache['summary']."""
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value={})
    mock_save = mocker.patch("dialogs.test_manager.save_last_run_results")
    dialog._on_pytest_done("tests/", 353, 353, "", False, [])
    saved = mock_save.call_args[0][1]
    assert saved["summary"]["passed"] == 353
    assert saved["summary"]["total"] == 353


def test_single_file_done_drops_stale_summary(
    tk_root, mock_config, tmp_path, mocker
):
    """A single-file run after a Run All invalidates the suite snapshot —
    the per-file rows have changed, so the old summary no longer holds."""
    stale = {
        "summary": {"passed": 353, "total": 353, "ran_at": "2026-05-27"},
        "results": {},
    }
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    mocker.patch("dialogs.test_manager.load_last_run_results",
                 return_value=stale)
    mock_save = mocker.patch("dialogs.test_manager.save_last_run_results")
    dialog._on_pytest_done("tests/test_foo.py", 5, 5, "", False, [])
    saved = mock_save.call_args[0][1]
    assert "summary" not in saved


# ── CI badge (Roadmap-9 Phase 2.1) ───────────────────────────────────────

def _dialog_on_branch(tk_root, mock_config, tmp_path, mocker, wait_for,
                      branch="main", status=None):
    """Build a dialog whose badge resolves *branch*, and join the worker.

    The badge query runs on a thread and reports back through after(0), so a
    test must drive the event loop before asserting — and must not leave the
    thread running past teardown, which the tk_root fixture correctly treats
    as a leak.
    """
    dlg = _build_dialog(tk_root, mock_config, tmp_path, mocker,
                        branch=branch, ci_status=status)
    # wait_for drives the event loop, which runs the main-thread pump that
    # applies whatever the worker posted onto the queue.
    wait_for(lambda: dlg._ci_status is not None, timeout_s=3.0)
    return dlg, dlg._ci_spy


def test_ci_badge_renders_the_current_branch(
        tk_root, mock_config, tmp_path, mocker, wait_for):
    """The badge must name the branch it describes, and it must be the
    CURRENT one — showing master's green while your branch is red is worse
    than showing nothing at all."""
    dlg, _ = _dialog_on_branch(tk_root, mock_config, tmp_path, mocker,
                               wait_for, branch="Roadmap-9")
    assert "Roadmap-9" in dlg._ci_var.get()


def test_ci_badge_queries_the_checked_out_branch(
        tk_root, mock_config, tmp_path, mocker, wait_for):
    """Guards against the old backlog hint, which hard-coded master."""
    _, spy = _dialog_on_branch(tk_root, mock_config, tmp_path, mocker,
                               wait_for, branch="Roadmap-9")
    assert spy.call_args[0][2] == "Roadmap-9"


def test_ci_badge_click_opens_the_run(
        tk_root, mock_config, tmp_path, mocker, wait_for):
    opener = mocker.patch("dialogs.test_manager.webbrowser.open")
    dlg, _ = _dialog_on_branch(
        tk_root, mock_config, tmp_path, mocker, wait_for,
        status=CIStatus(SUCCESS, branch="main", url="https://example/runs/7"))
    dlg._on_ci_click()
    opener.assert_called_once_with("https://example/runs/7")


def test_ci_badge_click_is_a_noop_without_a_run(
        tk_root, mock_config, tmp_path, mocker, wait_for):
    """A branch with no runs has nothing to open; clicking must not raise."""
    opener = mocker.patch("dialogs.test_manager.webbrowser.open")
    dlg, _ = _dialog_on_branch(
        tk_root, mock_config, tmp_path, mocker, wait_for,
        status=CIStatus(NO_RESULT, branch="main"))
    dlg._on_ci_click()
    opener.assert_not_called()


def test_detached_head_never_queries_gh(
        tk_root, mock_config, tmp_path, mocker):
    """No branch -> no gh call at all, and a label that says so.

    Also the reason the shared fixture patches the branch to None: without
    a branch there is no worker thread, so construction stays synchronous.
    """
    spy = mocker.patch("dialogs.test_manager.get_latest_run_status")
    dlg = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    spy.assert_not_called()
    assert "no branch" in dlg._ci_var.get()


def test_ci_polling_is_cancelled_on_close(
        tk_root, mock_config, tmp_path, mocker):
    """A pending after() must not fire into a destroyed widget."""
    dlg = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dlg._ci_after_id is not None
    assert dlg._ci_drain_id is not None
    dlg._on_destroy_ci()
    assert dlg._ci_after_id is None
    assert dlg._ci_drain_id is None


def test_unavailable_is_not_styled_as_failure(
        tk_root, mock_config, tmp_path, mocker):
    """"Could not ask gh" and "the build is broken" must not look alike."""
    dlg = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    dlg._apply_ci_status(CIStatus(UNAVAILABLE, branch="main"),
                         "CI unavailable")
    unavailable_fg = dlg._ci_lbl.cget("fg")
    dlg._apply_ci_status(CIStatus("failed", branch="main"), "CI failed")
    assert dlg._ci_lbl.cget("fg") != unavailable_fg
