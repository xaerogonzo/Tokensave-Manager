"""tests/test_dialog_checks.py — ChecksDialog (Tk-marked).

Verifies the dialog's UI orchestration logic:

* Bottom-bar buttons call the right helpers and update status label
* Pre-push hook toggle flips state correctly
* Checkbox state persists to cfg.raw
* ``_on_run`` populates rows and respects ``_enabled`` toggles

Uses the ``tk_root`` fixture (G-D thread-leak guard) and ``wait_for``
(G-G/G-M event-loop-driving polling) from conftest.py. Subprocess
side-effects are mocked at import-site (G-E).
"""
from __future__ import annotations

import pytest

# Skip the whole module if Tk is not available (e.g. SSH without DISPLAY).
tk = pytest.importorskip("tkinter")

from dialogs.checks_dialog import ChecksDialog

pytestmark = pytest.mark.tk


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_dialog(tk_root, mock_config, project_path, mocker):
    """Construct a ChecksDialog with the standard test seams.

    Returns the dialog. Caller is responsible for invoking handlers
    directly (we never start mainloop in tests).
    """
    # The dialog probes for the pre-push hook at construction time; default
    # the hook to "not installed" unless the test explicitly seeds one.
    log_calls = []
    return ChecksDialog(
        tk_root,
        path=str(project_path),
        cfg=mock_config,
        base="origin/main",
        on_log=lambda msg, colour="": log_calls.append((msg, colour)),
    )


# ── Construction + initial state ─────────────────────────────────────────

def test_dialog_constructs_without_error(tk_root, mock_config, tmp_path, mocker):
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog.title() == "Run checks"
    assert dialog._result_rows.keys() == {"syntax", "pyflakes", "doctor", "claude"}


def test_dialog_seeds_enabled_from_cfg(tk_root, mock_config, tmp_path, mocker):
    """The dialog should reflect cfg.raw['checks_enabled'] on open."""
    mock_config.raw["checks_enabled"] = {
        "syntax":   True,
        "pyflakes": False,    # explicit OFF override
        "doctor":   True,
        "claude":   False,
    }
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog._enabled["pyflakes"] is False
    assert dialog._enabled["syntax"] is True


def test_dialog_hook_state_reflects_existing_installation(
    tk_root, mock_config, tmp_path, mocker
):
    """If a pre-push hook is already installed, the toggle button text
    reflects that on first render."""
    mocker.patch("dialogs.checks_dialog.is_pre_push_hook_installed",
                 return_value=True)
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog._hook_installed is True
    assert "Remove" in dialog._hook_btn.cget("text")


def test_dialog_hook_state_reflects_no_installation(
    tk_root, mock_config, tmp_path, mocker
):
    mocker.patch("dialogs.checks_dialog.is_pre_push_hook_installed",
                 return_value=False)
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog._hook_installed is False
    assert "Install" in dialog._hook_btn.cget("text")


# ── Checkbox toggle persists to cfg ───────────────────────────────────────

def test_toggle_persists_to_cfg_save(tk_root, mock_config, tmp_path, mocker):
    """Clicking a checkbox should call cfg.save() — G-F symmetry check."""
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert mock_config._saved is False

    # Flip pyflakes OFF and invoke the same handler the GUI would.
    dialog._vars["pyflakes"].set(False)
    dialog._on_toggle("pyflakes", dialog._vars["pyflakes"])

    assert mock_config._saved is True
    assert mock_config.raw["checks_enabled"]["pyflakes"] is False


# ── _on_generate_workflow ────────────────────────────────────────────────

def test_generate_workflow_calls_helper_and_updates_status(
    tk_root, mock_config, tmp_path, mocker
):
    """The button handler should delegate to generate_github_workflow
    AND surface success in the status label."""
    mock_gen = mocker.patch(
        "dialogs.checks_dialog.generate_github_workflow",
        return_value=(True, "written: .github/workflows/quality-checks.yml"),
    )
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_generate_workflow()

    mock_gen.assert_called_once_with(str(tmp_path), dialog._enabled)
    label_text = dialog._ci_status_lbl.cget("text")
    assert "✓" in label_text
    assert "written" in label_text


def test_generate_workflow_surfaces_failure(
    tk_root, mock_config, tmp_path, mocker
):
    mocker.patch(
        "dialogs.checks_dialog.generate_github_workflow",
        return_value=(False, "could not create .github/workflows: permission denied"),
    )
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_generate_workflow()

    label_text = dialog._ci_status_lbl.cget("text")
    assert "✗" in label_text
    assert "permission denied" in label_text


# ── _on_toggle_hook — install → remove cycle ─────────────────────────────

def test_toggle_hook_install_path(tk_root, mock_config, tmp_path, mocker):
    """Initial state = not installed → click → install_pre_push_hook called,
    state flips, button label flips."""
    mocker.patch("dialogs.checks_dialog.is_pre_push_hook_installed",
                 return_value=False)
    mock_install = mocker.patch(
        "dialogs.checks_dialog.install_pre_push_hook",
        return_value=(True, "installed"),
    )
    mocker.patch(
        "dialogs.checks_dialog.prepush_runner_script_path",
        return_value="/fake/src/prepush_runner.py",
    )
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog._hook_installed is False

    dialog._on_toggle_hook()

    mock_install.assert_called_once()
    assert dialog._hook_installed is True
    assert "Remove" in dialog._hook_btn.cget("text")
    assert "✓" in dialog._ci_status_lbl.cget("text")


def test_toggle_hook_remove_path(tk_root, mock_config, tmp_path, mocker):
    """Initial state = installed → click → remove_pre_push_hook called,
    state flips."""
    mocker.patch("dialogs.checks_dialog.is_pre_push_hook_installed",
                 return_value=True)
    mock_remove = mocker.patch(
        "dialogs.checks_dialog.remove_pre_push_hook",
        return_value=(True, "removed"),
    )
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog._hook_installed is True

    dialog._on_toggle_hook()

    mock_remove.assert_called_once_with(str(tmp_path))
    assert dialog._hook_installed is False
    assert "Install" in dialog._hook_btn.cget("text")


def test_toggle_hook_install_failure_keeps_state(
    tk_root, mock_config, tmp_path, mocker
):
    """If install fails, _hook_installed must stay False so the user
    can retry without confusion."""
    mocker.patch("dialogs.checks_dialog.is_pre_push_hook_installed",
                 return_value=False)
    mocker.patch(
        "dialogs.checks_dialog.install_pre_push_hook",
        return_value=(False, "permission denied"),
    )
    mocker.patch(
        "dialogs.checks_dialog.prepush_runner_script_path",
        return_value="/fake/runner.py",
    )
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_toggle_hook()

    assert dialog._hook_installed is False
    assert "Install" in dialog._hook_btn.cget("text")
    assert "✗" in dialog._ci_status_lbl.cget("text")


# ── _on_run integration via real threads + wait_for ──────────────────────

def test_on_run_populates_rows_via_real_threads(
    tk_root, mock_config, tmp_path, wait_for, mocker
):
    """_on_run uses ThreadPoolExecutor — workers populate rows via
    self.after(0, ...). wait_for drives the event loop so those updates
    actually fire (G-G/G-M).
    """
    # Patch check implementations to return instantly.
    mocker.patch("dialogs.checks_dialog._check_syntax",
                 return_value=(True, "passed (0 errors)"))
    mocker.patch("dialogs.checks_dialog._check_pyflakes",
                 return_value=(True, "passed (0 warnings)"))
    mocker.patch("dialogs.checks_dialog._check_doctor",
                 return_value=(True, "passed — 0 violations"))

    mock_config.raw["checks_enabled"] = {
        "syntax":   True,
        "pyflakes": True,
        "doctor":   True,
        "claude":   False,
    }
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_run()

    # Poll until all three checks render their pass result.
    def all_passed():
        rows = dialog._result_rows
        return (
            "✓" in rows["syntax"]["icon"].cget("text")
            and "✓" in rows["pyflakes"]["icon"].cget("text")
            and "✓" in rows["doctor"]["icon"].cget("text")
        )

    wait_for(all_passed, timeout_s=2.0)
    # Claude was skipped — should render as "—".
    assert "—" in dialog._result_rows["claude"]["icon"].cget("text")


def test_on_run_failure_renders_red_x(
    tk_root, mock_config, tmp_path, wait_for, mocker
):
    """Failed checks render with ✗ + the summary message."""
    mocker.patch("dialogs.checks_dialog._check_syntax",
                 return_value=(False, "*** SyntaxError: invalid syntax"))
    mocker.patch("dialogs.checks_dialog._check_pyflakes",
                 return_value=(True, "passed (0 warnings)"))
    mocker.patch("dialogs.checks_dialog._check_doctor",
                 return_value=(True, "passed"))

    mock_config.raw["checks_enabled"] = {
        "syntax": True, "pyflakes": True, "doctor": True, "claude": False,
    }
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    dialog._on_run()

    def syntax_failed():
        icon = dialog._result_rows["syntax"]["icon"].cget("text")
        return "✗" in icon
    wait_for(syntax_failed, timeout_s=2.0)

    assert "SyntaxError" in dialog._result_rows["syntax"]["summary"].cget("text")


def test_on_close_cancels_in_flight_checks(
    tk_root, mock_config, tmp_path, mocker
):
    """Closing the dialog before checks finish sets the cancelled event."""
    dialog = _make_dialog(tk_root, mock_config, tmp_path, mocker)
    assert not dialog._cancelled.is_set()
    dialog._on_close()
    assert dialog._cancelled.is_set()
