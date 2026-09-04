"""tests/test_ipc_second_producer.py — the extension as a second producer.

`helpers/manager_ipc.py` was written for one producer and treats the request
file as untrusted input regardless, validating at write time and again at drain
time. The VS Code extension is now a second producer, and a second producer is
exactly when a protocol's implicit assumptions get discovered.

The property under test is **identity**. `write_request` derives a request id
from a SHA-256 over the canonicalised request, and `canonical_project` runs
`realpath` before `normcase` because two spellings of one directory must not
produce two authorization verdicts. The extension therefore files requests
*through the CLI* rather than building them itself — the point of these tests
is that nothing on the TypeScript side needs to know how an id is made, and
that the guarantees hold when the same request arrives spelled differently.

None of these open a dialog: they exercise the write and validation halves,
which is where a second producer can go wrong silently.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from helpers import commands, manager_ipc  # noqa: E402


@pytest.fixture()
def project(tmp_path):
    """A project the Manager would accept, with a real directory behind it."""
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _roots(project):
    """Search roots that contain *project* and nothing beside it.

    The project itself, not its parent: a parent root would also contain every
    sibling directory, and the containment test below would pass for a
    directory it is supposed to refuse.
    """
    return [project]


# ── what the extension actually files ───────────────────────────────────────

def test_every_exposed_action_is_accepted_by_the_inbox(project):
    """Each dialog the extension offers must survive a real write.

    The table and the allowlist agreeing is asserted elsewhere; this asserts
    the stronger thing, which is that filing one actually works — an action can
    be allowlisted and still be rejected by its payload schema.
    """
    for action in commands.MANAGER_ACTIONS:
        written = manager_ipc.write_request(
            project, action.action, {}, known_roots=_roots(project))
        assert written["id"], action.action


def test_a_filed_request_validates_on_the_drain_side(project):
    """Write-time validation passing is not the same as drain-time passing."""
    written = manager_ipc.write_request(
        project, "doctor", {}, known_roots=_roots(project))
    with open(written["path"], encoding="utf-8") as fh:
        payload = json.load(fh)
    # Raises RequestError if the drain side would refuse it.
    validated = manager_ipc.validate(
        payload, known_roots=_roots(project))
    assert validated["action"] == "doctor"


def test_the_extension_never_needs_to_send_a_payload(project):
    """Four of the five dialogs take no payload at all, and `open-project`
    deliberately does not take a project — the envelope already carries one,
    and two possible project identities in a security-relevant message is an
    ambiguity rather than a convenience."""
    for action in commands.MANAGER_ACTIONS:
        required, optional = manager_ipc.PAYLOAD_SCHEMA[action.action]
        assert required == (), (action.action, required)
        assert optional == (), (action.action, optional)


def test_an_action_outside_the_allowlist_is_refused(project):
    """`--action` is validated by choices at the CLI too, but the protocol
    must refuse it on its own — the CLI is not the only thing that can write
    into this directory."""
    with pytest.raises(manager_ipc.RequestError):
        manager_ipc.write_request(project, "rm -rf", {},
                                  known_roots=_roots(project))


# ── identity, which is the whole reason this goes through the CLI ───────────

def test_the_same_request_twice_is_one_request(project):
    """The id is a hash of what is being asked for, so re-filing is a no-op.

    This is what lets the extension report "already queued" instead of
    pretending it filed a second one.
    """
    first = manager_ipc.write_request(project, "doctor", {},
                                      known_roots=_roots(project))
    second = manager_ipc.write_request(project, "doctor", {},
                                       known_roots=_roots(project))
    assert first["id"] == second["id"]
    assert second["duplicate"] is True


def test_different_actions_get_different_ids(project):
    """A hash that collided across actions would let one dialog's
    acknowledgement answer for another's."""
    ids = {manager_ipc.write_request(project, action.action, {},
                                     known_roots=_roots(project))["id"]
           for action in commands.MANAGER_ACTIONS}
    assert len(ids) == len(commands.MANAGER_ACTIONS)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="case-insensitive path spelling is a Windows "
                           "property; normcase is a no-op on POSIX")
def test_two_windows_spellings_of_one_project_are_one_identity(project):
    """The trap a second producer walks into.

    An editor passes whatever `folder.uri.fsPath` gives it, which is not
    guaranteed to match the spelling a shell produced. If `C:\\Proj` and
    `c:\\proj` hashed differently, the extension would file a second request
    for a project that already had one pending and then poll an id the
    Manager was never going to acknowledge.

    `canonical_project` does `realpath` before `normcase` precisely for this.
    Note `normcase` is a no-op on POSIX, which this codebase has been caught by
    in CI before — hence the skip rather than a cross-platform assertion.
    """
    lower = project.lower()
    upper = project[0].upper() + project[1:]
    assert lower != upper or project == lower

    first = manager_ipc.write_request(lower, "doctor", {},
                                      known_roots=_roots(project))
    second = manager_ipc.write_request(upper, "doctor", {},
                                       known_roots=_roots(project))
    assert first["id"] == second["id"], (
        "two spellings of one project produced two request identities")
    assert second["duplicate"] is True


def test_a_trailing_separator_does_not_make_a_second_identity(project):
    """`fsPath` and a shell disagree about trailing separators often enough
    that this is worth pinning on every platform."""
    first = manager_ipc.write_request(project, "doctor", {},
                                      known_roots=_roots(project))
    second = manager_ipc.write_request(project + os.sep, "doctor", {},
                                       known_roots=_roots(project))
    assert first["id"] == second["id"]


def test_a_dot_segment_resolves_to_the_same_identity(project):
    """`realpath` before hashing, so `p/sub/..` is `p`."""
    indirect = os.path.join(project, "sub", "..")
    os.makedirs(os.path.join(project, "sub"), exist_ok=True)
    first = manager_ipc.write_request(project, "doctor", {},
                                      known_roots=_roots(project))
    second = manager_ipc.write_request(indirect, "doctor", {},
                                       known_roots=_roots(project))
    assert first["id"] == second["id"]


# ── authorization is not an argument ────────────────────────────────────────

def test_a_project_outside_the_search_roots_is_refused(project, tmp_path):
    """`--project` is an argument, not an authorization.

    Without this an editor — or anything else able to write the file — could
    steer the Manager's GUI at any directory on the machine.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(manager_ipc.RequestError):
        manager_ipc.write_request(str(outside), "doctor", {},
                                  known_roots=_roots(project))


def test_a_project_that_is_not_a_directory_is_refused(tmp_path):
    missing = str(tmp_path / "nope")
    with pytest.raises(manager_ipc.RequestError):
        manager_ipc.write_request(missing, "doctor", {}, known_roots=None)


# ── the ledger keeps its distinctions ───────────────────────────────────────

def test_a_filed_request_reads_back_as_pending_not_unknown(project):
    """The distinction the extension renders as two different messages."""
    written = manager_ipc.write_request(project, "doctor", {},
                                        known_roots=_roots(project))
    state = manager_ipc.status_of(project, written["id"], project=project)
    assert state["state"] == manager_ipc.PENDING


def test_an_id_that_was_never_filed_is_unknown_not_pending(project):
    """Absence is never success, and it is never "still waiting" either."""
    state = manager_ipc.status_of(project, "deadbeefcafe", project=project)
    assert state["state"] == manager_ipc.UNKNOWN
