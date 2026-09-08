"""PyScope's MCP entry — user-scoped by design, and why that matters here.

``pyscope mcp`` answers about *registered projects* and takes ``project`` as a
tool argument. It is therefore one server for the whole machine, not one per
project::

    "pyscope": {"type": "stdio", "command": "<pyscope_exe>", "args": ["mcp"]}

That single fact drives everything below.

Wiring PyScope is two things, not one
-------------------------------------
The MCP entry makes the server *reachable*; ``pyscope projects add`` makes a
project *answerable*. An entry without registration replies "unknown project"
to every question about it, so the two states are tracked separately and never
collapsed into one "bound" badge. :func:`reconcile` performs both and reports
each outcome, and it is explicitly **not** a transaction — "registered, MCP
absent" is a permitted outcome that the caller must be able to render.

User scope is correct here, and the manager must not warn about it
------------------------------------------------------------------
Everywhere else in this manager a user-scoped entry is a hazard: it shadows a
project's own ``.mcp.json`` (see the desktop-scope collision and the trust-gate
findings). PyScope has no project-scoped competitor to shadow, so the same
observation carries the opposite meaning. The special case lives in
:data:`helpers.mcp_scope.USER_SCOPED_SERVERS`, keyed on **server identity** —
a predicate on scope alone would silently reclassify tokensave.

Command drift is its own state
------------------------------
The entry embeds the resolved executable at write time. Change the configured
path afterwards and the entry still points at the old binary — present, green
to any presence check, and launching the wrong thing. ``stale_command``
compares *canonical executable identity* rather than strings, because
``C:\\x\\pyscope.exe`` and ``C:/x/pyscope.exe`` are the same binary.

What this module refuses to do
------------------------------
Write into a malformed or unreadable ``~/.claude.json``. That file holds every
MCP server the user has, and rewriting one we could not parse is a data-loss
bug wearing a feature's clothes. ``absent`` and ``malformed`` stay separate for
exactly that reason — absence is never success.

It also never falls back to a bare ``pyscope`` command. The whole point of
caching a resolved path is that the generated config does not depend on
whatever PATH the client happens to have.
"""

from __future__ import annotations

import dataclasses
import json
import os

from helpers.mcp_paths import _claude_json_path, _write_json_atomic


#: The server key written into ``mcpServers``. One name for one thing.
SERVER_KEY = "pyscope"

#: No ``pyscope`` entry in the file.
STATE_ABSENT = "absent"
#: Present, and its command resolves to the configured executable.
STATE_PRESENT = "present"
#: Present, but pointing at a different binary than the one configured now.
STATE_STALE_COMMAND = "stale_command"
#: The file exists and does not parse. NOT the same as absent.
STATE_MALFORMED = "malformed"
#: The file could not be read at all.
STATE_UNREADABLE = "unreadable"

STATE_LABELS = {
    STATE_ABSENT: "No PyScope MCP entry",
    STATE_PRESENT: "PyScope MCP entry present",
    STATE_STALE_COMMAND: "MCP entry points at a different executable",
    STATE_MALFORMED: "~/.claude.json is not valid JSON",
    STATE_UNREADABLE: "~/.claude.json could not be read",
}

#: What :func:`reconcile` was told to do about an entry that already differs.
DRIFT_REPLACE = "replace"
DRIFT_KEEP = "keep"
DRIFT_CANCEL = "cancel"

#: Overall verdicts. `partial` exists because binding is not atomic.
OVERALL_OK = "ok"
OVERALL_PARTIAL = "partial"
OVERALL_FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class BindResult:
    """Outcome of one reconcile pass, with each half reported separately.

    ``overall`` is derived from re-reading both systems afterwards, never from
    the return codes of the steps — a step that reports success while the
    post-read disagrees is a failure, and one the user needs told about.
    """

    overall: str = OVERALL_FAILED
    mcp_changed: bool = False
    registration_changed: bool = False
    mcp_state: str = STATE_ABSENT
    registration: str = "unknown"
    detail: str = ""


# ── Reading ─────────────────────────────────────────────────────────────────

def read_user_mcp(path: str = "") -> "tuple[dict | None, str]":
    """``(data, state)`` for the user-scoped Claude config.

    ``state`` is ABSENT / MALFORMED / UNREADABLE, or "" when the file parsed.
    A missing file is absent with no data; a broken one is malformed WITH no
    data, so a caller cannot accidentally merge into a half-understood dict.
    """
    path = path or _claude_json_path()
    if not os.path.isfile(path):
        return None, STATE_ABSENT
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        return None, STATE_MALFORMED
    except OSError:
        return None, STATE_UNREADABLE
    if not isinstance(data, dict):
        return None, STATE_MALFORMED
    return data, ""


def same_executable(a: str, b: str) -> bool:
    """Whether two command strings name the same binary.

    Compares canonical identity, not text. ``C:/x/pyscope.exe`` and
    ``C:\\x\\pyscope.exe`` are one file, and a byte comparison would report
    drift for a path the user never changed.

    Case is folded on Windows only, for the same reason it is in
    ``helpers/pyscope.same_path``: ``normcase`` is a no-op on POSIX, so a
    comparison written in terms of it passes Linux CI while doing nothing.
    """
    if not a or not b:
        return False
    import sys
    try:
        ra = os.path.realpath(os.path.abspath(os.path.expanduser(a)))
        rb = os.path.realpath(os.path.abspath(os.path.expanduser(b)))
    except (OSError, ValueError):
        return False
    ua = ra.replace("\\", "/").rstrip("/")
    ub = rb.replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        return ua.casefold() == ub.casefold()
    return ua == ub


def binding_state(exe: str, path: str = "") -> "tuple[str, str]":
    """``(state, detail)`` for PyScope's MCP entry.

    ``exe`` is the executable the manager would write today; drift is measured
    against it. Passing "" means "no executable configured", in which case a
    present entry cannot be judged stale — there is nothing to compare it to —
    and is reported as present.
    """
    data, read_state = read_user_mcp(path)
    if read_state in (STATE_MALFORMED, STATE_UNREADABLE):
        return read_state, STATE_LABELS[read_state]
    if data is None:
        return STATE_ABSENT, STATE_LABELS[STATE_ABSENT]

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return STATE_ABSENT, STATE_LABELS[STATE_ABSENT]

    entry = servers.get(SERVER_KEY)
    if not isinstance(entry, dict):
        return STATE_MALFORMED, "The pyscope entry is not an object."
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        return STATE_MALFORMED, "The pyscope entry has no command."
    if exe and not same_executable(command, exe):
        return (STATE_STALE_COMMAND,
                f"The entry runs {command}, but {exe} is configured.")
    return STATE_PRESENT, STATE_LABELS[STATE_PRESENT]


# ── Writing ─────────────────────────────────────────────────────────────────

def canonical_entry(exe: str) -> dict:
    """The entry body the manager writes.

    The resolved executable is embedded rather than a bare ``pyscope``: the
    generated config must not depend on whatever PATH the client happens to
    have, which is the reason the manager resolves and caches the path at all.
    """
    return {"type": "stdio", "command": exe, "args": ["mcp"]}


def bind_user_entry(exe: str, path: str = "") -> "tuple[bool, str]":
    """Write PyScope's server into the user-scoped Claude config.

    Merges. Every other server in the file survives, because this file is the
    user's whole MCP configuration and clobbering it would be far worse than
    the binding it came from.

    Refuses a malformed or unreadable file. That refusal is the entire reason
    those states are not folded into ``absent``.
    """
    if not exe:
        return False, "No PyScope executable configured."
    path = path or _claude_json_path()
    data, read_state = read_user_mcp(path)
    if read_state in (STATE_MALFORMED, STATE_UNREADABLE):
        return False, (f"{STATE_LABELS[read_state]}: {path}. "
                       "Fix or remove it, then try again.")
    if data is None:
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_KEY] = canonical_entry(exe)
    data["mcpServers"] = servers
    return _write_json_atomic(path, data)


def unbind_user_entry(path: str = "") -> "tuple[bool, str]":
    """Remove PyScope's server, leaving every other entry untouched."""
    path = path or _claude_json_path()
    data, read_state = read_user_mcp(path)
    if data is None:
        return (True, "") if read_state == STATE_ABSENT else (
            False, f"{STATE_LABELS[read_state]}: {path}")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return True, ""
    del servers[SERVER_KEY]
    data["mcpServers"] = servers
    return _write_json_atomic(path, data)


# ── Reconciliation ──────────────────────────────────────────────────────────

def reconcile(exe: str, project: str, drift_choice: str = DRIFT_REPLACE,
              path: str = "") -> BindResult:
    """Register the project and ensure the MCP entry, then re-read both.

    **Not a transaction.** Registered-with-no-entry and entry-with-no-
    registration are both reachable, and both are reported rather than hidden
    behind a single verdict.

    Order is deliberate: registration first. It is the narrow, per-project,
    idempotent, independently verifiable step, and advertising a server that
    answers "unknown project" is the worse failure — so the machine-wide
    config write happens only after the project can actually be answered
    about.

    ``drift_choice`` is decided by the caller BEFORE this runs, because asking
    is a main-thread modal and this is worker-thread work. ``DRIFT_KEEP``
    leaves an existing differing entry alone, which is a legitimate choice and
    an explicitly PARTIAL outcome — the user has a working server pointing
    somewhere else, and calling that "bound" would be the false green this
    whole two-state model exists to prevent.
    """
    from helpers.pyscope import (register, registration_state,
                                 REG_REGISTERED, REG_UNKNOWN)

    if drift_choice == DRIFT_CANCEL:
        state, detail = binding_state(exe, path)
        return BindResult(
            overall=OVERALL_FAILED, mcp_state=state,
            registration=registration_state(exe, project),
            detail="Cancelled; nothing was changed.")

    # ── 1. Registration ─────────────────────────────────────────────────
    reg = register(exe, project)
    notes = [reg.detail]

    # ── 2. The MCP entry ────────────────────────────────────────────────
    before_state, _ = binding_state(exe, path)
    mcp_changed = False
    if before_state in (STATE_MALFORMED, STATE_UNREADABLE):
        notes.append(STATE_LABELS[before_state] + " — left untouched.")
    elif before_state == STATE_STALE_COMMAND and drift_choice == DRIFT_KEEP:
        notes.append("Kept the existing MCP entry at the user's request.")
    elif before_state != STATE_PRESENT:
        ok, why = bind_user_entry(exe, path)
        mcp_changed = ok
        notes.append("Wrote the PyScope MCP entry." if ok
                     else f"Could not write the MCP entry: {why}")

    # ── 3. Verdict from the observed post-state, never the return codes ──
    after_state, after_detail = binding_state(exe, path)
    after_reg = registration_state(exe, project)

    if after_state == STATE_PRESENT and after_reg == REG_REGISTERED:
        overall = OVERALL_OK
    elif after_reg == REG_UNKNOWN or after_state in (STATE_MALFORMED,
                                                     STATE_UNREADABLE):
        overall = OVERALL_PARTIAL
    elif after_state == STATE_PRESENT or after_reg == REG_REGISTERED:
        # One half landed. Deliberately not OK: an entry the server cannot
        # answer through, or a registration nothing can reach, is half a
        # feature and the user has to be able to see which half.
        overall = OVERALL_PARTIAL
    else:
        overall = OVERALL_FAILED

    if after_state == STATE_STALE_COMMAND:
        notes.append(after_detail)

    return BindResult(
        overall=overall,
        mcp_changed=mcp_changed,
        registration_changed=reg.changed,
        mcp_state=after_state,
        registration=after_reg,
        detail="  ".join(n for n in notes if n),
    )
