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

pytestmark = pytest.mark.tk


# ── Helper: build a dialog with everything mocked to defaults ────────────

def _build_dialog(tk_root, mock_config, tmp_path, mocker):
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
    return TestManagerDialog(tk_root, str(tmp_path), mock_config)


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
