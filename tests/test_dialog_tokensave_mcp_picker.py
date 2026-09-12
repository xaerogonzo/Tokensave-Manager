"""tests/test_dialog_tokensave_mcp_picker.py — agent-wiring picker (Tk-marked).

Covers TokensaveMCPPickerDialog + the doctor-output nag parser.

The load-bearing test here is ``test_runs_one_subprocess_per_agent``: tokensave's
``install`` takes a SINGULAR ``--agent``, unlike codegraph's ``--target=<csv>``.
Anyone "simplifying" the loop into a comma-joined single call would break the
feature silently, and that test is the guard.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.tokensave_mcp_picker import (
    TokensaveMCPPickerDialog,
    build_install_argv,
    describe_hook_changes,
    git_hook_support,
)

pytestmark = pytest.mark.tk


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _no_real_probes(mocker):
    """No test here may run the real tokensave or git.

    The dialog probes the installed version on open and snapshots hook
    routing around a run. Both go through `subprocess`, which several tests
    patch wholesale to count install invocations -- so the probes are stubbed
    at the import site, and the version reads as a supported 7.12.1 unless a
    test says otherwise.
    """
    mocker.patch("dialogs.tokensave_mcp_picker._installed_extractor_version",
                 return_value=("7.12.1", ""))
    mocker.patch("dialogs.tokensave_mcp_picker.snapshot_hook_state",
                 return_value={})


@pytest.fixture
def all_detected(mocker):
    """Treat every agent as installed (G-E: patch at the import site)."""
    mocker.patch("dialogs.tokensave_mcp_picker._tokensave_agent_installed",
                 return_value=True)
    mocker.patch(
        "dialogs.tokensave_mcp_picker._tokensave_agent_destination_path",
        return_value="/fake/path")


# ── argv construction (pure — no Tk needed) ───────────────────────────────

def test_argv_is_singular_agent():
    """--agent takes exactly one id; --git-hook is always pinned."""
    argv = build_install_argv("ts.exe", "claude")
    assert argv == ["ts.exe", "install", "--agent", "claude",
                    "--git-hook", "no"]


def test_argv_git_hook_opt_in_means_global():
    """Since 7.12.0 (#506) `yes` means the CURRENT repository, not global.

    The dialog passes no cwd, so `yes` would install into whichever repo the
    Manager was launched from. `global` is what the checkbox describes.
    """
    argv = build_install_argv("ts.exe", "claude", git_hook=True)
    assert argv[argv.index("--git-hook") + 1] == "global"
    assert "--local" not in argv          # upstream tracks the agent


def test_argv_wildcard_permissions_passthrough():
    argv = build_install_argv("ts.exe", "claude", wildcard_permissions=True)
    assert "--wildcard-permissions" in argv
    argv_off = build_install_argv("ts.exe", "claude")
    assert "--wildcard-permissions" not in argv_off


# ── Construction + initial state ──────────────────────────────────────────

def test_dialog_exposes_all_twenty_agents(tk_root, mock_config, all_detected):
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    assert len(dialog._agent_vars) == 20
    assert "claude" in dialog._agent_vars
    assert "roo-code" in dialog._agent_vars


def test_only_detected_agents_are_prechecked(tk_root, mock_config, mocker):
    mocker.patch("dialogs.tokensave_mcp_picker._tokensave_agent_installed",
                 side_effect=lambda a: a == "claude")
    mocker.patch(
        "dialogs.tokensave_mcp_picker._tokensave_agent_destination_path",
        return_value="/fake/path")
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    assert dialog._selected_agents() == ["claude"]


def test_preselect_overrides_detection(tk_root, mock_config, mocker):
    """Doctor passes the agents it nagged about; those win over detection."""
    mocker.patch("dialogs.tokensave_mcp_picker._tokensave_agent_installed",
                 side_effect=lambda a: a == "claude")
    mocker.patch(
        "dialogs.tokensave_mcp_picker._tokensave_agent_destination_path",
        return_value="/fake/path")
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config,
                                      preselect=["cursor"])
    assert dialog._selected_agents() == ["cursor"]
    # An undetected preselection must reveal the collapsed list, or the user
    # can't see what's ticked.
    assert dialog._show_all is True


# ── _on_install behaviour ─────────────────────────────────────────────────

def _install_dialog(tk_root, mock_config, mocker, patch_after, tmp_path,
                    agents, **flags):
    """Build a dialog with exactly `agents` selected, confirm auto-accepted.

    Returns (dialog, harness).

    Two deliberate choices here:

    * ``patch_after`` is mandatory — the worker thread reports progress via
      ``self.after(0, …)``, which raises "main thread is not in main loop"
      against a real Tk root with no mainloop running. The harness captures
      those into a queue instead.
    * The tokensave binary is a REAL file under ``tmp_path``. Patching
      ``os.path.isfile`` would rebind it on the shared ``os`` module for
      every other module in the process (pytest internals and logging call
      it constantly), which poisons unrelated tests.
    """
    ts_exe = tmp_path / "tokensave.exe"
    ts_exe.write_bytes(b"")
    mocker.patch("dialogs.tokensave_mcp_picker._tokensave_agent_installed",
                 return_value=False)
    mocker.patch(
        "dialogs.tokensave_mcp_picker._tokensave_agent_destination_path",
        return_value="/fake/path")
    mocker.patch("dialogs.tokensave_mcp_picker.messagebox.askyesno",
                 return_value=True)
    mock_config.raw["tokensave_exe"] = str(ts_exe)
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config, preselect=agents)
    for key, val in flags.items():
        getattr(dialog, f"_{key}_var").set(val)
    return dialog, patch_after(dialog)


def test_runs_one_subprocess_per_agent(tk_root, mock_config, mocker,
                                       patch_after, wait_for, tmp_path):
    """REGRESSION GUARD: tokensave has no --target=<csv>. One call per agent.

    If someone collapses the loop into a single comma-joined invocation this
    fails — which is the point.
    """
    run = mocker.patch("dialogs.tokensave_mcp_picker.subprocess.run",
                       return_value=_proc(0))
    dialog, harness = _install_dialog(tk_root, mock_config, mocker,
                                      patch_after, tmp_path,
                                      ["claude", "cursor", "codex"])
    dialog._on_install()
    wait_for(lambda: run.call_count == 3, timeout_s=5)
    harness.drain()

    seen = [c.args[0][c.args[0].index("--agent") + 1] for c in run.call_args_list]
    assert seen == ["claude", "cursor", "codex"]
    # Every invocation names exactly one agent.
    for call in run.call_args_list:
        assert call.args[0].count("--agent") == 1


def test_git_hook_pinned_no_on_every_call(tk_root, mock_config, mocker,
                                          patch_after, wait_for, tmp_path):
    run = mocker.patch("dialogs.tokensave_mcp_picker.subprocess.run",
                       return_value=_proc(0))
    dialog, harness = _install_dialog(tk_root, mock_config, mocker,
                                      patch_after, tmp_path,
                                      ["claude", "cursor"])
    dialog._on_install()
    wait_for(lambda: run.call_count == 2, timeout_s=5)
    harness.drain()

    for call in run.call_args_list:
        argv = call.args[0]
        assert argv[argv.index("--git-hook") + 1] == "no"


def test_git_hook_opt_in_reaches_subprocess(tk_root, mock_config, mocker,
                                            patch_after, wait_for, tmp_path):
    run = mocker.patch("dialogs.tokensave_mcp_picker.subprocess.run",
                       return_value=_proc(0))
    dialog, harness = _install_dialog(tk_root, mock_config, mocker,
                                      patch_after, tmp_path,
                                      ["claude"], git_hook=True)
    dialog._on_install()
    wait_for(lambda: run.call_count == 1, timeout_s=5)
    harness.drain()

    argv = run.call_args_list[0].args[0]
    assert argv[argv.index("--git-hook") + 1] == "global"


def test_one_failing_agent_does_not_abort_the_rest(tk_root, mock_config,
                                                   mocker, patch_after,
                                                   wait_for, tmp_path):
    """Middle agent fails; the third must still run."""
    run = mocker.patch(
        "dialogs.tokensave_mcp_picker.subprocess.run",
        side_effect=[_proc(0), _proc(1, stderr="boom"), _proc(0)])
    dialog, harness = _install_dialog(tk_root, mock_config, mocker,
                                      patch_after, tmp_path,
                                      ["claude", "cursor", "codex"])
    dialog._on_install()
    wait_for(lambda: run.call_count == 3, timeout_s=5)
    harness.drain()

    assert run.call_count == 3
    assert dialog._in_flight is False


def test_declining_confirmation_runs_nothing(tk_root, mock_config, mocker,
                                             patch_after, tmp_path):
    """The pre-run confirmation is the gate on agent-control config edits."""
    run = mocker.patch("dialogs.tokensave_mcp_picker.subprocess.run",
                       return_value=_proc(0))
    dialog, _ = _install_dialog(tk_root, mock_config, mocker, patch_after,
                                tmp_path, ["claude"])
    mocker.patch("dialogs.tokensave_mcp_picker.messagebox.askyesno",
                 return_value=False)
    dialog._on_install()
    run.assert_not_called()


def test_no_selection_shows_info_and_runs_nothing(tk_root, mock_config,
                                                  mocker, all_detected):
    run = mocker.patch("dialogs.tokensave_mcp_picker.subprocess.run",
                       return_value=_proc(0))
    info = mocker.patch("dialogs.tokensave_mcp_picker.messagebox.showinfo")
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    for var in dialog._agent_vars.values():
        var.set(False)
    dialog._on_install()
    info.assert_called_once()
    run.assert_not_called()


# ── Confirmation text ─────────────────────────────────────────────────────

def test_confirm_text_lists_commands_and_files(tk_root, mock_config,
                                               all_detected):
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    text = dialog._confirm_text(["claude"], "ts.exe")
    assert "install --agent claude" in text
    # Claude's hook/permission file isn't the MCP destination — call it out.
    assert "settings.json" in text


def test_confirm_text_warns_about_global_git_hook(tk_root, mock_config,
                                                  all_detected):
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    dialog._git_hook_var.set(True)
    assert "GLOBAL" in dialog._confirm_text(["claude"], "ts.exe")


# ── Global-hook capability and the measured before/after (7.12 / #506) ────

@pytest.mark.parametrize("version,problem,supported,fragment", [
    ("7.12.1", "", True, ""),
    ("7.12.0", "", True, ""),
    ("7.11.1", "", False, "requires tokensave 7.12+ (installed: 7.11.1)"),
    ("", "could not run `tokensave --version` (x)", False,
     "couldn't determine the tokensave version: could not run"),
    ("", "", False, "couldn't determine the tokensave version: tokensave is "
                    "not configured"),
])
def test_git_hook_support_distinguishes_too_old_from_unknown(
        version, problem, supported, fragment):
    ok, reason = git_hook_support(version, problem)
    assert ok is supported
    assert fragment in reason


def test_git_hook_checkbox_disabled_with_reason_when_too_old(
        tk_root, mock_config, all_detected, mocker):
    mocker.patch("dialogs.tokensave_mcp_picker._installed_extractor_version",
                 return_value=("7.11.1", ""))
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    assert "disabled" in str(dialog._git_hook_chk.cget("state"))
    assert "requires tokensave 7.12+" in dialog._hook_reason
    # a var set programmatically still cannot reach the argv or the warning
    dialog._git_hook_var.set(True)
    assert "GLOBAL" not in dialog._confirm_text(["cursor"], "ts.exe")


def test_git_hook_checkbox_disabled_with_reason_when_unknown(
        tk_root, mock_config, all_detected, mocker):
    mocker.patch("dialogs.tokensave_mcp_picker._installed_extractor_version",
                 return_value=("", "could not run `tokensave --version` (x)"))
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    assert "disabled" in str(dialog._git_hook_chk.cget("state"))
    assert "couldn't determine" in dialog._hook_reason


def test_confirm_text_names_every_global_hook_consequence(
        tk_root, mock_config, all_detected):
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    dialog._git_hook_var.set(True)
    text = dialog._confirm_text(["cursor"], "ts.exe")
    assert "core.hooksPath machine-wide" in text
    assert "pre-commit and pre-push" in text
    assert "#545" in text


def test_confirm_text_states_the_installed_agents_consequence_for_claude(
        tk_root, mock_config, all_detected):
    dialog = TokensaveMCPPickerDialog(tk_root, mock_config)
    assert "installed_agents" in dialog._confirm_text(["claude"], "ts.exe")
    assert "installed_agents" not in dialog._confirm_text(["cursor"], "ts.exe")


def test_hook_changes_unticked_means_any_change_is_unexpected():
    before = {"global core.hooksPath": "<unset>"}
    after = {"global core.hooksPath": "C:/ts/hooks"}
    [(line, unexpected)] = describe_hook_changes(before, after, git_hook=False)
    assert unexpected and "<unset> -> C:/ts/hooks" in line


def test_hook_changes_ticked_allows_only_the_global_redirect():
    before = {"global core.hooksPath": "<unset>",
              "repository hook post-commit": "<absent>"}
    after = {"global core.hooksPath": "C:/ts/hooks",
             "repository hook post-commit": "120:1789300000"}
    changes = dict(describe_hook_changes(before, after, git_hook=True))
    assert changes == {
        "global core.hooksPath: <unset> -> C:/ts/hooks": False,
        "repository hook post-commit: <absent> -> 120:1789300000": True,
    }


def test_run_reports_an_unexpected_repository_hook_in_red(
        tk_root, mock_config, mocker, patch_after, wait_for, tmp_path):
    """The install's exit code says nothing about where hooks went."""
    mocker.patch("dialogs.tokensave_mcp_picker.subprocess.run",
                 return_value=_proc(0))
    snaps = iter([{}, {"repository hook post-commit": "120:1"}])
    mocker.patch("dialogs.tokensave_mcp_picker.snapshot_hook_state",
                 side_effect=lambda repo, git: next(snaps))
    dialog, harness = _install_dialog(tk_root, mock_config, mocker,
                                      patch_after, tmp_path, ["cursor"])
    dialog._on_install()
    wait_for(lambda: (harness.drain() or True) and "UNEXPECTED" in
             dialog._log_txt.get("1.0", "end"), timeout_s=5)
    from constants import C
    red = dialog._log_txt.tag_ranges(C["red"])
    assert red and "UNEXPECTED" in dialog._log_txt.get(red[0], red[1])
