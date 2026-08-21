"""tests/test_tokensave_daemon.py — attribution, and refusing to guess.

The dangerous operation this module enables is "stop that server". Getting the
project wrong means killing the one serving somebody's live Claude session
instead of the one holding the lock you wanted released. So most of what is
asserted here is the *refusal* path: which states decline to be stopped, and
why.

The last test is different in kind — it cross-checks the heuristic against
processes that declare their project on the command line, which are free
ground truth. It skips when the machine has none.
"""
from __future__ import annotations

import os
import sys

import pytest

from helpers import tokensave_daemon as td
from helpers.tokensave_daemon import (
    AMBIGUOUS,
    AUTHORITATIVE,
    HEURISTIC,
    UNATTRIBUTED,
    TokensaveServer,
    attribute_servers,
    stop_tokensave_server,
)

PROJ_A = os.path.join("D:", os.sep, "work", "alpha")
PROJ_B = os.path.join("D:", os.sep, "work", "beta")


def _srv(pid, started_at, cmdline="tokensave.exe serve"):
    return TokensaveServer(pid=pid, command_line=cmdline, started_at=started_at)


def _shm(mocker, mapping):
    """Pretend each project's database was opened at the given epoch time."""
    mocker.patch.object(td, "_shm_mtime",
                        side_effect=lambda p: mapping.get(p))


# ── authoritative: -p on the command line ────────────────────────────────

def test_a_declared_project_is_authoritative(mocker):
    _shm(mocker, {})
    out = attribute_servers(
        [_srv(1, 1000.0, 'tokensave.exe serve -p "%s"' % PROJ_A)], [PROJ_A])
    assert out[0].attribution == AUTHORITATIVE
    assert out[0].project == PROJ_A
    assert out[0].can_stop and not out[0].needs_confirmation


def test_a_declared_project_matches_despite_redundant_path_parts(mocker):
    """Resolves to the configured spelling, portably.

    A trailing separator and a `.` hop name the same directory on every
    platform. Case-folding and backslash-vs-slash do not — on Linux
    `/work/alpha` and `/WORK/ALPHA` are different directories — so those are
    asserted separately, under a Windows guard.
    """
    _shm(mocker, {})
    declared = os.path.join(PROJ_A, ".") + os.sep
    out = attribute_servers(
        [_srv(1, 1000.0, 'tokensave.exe serve -p "%s"' % declared)], [PROJ_A])
    assert out[0].attribution == AUTHORITATIVE
    assert out[0].project == PROJ_A, "should resolve to the configured spelling"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="case- and separator-insensitivity is Windows-only")
def test_windows_matches_a_declared_project_despite_case_and_separators(mocker):
    _shm(mocker, {})
    declared = PROJ_A.replace(os.sep, "/").upper()
    out = attribute_servers(
        [_srv(1, 1000.0, 'tokensave.exe serve -p "%s"' % declared)], [PROJ_A])
    assert out[0].attribution == AUTHORITATIVE
    assert out[0].project == PROJ_A


def test_a_declared_but_unknown_project_is_still_not_a_guess(mocker):
    """`-p` is evidence even when the manager does not track that project."""
    _shm(mocker, {})
    out = attribute_servers(
        [_srv(1, 1000.0, 'tokensave.exe serve -p "C:/elsewhere"')], [PROJ_A])
    assert out[0].attribution == AUTHORITATIVE
    assert "not a known project" in out[0].detail


# ── heuristic: the -shm correlation ──────────────────────────────────────

def test_a_lone_shm_match_is_heuristic_not_authoritative(mocker):
    """Good evidence is still not proof, and the difference is the whole point."""
    _shm(mocker, {PROJ_A: 1000.5})
    out = attribute_servers([_srv(1, 1000.0)], [PROJ_A])
    assert out[0].attribution == HEURISTIC
    assert out[0].project == PROJ_A
    assert out[0].can_stop, "a heuristic row may offer Stop..."
    assert out[0].needs_confirmation, "...but only behind a confirmation"


def test_a_match_outside_the_tolerance_window_is_not_a_match(mocker):
    _shm(mocker, {PROJ_A: 1100.0})
    out = attribute_servers([_srv(1, 1000.0)], [PROJ_A])
    assert out[0].attribution == UNATTRIBUTED


# ── the known-broken case, asserted rather than hoped ────────────────────

def test_a_second_server_on_an_already_open_database_is_unattributed(mocker):
    """SQLite stamps the -shm for the FIRST connection only.

    Server A opened the project and set the mtime; server B attached later and
    restamped nothing. B therefore matches no project, and the module must say
    so rather than inventing an attribution.
    """
    _shm(mocker, {PROJ_A: 1000.0})
    first, second = _srv(1, 1000.0), _srv(2, 5000.0)
    out = {s.pid: s for s in attribute_servers([first, second], [PROJ_A])}
    assert out[2].attribution == UNATTRIBUTED
    assert out[2].project is None
    assert not out[2].can_stop
    assert "already-open" in out[2].detail


def test_two_projects_opened_at_once_is_ambiguous(mocker):
    """Two candidates is strictly worse than none — never stoppable."""
    _shm(mocker, {PROJ_A: 1000.2, PROJ_B: 1000.3})
    out = attribute_servers([_srv(1, 1000.0)], [PROJ_A, PROJ_B])
    assert out[0].attribution == AMBIGUOUS
    assert out[0].project is None
    assert not out[0].can_stop
    assert set(out[0].candidates) == {PROJ_A, PROJ_B}


def test_two_servers_matching_one_project_are_both_ambiguous(mocker):
    """Which of the two actually holds the lock is not knowable from here."""
    _shm(mocker, {PROJ_A: 1000.0})
    out = attribute_servers([_srv(1, 1000.0), _srv(2, 1000.4)], [PROJ_A])
    assert [s.attribution for s in out] == [AMBIGUOUS, AMBIGUOUS]
    assert not any(s.can_stop for s in out)


def test_a_bare_server_matching_an_already_claimed_project_is_ambiguous(mocker):
    """A `-p` server already claims this project, so the bare one is a rival.

    Attributing it anyway would let the user stop the wrong process while
    believing they had stopped the lock holder.
    """
    _shm(mocker, {PROJ_A: 1000.0})
    declared = _srv(1, 1000.0, 'tokensave.exe serve -p "%s"' % PROJ_A)
    bare = _srv(2, 1000.1)
    out = {s.pid: s for s in attribute_servers([declared, bare], [PROJ_A])}
    assert out[1].attribution == AUTHORITATIVE
    assert out[2].attribution == AMBIGUOUS
    assert not out[2].can_stop


def test_no_known_projects_means_nothing_can_be_attributed(mocker):
    _shm(mocker, {})
    out = attribute_servers([_srv(1, 1000.0)], [])
    assert out[0].attribution == UNATTRIBUTED


# ── the stop policy, enforced where it costs something ───────────────────

def test_an_unattributed_server_refuses_to_stop(mocker):
    kp = mocker.patch.object(td, "kill_process")
    ok, detail = stop_tokensave_server(
        TokensaveServer(pid=1, command_line="", started_at=0.0,
                        attribution=UNATTRIBUTED))
    assert ok is False
    assert "could not be identified" in detail
    kp.assert_not_called()


def test_an_ambiguous_server_refuses_to_stop(mocker):
    kp = mocker.patch.object(td, "kill_process")
    ok, _detail = stop_tokensave_server(
        TokensaveServer(pid=1, command_line="", started_at=0.0,
                        attribution=AMBIGUOUS))
    assert ok is False
    kp.assert_not_called()


def test_a_heuristic_server_refuses_to_stop_without_confirmation(mocker):
    kp = mocker.patch.object(td, "kill_process")
    srv = TokensaveServer(pid=1, command_line="", started_at=0.0,
                          project=PROJ_A, attribution=HEURISTIC)
    ok, detail = stop_tokensave_server(srv)
    assert ok is False
    assert "heuristic" in detail and PROJ_A in detail
    kp.assert_not_called()


def test_a_heuristic_server_stops_once_confirmed(mocker):
    kp = mocker.patch.object(td, "kill_process", return_value=(True, "done"))
    srv = TokensaveServer(pid=1, command_line="", started_at=0.0,
                          project=PROJ_A, attribution=HEURISTIC)
    ok, _detail = stop_tokensave_server(srv, confirmed=True)
    assert ok is True
    kp.assert_called_once()


def test_an_authoritative_server_stops_without_ceremony(mocker):
    kp = mocker.patch.object(td, "kill_process", return_value=(True, "done"))
    srv = TokensaveServer(pid=7, command_line="", started_at=0.0,
                          project=PROJ_A, attribution=AUTHORITATIVE)
    ok, _detail = stop_tokensave_server(srv)
    assert ok is True
    assert kp.call_args.kwargs["tree"] is False, "never kill a server's tree"
    assert kp.call_args.kwargs["graceful"] is True


def test_stopping_forwards_the_scanned_identity(mocker):
    """Without this the PID-reuse guard in proc_kill is never engaged."""
    from helpers.proc_kill import ProcessIdentity
    ident = ProcessIdentity(pid=7, created_at=999, image="tokensave.exe")
    kp = mocker.patch.object(td, "kill_process", return_value=(True, "done"))
    stop_tokensave_server(
        TokensaveServer(pid=7, command_line="", started_at=0.0,
                        project=PROJ_A, attribution=AUTHORITATIVE,
                        identity=ident))
    assert kp.call_args.kwargs["expect"] is ident


# ── identifying our own binary ───────────────────────────────────────────

def test_a_process_merely_named_tokensave_is_not_ours_when_a_path_is_known():
    """Name is not identification — an impostor must not get a Stop button.

    Paths are built with os.path.join so `basename` resolves them on both
    platforms: a hardcoded Windows path has no separators at all on
    Linux, so its basename is the whole string and nothing matches.
    """
    ours = os.path.join(os.sep, "somewhere", "else", "tokensave.exe")
    theirs = os.path.join(os.sep, "real", "tokensave.exe")
    proc = {"exe": ours}
    assert td._is_tokensave(proc, td._normalise_path(theirs)) is False
    assert td._is_tokensave(proc, td._normalise_path(ours)) is True


def test_without_a_configured_binary_we_fall_back_to_the_image_name():
    """Degraded but useful: better than showing nothing when unconfigured."""
    assert td._is_tokensave(
        {"exe": os.path.join(os.sep, "x", "tokensave.exe")}, "") is True
    assert td._is_tokensave(
        {"exe": os.path.join(os.sep, "x", "notepad.exe")}, "") is False


# ── PowerShell/CIM output shapes ─────────────────────────────────────────

def test_cim_json_accepts_a_single_object():
    """ConvertTo-Json emits a bare object for one row and an array for many."""
    rows = td._parse_cim_json(
        '{"ProcessId":12,"ExecutablePath":"t.exe","CommandLine":"t serve",'
        '"Start":1000}')
    assert len(rows) == 1 and rows[0]["pid"] == 12


def test_cim_json_accepts_an_array():
    rows = td._parse_cim_json(
        '[{"ProcessId":1,"ExecutablePath":"a","CommandLine":"","Start":1},'
        '{"ProcessId":2,"ExecutablePath":"b","CommandLine":"","Start":2}]')
    assert [r["pid"] for r in rows] == [1, 2]


def test_cim_json_is_fail_open_on_garbage():
    assert td._parse_cim_json("not json at all") == []
    assert td._parse_cim_json("") == []


def test_project_argument_parsing_handles_the_shapes_seen_live():
    assert td._declared_project('tokensave.exe serve -p "D:\\a b\\c"') == "D:\\a b\\c"
    assert td._declared_project("tokensave serve -p /home/x") == "/home/x"
    assert td._declared_project("tokensave serve --project=/home/y") == "/home/y"
    assert td._declared_project("tokensave.exe serve") is None


# ── ground truth: the heuristic checked against declared projects ────────

def test_the_shm_heuristic_agrees_with_every_declared_project():
    """Cross-check the guess against processes that state the answer.

    A server started with `-p` tells us its project outright. Running the
    `-shm` correlation over those same processes therefore has a known correct
    answer, which makes them free labelled data — the only way to test this
    heuristic against reality rather than against constructed fixtures.

    Disagreement here is a defect in the correlation, not a tolerance to be
    widened until it passes.

    Skips when the machine is running no labelled servers (CI, or a developer
    with nothing indexed).
    """
    procs = [p for p in td._enumerate_processes()
             if td._is_tokensave(p, "") and td._declared_project(p.get("cmdline", ""))]
    if not procs:
        pytest.skip("no tokensave server declares -p on this machine")

    checked = 0
    for proc in procs:
        declared = td._declared_project(proc["cmdline"])
        if td._shm_mtime(declared) is None:
            continue                      # project has no open database to match
        # Attribute it as if it had NOT declared anything.
        bare = TokensaveServer(pid=proc["pid"], command_line="tokensave serve",
                               started_at=proc["started_at"])
        out = attribute_servers([bare], [declared])[0]
        assert out.attribution != AUTHORITATIVE     # we stripped the evidence
        if out.attribution == HEURISTIC:
            assert td._normalise_path(out.project) == td._normalise_path(declared), (
                "the -shm heuristic attributed PID %d to %r, but it declares %r"
                % (proc["pid"], out.project, declared))
            checked += 1
    if not checked:
        pytest.skip("no declared server had a matching open database")


def test_the_windows_start_time_is_converted_via_utc_not_local_midnight():
    """Pins a fix for a bug that failed silently and that CI cannot see.

    The first version subtracted `Get-Date '1970-01-01'` — LOCAL midnight —
    from a local `CreationDate`, producing an "epoch" offset by the machine's
    whole UTC offset. Measured at 14400 s here: 7200x the correlation
    tolerance, so every `-shm` match silently failed and every bare server
    came back `unattributed`. Nothing raised; the feature just quietly did
    nothing.

    `test_the_shm_heuristic_agrees_with_every_declared_project` is what caught
    it, but that test skips wherever no tokensave server is running — which
    includes CI. This one is structural so it runs everywhere.
    """
    import inspect
    src = inspect.getsource(td._enumerate_windows)
    assert "DateTimeOffset" in src, "start time must be converted through UTC"
    assert "Get-Date '1970-01-01'" not in src, (
        "local-midnight subtraction reintroduces the UTC-offset bug")
