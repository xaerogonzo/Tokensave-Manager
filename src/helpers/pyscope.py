"""PyScope integration client — the Manager's only route to PyScope.

Modelled on PolyScour's ``integrations/polyshield.py``: the Manager is a
complete application without PyScope, and if PyScope happens to be installed a
handful of surfaces gain real answers instead of being absent. That is the whole
relationship. Nothing here can ask PyScope to change a project, and PyScope
being installed never grants the Manager new authority over it.

Read-only vs mutating
---------------------
    read-only   is_available · version · status · registered ·
                registration_state · analyze
    spawns      launch_gui   (starts PyScope's window; changes no PyScope state)
    mutating    register

``register`` is the ONE call that changes PyScope's state, and it must only ever
be reached from an explicit user action — a menu item the user clicked, never a
refresh, a poll, or a side effect of opening a tab. An earlier draft of this
module called itself "the read-only client" while shipping ``register``, which
is exactly the kind of comment that stops being true without anyone noticing.

Why every call goes through one ``_run``
----------------------------------------
A binary can fail in ways that are not the same failure: missing, present but
unlaunchable, spawned and hung, exited non-zero, or answering with text nobody
can parse. Five call sites writing their own ``subprocess.run`` is how those
five end up handled four different ways. ``_run`` decides once, and returns a
record rather than ``None`` — because the diagnostic surface needs, one call
later, precisely the detail a bare ``None`` throws away.

Configured is not executable is not healthy
-------------------------------------------
``cfg.pyscope_exe`` being non-empty proves a path is *configured*. It does not
prove the file is launchable, and launching it does not prove PyScope answers
sanely. ``status()`` reports all three separately so the UI never has to infer
one from another; "Configured: yes / Executable: yes / Status: unhealthy" is a
useful thing to tell someone, and "PyScope installed" is not.

Public interfaces only
----------------------
Every function here shells the documented ``pyscope`` CLI. Nothing reads
``.pyscope/`` internals, PyScope's registry file, or its cache database — if an
answer is not available through the CLI, the fix is a PyScope CLI addition, not
a shortcut into its private storage. PyScope reciprocates: it never reads
``manager-config.json``.

Pure module — no Tkinter, no globals. Safe to call from any thread.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time

from constants import CREATE_NO_WINDOW


# ── Timeouts ────────────────────────────────────────────────────────────────
#
# `version` and `projects list` read a small file and return; `analyze` walks a
# whole source tree and legitimately takes minutes on a large one. One timeout
# for both would either abort real work or make a hung probe feel like a hang.

TIMEOUT_PROBE = 20
TIMEOUT_REGISTER = 120
TIMEOUT_ANALYZE = 900

#: Shorter ceiling for probes that run while a dialog is opening. The Tool
#: Manager refreshes every row synchronously, so a hung binary there freezes
#: the window — `install_codegraph.codegraph_version` bounds itself at 5s for
#: exactly this reason and this matches it.
TIMEOUT_DIALOG_PROBE = 5


# ── Status vocabulary ───────────────────────────────────────────────────────

#: No path configured, or the configured path is not a launchable file.
STATE_ABSENT = "absent"
#: The binary launched but failed — non-zero exit, timeout, or a spawn error.
STATE_UNHEALTHY = "unhealthy"
#: The binary ran and succeeded, but its output could not be understood.
STATE_MALFORMED = "malformed_output"
#: Ran, succeeded, and answered in the shape we expected.
STATE_OK = "ok"

#: This project is in PyScope's registry.
REG_REGISTERED = "registered"
#: PyScope answered, and this project is not in its registry.
REG_UNREGISTERED = "unregistered"
#: PyScope could not be asked. NOT the same as "no" — never render it as one.
REG_UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class _Run:
    """One subprocess attempt, with the failure kept rather than collapsed."""

    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_error: str = ""

    @property
    def launched(self) -> bool:
        """Whether the process started at all (as opposed to failing to spawn)."""
        return not self.spawn_error

    @property
    def ok(self) -> bool:
        return self.launched and not self.timed_out and self.returncode == 0


@dataclasses.dataclass(frozen=True)
class Status:
    """What the diagnostic surfaces render. Three axes, none inferred."""

    configured: str = ""
    executable: bool = False
    state: str = STATE_ABSENT
    version: str = ""
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class RegisterResult:
    """Outcome of one ``pyscope projects add``.

    ``already_present`` is a SUCCESS. PyScope's ``Registry.add`` registers or
    updates under the same identity, so re-registering a known project is
    idempotent — reporting it as a failure would make the Manager's bulk
    "Register with PyScope" action look broken the second time it is run.
    """

    ok: bool = False
    changed: bool = False
    already_present: bool = False
    detail: str = ""


# ── The single subprocess boundary ──────────────────────────────────────────

def _run(exe: str, argv: list, timeout: int) -> _Run:
    """Run one pyscope command. Never raises; always returns a record.

    Windows console suppression is not optional here — every subprocess in this
    manager passes CREATE_NO_WINDOW, or a console flashes on each invocation.
    The constant is 0 off-Windows, so this is portable as written.
    """
    if not exe:
        return _Run(spawn_error="no pyscope executable configured")
    try:
        proc = subprocess.run(
            [exe, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return _Run(timed_out=True)
    except (OSError, ValueError) as exc:
        # OSError covers "not found", "is a directory" and "not executable";
        # ValueError covers a malformed argv. All are "it did not start".
        return _Run(spawn_error=str(exc))
    return _Run(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _first_line(text: str) -> str:
    """First non-empty line of `text`, stripped. "" when there is none."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _last_line(run: "_Run") -> str:
    """The most informative line of a failed run — stderr wins over stdout."""
    for stream in (run.stderr, run.stdout):
        lines = [ln.strip() for ln in (stream or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1]
    return "no output"


# ── Paths ───────────────────────────────────────────────────────────────────

def canonical_project(path: str) -> str:
    """The one canonical form of a project path crossing this boundary.

    Called exactly here — controllers pass whatever they hold and let this
    module normalise it. Two canonicalisers with slightly different rules is
    how a project ends up registered twice under paths that differ only by a
    trailing separator.
    """
    if not path:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def same_path(a: str, b: str) -> bool:
    r"""Whether two paths name the same location, per the RUNTIME platform.

    Separator normalisation and case folding are two different behaviours
    applied for two different reasons. Separators are unified everywhere,
    because ``src\foo.py`` and ``src/foo.py`` are the same file on any platform
    that accepts both spellings. Case is folded ONLY on Windows, because
    ``Src/Foo.py`` and ``src/foo.py`` are the same file there and are two
    different files on POSIX.

    The decision is made from ``sys.platform``, never from how the strings
    happen to be spelled — ``os.path.normcase`` is a no-op on POSIX, so a
    comparison written in terms of it passes Linux CI while doing nothing.
    """
    if not a or not b:
        return False
    unified_a = os.path.normpath(a).replace("\\", "/").rstrip("/")
    unified_b = os.path.normpath(b).replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        return unified_a.casefold() == unified_b.casefold()
    return unified_a == unified_b


# ── Read-only surface ───────────────────────────────────────────────────────

def version(exe: str) -> str:
    """PyScope's version string, or "".

    Uses the ``version`` SUBCOMMAND. ``pyscope --version`` is not an option and
    exits non-zero with a usage error, so a probe written against it would
    report every healthy install as broken.
    """
    run = _run(exe, ["version"], TIMEOUT_PROBE)
    return _first_line(run.stdout) if run.ok else ""


def is_available(exe: str) -> bool:
    """Whether PyScope is present, launchable and answering."""
    return bool(version(exe))


def status(exe: str, timeout: int = TIMEOUT_PROBE) -> Status:
    """Full diagnostic state — the one function that keeps the failure detail.

    Everything else here collapses an unusable answer to None/[]/False so a
    caller cannot accidentally build a fact out of a failure. This is where the
    difference between "not installed" and "installed but crashing" survives,
    because that difference is the whole reason someone opens the status row.
    """
    if not exe:
        return Status(detail="No PyScope executable configured or detected.")
    if not os.path.isfile(exe):
        return Status(configured=exe, executable=False, state=STATE_ABSENT,
                      detail="The configured path does not name a file.")

    run = _run(exe, ["version"], timeout)
    if not run.launched:
        return Status(exe, False, STATE_ABSENT,
                      detail="Could not launch: " + run.spawn_error)
    if run.timed_out:
        return Status(exe, True, STATE_UNHEALTHY,
                      detail="pyscope version did not answer within "
                             f"{timeout}s.")
    if run.returncode != 0:
        return Status(exe, True, STATE_UNHEALTHY,
                      detail=f"pyscope version exited {run.returncode}: "
                             + _last_line(run))

    ver = _first_line(run.stdout)
    if not ver:
        return Status(exe, True, STATE_MALFORMED,
                      detail="pyscope version succeeded but printed nothing.")
    return Status(exe, True, STATE_OK, version=ver, detail="PyScope " + ver)


def registered(exe: str):
    """Every project in PyScope's registry, or None if it could not be asked.

    None and [] are different answers — None means "PyScope did not tell us",
    [] means "PyScope told us there are none" — so callers must not treat a
    falsy result as an empty registry.
    """
    run = _run(exe, ["projects", "list", "--json"], TIMEOUT_PROBE)
    if not run.ok:
        return None
    try:
        data = json.loads(run.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return [entry for entry in data if isinstance(entry, dict)]


def registration_state(exe: str, project: str) -> str:
    """Tri-state: is ``project`` registered with PyScope?

    Returns REG_UNKNOWN when PyScope could not be asked. That is deliberately
    not REG_UNREGISTERED: one means "PyScope does not know this project", the
    other means "we do not know what PyScope knows", and a UI that renders them
    identically will tell users to register a project that already is.
    """
    canon = canonical_project(project)
    if not canon:
        return REG_UNKNOWN
    entries = registered(exe)
    if entries is None:
        return REG_UNKNOWN
    for entry in entries:
        if same_path(str(entry.get("root", "")), canon):
            return REG_REGISTERED
    return REG_UNREGISTERED


def analyze(exe: str, project: str):
    """Run ``pyscope analyze --json`` and return its stats dict, or None.

    Returns None on every unusable outcome, so no caller can build a fact from
    a failure. Use ``status()`` when the reason matters.
    """
    canon = canonical_project(project)
    if not canon or not os.path.isdir(canon):
        return None
    run = _run(exe, ["analyze", canon, "--json"], TIMEOUT_ANALYZE)
    if not run.ok:
        return None
    try:
        data = json.loads(run.stdout)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


#: How long to watch a spawned GUI before deciding it launched successfully.
#: Without the `gui` extra, `pyscope gui` reports how to install it and exits at
#: once, so an early exit inside this window is worth reporting; anything later
#: is the user closing their own window, which is not our business.
GUI_EARLY_EXIT_SECONDS = 4.0


def launch_gui(exe: str, project: str) -> "tuple[bool, str]":
    """Open PyScope's desktop app on `project`. Returns (launched, detail).

    Lives here rather than in the controller so that every process this
    manager starts for PyScope goes through one module — a `Popen` in a
    controller is how the CREATE_NO_WINDOW rule and the timeout policy end up
    with a second, divergent implementation.

    Detached, but watched briefly: launching and then walking away would
    swallow the "install the gui extra" message and make the menu item look
    like a button that does nothing.
    """
    canon = canonical_project(project)
    if not canon or not os.path.isdir(canon):
        return False, "No longer a directory: " + str(project)
    try:
        proc = subprocess.Popen(
            [exe, "gui", canon],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, ValueError) as exc:
        return False, "Could not launch PyScope: " + str(exc)

    deadline = time.monotonic() + GUI_EARLY_EXIT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    if proc.poll() is None:
        return True, "PyScope window opened."

    # It died immediately. Re-run captured: the failure is a missing import, so
    # it fails again at once and this time we can read why.
    again = _run(exe, ["gui", canon], TIMEOUT_PROBE)
    return False, _last_line(again)


# ── The one mutating call ───────────────────────────────────────────────────

def register(exe: str, project: str) -> RegisterResult:
    """Register ``project`` with PyScope. Explicit user action only.

    Revalidates the directory immediately before invoking PyScope. A project
    can be discovered, listed, and then moved or deleted before the user gets
    around to clicking — without this check that race surfaces as a subprocess
    error dialog instead of an ordinary "no longer there" result.

    The verdict comes from re-reading PyScope's registry, not from the exit
    code: a command that reports success while the registry still disagrees is
    a failure, and it is one the user needs told about rather than hidden.
    """
    canon = canonical_project(project)
    if not canon:
        return RegisterResult(detail="No project path given.")
    if not os.path.isdir(canon):
        return RegisterResult(detail="No longer a directory: " + canon)

    before = registration_state(exe, canon)
    run = _run(exe, ["projects", "add", canon], TIMEOUT_REGISTER)
    if not run.launched:
        return RegisterResult(detail="Could not launch PyScope: " + run.spawn_error)
    if run.timed_out:
        return RegisterResult(
            detail=f"pyscope projects add timed out after {TIMEOUT_REGISTER}s.")
    if run.returncode != 0:
        return RegisterResult(
            detail=f"pyscope projects add exited {run.returncode}: " + _last_line(run))

    after = registration_state(exe, canon)
    if after == REG_UNKNOWN:
        return RegisterResult(
            detail="pyscope projects add reported success, but the registry "
                   "could not be re-read.")
    if after != REG_REGISTERED:
        return RegisterResult(
            detail="pyscope projects add reported success, but the project is "
                   "still unregistered.")

    already = before == REG_REGISTERED
    return RegisterResult(
        ok=True,
        changed=not already,
        already_present=already,
        detail="Already registered; entry refreshed." if already
               else "Registered with PyScope.",
    )
