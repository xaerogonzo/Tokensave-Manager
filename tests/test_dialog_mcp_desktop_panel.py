"""tests/test_dialog_mcp_desktop_panel.py — the Desktop panel actually renders.

The rest of this feature is covered by pure tests, which is where the logic
belongs — but the existing MCP dialog tests all use ``object.__new__`` and
never build a widget, so nothing exercised the renderer itself. A panel that
raises inside ``_render`` takes the whole MCP dialog down with it, and the
states that matter most here (a regression notice, a disabled Apply) are the
ones a developer is least likely to have open while working.

Rendered against a stub host rather than the real dialog: every method under
test needs only ``_body``, ``_cfg`` and ``_migration_status``, and building a
real ``MCPConfigDialog`` would drag in project discovery, ``grab_set`` and a
modal-bearing constructor to test four labels and a button.
"""
from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from dialogs.mcp_desktop_panel import DesktopMigrationMixin

PROJECT_A = r"D:\Random Projects\OpenChem Studio"
PROJECT_B = r"D:\Claude Co worker\Token Save Manager Source"


class _Srv:
    def __init__(self, project, selection="pin"):
        self.pid = 14796
        self.project = project
        self.selection = selection
        self.attribution = "authoritative"
        self.started_at = 100.0
        self.is_guess = False


class _Host(DesktopMigrationMixin):
    """The collaborators the panel reads, and nothing else."""

    def __init__(self, tk_root, cfg, servers=None, desktop_running=None):
        self._body = tk.Frame(tk_root)
        self._cfg = cfg
        self._servers = servers
        self._desktop_running = desktop_running
        self._shadow_scanning = False
        self.rendered = 0

    def _migration_status(self, rows):
        return {"ready": True, "bound": [("a", "b"), ("c", "d")]}

    def _render(self):
        self.rendered += 1

    def _log_to_app(self, *a, **kw):
        pass

    def texts(self):
        """Every label string currently in the panel."""
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    out.append(child.cget("text"))
                walk(child)

        walk(self._body)
        return out


def _facts(mocker, *, present=True, running=False, retired=False):
    mocker.patch("helpers.mcp_desktop.discover_desktop_configs",
                 return_value=[])
    mocker.patch("helpers.mcp_desktop.desktop_entry_present",
                 return_value=present)
    mocker.patch("helpers.mcp_desktop.desktop_app_running",
                 return_value=(running, "detail"))


def test_panel_renders_nothing_when_there_is_no_entry(tk_root, mock_config,
                                                      mocker):
    """A machine that never had this problem is not shown a migration."""
    _facts(mocker, present=False)
    host = _Host(tk_root, mock_config, servers=[])

    host._render_desktop_migration([])

    assert host.texts() == []


def test_panel_names_the_served_project(tk_root, mock_config, mocker):
    _facts(mocker)
    host = _Host(tk_root, mock_config, servers=[_Srv(PROJECT_A)],
                 desktop_running=(True, "d"))

    host._render_desktop_migration([])
    blob = "\n".join(host.texts())

    assert "Claude Desktop defines its own tokensave" in blob
    assert PROJECT_A in blob
    assert "only one project at a time" in blob      # the structural note


def test_panel_says_it_is_checking_before_the_scan_lands(tk_root, mock_config,
                                                         mocker):
    """`None` means "not scanned yet" and must not read as "nothing running"."""
    _facts(mocker)
    started = {}
    mocker.patch.object(DesktopMigrationMixin, "_start_shadow_scan",
                        lambda self: started.setdefault("yes", True))
    host = _Host(tk_root, mock_config, servers=None)

    host._render_desktop_migration([])

    assert any("checking which project" in t for t in host.texts())
    assert started == {"yes": True}


def test_apply_is_disabled_while_desktop_is_running(tk_root, mock_config,
                                                    mocker):
    """The hard gate has to be visible as a disabled control, not just refused."""
    _facts(mocker, running=True)
    host = _Host(tk_root, mock_config, servers=[_Srv(PROJECT_A)],
                 desktop_running=(True, "detail"))

    host._render_desktop_migration([])

    buttons = []

    def walk(w):
        for child in w.winfo_children():
            if child.winfo_class() == "TButton":
                buttons.append(child)
            walk(child)

    walk(host._body)
    retire = [b for b in buttons if "Retire" in b.cget("text")]
    assert retire and "disabled" in retire[0].state()
    assert any("Quit Claude Desktop first" in t for t in host.texts())


def test_apply_is_enabled_when_desktop_is_closed(tk_root, mock_config, mocker):
    _facts(mocker, running=False)
    host = _Host(tk_root, mock_config, servers=[_Srv(PROJECT_A)],
                 desktop_running=(False, "closed"))

    host._render_desktop_migration([])

    assert not any("Quit Claude Desktop first" in t for t in host.texts())


def test_retired_state_offers_undo(tk_root, mock_config, mocker):
    _facts(mocker, present=False)
    mock_config.raw["mcp_desktop_scope_retired"] = True
    host = _Host(tk_root, mock_config, servers=[])

    host._render_desktop_migration([])

    assert any("is retired" in t for t in host.texts())


def test_returned_entry_renders_the_regression_notice(tk_root, mock_config,
                                                      mocker):
    """Entry present AND previously retired — the state the flag exists for."""
    _facts(mocker, present=True, running=False)
    mock_config.raw["mcp_desktop_scope_retired"] = True
    host = _Host(tk_root, mock_config, servers=[_Srv(PROJECT_A)],
                 desktop_running=(True, "d"))

    host._render_desktop_migration([])

    assert any("come BACK" in t for t in host.texts())


def test_rendering_never_spawns_the_process_check(tk_root, mock_config, mocker):
    """`_render` runs on the Tk thread; asking costs a subprocess.

    An earlier version called `desktop_app_running()` from inside the render
    path. It stalled the dialog, and when the enumeration failed it printed
    "could not determine whether Claude Desktop is running" directly beside a
    line asserting Desktop was closed.
    """
    mocker.patch("helpers.mcp_desktop.discover_desktop_configs",
                 return_value=[])
    mocker.patch("helpers.mcp_desktop.desktop_entry_present",
                 return_value=True)
    asked = mocker.patch("helpers.mcp_desktop.desktop_app_running",
                         return_value=(False, "closed"))
    host = _Host(tk_root, mock_config, servers=[_Srv(PROJECT_A)],
                 desktop_running=(False, "closed"))

    host._render_desktop_migration([])

    assert asked.call_count == 0


def test_scan_result_is_applied_and_triggers_one_rerender(tk_root, mock_config):
    host = _Host(tk_root, mock_config, servers=None)
    host._shadow_scanning = True

    host._apply_shadow_scan([_Srv(PROJECT_A)])

    assert host._shadow_scanning is False
    assert host.rendered == 1
