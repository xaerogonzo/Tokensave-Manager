"""tests/test_request_drain.py — the Manager's side of the request inbox.

`App._drain_requests` / `_dispatch_request` decide what actually happens to a
queued request, and the interesting behaviour is all in the failure paths:

* a **transient** failure keeps the request, because deleting on the first
  stumble silently loses what the user asked for;
* the **attempt limit** gives up, and says `quarantined` rather than `drained`;
* a handler that **raises** is rejected with the exception recorded, not
  retried forever;
* a request that was **already acknowledged** — the crash-recovery case — is
  cleared instead of dispatched a second time;
* a **malformed** file is logged, never silently dropped.

The methods are exercised **unbound against a stub** rather than through a real
`App`. They touch only a handful of attributes, and building a Tk application
to test a dispatch table would make the slowest tests in the suite out of the
ones with the least UI in them.
"""
from __future__ import annotations

import json
import os

import pytest

import app as app_module
from helpers import manager_ipc
from helpers.manager_ipc import (
    DRAINED,
    QUARANTINED,
    REJECTED,
    load_requests,
    status_of,
    write_request,
)

App = app_module.App


class _Stub:
    """The few attributes the drain actually reads."""

    def __init__(self, projects, roots, handlers=None):
        self._paths = list(projects)
        self._roots = list(roots)
        self._current_proc = None
        self._projects = None
        self.focused = []
        self.opened = []
        self._REQUEST_HANDLERS = (App._REQUEST_HANDLERS if handlers is None
                                  else handlers)

    # Borrowed straight from App, so the tests exercise the real logic.
    _drain_requests = App._drain_requests
    _dispatch_request = App._dispatch_request

    def _known_roots(self):
        return list(self._roots)

    def _request_projects(self):
        return list(self._paths)


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture()
def project(workspace):
    root = workspace / "proj"
    root.mkdir()
    return str(root)


@pytest.fixture()
def stub(project, workspace):
    return _Stub([project], [str(workspace)])


def _queue(project, action="doctor", payload=None, created_at=1788000000):
    return write_request(project, action, payload, created_at=created_at,
                         known_roots=None)["id"]


def _handlers(**overrides):
    """A dispatch table whose handlers record and return what a test wants."""
    return dict(overrides)


# ── The happy path ────────────────────────────────────────────────────────

def test_a_dispatched_request_is_acknowledged_and_leaves_the_queue(
        project, workspace):
    calls = []
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req:
                           calls.append(path) or True))
    ident = _queue(project)

    assert stub._drain_requests() == 0
    assert calls == [project]
    assert status_of(project, ident)["state"] == DRAINED
    assert load_requests(project)[0] == []


def test_the_acknowledgement_records_which_action_ran(project, workspace):
    stub = _Stub([project], [str(workspace)],
                 _handlers(savings=lambda self, path, req: True))
    ident = _queue(project, "savings")
    stub._drain_requests()

    record = status_of(project, ident)
    assert record["action"] == "savings"
    assert "savings" in record["detail"]


def test_requests_are_dispatched_in_queue_order(project, workspace):
    seen = []
    stub = _Stub([project], [str(workspace)], _handlers(
        doctor=lambda self, path, req: seen.append("doctor") or True,
        savings=lambda self, path, req: seen.append("savings") or True,
    ))
    _queue(project, "savings", created_at=1788000200)
    _queue(project, "doctor", created_at=1788000100)

    stub._drain_requests()
    assert seen == ["doctor", "savings"]


# ── Transient failure keeps the request ───────────────────────────────────

def test_a_transient_failure_leaves_the_request_pending(project, workspace):
    """A handler returning False means "not now", not "never".

    The common cause is a project the Manager has not finished discovering.
    Deleting the request there would lose the user's action for a reason that
    resolves itself a second later.
    """
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req: False))
    ident = _queue(project)

    assert stub._drain_requests() == 1
    assert status_of(project, ident)["state"] == "pending"
    assert load_requests(project)[0][0]["attempts"] == 1


def test_attempts_accumulate_until_the_limit_then_quarantine(project,
                                                             workspace):
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req: False))
    ident = _queue(project)

    for _ in range(manager_ipc.MAX_ATTEMPTS):
        stub._drain_requests()

    assert status_of(project, ident)["state"] == QUARANTINED
    assert load_requests(project)[0] == []


def test_quarantined_is_not_drained(project, workspace):
    """The distinction the ledger exists to preserve, at the point it is set."""
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req: False))
    ident = _queue(project)
    for _ in range(manager_ipc.MAX_ATTEMPTS):
        stub._drain_requests()

    record = status_of(project, ident)
    assert record["state"] != DRAINED
    assert "attempt" in record["detail"]


def test_a_later_success_still_drains_after_earlier_stumbles(project,
                                                             workspace):
    outcome = {"ready": False}
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req: outcome["ready"]))
    ident = _queue(project)

    stub._drain_requests()
    assert status_of(project, ident)["state"] == "pending"

    outcome["ready"] = True
    stub._drain_requests()
    assert status_of(project, ident)["state"] == DRAINED


# ── Rejection ─────────────────────────────────────────────────────────────

def test_a_handler_that_raises_is_rejected_with_the_reason(project, workspace):
    def _boom(self, path, req):
        raise RuntimeError("dialog exploded")

    stub = _Stub([project], [str(workspace)], _handlers(doctor=_boom))
    ident = _queue(project)
    stub._drain_requests()

    record = status_of(project, ident)
    assert record["state"] == REJECTED
    assert "dialog exploded" in record["detail"]


def test_an_action_with_no_handler_is_rejected_not_retried(project, workspace):
    """An empty table is the "somebody added an action and forgot the handler"
    case. Retrying it forever would wedge the queue behind it."""
    stub = _Stub([project], [str(workspace)], _handlers())
    ident = _queue(project)

    assert stub._drain_requests() == 0
    assert status_of(project, ident)["state"] == REJECTED
    assert load_requests(project)[0] == []


def test_every_allowlisted_action_has_a_handler():
    """The reverse of the test above, as a standing guard.

    `manager_ipc.ACTIONS` is what the CLI will accept; a mismatch here means
    the CLI queues something the Manager can only reject.
    """
    assert set(App._REQUEST_HANDLERS) == set(manager_ipc.ACTIONS)


# ── Crash recovery ────────────────────────────────────────────────────────

def test_an_already_acknowledged_request_is_cleared_not_re_dispatched(
        project, workspace):
    """The crash-between-write-and-delete case.

    The acknowledgement is written before the pending file is removed, so a
    crash in between leaves a request that is queued *and* already done. A
    drain that consulted only the queue would open the dialog a second time.
    """
    calls = []
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req:
                           calls.append(1) or True))
    ident = _queue(project)
    request = load_requests(project)[0][0]

    # Acknowledge without removing the pending file — the interrupted state.
    manager_ipc._atomic_write_json(
        os.path.join(manager_ipc.done_dir(project),
                     os.path.basename(request["path"])),
        {"id": ident, "action": "doctor", "project": project,
         "outcome": DRAINED, "at": 1788000001, "detail": "first pass"})

    assert stub._drain_requests() == 0
    assert calls == []                       # the handler never ran again
    assert load_requests(project)[0] == []   # and the queue was cleared


# ── Malformed input ───────────────────────────────────────────────────────

def test_a_malformed_request_is_logged_rather_than_vanishing(
        project, workspace, caplog):
    """A request that disappears silently reads as "nothing happened"."""
    stub = _Stub([project], [str(workspace)],
                 _handlers(doctor=lambda self, path, req: True))
    os.makedirs(manager_ipc.pending_dir(project), exist_ok=True)
    junk = os.path.join(manager_ipc.pending_dir(project),
                        "00001788000000-ffffffffffff.json")
    with open(junk, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    with caplog.at_level("WARNING"):
        stub._drain_requests()
    assert any("malformed request" in r.message for r in caplog.records)


def test_a_request_outside_the_known_roots_never_reaches_a_handler(
        tmp_path, workspace):
    """`--project` is an argument, not an authorization.

    The file is written into a project the Manager is watching, but names a
    directory outside its configured roots — the shape an attacker-controlled
    request would take.
    """
    watched = workspace / "watched"
    watched.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    calls = []
    stub = _Stub([str(watched)], [str(workspace)],
                 _handlers(doctor=lambda self, path, req:
                           calls.append(1) or True))
    path = os.path.join(manager_ipc.pending_dir(str(watched)),
                        "00001788000000-aaaaaaaaaaaa.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"request_schema_version": manager_ipc.REQUEST_SCHEMA_VERSION,
                   "project": str(outside), "action": "doctor",
                   "payload": {}, "created_at": 1788000000}, fh)

    stub._drain_requests()
    assert calls == []


# ── Several projects ──────────────────────────────────────────────────────

def test_every_watched_project_is_drained(workspace):
    one, two = workspace / "one", workspace / "two"
    one.mkdir()
    two.mkdir()
    seen = []
    stub = _Stub([str(one), str(two)], [str(workspace)],
                 _handlers(doctor=lambda self, path, req:
                           seen.append(path) or True))
    _queue(str(one))
    _queue(str(two))

    stub._drain_requests()
    assert sorted(seen) == sorted([str(one), str(two)])


def test_a_project_with_no_inbox_is_not_an_error(project, workspace):
    stub = _Stub([project, str(workspace / "never-created")],
                 [str(workspace)],
                 _handlers(doctor=lambda self, path, req: True))
    assert stub._drain_requests() == 0


# ── Timer cadence ─────────────────────────────────────────────────────────

def test_the_queued_cadence_is_faster_than_the_idle_one():
    """A handoff nobody sees land stops being used."""
    assert App._REQUEST_TICK_MS < App._REQUEST_IDLE_MS


def test_the_inbox_does_not_ride_the_project_refresh_tick():
    """Measured: hooking `_auto_refresh` gave a 60s first response.

    That tick is first scheduled a full period after startup and also runs
    `refresh()`, which walks every project — so speeding it up to stay
    responsive would drag a full rescan along at handoff speed. The timers are
    separate, and this pins that they stayed separate.
    """
    import inspect
    source = inspect.getsource(App._auto_refresh)
    assert "_drain_requests" not in source
    assert App._REQUEST_TICK_MS < app_module.AUTO_REFRESH_MS
