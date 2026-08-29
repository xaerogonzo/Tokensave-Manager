"""tests/test_manager_ipc.py — the Manager's request inbox.

This is a local IPC protocol whose transport is a file in the project
directory, so anything on the machine can write one. The tests are therefore
mostly adversarial: they check that a request asking for something it may not
have is *refused*, not partially honoured, and that the answers a consumer gets
are never inferred from something's absence.

Four properties carry the design, and each one replaced a plausible-looking
alternative that does not work:

* **Identity includes the project.** Without it two projects filing the same
  request collide and a status lookup cannot say which it answered for.
* **Absence is not success.** An earlier design deleted the request on dispatch
  *and* offered a status lookup. Once the file is gone, "drained" is
  indistinguishable from "never existed", "rejected" and "quarantined" — so a
  durable acknowledgement is written before the pending file is removed.
* **A transient failure keeps the request.** Deleting on the first stumble
  loses the user's action; only the attempt limit quarantines it.
* **Retention touches `done/` only.** A pruner that could reach `pending/`
  turns a slow week into lost work.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from helpers import manager_ipc
from helpers.manager_ipc import (
    DRAINED,
    PENDING,
    QUARANTINED,
    REJECTED,
    REQUEST_SCHEMA_VERSION,
    UNKNOWN,
    RequestError,
    acknowledge,
    canonical_project,
    load_requests,
    prune_acknowledgements,
    record_attempt,
    request_id,
    status_of,
    validate,
    write_request,
)


@pytest.fixture()
def workspace(tmp_path):
    """The configured search root. Only what is under here is authorised."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture()
def project(workspace):
    root = workspace / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return str(root)


@pytest.fixture()
def roots(workspace):
    return [str(workspace)]


@pytest.fixture()
def outside(tmp_path):
    """A real directory deliberately NOT under the configured root.

    A sibling of `workspace` rather than of `project`: an earlier version of
    these fixtures put it one level too deep, so it sat *inside* the root and
    the containment tests passed without exercising anything.
    """
    other = tmp_path / "elsewhere"
    other.mkdir()
    return other


def _request(project, action="doctor", payload=None, created_at=1788000000,
             version=REQUEST_SCHEMA_VERSION):
    return {"request_schema_version": version, "project": project,
            "action": action, "payload": payload or {},
            "created_at": created_at}


# ── Identity ──────────────────────────────────────────────────────────────

def test_key_order_does_not_change_the_id(project):
    """The TypeScript extension is a second producer and must agree exactly.

    A dict built in a different order — which is all a different language
    guarantees — has to hash identically, or the same logical request gets two
    ids and the duplicate check stops working.
    """
    a = request_id(project, "commit",
                   {"files": ["b.py", "a.py"], "note": "n", "scope": "s"})
    b = request_id(project, "commit",
                   {"scope": "s", "note": "n", "files": ["b.py", "a.py"]})
    assert a == b


def test_two_projects_with_identical_payloads_get_different_ids(tmp_path):
    """Without the project in the hash, a status lookup is ambiguous."""
    one, two = str(tmp_path / "one"), str(tmp_path / "two")
    assert request_id(one, "doctor", {}) != request_id(two, "doctor", {})


def test_path_separator_does_not_change_the_id(project):
    """Windows and POSIX producers name the same file differently."""
    assert (request_id(project, "commit", {"files": ["src/app.py"]})
            == request_id(project, "commit",
                          {"files": ["src" + chr(92) + "app.py"]}))


def test_action_case_does_not_change_the_id(project):
    assert (request_id(project, "DOCTOR", {})
            == request_id(project, "doctor", {}))


def test_ids_are_short_and_hex(project):
    ident = request_id(project, "doctor", {})
    assert len(ident) == 12
    assert all(c in "0123456789abcdef" for c in ident)


# ── Project canonicalisation and containment ──────────────────────────────

def test_traversal_resolves_before_it_is_compared(project):
    """`realpath` first, so `foo/..` cannot buy a different verdict."""
    sneaky = os.path.join(project, "src", "..")
    assert canonical_project(sneaky) == canonical_project(project)


def test_a_project_outside_the_known_roots_is_refused(roots, outside):
    with pytest.raises(RequestError, match="outside the Manager"):
        validate(_request(str(outside)), known_roots=roots)


def test_a_traversal_out_of_the_roots_is_refused(project, roots, outside):
    """`--project` is an argument, not an authorization.

    The path spells itself as living under the root and resolves elsewhere,
    which is exactly what canonicalising before comparing is for.
    """
    escape = os.path.join(project, "..", "..", outside.name)
    with pytest.raises(RequestError, match="outside the Manager"):
        validate(_request(escape), known_roots=roots)


def test_a_project_that_is_not_a_directory_is_refused(project, roots):
    target = os.path.join(project, "src", "app.py")     # a file, not a dir
    with pytest.raises(RequestError, match="not a directory"):
        validate(_request(target), known_roots=roots)


def test_a_missing_known_roots_list_skips_the_root_check(project):
    """`None` means "the caller has no roots to check against", which is not
    the same as "no roots are allowed"."""
    assert validate(_request(project), known_roots=None)["action"] == "doctor"


# ── Schema version ────────────────────────────────────────────────────────

def test_an_unsupported_schema_version_is_its_own_failure(project, roots):
    """Distinct from "malformed": a newer producer against an older Manager is
    a legible situation whose remedy is to update, not to fix a broken file."""
    with pytest.raises(RequestError, match="unsupported request_schema_version"):
        validate(_request(project, version=REQUEST_SCHEMA_VERSION + 1),
                 known_roots=roots)


def test_a_missing_schema_version_is_rejected(project, roots):
    request = _request(project)
    del request["request_schema_version"]
    with pytest.raises(RequestError, match="unsupported request_schema_version"):
        validate(request, known_roots=roots)


# ── Action allowlist and payload schemas ──────────────────────────────────

def test_an_unknown_action_is_refused(project, roots):
    with pytest.raises(RequestError, match="unknown action"):
        validate(_request(project, action="rm-rf"), known_roots=roots)


def test_there_is_no_free_form_command_field(project, roots):
    """Every action opens a dialog; none takes an arbitrary instruction."""
    with pytest.raises(RequestError, match="unexpected payload key"):
        validate(_request(project, payload={"command": "shutdown"}),
                 known_roots=roots)


def test_open_project_carries_no_second_project_selector(project, roots):
    """The envelope owns project identity.

    Two possible project identities in one security-relevant message is an
    ambiguity, not a convenience — so `open-project`'s payload is empty and the
    Manager opens the request's own canonical project.
    """
    assert manager_ipc.PAYLOAD_SCHEMA["open-project"] == ((), ())
    with pytest.raises(RequestError, match="unexpected payload key"):
        validate(_request(project, action="open-project",
                          payload={"project": "C:/elsewhere"}),
                 known_roots=roots)


def test_commit_requires_files(project, roots):
    with pytest.raises(RequestError, match="missing payload key"):
        validate(_request(project, action="commit", payload={"note": "hi"}),
                 known_roots=roots)


def test_commit_files_must_live_inside_the_request_project(project, roots,
                                                           outside):
    """Project A naming files under B is refused, not filtered.

    Honouring the acceptable half of a request that asks for something it may
    not have would hide the fact that it asked.
    """
    with pytest.raises(RequestError, match="escapes the project"):
        validate(_request(project, action="commit",
                          payload={"files": [str(outside / "secret.py")]}),
                 known_roots=roots)


def test_a_sibling_project_inside_the_root_is_still_out_of_bounds(
        project, roots, workspace):
    """Being authorised is not the same as being in scope.

    A sibling project passes the search-root check, so only the per-file
    containment rule stops a commit request reaching across it.
    """
    sibling = workspace / "other-proj"
    sibling.mkdir()
    with pytest.raises(RequestError, match="escapes the project"):
        validate(_request(project, action="commit",
                          payload={"files": [str(sibling / "secret.py")]}),
                 known_roots=roots)


def test_a_relative_traversal_out_of_the_project_is_refused(project, roots):
    with pytest.raises(RequestError, match="escapes the project"):
        validate(_request(project, action="commit",
                          payload={"files": ["../../etc/passwd"]}),
                 known_roots=roots)


def test_a_legitimate_relative_commit_path_is_accepted(project, roots):
    request = validate(_request(project, action="commit",
                                payload={"files": ["src/app.py"]}),
                       known_roots=roots)
    assert request["payload"]["files"] == ["src/app.py"]


def test_the_file_count_is_capped(project, roots):
    files = [f"src/f{i}.py" for i in range(manager_ipc.MAX_FILES + 1)]
    with pytest.raises(RequestError, match="the cap is"):
        validate(_request(project, action="commit", payload={"files": files}),
                 known_roots=roots)


def test_the_payload_size_is_capped(project, roots):
    with pytest.raises(RequestError, match="the cap is"):
        validate(_request(project, action="commit",
                          payload={"files": ["src/app.py"],
                                   "note": "x" * (manager_ipc.MAX_PAYLOAD_BYTES
                                                  + 1)}),
                 known_roots=roots)


def test_created_at_must_be_a_positive_epoch(project, roots):
    with pytest.raises(RequestError, match="created_at"):
        validate(_request(project, created_at=0), known_roots=roots)


# ── Writing ───────────────────────────────────────────────────────────────

def test_writing_produces_a_readable_pending_request(project, roots):
    written = write_request(project, "doctor", known_roots=roots)
    requests, skipped = load_requests(project, roots)

    assert not skipped
    assert len(requests) == 1
    assert requests[0]["id"] == written["id"]
    assert requests[0]["action"] == "doctor"


def test_re_filing_the_same_request_is_a_no_op(project, roots):
    first = write_request(project, "doctor", created_at=1788000000,
                          known_roots=roots)
    second = write_request(project, "doctor", created_at=1788009999,
                           known_roots=roots)

    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    assert len(load_requests(project, roots)[0]) == 1


def test_the_queue_is_fifo_by_creation_time(project, roots):
    """Filename order, not hash order and not mtime.

    Hash order is arbitrary; mtime would make the queue depend on the
    filesystem rather than on when the user asked.
    """
    write_request(project, "savings", created_at=1788000300, known_roots=roots)
    write_request(project, "doctor", created_at=1788000100, known_roots=roots)
    write_request(project, "test-manager", created_at=1788000200,
                  known_roots=roots)

    order = [r["action"] for r in load_requests(project, roots)[0]]
    assert order == ["doctor", "test-manager", "savings"]


def test_writing_is_atomic(project, roots, monkeypatch):
    """A reader must never see half a request.

    The drain runs while a producer writes; without temp-then-replace it
    observes a truncated file and logs a malformed request that never was.
    """
    seen = {}
    real_replace = os.replace

    def _spy(src, dst):
        # Mid-write, the destination does not exist yet and the temp file is
        # not named like a request, so a concurrent load sees nothing.
        seen["pending_during_write"] = load_requests(project, roots)[0]
        return real_replace(src, dst)

    monkeypatch.setattr(manager_ipc.os, "replace", _spy)
    write_request(project, "doctor", known_roots=roots)

    assert seen["pending_during_write"] == []
    assert len(load_requests(project, roots)[0]) == 1


def test_an_invalid_request_is_never_written(project, roots):
    with pytest.raises(RequestError):
        write_request(project, "rm-rf", known_roots=roots)
    assert load_requests(project, roots)[0] == []


# ── Reading: malformed files are counted, not swallowed ───────────────────

def test_a_malformed_file_is_reported_rather_than_vanishing(project, roots):
    """A request that disappears silently reads as "nothing happened"."""
    write_request(project, "doctor", known_roots=roots)
    junk = os.path.join(manager_ipc.pending_dir(project),
                        "00001788000000-deadbeefcafe.json")
    with open(junk, "w", encoding="utf-8") as fh:
        fh.write("{not json")

    requests, skipped = load_requests(project, roots)
    assert len(requests) == 1
    assert len(skipped) == 1
    assert skipped[0]["path"] == junk
    assert "unreadable" in skipped[0]["reason"]


def test_a_valid_json_file_that_fails_validation_is_reported(project, roots):
    path = os.path.join(manager_ipc.pending_dir(project),
                        "00001788000000-aaaaaaaaaaaa.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_request(project, action="not-an-action"), fh)

    requests, skipped = load_requests(project, roots)
    assert requests == []
    assert "unknown action" in skipped[0]["reason"]


def test_validation_runs_again_at_read_time(project, roots, outside):
    """Validating only on the way in checks the one producer that behaves.

    The file is writable by anything on the machine, so a request that was
    never written through `write_request` still has to be refused.
    """
    path = os.path.join(manager_ipc.pending_dir(project),
                        "00001788000000-bbbbbbbbbbbb.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_request(str(outside)), fh)

    requests, skipped = load_requests(project, roots)
    assert requests == []
    assert "outside the Manager" in skipped[0]["reason"]


# ── The acknowledgement ledger ────────────────────────────────────────────

def test_status_is_unknown_before_anything_happens(project):
    assert status_of(project, "000000000000")["state"] == UNKNOWN


def test_status_is_pending_once_written(project, roots):
    written = write_request(project, "doctor", known_roots=roots)
    assert status_of(project, written["id"])["state"] == PENDING


@pytest.mark.parametrize("outcome", [DRAINED, REJECTED, QUARANTINED])
def test_every_outcome_is_recorded_distinctly(project, roots, outcome):
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]
    acknowledge(project, request, outcome, detail="because")

    state = status_of(project, request["id"])
    assert state["state"] == outcome
    assert state["detail"] == "because"


def test_quarantined_never_reads_as_drained(project, roots):
    """The distinction the whole ledger exists to preserve."""
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]
    acknowledge(project, request, QUARANTINED, "gave up after 5 attempts")
    assert status_of(project, request["id"])["state"] != DRAINED


def test_the_pending_file_is_gone_only_after_the_record_exists(project, roots,
                                                               monkeypatch):
    """The crash-safety invariant, checked at the moment it matters.

    A crash between the two steps must leave a duplicate — recoverable — not a
    request that vanished with no record, which nothing can explain afterwards.
    """
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]

    observed = {}
    real_remove = os.remove

    def _spy(path):
        observed["done_existed_first"] = bool(
            os.listdir(manager_ipc.done_dir(project)))
        return real_remove(path)

    monkeypatch.setattr(manager_ipc.os, "remove", _spy)
    acknowledge(project, request, DRAINED)

    assert observed["done_existed_first"] is True
    assert load_requests(project, roots)[0] == []


def test_a_crash_before_the_delete_leaves_a_recoverable_duplicate(
        project, roots, monkeypatch):
    """Simulated by failing the delete: the record survives, so does the request.

    The state afterwards is genuinely both — queued *and* already acted on —
    and the two questions get different, correct answers. `status_of` says
    `pending`, because the queue is the truth for "is my request still
    waiting?"; `completed_outcome` says `drained`, because the ledger is the
    truth for the drain's "did I already do this?". A drain that consulted only
    the queue would dispatch a second time.
    """
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]

    monkeypatch.setattr(manager_ipc.os, "remove",
                        lambda *a: (_ for _ in ()).throw(OSError("boom")))
    acknowledge(project, request, DRAINED)

    assert len(load_requests(project, roots)[0]) == 1
    assert status_of(project, request["id"])["state"] == PENDING
    assert manager_ipc.completed_outcome(project, request["id"]) == DRAINED


def test_completed_outcome_is_empty_for_an_untouched_request(project, roots):
    """So the drain can tell "already done" from "never started"."""
    written = write_request(project, "doctor", known_roots=roots)
    assert manager_ipc.completed_outcome(project, written["id"]) == ""


def test_a_status_query_for_the_wrong_project_does_not_answer(project, roots,
                                                              tmp_path):
    """A cross-project lookup fails loudly rather than answering about a
    different repository that happens to share an id."""
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]
    acknowledge(project, request, DRAINED)

    other = str(tmp_path.parent / "somewhere-else")
    state = status_of(project, request["id"], project=other)
    assert state["state"] == UNKNOWN
    assert "different project" in state["reason"]


def test_a_status_query_for_the_right_project_answers(project, roots):
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]
    acknowledge(project, request, DRAINED)
    assert status_of(project, request["id"], project=project)["state"] == DRAINED


# ── Transient failure ─────────────────────────────────────────────────────

def test_a_transient_failure_keeps_the_request_pending(project, roots):
    """Deleting on the first stumble silently loses the user's action."""
    write_request(project, "doctor", known_roots=roots)
    request = load_requests(project, roots)[0][0]

    assert record_attempt(request) == 1
    reloaded = load_requests(project, roots)[0]
    assert len(reloaded) == 1
    assert reloaded[0]["attempts"] == 1


def test_attempts_accumulate_across_drains(project, roots):
    write_request(project, "doctor", known_roots=roots)
    for expected in (1, 2, 3):
        request = load_requests(project, roots)[0][0]
        assert record_attempt(request) == expected


def test_the_attempt_limit_is_a_real_number():
    """A retry loop with no ceiling is a stuck queue with extra steps."""
    assert manager_ipc.MAX_ATTEMPTS >= 1


# ── Retention ─────────────────────────────────────────────────────────────

def test_pruning_trims_old_acknowledgements(project, roots):
    for i in range(3):
        write_request(project, "doctor", created_at=1788000000 + i,
                      known_roots=roots)
        request = load_requests(project, roots)[0][0]
        acknowledge(project, request, DRAINED)

    # Age them past the window.
    ancient = time.time() - manager_ipc.MAX_DONE_AGE_SECONDS - 10
    for name in os.listdir(manager_ipc.done_dir(project)):
        path = os.path.join(manager_ipc.done_dir(project), name)
        os.utime(path, (ancient, ancient))

    assert prune_acknowledgements(project) == 3
    assert os.listdir(manager_ipc.done_dir(project)) == []


def test_pruning_keeps_the_most_recent_records(project, roots):
    for i in range(5):
        write_request(project, "doctor", created_at=1788000000 + i,
                      known_roots=roots)
        acknowledge(project, load_requests(project, roots)[0][0], DRAINED)

    prune_acknowledgements(project, max_records=2)
    assert len(os.listdir(manager_ipc.done_dir(project))) == 2


def test_pruning_never_touches_a_pending_request(project, roots):
    """An active request must not be lost for being old.

    The pruner and the queue are separate concerns; conflating them turns a
    slow week into lost work.
    """
    write_request(project, "doctor", created_at=1700000000, known_roots=roots)
    ancient = time.time() - manager_ipc.MAX_DONE_AGE_SECONDS - 10
    for name in os.listdir(manager_ipc.pending_dir(project)):
        path = os.path.join(manager_ipc.pending_dir(project), name)
        os.utime(path, (ancient, ancient))

    prune_acknowledgements(project)
    assert len(load_requests(project, roots)[0]) == 1


def test_pruning_an_empty_ledger_is_harmless(project):
    assert prune_acknowledgements(project) == 0


# ── The running Manager ───────────────────────────────────────────────────

def test_manager_running_is_false_off_windows(monkeypatch):
    """A negative result, never an exception, on a platform without the mutex."""
    monkeypatch.setattr(manager_ipc.os, "name", "posix")
    assert manager_ipc.manager_running() is False


def test_focus_is_a_negative_result_off_windows(monkeypatch):
    monkeypatch.setattr(manager_ipc.os, "name", "posix")
    result = manager_ipc.focus_manager()
    assert result == {"running": False, "focused": False,
                      "reason": "focus is Windows-only"}


def test_focus_reports_running_and_focused_separately(monkeypatch):
    """Windows routinely refuses `SetForegroundWindow` to a background process.

    "Running but the OS declined to raise it" is a normal outcome with a
    reason, not a failure — collapsing the two would have the extension report
    the Manager as absent whenever the desktop said no.
    """
    monkeypatch.setattr(manager_ipc, "manager_running", lambda: False)
    result = manager_ipc.focus_manager()
    assert result["running"] is False
    assert result["focused"] is False
    assert result["reason"]


def _code_of(fn) -> str:
    """A function's source with its docstring removed.

    The docstrings here name the API that must NOT be called, in order to
    explain why — so a naive substring check over the whole source fails on the
    very comment that documents the rule.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = node.body[1:] if ast.get_docstring(node) else node.body
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_the_mutex_is_never_created_by_the_probe():
    """`OpenMutexW`, not `CreateMutexW`.

    `runtime.py` uses the latter, which *creates* the mutex when it is absent —
    calling that from the CLI would answer the question wrongly AND take the
    GUI's single-instance lock, which `cli.py`'s own contract forbids.
    """
    code = _code_of(manager_ipc.manager_running)
    assert "OpenMutexW" in code
    assert "CreateMutexW" not in code


def test_a_window_title_is_not_treated_as_identity():
    """Any process can create a window called "TokenSave Manager"."""
    assert "_owns_manager_process" in _code_of(manager_ipc._find_manager_window)
