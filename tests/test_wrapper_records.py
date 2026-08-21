"""tests/test_wrapper_records.py — why a server is serving what it serves.

The manager could already tell you Claude Desktop was serving project X. It
could not tell you *why*: a deliberate pin, or the fallback picking whichever
project happened to be indexed most recently. Those are very different facts
when several projects are active, and the second one moves under you every
time another project syncs.

So the wrapper now writes down its choice and its reason, and the daemon
manager reads it back. Two properties matter most here:

* **Additive.** The wrapper already passes ``-p``, so its servers were
  attributed authoritatively before any of this existed. A record must add
  the reason and change nothing else — an attribution that shifts because a
  side file appeared would be worse than no side file.
* **Stale-proof.** PIDs get reused. A record left by a dead server must not
  be allowed to explain a live one that happens to have inherited its number.
"""
from __future__ import annotations

import json
import pathlib

from helpers import tokensave_daemon as td
from helpers.tokensave_daemon import (
    AUTHORITATIVE,
    HEURISTIC,
    UNATTRIBUTED,
    TokensaveServer,
    read_wrapper_records,
)


def _records(tmp_path, mocker, entries):
    """Point the reader at a temp directory holding *entries* (pid -> dict)."""
    directory = tmp_path / "wrapper-runs"
    directory.mkdir()
    for pid, payload in entries.items():
        (directory / ("%s.json" % pid)).write_text(
            json.dumps(payload), encoding="utf-8")
    mocker.patch.object(td, "_records_dir", return_value=str(directory))
    return directory


def _server(pid=100, started_at=1000.0, cmdline='tokensave.exe serve -p "P"'):
    return TokensaveServer(pid=pid, command_line=cmdline,
                           started_at=started_at)


# ── reading ──────────────────────────────────────────────────────────────

def test_records_are_keyed_by_the_pid_they_describe(tmp_path, mocker):
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "pin", "written_at": 1.0},
        200: {"pid": 200, "project": "Q", "reason": "none", "written_at": 2.0},
    })
    records = read_wrapper_records()
    assert set(records) == {100, 200}
    assert records[100]["reason"] == "pin"


def test_a_missing_directory_is_not_an_error(mocker, tmp_path):
    """Nothing has spawned through the wrapper yet — a normal state."""
    mocker.patch.object(td, "_records_dir",
                        return_value=str(tmp_path / "nope"))
    assert read_wrapper_records() == {}


def test_unreadable_records_are_skipped_not_fatal(tmp_path, mocker):
    """One corrupt file must not hide every other server's reason."""
    directory = _records(tmp_path, mocker, {
        100: {"pid": 100, "reason": "pin", "written_at": 1.0}})
    (directory / "garbage.json").write_text("{not json", encoding="utf-8")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    assert set(read_wrapper_records()) == {100}


def test_a_record_without_a_usable_pid_is_skipped(tmp_path, mocker):
    _records(tmp_path, mocker, {"bad": {"pid": "not-a-number"}})
    assert read_wrapper_records() == {}


# ── the staleness guard ──────────────────────────────────────────────────

def test_a_record_explains_a_server_that_started_alongside_it(tmp_path,
                                                              mocker):
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "pin",
              "written_at": 1000.5}})
    out = td.attribute_servers([_server(started_at=1000.0)], [])
    assert out[0].selection == "pin"


def test_a_record_from_a_dead_server_cannot_explain_a_reused_pid(tmp_path,
                                                                 mocker):
    """The PID-reuse case, in a different guise.

    The record was written hours ago for a server that has since exited. A
    new server now wears that number; attaching the old reason would put a
    confident, wrong explanation on screen.
    """
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "OLD", "reason": "pin",
              "written_at": 1000.0}})
    out = td.attribute_servers([_server(started_at=99000.0)], [])
    assert out[0].selection == ""


def test_a_record_with_no_timestamp_is_not_trusted(tmp_path, mocker):
    """Without `written_at` there is no way to tell fresh from stale."""
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "pin"}})
    out = td.attribute_servers([_server(started_at=1000.0)], [])
    assert out[0].selection == ""


# ── additive: attribution must not move ──────────────────────────────────

def test_a_record_never_changes_the_attribution_state(tmp_path, mocker):
    """It explains an answer; it does not become one.

    A `-p` server was already authoritative. If a side file could promote or
    demote that, the file would be load-bearing for a safety decision — and
    the whole point of the four-state contract is that stopping a server
    depends on evidence, not on bookkeeping.
    """
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "pin",
              "written_at": 1000.0}})
    server = _server(started_at=1000.0,
                     cmdline='tokensave.exe serve -p "D:/known"')
    out = td.attribute_servers([server], ["D:/known"])
    assert out[0].attribution == AUTHORITATIVE


def test_a_record_cannot_promote_an_unattributed_server(tmp_path, mocker):
    """A bare server with a record still has no project we can verify.

    The record says what the wrapper *intended*. Treating intent as proof
    would make an unstoppable row stoppable on the strength of a file.
    """
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "D:/somewhere", "reason": "pin",
              "written_at": 1000.0}})
    mocker.patch.object(td, "_shm_mtime", return_value=None)
    out = td.attribute_servers(
        [_server(started_at=1000.0, cmdline="tokensave.exe serve")], [])
    assert out[0].attribution == UNATTRIBUTED
    assert out[0].can_stop is False
    assert out[0].selection == "pin"      # explained, still not actionable


def test_servers_with_no_record_are_untouched(tmp_path, mocker):
    """A Claude Code session did not come through the wrapper."""
    _records(tmp_path, mocker, {})
    mocker.patch.object(td, "_shm_mtime", side_effect=lambda p: 1000.2)
    out = td.attribute_servers(
        [_server(started_at=1000.0, cmdline="tokensave.exe serve")],
        ["D:/proj"])
    assert out[0].selection == ""
    assert out[0].attribution == HEURISTIC


# ── the reason reaches the user in words ─────────────────────────────────

def test_the_drifting_fallback_says_so(tmp_path, mocker):
    """The whole reason this exists.

    "Most recently indexed" is not a stable choice — it moves the next time
    another project syncs — and that is exactly what a user running several
    projects needs told.
    """
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "most-recent-index",
              "written_at": 1000.0}})
    out = td.attribute_servers([_server(started_at=1000.0)], [])
    assert "most recently indexed" in out[0].detail
    assert "moves when another project syncs" in out[0].detail


def test_a_pinned_choice_says_so(tmp_path, mocker):
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "pin",
              "written_at": 1000.0}})
    out = td.attribute_servers([_server(started_at=1000.0)], [])
    assert "pinned project" in out[0].detail


def test_an_unknown_reason_is_recorded_without_inventing_prose(tmp_path,
                                                               mocker):
    """A future wrapper may add a reason this build has no wording for."""
    _records(tmp_path, mocker, {
        100: {"pid": 100, "project": "P", "reason": "some-future-rule",
              "written_at": 1000.0}})
    out = td.attribute_servers([_server(started_at=1000.0)], [])
    assert out[0].selection == "some-future-rule"


# ── the wrapper's side of the contract ───────────────────────────────────
#
# `tokensave-wrapper.py` runs its work at module scope — importing it would
# spawn a real MCP server — so its guarantees are asserted from the source.

def _wrapper_source() -> str:
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "src" / "tokensave-wrapper.py").read_text(encoding="utf-8")


def test_the_record_is_written_after_the_child_is_spawned():
    """The child PID is the key the manager looks it up by."""
    src = _wrapper_source()
    assert src.index("subprocess.Popen") < src.index("_write_record(proc.pid")


def test_the_record_is_removed_in_a_finally():
    """A normal exit must not leave the directory growing."""
    src = _wrapper_source()
    tail = src[src.index("_record_path = _write_record"):]
    assert "finally:" in tail
    assert "_remove_record(_record_path)" in tail


def test_bookkeeping_can_never_stop_the_server_starting():
    """Every failure here is swallowed, on purpose.

    A server that runs unrecorded is a small loss; one that fails to start
    because a JSON write raised is a large one — and this file is the single
    path Claude Desktop uses to reach tokensave at all.
    """
    src = _wrapper_source()
    body = src[src.index("def _write_record"):src.index("def _remove_record")]
    assert "except (OSError, TypeError, ValueError):" in body
    assert "return \"\"" in body


def test_the_wrapper_stays_single_threaded():
    """Its stdio handling is load-bearing; see MCP_INTEGRATION_GOTCHAS.md.

    An earlier revision added a watcher thread here and had to be reverted,
    so the constraint is asserted rather than remembered.
    """
    src = _wrapper_source()
    assert "import threading" not in src
    assert "Thread(" not in src


def test_stale_records_are_pruned_by_age_not_by_liveness():
    """Checking whether a PID is alive would put process introspection into
    the one file that has to stay small and predictable."""
    src = _wrapper_source()
    assert "def _prune_old_records" in src
    assert "getmtime" in src
