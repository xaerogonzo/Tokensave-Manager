"""tests/test_dialog_tokensave_daemon_manager.py — the Stop gate, in the UI.

`helpers/tokensave_daemon.py` enforces the attribution contract at the point
of killing, and its own tests cover that. What is asserted here is that the
dialog does not *offer* what the helper would refuse — a Stop button that
looks live and then errors is a worse experience than one that is visibly
disabled with the reason next to it.

The confirmation wording matters too, and is asserted rather than eyeballed:
for a heuristic row it has to name the project and say the match is a guess,
because that is the only thing standing between the user and stopping the
wrong project's server.

Uses the tk_root / mock_config / wait_for fixtures from conftest.py; the
process and project-discovery boundaries are mocked at the import site.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from helpers.tokensave_daemon import (
    AMBIGUOUS,
    AUTHORITATIVE,
    HEURISTIC,
    UNATTRIBUTED,
    TokensaveServer,
)
import dialogs.tokensave_daemon_manager as tdm
from dialogs.tokensave_daemon_manager import TokensaveDaemonManagerDialog

pytestmark = pytest.mark.tk

PROJ = "D:/work/alpha"


def _server(attribution, pid=100, project=PROJ):
    return TokensaveServer(pid=pid, command_line="tokensave serve",
                           started_at=1000.0, project=project,
                           attribution=attribution, detail="why this state")


def _dialog(tk_root, mock_config, mocker, servers):
    """Build the dialog with discovery + enumeration mocked out."""
    mocker.patch.object(tdm, "find_projects", return_value=[{"path": PROJ}])
    mocker.patch.object(tdm, "list_tokensave_servers", return_value=servers)
    dlg = TokensaveDaemonManagerDialog(tk_root, mock_config)
    dlg.update()                       # let the pump drain the refresh post
    return dlg


def _rows(dlg):
    return dlg._row_widgets


# ── which rows offer a Stop button at all ────────────────────────────────

@pytest.mark.parametrize("attribution,enabled", [
    (AUTHORITATIVE, True),
    (HEURISTIC, True),
    (UNATTRIBUTED, False),
    (AMBIGUOUS, False),
])
def test_stop_is_offered_only_for_identified_servers(
        tk_root, mock_config, mocker, wait_for, attribution, enabled):
    """Unidentified servers must not present an actionable control.

    Stopping one risks killing the server behind somebody's live session
    rather than the one holding the lock you wanted released.
    """
    dlg = _dialog(tk_root, mock_config, mocker, [_server(attribution)])
    try:
        wait_for(lambda: 100 in _rows(dlg))
        state = str(_rows(dlg)[100]["stop_btn"]["state"])
        assert (state == "normal") is enabled, (
            "%s row: expected stop enabled=%s, got state=%r"
            % (attribution, enabled, state))
    finally:
        dlg.destroy()


def test_a_disabled_row_still_explains_itself(tk_root, mock_config, mocker,
                                              wait_for):
    """The reason is the useful part — a bare greyed button reads as a bug."""
    srv = TokensaveServer(pid=100, command_line="", started_at=0.0,
                          attribution=UNATTRIBUTED,
                          detail="no project database was opened at this "
                                 "process's start time")
    dlg = _dialog(tk_root, mock_config, mocker, [srv])
    try:
        wait_for(lambda: 100 in _rows(dlg))
        texts = _label_texts(_rows(dlg)[100]["frame"])
        assert any("no project database" in t for t in texts)
        assert any("unidentified" in t.lower() for t in texts)
    finally:
        dlg.destroy()


def _label_texts(widget) -> list:
    out = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            out.append(str(child.cget("text")))
        out.extend(_label_texts(child))
    return out


# ── the confirmation is where the guess is disclosed ─────────────────────

def test_stopping_a_heuristic_row_warns_that_the_match_is_a_guess(
        tk_root, mock_config, mocker, wait_for):
    ask = mocker.patch.object(tdm.messagebox, "askyesno", return_value=False)
    dlg = _dialog(tk_root, mock_config, mocker, [_server(HEURISTIC)])
    try:
        wait_for(lambda: 100 in _rows(dlg))
        dlg._on_stop(_server(HEURISTIC))
        ask.assert_called_once()
        body = ask.call_args.args[1]
        assert PROJ in body, "the confirmation must name the matched project"
        assert "not proof" in body or "guess" in body.lower()
        assert "different project" in body
    finally:
        dlg.destroy()


def test_declining_the_confirmation_stops_nothing(tk_root, mock_config,
                                                  mocker, wait_for):
    mocker.patch.object(tdm.messagebox, "askyesno", return_value=False)
    stop = mocker.patch.object(tdm, "stop_tokensave_server")
    dlg = _dialog(tk_root, mock_config, mocker, [_server(AUTHORITATIVE)])
    try:
        wait_for(lambda: 100 in _rows(dlg))
        dlg._on_stop(_server(AUTHORITATIVE))
        stop.assert_not_called()
    finally:
        dlg.destroy()


def test_an_unstoppable_row_is_inert_even_if_its_handler_is_reached(
        tk_root, mock_config, mocker, wait_for):
    """Belt and braces: the guard is on the handler, not only the button.

    The button being disabled is a presentation detail; a later refactor
    could re-enable it. The handler refusing is the part that keeps the
    guarantee.
    """
    ask = mocker.patch.object(tdm.messagebox, "askyesno", return_value=True)
    stop = mocker.patch.object(tdm, "stop_tokensave_server")
    dlg = _dialog(tk_root, mock_config, mocker, [_server(AMBIGUOUS)])
    try:
        wait_for(lambda: 100 in _rows(dlg))
        dlg._on_stop(_server(AMBIGUOUS))
        ask.assert_not_called()
        stop.assert_not_called()
    finally:
        dlg.destroy()


# ── the list itself ──────────────────────────────────────────────────────

def test_an_empty_list_says_so_rather_than_rendering_nothing(
        tk_root, mock_config, mocker, wait_for):
    dlg = _dialog(tk_root, mock_config, mocker, [])
    try:
        wait_for(lambda: any("No tokensave servers" in t
                             for t in _label_texts(dlg._list_body)))
    finally:
        dlg.destroy()


def test_every_attribution_state_has_a_badge():
    """A new state must not fall through to an unlabelled row."""
    from helpers import tokensave_daemon as td
    for state in (td.AUTHORITATIVE, td.HEURISTIC, td.UNATTRIBUTED,
                  td.AMBIGUOUS):
        assert state in tdm._BADGE, "no badge defined for %r" % state


def test_there_is_no_stop_all_control(tk_root, mock_config, mocker, wait_for):
    """Deliberate omission, asserted so it is not "helpfully" added later.

    Claude Code respawns its MCP servers, so stopping them all does not
    converge — it just churns the user's live sessions.
    """
    dlg = _dialog(tk_root, mock_config, mocker,
                  [_server(AUTHORITATIVE), _server(AUTHORITATIVE, pid=101)])
    try:
        wait_for(lambda: 101 in _rows(dlg))
        labels = " ".join(_all_button_texts(dlg)).lower()
        assert "stop all" not in labels
    finally:
        dlg.destroy()


def _all_button_texts(widget) -> list:
    from tkinter import ttk
    out = []
    for child in widget.winfo_children():
        if isinstance(child, (ttk.Button, tk.Button)):
            try:
                out.append(str(child.cget("text")))
            except tk.TclError:
                pass
        out.extend(_all_button_texts(child))
    return out
