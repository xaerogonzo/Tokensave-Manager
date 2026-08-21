"""PinWatcherController — make ★ Set as Active take effect without a restart.

Claude Desktop spawns its MCP server once and keeps it. ``tokensave-wrapper``
reads the pin file *at spawn* and passes the chosen project as ``-p``, so
changing the pin does nothing until Desktop is restarted — the running server
goes on serving whatever was pinned when it started. Observed in this very
repository: the pin said one project while Desktop's two servers were serving
another.

``docs/MCP_INTEGRATION_GOTCHAS.md`` deferred the fix and named its blocker
exactly:

    The watcher needs a reliable way to discover the tokensave PID — probably
    by scanning `Win32_Process` for `tokensave.exe serve` instances…

``helpers/tokensave_daemon.list_tokensave_servers`` is that, so the deferred
"Option A" is now small: notice the pin changed, end the stale server, and let
Desktop's own supervisor respawn the wrapper, which reads the new pin. The
wrapper stays untouched and single-threaded, which is the whole reason the
earlier in-wrapper attempt was reverted.

## What it will and will not kill

Only a server this manager can *prove* belongs to the wrapper: one carrying a
run record (Roadmap-10 phase B) whose recorded project differs from the new
pin. That is a deliberately narrow target, and everything else is left alone:

* a ``heuristic``/``ambiguous``/``unattributed`` row — the four-state contract
  in ``helpers/tokensave_daemon`` exists precisely so a guess never becomes a
  kill, and a pin change is not new evidence;
* any server without a wrapper record — a Claude Code session binds itself per
  project and has nothing to do with the pin;
* a server already serving the newly-pinned project — nothing to do.

Termination goes through ``proc_kill.kill_process(expect=…)``, so a PID
recycled between the scan and the kill is refused rather than hit.

## Honest scope

This works **only while the manager is running.** Close it and pin changes go
back to needing a Desktop restart. A standalone always-on daemon would remove
that caveat and is a much larger commitment; this is the version that fits the
process the user already has open. The Settings text says so — a limitation
that lives only in a docstring is one the user discovers by being confused.

Off by default: it ends a live MCP server, which should be something you
switched on rather than something that started happening.

No Tk. Logging goes through the injected thread-safe callback, the same
contract ``UpdatePollerController`` uses.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Callable

from constants import C, desktop_project_file
from helpers.proc_kill import kill_process
from helpers.tokensave_daemon import read_wrapper_records

if TYPE_CHECKING:
    from state import ManagerConfig

#: How often the pin file is checked. Two seconds is the figure the deferred
#: design in MCP_INTEGRATION_GOTCHAS.md proposed; a stat() of one small file
#: is cheap enough that there is no reason to be cleverer.
POLL_INTERVAL_S = 2.0

#: Config key. Absent means off, which is the default.
ENABLED_KEY = "pin_watcher_enabled"


class PinWatcherController:
    """Watch the active-project pin and retire a server left on the old one."""

    def __init__(self, cfg: "ManagerConfig", on_log: Callable) -> None:
        self._cfg = cfg
        self._on_log = on_log
        self._stop = threading.Event()
        self._last_pin: "str | None" = None

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Read at every tick, not cached, so Settings applies immediately."""
        return bool(self._cfg.raw.get(ENABLED_KEY, False))

    def start(self) -> None:
        """Begin watching. Safe to call when disabled — the loop no-ops."""
        # The pin as it stands now is the baseline, not a change: starting the
        # manager should never retire a server that was already correct.
        self._last_pin = _read_pin()
        threading.Thread(target=self._loop, daemon=True,
                         name="tokensave-pin-watch").start()

    def stop(self) -> None:
        """Ask the loop to exit — used on app shutdown and by tests."""
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL_S):
            try:
                self.tick()
            except Exception:                      # noqa: BLE001
                # A watcher that dies silently is worse than one that misses a
                # tick: the feature would appear to work and simply stop.
                from helpers.runtime import log
                log.exception("pin watcher tick failed")

    # ── one pass ─────────────────────────────────────────────────────────

    def tick(self) -> "list[int]":
        """Check the pin once. Returns the PIDs retired (usually none).

        Split out from the loop so the decision logic can be tested without
        threads or timing.
        """
        if not self.enabled:
            # Still track the pin while disabled, so enabling the setting does
            # not immediately fire on a change that happened while it was off.
            self._last_pin = _read_pin()
            return []

        pin = _read_pin()
        if pin == self._last_pin:
            return []
        self._last_pin = pin
        if not pin:
            # The pin was cleared. The wrapper would now fall back to
            # most-recently-indexed, which is not an improvement on whatever
            # is running — so leave it be rather than churn a live session.
            return []
        return self._retire_servers_on_other_projects(pin)

    def _retire_servers_on_other_projects(self, pin: str) -> "list[int]":
        stale = [s for s in self._wrapper_servers()
                 if _norm(s["project"]) != _norm(pin)]
        retired = []
        for server in stale:
            ok, detail = kill_process(server["pid"], tree=False, graceful=True,
                                      expect=server["identity"])
            name = os.path.basename(server["project"]) or server["project"]
            if ok:
                retired.append(server["pid"])
                self._on_log(
                    "  Pin changed — retired the tokensave server for %s "
                    "(pid %d). Claude Desktop will restart it on the newly "
                    "pinned project." % (name, server["pid"]), C["green"])
            else:
                self._on_log(
                    "  Pin changed, but the server for %s (pid %d) could not "
                    "be stopped: %s" % (name, server["pid"], detail),
                    C["peach"])
        return retired

    def _wrapper_servers(self) -> list:
        """Live servers this manager's wrapper started, with their projects.

        A wrapper run record is the proof of ownership. Everything without one
        is somebody else's — most often a Claude Code session, which binds
        itself per project and is not driven by the pin at all.
        """
        from helpers.project_discovery import find_projects
        from helpers.tokensave_daemon import (
            AUTHORITATIVE, list_tokensave_servers,
        )

        records = read_wrapper_records()
        if not records:
            return []
        projects = [p["path"] for p in find_projects(self._cfg.search_roots)]
        out = []
        for server in list_tokensave_servers(self._cfg.tokensave_exe or "",
                                             projects):
            record = records.get(server.pid)
            if not record or not record.get("project"):
                continue
            if server.attribution != AUTHORITATIVE or not server.identity:
                # Belt and braces. A wrapper server carries `-p`, so it should
                # always be authoritative; if it somehow is not, this is a
                # guess, and guesses are never killed.
                continue
            out.append({"pid": server.pid,
                        "project": record["project"],
                        "identity": server.identity})
        return out


# ── helpers ──────────────────────────────────────────────────────────────

def _read_pin() -> str:
    """The pinned project path, or "" when unset/unreadable."""
    try:
        with open(desktop_project_file(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _norm(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    except (OSError, ValueError):
        return os.path.normcase(path)
