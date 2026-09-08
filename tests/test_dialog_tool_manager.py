"""tests/test_dialog_tool_manager.py — ToolManagerDialog (Tk-marked).

Verifies the v4.8 + v4.11 dialog's lifecycle orchestration:

* Construction renders two tool rows (tokensave + codegraph)
* ``_refresh_state`` reflects install + MCP wiring state correctly
* Codegraph install pipeline calls helper + sets cfg.codegraph_exe + saves
* Codegraph uninstall cascade calls MCP cleanup FIRST then npm uninstall
* G-G: button disabled before worker spawns (lifecycle ordering)
* G-F: cfg.save() called after every cfg mutation

Real threads + ``wait_for`` (G-G / G-M) — see conftest.py for the
fixture rationale.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.tool_manager import ToolManagerDialog

pytestmark = pytest.mark.tk


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── Construction + initial state ─────────────────────────────────────────

def test_dialog_constructs_with_three_rows(tk_root, mock_config, mocker):
    """The dialog should render a row per tool without crashing.

    A ratcheted set rather than a count, so adding a tool is a visible act
    and so the failure names which row appeared or vanished.
    """
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    assert set(dialog._tool_widgets.keys()) == {"tokensave", "codegraph", "pyscope"}
    assert set(dialog._row_busy.keys()) == {"tokensave", "codegraph", "pyscope"}


class TestPyScopeRow:
    """The status-only row.

    PyScope is a uv tool over a local checkout, not a package this manager
    can fetch, so its row carries no lifecycle actions. The properties worth
    holding are that it renders no dead buttons, that Locate stays usable
    precisely when the row says "not installed", and that the machinery
    written for rows WITH buttons does not fall over on a row without them.
    """

    def _dialog(self, tk_root, mock_config, mocker, status=None):
        import helpers.pyscope as ps
        mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
        mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
        mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                     return_value=(False, ""))
        mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                            return_value=False)
        mocker.patch.object(ps, "status",
                            return_value=status or ps.Status())
        return ToolManagerDialog(tk_root, mock_config)

    def test_the_row_offers_no_lifecycle_buttons(self, tk_root, mock_config, mocker):
        """Three greyed-out buttons would claim a feature that never existed."""
        dialog = self._dialog(tk_root, mock_config, mocker)
        widgets = dialog._tool_widgets["pyscope"]
        for key in ("install_btn", "update_btn", "uninstall_btn"):
            assert key not in widgets
        assert "locate_btn" in widgets

    def test_locate_stays_enabled_while_the_row_reports_absent(
            self, tk_root, mock_config, mocker):
        """Saying where the binary is IS the remedy for "not installed".

        Gating Locate on `installed`, the way Update and Uninstall are
        gated, would disable it in exactly the state it exists to fix.
        """
        import tkinter as tk
        dialog = self._dialog(tk_root, mock_config, mocker)
        assert str(dialog._tool_widgets["pyscope"]["locate_btn"]["state"]) == str(tk.NORMAL)

    def test_unhealthy_is_not_rendered_as_absent(self, tk_root, mock_config, mocker):
        """The row keeps "broken" and "missing" apart, in words and colour."""
        import helpers.pyscope as ps
        from constants import C
        broken = ps.Status(configured=r"C:\bin\pyscope.exe", executable=True,
                           state=ps.STATE_UNHEALTHY,
                           detail="pyscope version exited 2: ImportError")
        dialog = self._dialog(tk_root, mock_config, mocker, status=broken)
        label = dialog._tool_widgets["pyscope"]["bin_lbl"]
        assert "not installed" not in label.cget("text")
        assert "ImportError" in label.cget("text")
        assert label.cget("fg") == C["yellow"]

    def test_absent_offers_the_install_command_on_the_second_line(
            self, tk_root, mock_config, mocker):
        dialog = self._dialog(tk_root, mock_config, mocker)
        second = dialog._tool_widgets["pyscope"]["mcp_lbl"].cget("text")
        assert "uv tool install" in second

    def test_the_second_line_claims_nothing_about_mcp_wiring(
            self, tk_root, mock_config, mocker):
        """Nothing in this build writes PyScope's MCP entry.

        A wiring verdict here would be a claim about something no code has
        looked at — the other two rows earn theirs by reading ~/.claude.json.
        """
        import helpers.pyscope as ps
        healthy = ps.Status(configured="x", executable=True,
                            state=ps.STATE_OK, version="0.1.0")
        dialog = self._dialog(tk_root, mock_config, mocker, status=healthy)
        second = dialog._tool_widgets["pyscope"]["mcp_lbl"].cget("text")
        assert "MCP" not in second

    def test_set_row_busy_survives_a_row_with_no_lifecycle_buttons(
            self, tk_root, mock_config, mocker):
        """_row_busy machinery was written for rows that have buttons."""
        dialog = self._dialog(tk_root, mock_config, mocker)
        dialog._set_row_busy("pyscope", True)
        dialog._set_row_busy("pyscope", False)

    def test_locate_persists_the_choice_and_refreshes(
            self, tk_root, mock_config, mocker, tmp_path):
        """G-F: every cfg mutation is followed by save + refresh_derived."""
        exe = tmp_path / "pyscope.exe"
        exe.write_text("", encoding="utf-8")
        dialog = self._dialog(tk_root, mock_config, mocker)
        mocker.patch("dialogs.tool_manager.filedialog.askopenfilename",
                     return_value=str(exe))
        dialog._on_locate("pyscope")
        assert mock_config.raw["pyscope_exe"] == str(exe)
        assert mock_config._saved is True

    def test_the_dialog_is_tall_enough_for_every_row_it_builds(
            self, tk_root, mock_config, mocker):
        """The minimum size must cover what the content actually asks for.

        Adding the third row broke this and nothing caught it. The row was
        built, its LabelFrame border drew, and its children never mapped —
        so it rendered as an empty titled box. The geometry oracle could
        not see it either: it SKIPS unmapped widgets, because a widget may
        be legitimately hidden, so five clipped children were counted as
        "5 unmapped" rather than reported as findings.

        Asserted as an invariant rather than a pixel count, so a fourth row
        fails this instead of quietly clipping again.
        """
        dialog = self._dialog(tk_root, mock_config, mocker)
        dialog.update_idletasks()
        required = dialog.winfo_reqheight()
        floor = dialog.minsize()[1]
        assert floor >= required, (
            f"minsize height {floor} is below the {required} its own content "
            f"requests; the last row will be laid out with no room and its "
            f"children will not map"
        )

    def test_cancelling_locate_changes_nothing(
            self, tk_root, mock_config, mocker):
        dialog = self._dialog(tk_root, mock_config, mocker)
        mocker.patch("dialogs.tool_manager.filedialog.askopenfilename",
                     return_value="")
        dialog._on_locate("pyscope")
        assert mock_config.raw["pyscope_exe"] == ""
        assert mock_config._saved is False


def test_initial_state_shows_not_installed_when_paths_empty(
    tk_root, mock_config, mocker
):
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    ts_text = dialog._tool_widgets["tokensave"]["bin_lbl"].cget("text")
    cg_text = dialog._tool_widgets["codegraph"]["bin_lbl"].cget("text")
    assert "not installed" in ts_text
    assert "not installed" in cg_text


def test_install_button_enabled_when_not_installed(
    tk_root, mock_config, mocker
):
    """Button enablement: install ENABLED when binary absent, update/uninstall
    DISABLED. This is the inverse of when the binary IS present."""
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    w = dialog._tool_widgets["codegraph"]
    # ttk Buttons reflect state via .instate / cget
    assert str(w["install_btn"].cget("state")) == "normal"
    assert str(w["update_btn"].cget("state")) == "disabled"
    assert str(w["uninstall_btn"].cget("state")) == "disabled"


def test_install_button_disabled_when_already_installed(
    tk_root, mock_config, mocker, tmp_path
):
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(True, "codegraph"))
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    w = dialog._tool_widgets["codegraph"]
    assert str(w["install_btn"].cget("state")) == "disabled"
    assert str(w["update_btn"].cget("state")) == "normal"
    assert str(w["uninstall_btn"].cget("state")) == "normal"


# ── _set_row_busy (G-G) ──────────────────────────────────────────────────

def test_set_row_busy_disables_all_three_buttons(
    tk_root, mock_config, mocker
):
    """G-G: synchronously disabling buttons BEFORE spawning a worker
    is the single defence against double-click races."""
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    dialog._set_row_busy("codegraph", True)

    w = dialog._tool_widgets["codegraph"]
    assert str(w["install_btn"].cget("state")) == "disabled"
    assert str(w["update_btn"].cget("state")) == "disabled"
    assert str(w["uninstall_btn"].cget("state")) == "disabled"
    assert dialog._row_busy["codegraph"] is True


def test_set_row_busy_restores_on_false(tk_root, mock_config, mocker):
    """Symmetric: passing busy=False restores normal labels + state."""
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    dialog._set_row_busy("codegraph", True, "Install")
    dialog._set_row_busy("codegraph", False)

    w = dialog._tool_widgets["codegraph"]
    assert dialog._row_busy["codegraph"] is False
    assert w["install_btn"].cget("text") == "Install"
    assert w["update_btn"].cget("text") == "Update"
    assert w["uninstall_btn"].cget("text") == "Uninstall"


# ── Codegraph install pipeline ───────────────────────────────────────────

def test_codegraph_install_sets_cfg_and_saves(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """End-to-end: click Install → worker calls install_codegraph,
    detect_codegraph_after_install finds the binary, cfg.raw is updated,
    cfg.save() is called (G-F).

    Worker threads call ``self.after(0, ...)`` — Tk's createcommand is
    NOT thread-safe without a running mainloop, so we patch_after on the
    dialog instance to redirect those into the AfterHarness queue. The
    cfg mutations happen INSIDE the worker thread (before the after()
    calls), so we can assert them directly without draining the harness.
    """
    found_path = str(tmp_path / "codegraph.cmd")
    (tmp_path / "codegraph.cmd").write_bytes(b"")

    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="/fake/npm.cmd")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch("dialogs.tool_manager.install_codegraph",
                 return_value=(True, "added 1 package"))
    mocker.patch("dialogs.tool_manager.detect_codegraph_after_install",
                 return_value=found_path)
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)

    dialog = ToolManagerDialog(tk_root, mock_config)
    patch_after(dialog)         # G-A: redirect after() into the harness
    dialog._install_codegraph()

    # Worker writes cfg.raw + calls cfg.save() inside its try block,
    # BEFORE the finally's after() calls. Poll for the saved flag.
    wait_for(lambda: mock_config._saved, timeout_s=3.0)

    assert mock_config.raw["codegraph_exe"] == found_path
    assert mock_config._saved is True


def test_codegraph_install_refuses_without_npm(
    tk_root, mock_config, mocker
):
    """Install button click without npm → messagebox.showerror, no work."""
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    mock_install = mocker.patch("dialogs.tool_manager.install_codegraph")
    mock_err = mocker.patch("dialogs.tool_manager.messagebox.showerror")

    dialog = ToolManagerDialog(tk_root, mock_config)
    dialog._install_codegraph()

    mock_err.assert_called_once()
    mock_install.assert_not_called()


# ── Codegraph uninstall cascade ──────────────────────────────────────────

def test_codegraph_uninstall_cascade_order(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """Cascade: MCP cleanup (codegraph uninstall) MUST be called BEFORE
    npm uninstall. Verifies the order via call-sequence inspection.
    """
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)

    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="/fake/npm.cmd")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(True, "codegraph"))
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    mocker.patch("tkinter.messagebox.askyesno", return_value=True)

    # Track call sequence by appending labels to a list.
    call_order: list = []
    mocker.patch(
        "dialogs.tool_manager.subprocess.run",
        side_effect=lambda *a, **kw: (
            call_order.append("codegraph_uninstall"),
            _proc(rc=0),
        )[1],
    )
    mock_npm_uninstall = mocker.patch(
        "dialogs.tool_manager.uninstall_codegraph",
        side_effect=lambda *a, **kw: (
            call_order.append("npm_uninstall"),
            (True, ""),
        )[1],
    )

    dialog = ToolManagerDialog(tk_root, mock_config)
    patch_after(dialog)
    dialog._uninstall_codegraph()

    # Both cascade steps happen inside the worker BEFORE the finally's
    # after() calls. Wait for both labels to land in call_order.
    wait_for(lambda: "npm_uninstall" in call_order, timeout_s=3.0)

    # Cascade order assertion — MCP step (subprocess.run) before npm step.
    assert "codegraph_uninstall" in call_order
    assert "npm_uninstall" in call_order
    assert call_order.index("codegraph_uninstall") < call_order.index("npm_uninstall")
    mock_npm_uninstall.assert_called_once()


def test_codegraph_uninstall_clears_cfg_and_saves(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """After cascade succeeds: cfg.raw['codegraph_exe'] cleared + save() called."""
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)

    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="/fake/npm.cmd")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(True, "codegraph"))
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    mocker.patch("tkinter.messagebox.askyesno", return_value=True)
    mocker.patch("dialogs.tool_manager.subprocess.run", return_value=_proc(rc=0))
    mocker.patch("dialogs.tool_manager.uninstall_codegraph",
                 return_value=(True, ""))

    dialog = ToolManagerDialog(tk_root, mock_config)
    patch_after(dialog)
    dialog._uninstall_codegraph()
    wait_for(lambda: mock_config.raw["codegraph_exe"] == "", timeout_s=3.0)

    assert mock_config.raw["codegraph_exe"] == ""
    assert mock_config._saved is True


def test_codegraph_uninstall_declined_does_nothing(
    tk_root, mock_config, mocker, tmp_path
):
    """User clicks No on the confirmation → no work, cfg untouched."""
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)

    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="/fake/npm.cmd")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(True, "codegraph"))
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    mocker.patch("tkinter.messagebox.askyesno", return_value=False)
    mock_uninstall = mocker.patch("dialogs.tool_manager.uninstall_codegraph")

    dialog = ToolManagerDialog(tk_root, mock_config)
    dialog._uninstall_codegraph()
    mock_uninstall.assert_not_called()
    assert mock_config.raw["codegraph_exe"] == str(cg_exe)  # untouched


def test_codegraph_uninstall_continues_when_mcp_cleanup_fails(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """G-D non-fatal: MCP cleanup (codegraph uninstall) returns rc=1 →
    log a warning, CONTINUE with the binary uninstall."""
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)

    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="/fake/npm.cmd")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(True, "codegraph"))
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    mocker.patch("tkinter.messagebox.askyesno", return_value=True)
    # MCP step fails…
    mocker.patch("dialogs.tool_manager.subprocess.run",
                 return_value=_proc(rc=1, stderr="cleanup failed"))
    # …but binary uninstall still succeeds
    mock_npm = mocker.patch("dialogs.tool_manager.uninstall_codegraph",
                            return_value=(True, ""))

    dialog = ToolManagerDialog(tk_root, mock_config)
    patch_after(dialog)
    dialog._uninstall_codegraph()
    wait_for(lambda: mock_npm.called, timeout_s=3.0)

    # Despite MCP failure, npm uninstall still ran AND cfg was cleared.
    mock_npm.assert_called_once()
    assert mock_config.raw["codegraph_exe"] == ""
    assert mock_config._saved is True


# ── Codegraph update ─────────────────────────────────────────────────────

def test_codegraph_update_calls_update_helper(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)

    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="/fake/npm.cmd")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch("dialogs.tool_manager.codegraph_version", return_value="1.4.2")
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    mock_update = mocker.patch("dialogs.tool_manager.update_codegraph",
                                return_value=(True, ""))

    dialog = ToolManagerDialog(tk_root, mock_config)
    patch_after(dialog)
    dialog._update_codegraph()
    wait_for(lambda: mock_update.called, timeout_s=3.0)
    mock_update.assert_called_once()


# ── Agent-wiring buttons (tokensave row only) ────────────────────────────

def _bare_dialog(tk_root, mock_config, mocker):
    """Dialog with both tools absent — enough to exercise the row plumbing."""
    mocker.patch("dialogs.tool_manager._detect_codegraph", return_value="")
    mocker.patch("dialogs.tool_manager._detect_npm", return_value="")
    mocker.patch("dialogs.tool_manager._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mocker.patch.object(ToolManagerDialog, "_tokensave_mcp_wired",
                        return_value=False)
    return ToolManagerDialog(tk_root, mock_config)


def test_wiring_buttons_exist_on_tokensave_row_only(tk_root, mock_config,
                                                    mocker):
    """Codegraph keeps its own picker in Settings — no buttons on its row."""
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    assert "wire_btn" in dialog._tool_widgets["tokensave"]
    assert "refresh_btn" in dialog._tool_widgets["tokensave"]
    assert "wire_btn" not in dialog._tool_widgets["codegraph"]
    assert "refresh_btn" not in dialog._tool_widgets["codegraph"]


def test_set_row_busy_survives_row_without_wiring_buttons(tk_root, mock_config,
                                                          mocker):
    """REGRESSION: the shared button loops must not KeyError on codegraph.

    _tool_widgets now holds different keys per row. Any loop that indexes
    button keys blindly blows up here.
    """
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    for tool_id in ("codegraph", "tokensave"):
        dialog._set_row_busy(tool_id, True, "Refresh")
        dialog._set_row_busy(tool_id, False)
    dialog._refresh_state()   # also loops button keys


def test_refresh_agents_aborts_when_binary_missing(tk_root, mock_config,
                                                   mocker):
    mock_config.raw["tokensave_exe"] = ""
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    err = mocker.patch("dialogs.tool_manager.messagebox.showerror")
    run = mocker.patch("dialogs.tool_manager.subprocess.run")
    dialog._on_refresh_agents()
    err.assert_called_once()
    run.assert_not_called()


def test_refresh_agents_runs_bare_reinstall(tk_root, mock_config, mocker,
                                            patch_after, wait_for, tmp_path):
    """`reinstall` takes no --agent: it refreshes everything already wired."""
    ts_exe = tmp_path / "tokensave.exe"
    ts_exe.write_bytes(b"")
    mock_config.raw["tokensave_exe"] = str(ts_exe)
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    patch_after(dialog)
    mocker.patch("dialogs.tool_manager.messagebox.askyesno", return_value=True)
    run = mocker.patch("dialogs.tool_manager.subprocess.run",
                       return_value=_proc(0, stdout="ok"))
    dialog._on_refresh_agents()
    wait_for(lambda: run.called, timeout_s=3.0)
    # Assert on the reinstall invocation itself, not on the LAST call. The
    # worker posts _refresh_state through the UI pump, and since the pump
    # really runs it, a `--version` probe lands after the reinstall.
    argvs = [c.args[0] for c in run.call_args_list]
    assert [str(ts_exe), "reinstall"] in argvs, argvs


def test_refresh_agents_declined_runs_nothing(tk_root, mock_config, mocker,
                                              tmp_path):
    ts_exe = tmp_path / "tokensave.exe"
    ts_exe.write_bytes(b"")
    mock_config.raw["tokensave_exe"] = str(ts_exe)
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    mocker.patch("dialogs.tool_manager.messagebox.askyesno", return_value=False)
    run = mocker.patch("dialogs.tool_manager.subprocess.run")
    dialog._on_refresh_agents()
    run.assert_not_called()


# ── Daemon-management button (codegraph row only) ────────────────────────

def test_daemons_button_exists_on_codegraph_row_only(tk_root, mock_config,
                                                     mocker):
    """Codegraph gets daemon management; tokensave gets agent wiring —
    neither row should pick up the other's buttons."""
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    assert "daemons_btn" in dialog._tool_widgets["codegraph"]
    assert "daemons_btn" not in dialog._tool_widgets["tokensave"]
    assert "wire_btn" not in dialog._tool_widgets["codegraph"]


def test_set_row_busy_survives_three_different_button_sets(tk_root,
                                                           mock_config,
                                                           mocker):
    """REGRESSION: install/update/uninstall + wire/refresh (tokensave) +
    daemons (codegraph) are three different key sets on _tool_widgets now.
    Any loop indexing button keys blindly blows up here."""
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    for tool_id in ("codegraph", "tokensave"):
        dialog._set_row_busy(tool_id, True, "Refresh")
        dialog._set_row_busy(tool_id, False)
    dialog._refresh_state()


def test_manage_daemons_aborts_when_binary_missing(tk_root, mock_config,
                                                   mocker):
    mock_config.raw["codegraph_exe"] = ""
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    err = mocker.patch("dialogs.tool_manager.messagebox.showerror")
    dialog._on_manage_codegraph_daemons()
    err.assert_called_once()


def test_manage_daemons_opens_dialog_when_binary_present(tk_root,
                                                         mock_config, mocker,
                                                         tmp_path):
    cg_exe = tmp_path / "codegraph.cmd"
    cg_exe.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg_exe)
    dialog = _bare_dialog(tk_root, mock_config, mocker)
    opened = mocker.patch(
        "dialogs.codegraph_daemon_manager.CodegraphDaemonManagerDialog")
    dialog._on_manage_codegraph_daemons()
    opened.assert_called_once_with(dialog, dialog._cfg)
