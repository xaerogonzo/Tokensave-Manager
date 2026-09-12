"""PyScopeController — the PyScope commands for the Projects tab.

Same dependency contract as ``CodeGraphController``: the controller receives
only what it needs and holds no reference to the Projects tab controller.

Dependency contract:
  • cfg        — read-only ManagerConfig (needs .pyscope_exe)
  • tab        — the Projects tk.Frame; used for after() scheduling and
                 winfo_toplevel() dialog parenting
  • on_log     — thread-safe log callback  (msg: str, colour: str)
  • on_settings — (page: str) -> None, opens Settings on that page when
                  PyScope is not installed

Two rules this controller exists to keep
----------------------------------------
**Registration is never automatic.** ``cmd_register`` is reachable only from the
menu item the user clicks. Nothing here registers a project as a side effect of
opening a tab, refreshing, or checking status — `helpers/pyscope.register` is
the Manager's only mutation of PyScope's state, and it stays that way.

**Nothing reads ``.pyscope/`` internals.** Every answer comes from the
documented CLI through ``helpers/pyscope``. The one exception is
``_is_pyscope_project``, which asks the filesystem whether a *cache directory
exists* — a question about this project's layout, not about PyScope's data.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from helpers.detection import _is_pyscope_project
from theme import C

if TYPE_CHECKING:
    from state import ManagerConfig


class PyScopeController:
    """PyScope analyze / open / register / status for a selected project."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable[[str, str], None],
        on_settings: Callable[..., None],
    ) -> None:
        self._tab = tab
        self._cfg = cfg
        self._on_log = on_log
        self._on_settings = on_settings

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Guard ─────────────────────────────────────────────────────────────

    def _require_installed(self) -> bool:
        """True when PyScope is configured and launchable; else offer Settings.

        Checks the file, not the health of the install. A broken PyScope should
        reach the command and fail with its own message rather than be reported
        here as "not installed" — those are different problems with different
        fixes, and Settings → PyScope is where the difference is shown.
        """
        exe = self._cfg.pyscope_exe
        if exe and os.path.isfile(exe):
            return True
        if messagebox.askyesno(
                "PyScope is not installed",
                "PyScope was not found on this machine.\n\n"
                "PyScope explains a codebase from deterministic analysis — it "
                "reports how much of a relationship is actually established, "
                "rather than asserting one. It is optional; the manager works "
                "without it.\n\n"
                "Open Settings to point at it?",
                parent=self._root):
            self._on_settings("integrations")
        return False

    # ── Commands ──────────────────────────────────────────────────────────

    def cmd_analyze(self, path: str) -> None:
        """Run `pyscope analyze --json` and log what it established."""
        if not self._require_installed():
            return
        name = os.path.basename(path)
        self._on_log(f"Running pyscope analyze in {name}…", C["peach"])

        def worker():
            from helpers.pyscope import analyze, status
            stats = analyze(self._cfg.pyscope_exe, path)
            if stats is None:
                # analyze() collapses every failure; ask status() what happened
                # so the log says which failure rather than just "it failed".
                probe = status(self._cfg.pyscope_exe)
                self._on_log(f"  pyscope analyze failed — {probe.detail}", C["red"])
                return
            for line in self._summarise(stats):
                self._on_log(f"  {line}", C["green"])

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _summarise(stats: dict) -> list:
        """Turn `analyze --json` into a few log lines.

        Reports the confidence breakdown because that is the thing PyScope
        knows that the other two tools do not — a symbol count is available
        anywhere, whereas "how much of this is actually established" is the
        reason to run PyScope at all.
        """
        lines = [
            f"files {stats.get('files_indexed', '?')}  "
            f"symbols {stats.get('symbols', '?')}  "
            f"edges {stats.get('edges', '?')}"
        ]
        confidence = stats.get("confidence")
        if isinstance(confidence, dict) and confidence:
            parts = ", ".join(f"{k} {v}" for k, v in confidence.items())
            lines.append(f"confidence: {parts}")
        failures = stats.get("parse_failures")
        if failures:
            lines.append(f"parse failures: {failures}")
        return lines

    def cmd_open_gui(self, path: str) -> None:
        """Launch PyScope's desktop app on this project.

        The spawn itself lives in ``helpers/pyscope.launch_gui`` rather than
        here: a ``Popen`` in a controller is how the CREATE_NO_WINDOW rule and
        the timeout policy acquire a second implementation that drifts from
        the first.
        """
        if not self._require_installed():
            return
        name = os.path.basename(path)
        self._on_log(f"Opening {name} in PyScope…", C["peach"])

        def worker():
            from helpers.pyscope import launch_gui
            launched, detail = launch_gui(self._cfg.pyscope_exe, path)
            self._on_log(f"  {detail}", C["green"] if launched else C["red"])

        threading.Thread(target=worker, daemon=True).start()

    def cmd_register(self, path: str) -> None:
        """Register this project with PyScope. Explicit user action only."""
        if not self._require_installed():
            return
        name = os.path.basename(path)
        self._on_log(f"Registering {name} with PyScope…", C["peach"])

        def worker():
            from helpers.pyscope import register
            result = register(self._cfg.pyscope_exe, path)
            colour = C["green"] if result.ok else C["red"]
            self._on_log(f"  {result.detail}", colour)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_bind(self, path: str) -> None:
        """Make this project answerable through PyScope's MCP server.

        Two separate things, reported separately (see helpers/mcp_pyscope):
        `pyscope projects add` makes the project answerable, and the
        user-scoped MCP entry makes the server reachable. Neither implies the
        other, and this is a best-effort reconciliation rather than a
        transaction — "registered, entry absent" is a real outcome the log has
        to be able to show.

        The drift question is asked HERE, on the Tk thread, before the worker
        starts. A modal from a worker would need a cross-thread bridge for a
        question that costs one cheap file read to answer up front.
        """
        if not self._require_installed():
            return
        from helpers.mcp_pyscope import (binding_state, STATE_STALE_COMMAND,
                                         DRIFT_REPLACE, DRIFT_KEEP, DRIFT_CANCEL)
        exe = self._cfg.pyscope_exe
        state, detail = binding_state(exe)

        choice = DRIFT_REPLACE
        if state == STATE_STALE_COMMAND:
            # Three answers, so askyesnocancel rather than a yes/no: keeping a
            # differing entry is a legitimate choice, and forcing it into "no"
            # would make declining a replacement indistinguishable from
            # cancelling the whole action.
            answer = messagebox.askyesnocancel(
                "PyScope MCP entry differs",
                f"{detail}\n\n"
                "Replace it with the configured executable?\n\n"
                "Yes  — point the entry at the configured executable\n"
                "No   — keep the existing entry as it is\n"
                "Cancel — change nothing",
                parent=self._root)
            if answer is None:
                choice = DRIFT_CANCEL
            elif answer:
                choice = DRIFT_REPLACE
            else:
                choice = DRIFT_KEEP

        name = os.path.basename(path)
        self._on_log(f"Binding {name} to PyScope…", C["peach"])

        def worker():
            from helpers.mcp_pyscope import (reconcile, OVERALL_OK,
                                             OVERALL_PARTIAL, STATE_LABELS)
            result = reconcile(exe, path, choice)
            colour = (C["green"] if result.overall == OVERALL_OK
                      else C["yellow"] if result.overall == OVERALL_PARTIAL
                      else C["red"])
            self._on_log(f"  {result.overall}: {result.detail}", colour)
            # Both halves, always, and never merged into one verdict.
            self._on_log(f"  mcp: {STATE_LABELS.get(result.mcp_state, result.mcp_state)}",
                         C["overlay0"])
            self._on_log(f"  registration: {result.registration}", C["overlay0"])

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self, path: str) -> None:
        """Report what PyScope knows about this project.

        Registration and cache location are separate answers and are printed as
        separate lines. A project can be registered while keeping its cache in
        user data, and `.pyscope/` can exist for a project PyScope has since
        forgotten; collapsing the two would misreport both cases.
        """
        if not self._require_installed():
            return
        name = os.path.basename(path)
        self._on_log(f"PyScope status for {name}…", C["peach"])

        def worker():
            from helpers.pyscope import (registration_state, status,
                                         REG_REGISTERED, REG_UNKNOWN)
            probe = status(self._cfg.pyscope_exe)
            self._on_log(f"  binary: {probe.state} — {probe.detail}",
                         C["green"] if probe.state == "ok" else C["yellow"])

            state = registration_state(self._cfg.pyscope_exe, path)
            if state == REG_REGISTERED:
                self._on_log("  registered with PyScope", C["green"])
            elif state == REG_UNKNOWN:
                # Not "no". PyScope could not be asked, and saying "not
                # registered" here would send the user to register a project
                # that may already be registered.
                self._on_log("  registration unknown — PyScope could not be asked",
                             C["yellow"])
            else:
                self._on_log("  not registered with PyScope", C["yellow"])

            from helpers.mcp_pyscope import binding_state, STATE_PRESENT
            mcp_state, mcp_detail = binding_state(self._cfg.pyscope_exe)
            self._on_log(f"  mcp entry: {mcp_detail}",
                         C["green"] if mcp_state == STATE_PRESENT else C["yellow"])

            if _is_pyscope_project(path):
                self._on_log("  cache: project-local (.pyscope/)", C["green"])
            else:
                self._on_log("  cache: user data — PyScope only writes "
                             ".pyscope/ when git ignores it", C["overlay0"])

        threading.Thread(target=worker, daemon=True).start()
