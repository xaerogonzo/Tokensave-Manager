"""tests/test_pin_watcher.py — retiring a stale server, and refusing to.

The watcher exists because Claude Desktop reads the active-project pin only
when it starts its tokensave server, so "Set as Active" does nothing to a
running Desktop. It fixes that by ending the stale server and letting
Desktop's supervisor start a fresh one.

That means its normal operation is **killing a live MCP server**, so most of
what is asserted here is restraint: which servers it will not touch, and under
what conditions it does nothing at all. A watcher that is merely eager is
worse than no watcher, because it would churn sessions belonging to work the
user never asked it to interrupt.

`tick()` is deliberately separable from the polling loop so every decision can
be tested without threads or sleeping.
"""
from __future__ import annotations

import os
import sys

import pytest

from helpers.proc_kill import ProcessIdentity
from helpers.tokensave_daemon import (
    AUTHORITATIVE,
    HEURISTIC,
    UNATTRIBUTED,
    TokensaveServer,
)
import controllers.pin_watcher as pw
from controllers.pin_watcher import ENABLED_KEY, PinWatcherController

PROJ_A = "D:/work/alpha"
PROJ_B = "D:/work/beta"


class _Cfg:
    """Minimal stand-in — the watcher reads raw, search_roots, tokensave_exe."""

    def __init__(self, enabled=True):
        self.raw = {ENABLED_KEY: enabled}
        self.search_roots = []
        self.tokensave_exe = "tokensave.exe"


def _watcher(mocker, *, enabled=True, pin=PROJ_B, last=PROJ_A):
    mocker.patch.object(pw, "_read_pin", return_value=pin)
    watcher = PinWatcherController(cfg=_Cfg(enabled), on_log=lambda *a: None)
    watcher._last_pin = last
    return watcher


def _server(pid=100, project=PROJ_A, attribution=AUTHORITATIVE):
    return TokensaveServer(
        pid=pid, command_line='tokensave.exe serve -p "%s"' % project,
        started_at=1000.0, project=project, attribution=attribution,
        identity=ProcessIdentity(pid=pid, created_at=555, image="tokensave.exe"))


def _wired(mocker, servers, records):
    """Mock the two sources `_wrapper_servers` consults."""
    mocker.patch.object(pw, "read_wrapper_records", return_value=records)
    mocker.patch("helpers.tokensave_daemon.list_tokensave_servers",
                 return_value=servers)
    mocker.patch("helpers.project_discovery.find_projects", return_value=[])


# ── the thing it is for ──────────────────────────────────────────────────

def test_a_changed_pin_retires_the_server_left_on_the_old_project(mocker):
    kill = mocker.patch.object(pw, "kill_process", return_value=(True, "done"))
    watcher = _watcher(mocker)
    _wired(mocker, [_server(project=PROJ_A)],
           {100: {"pid": 100, "project": PROJ_A}})

    assert watcher.tick() == [100]
    assert kill.call_args.args[0] == 100


def test_the_kill_is_identity_checked_and_not_a_tree_kill(mocker):
    """A recycled PID must be refused, and a server has no tree to reap."""
    kill = mocker.patch.object(pw, "kill_process", return_value=(True, "done"))
    watcher = _watcher(mocker)
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    watcher.tick()

    assert kill.call_args.kwargs["expect"] is not None
    assert kill.call_args.kwargs["tree"] is False
    assert kill.call_args.kwargs["graceful"] is True


def test_a_server_already_on_the_new_pin_is_left_alone(mocker):
    """Nothing to fix — restarting it would cost a session for no reason."""
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker, pin=PROJ_B)
    _wired(mocker, [_server(project=PROJ_B)],
           {100: {"pid": 100, "project": PROJ_B}})

    assert watcher.tick() == []
    kill.assert_not_called()


def test_a_path_spelled_differently_is_the_same_project(mocker):
    """A trailing separator and a `.` hop do not make it a different project.

    Only equivalences that hold on BOTH platforms are asserted here. Case and
    backslash-vs-slash are Windows-only facts — on Linux `/work/beta` and
    `/WORK/BETA` are two different directories — and asserting them
    unconditionally is what failed the Linux gate.
    """
    kill = mocker.patch.object(pw, "kill_process")
    spelled_differently = os.path.join(PROJ_B, ".") + os.sep
    watcher = _watcher(mocker, pin=spelled_differently)
    _wired(mocker, [_server(project=PROJ_B)],
           {100: {"pid": 100, "project": PROJ_B}})

    assert watcher.tick() == []
    kill.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="case- and separator-insensitivity is Windows-only")
def test_windows_treats_case_and_separator_variants_as_one_project(mocker):
    """The other half, where it is actually true.

    `os.path.normcase` folds case and rewrites separators on Windows and is
    the identity on POSIX, which is the correct behaviour on both — so this
    is a platform fact worth pinning, not a portability gap to paper over.
    """
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker, pin=PROJ_B.replace("/", "\\").upper())
    _wired(mocker, [_server(project=PROJ_B)],
           {100: {"pid": 100, "project": PROJ_B}})

    assert watcher.tick() == []
    kill.assert_not_called()


# ── restraint ────────────────────────────────────────────────────────────

def test_nothing_happens_while_the_setting_is_off(mocker):
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker, enabled=False)
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    assert watcher.tick() == []
    kill.assert_not_called()


def test_enabling_it_does_not_fire_on_a_change_that_happened_while_off(mocker):
    """The pin is tracked even when disabled.

    Otherwise switching the setting on would immediately retire a server over
    a pin change from an hour ago, which is not what "apply changes from now
    on" means to anyone.
    """
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker, enabled=False, pin=PROJ_B, last=PROJ_A)
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    watcher.tick()                      # while off: absorbs the change
    watcher._cfg.raw[ENABLED_KEY] = True
    assert watcher.tick() == []         # now on: nothing new to react to
    kill.assert_not_called()


def test_an_unchanged_pin_does_nothing(mocker):
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker, pin=PROJ_A, last=PROJ_A)
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    assert watcher.tick() == []
    kill.assert_not_called()


def test_clearing_the_pin_retires_nothing(mocker):
    """With no pin the wrapper falls back to most-recently-indexed.

    That is not better than whatever is already running, so churning a live
    session to reach it would be a pure loss.
    """
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker, pin="", last=PROJ_A)
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    assert watcher.tick() == []
    kill.assert_not_called()


def test_a_server_without_a_wrapper_record_is_never_touched(mocker):
    """Almost certainly a Claude Code session.

    Those bind themselves per project and have nothing to do with the pin;
    ending one would interrupt work in a different project entirely.
    """
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker)
    _wired(mocker, [_server(project=PROJ_A)], {})       # no records at all

    assert watcher.tick() == []
    kill.assert_not_called()


def test_only_the_recorded_server_is_retired_among_several(mocker):
    kill = mocker.patch.object(pw, "kill_process", return_value=(True, "ok"))
    watcher = _watcher(mocker)
    _wired(mocker,
           [_server(pid=100, project=PROJ_A),
            _server(pid=200, project=PROJ_A)],
           {100: {"pid": 100, "project": PROJ_A}})       # 200 is not ours

    assert watcher.tick() == [100]
    assert kill.call_count == 1


@pytest.mark.parametrize("attribution", [HEURISTIC, UNATTRIBUTED])
def test_a_server_that_is_not_authoritative_is_never_retired(mocker,
                                                             attribution):
    """A pin change is not new evidence about an unproven attribution.

    The four-state contract exists so a guess never becomes a kill, and this
    watcher must not be the hole in it.
    """
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker)
    _wired(mocker, [_server(attribution=attribution)],
           {100: {"pid": 100, "project": PROJ_A}})

    assert watcher.tick() == []
    kill.assert_not_called()


def test_a_record_with_no_project_is_not_actionable(mocker):
    """The wrapper found nothing to serve, so there is no "old project"."""
    kill = mocker.patch.object(pw, "kill_process")
    watcher = _watcher(mocker)
    _wired(mocker, [_server()], {100: {"pid": 100, "project": None}})

    assert watcher.tick() == []
    kill.assert_not_called()


# ── failure is reported, not swallowed ───────────────────────────────────

def test_a_failed_kill_is_reported_and_not_counted_as_retired(mocker):
    logged = []
    mocker.patch.object(pw, "kill_process",
                        return_value=(False, "access denied"))
    watcher = PinWatcherController(
        cfg=_Cfg(True), on_log=lambda msg, colour="": logged.append(msg))
    mocker.patch.object(pw, "_read_pin", return_value=PROJ_B)
    watcher._last_pin = PROJ_A
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    assert watcher.tick() == []
    assert any("could not be stopped" in m for m in logged)
    assert any("access denied" in m for m in logged)


def test_a_successful_retirement_explains_what_happens_next(mocker):
    """The server vanishing is not self-explanatory to a user."""
    logged = []
    mocker.patch.object(pw, "kill_process", return_value=(True, "done"))
    watcher = PinWatcherController(
        cfg=_Cfg(True), on_log=lambda msg, colour="": logged.append(msg))
    mocker.patch.object(pw, "_read_pin", return_value=PROJ_B)
    watcher._last_pin = PROJ_A
    _wired(mocker, [_server()], {100: {"pid": 100, "project": PROJ_A}})

    watcher.tick()

    assert any("will restart it" in m for m in logged)


# ── startup + shutdown ───────────────────────────────────────────────────

def test_starting_adopts_the_current_pin_as_the_baseline(mocker):
    """Launching the manager must not retire a correctly-running server."""
    kill = mocker.patch.object(pw, "kill_process")
    mocker.patch.object(pw, "_read_pin", return_value=PROJ_B)
    mocker.patch.object(pw.threading, "Thread")      # no real loop
    watcher = PinWatcherController(cfg=_Cfg(True), on_log=lambda *a: None)

    watcher.start()

    assert watcher._last_pin == PROJ_B
    _wired(mocker, [_server(project=PROJ_A)],
           {100: {"pid": 100, "project": PROJ_A}})
    assert watcher.tick() == []
    kill.assert_not_called()


def test_stop_ends_the_loop():
    """The G-D thread-leak guard fails the suite if this ever stops working."""
    watcher = PinWatcherController(cfg=_Cfg(True), on_log=lambda *a: None)
    assert watcher._stop.is_set() is False
    watcher.stop()
    assert watcher._stop.is_set() is True

