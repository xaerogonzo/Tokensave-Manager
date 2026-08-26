"""tests/test_doctor_desktop_shadow.py — Doctor on Claude Desktop's tokensave.

The check no other binding report can make. ``claude mcp get`` reads
``~/.claude.json`` and never ``claude_desktop_config.json``, so a
Desktop-registered ``tokensave`` was invisible to every tier the Doctor had —
which is how a session spent four queries believing its index was stale while
being answered from another project's graph.

What is tested here is mostly RESTRAINT. The rule has four ways to be wrong
and only one of them is silence:

  * shouting about a Desktop server that is serving the project being
    inspected (correct for that project);
  * shouting about a config entry while Desktop is closed and nothing runs;
  * calling a ``-shm`` guess a confirmed shadow;
  * staying quiet when the entry comes back after a retirement.

This Doctor has already had to be fixed once for nagging, so each of those
gets its own test.
"""
from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from controllers.doctor_ctrl import DoctorController

PROJECT_A = r"D:\Random Projects\OpenChem Studio"
PROJECT_B = r"D:\Claude Co worker\Token Save Manager Source"


class _Srv:
    """Enough of ``TokensaveServer`` for the classifier."""

    def __init__(self, project, selection="pin", is_guess=False, pid=14796):
        self.pid = pid
        self.project = project
        self.selection = selection
        self.attribution = "heuristic" if is_guess else "authoritative"
        self.started_at = 100.0
        self.is_guess = is_guess


def _ctrl(tk_root, mock_config):
    tab = tk.Frame(tk_root)
    logs = []
    ctl = DoctorController(
        tab, mock_config,
        on_log=lambda m, c="": logs.append(m),
        on_set_running=lambda *a: None,
        on_set_proc=lambda *a: None,
    )
    return ctl, logs


def _setup(mocker, ctl, *, present=True, servers=(), retired=False):
    """Point the rule at constructed facts rather than the real machine."""
    mocker.patch("helpers.mcp_desktop.discover_desktop_configs",
                 return_value=[])
    mocker.patch("helpers.mcp_desktop.desktop_entry_present",
                 return_value=present)
    mocker.patch.object(ctl, "_desktop_servers", return_value=list(servers))
    ctl._cfg.raw["mcp_desktop_scope_retired"] = retired


def test_shadow_names_the_project_actually_being_served(tk_root, mock_config,
                                                        mocker):
    """The whole point: say WHICH tree is answering, and how to stop it."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, servers=[_Srv(PROJECT_A)])

    ctl._report_desktop_shadow(PROJECT_B)

    assert len(logs) == 1
    assert PROJECT_A in logs[0]
    assert "14796" in logs[0]
    assert "Retire Desktop tokensave" in logs[0]


def test_silent_when_desktop_serves_the_project_being_inspected(
        tk_root, mock_config, mocker):
    """A running Desktop wrapper is not a fault in itself.

    Inspecting the project it serves must produce nothing at all — a rule that
    fires here would be loudest about the one project that works.
    """
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, servers=[_Srv(PROJECT_A)])

    ctl._report_desktop_shadow(PROJECT_A)

    assert logs == []


def test_silent_when_the_entry_exists_but_nothing_is_running(
        tk_root, mock_config, mocker):
    """Desktop is closed. A config-level fact is not a runtime fault."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, servers=[])

    ctl._report_desktop_shadow(PROJECT_B)

    assert logs == []


def test_silent_when_there_is_no_desktop_entry(tk_root, mock_config, mocker):
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, present=False, servers=[_Srv(PROJECT_A)])

    ctl._report_desktop_shadow(PROJECT_B)

    assert logs == []


def test_a_guess_is_reported_as_unknown_not_as_a_shadow(tk_root, mock_config,
                                                        mocker):
    """`-shm` correlation names a project as a guess. Do not accuse on it."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, servers=[_Srv(PROJECT_A, is_guess=True)])

    ctl._report_desktop_shadow(PROJECT_B)

    assert len(logs) == 1
    assert "could not be established" in logs[0]
    assert PROJECT_A not in logs[0]


def test_claude_code_servers_are_not_desktop_shadows(tk_root, mock_config,
                                                     mocker):
    """Another project's own session must not read as a Desktop shadow."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, servers=[_Srv(PROJECT_A, selection="")])

    ctl._report_desktop_shadow(PROJECT_B)

    assert logs == []


def test_entry_returning_after_retirement_is_reported(tk_root, mock_config,
                                                      mocker):
    """The regression case the intent flag exists to make visible."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, present=True, servers=[], retired=True)

    ctl._report_desktop_shadow(PROJECT_B)

    assert any("come back" in m for m in logs)


def test_retired_and_absent_says_nothing(tk_root, mock_config, mocker):
    ctl, logs = _ctrl(tk_root, mock_config)
    _setup(mocker, ctl, present=False, servers=[], retired=True)

    ctl._report_desktop_shadow(PROJECT_B)

    assert logs == []


def test_a_broken_discovery_never_breaks_the_doctor_run(tk_root, mock_config,
                                                        mocker):
    """Doctor's other findings must survive this rule failing."""
    ctl, logs = _ctrl(tk_root, mock_config)
    mocker.patch("helpers.mcp_desktop.discover_desktop_configs",
                 side_effect=OSError("boom"))

    ctl._report_desktop_shadow(PROJECT_B)      # must not raise

    assert logs == []
