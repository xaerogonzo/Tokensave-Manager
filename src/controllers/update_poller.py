"""UpdatePollerController — tokensave version probe + update-check loop.

Extracted from App (Round 5).

Dependency contract:
  • cfg      — read-only ManagerConfig (.tokensave_exe, .raw)
  • on_log   — thread-safe log callback  (msg: str, colour: str = "")
  • on_run   — run a tokensave sub-command (args: list, cwd: str, label: str)
  • root     — tk.Tk root window (messagebox parent)
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from tkinter import messagebox
from typing import TYPE_CHECKING, Callable

from constants import C, CREATE_NO_WINDOW
from helpers.detection import _version_lt
from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


class UpdatePollerController:
    """Probes the installed tokensave version and polls GitHub for updates.

    Call start() once after App construction. Exposes cmd_upgrade() for the
    Settings dialog Upgrade button. The two version attributes are read by
    App._run() (to capture opportunistic "Update available: vA → vB" lines
    from sync output) and by SettingsDialog to decide whether to show the
    Upgrade button.
    """

    # GitHub releases API endpoint for tokensave.
    _RELEASES_API = (
        "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest"
    )

    def __init__(
        self,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_run: Callable,
        root,
    ) -> None:
        self._cfg    = cfg
        self._on_log = on_log
        self._on_run = on_run
        self._root   = root

        self._current_version:   str | None = None
        self._available_version: str | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def current_version(self) -> str | None:
        return self._current_version

    @current_version.setter
    def current_version(self, value: str | None) -> None:
        """Allow App._run() to set the version when it parses sync output."""
        self._current_version = value

    @property
    def available_version(self) -> str | None:
        return self._available_version

    @available_version.setter
    def available_version(self, value: str | None) -> None:
        """Allow App._run() to set/clear the available version."""
        self._available_version = value

    def start(self) -> None:
        """Spawn the version-probe thread and the hourly update-poll loop.

        Called once from App.__init__ after the GUI is built.
        """
        threading.Thread(
            target=self._probe_worker,
            daemon=True,
            name="tokensave-version-probe",
        ).start()
        threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="tokensave-update-poll",
        ).start()

    def cmd_upgrade(self) -> None:
        """Run `tokensave upgrade` from the manager.

        Streams output to the OUTPUT pane via on_run. cwd is the
        tokensave_exe's directory — stable for any upgrade flow. Clears the
        cached available_version on entry so the Settings button auto-hides
        until the next sync re-reports an update.
        """
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            messagebox.showwarning(
                "tokensave not found",
                "Set the tokensave.exe path in Settings first.",
                parent=self._root,
            )
            return
        target = self._available_version
        cur    = self._current_version
        if target:
            msg = f"Upgrade tokensave from v{cur or '?'} to v{target}?\n\n"
        elif cur:
            msg = (
                f"Run `tokensave upgrade`?  (Currently installed: v{cur}.)\n\n"
                "tokensave will check GitHub for a newer release and apply it. "
                "No-op if you're already on the latest.\n\n"
            )
        else:
            msg = (
                "Run `tokensave upgrade`?\n\n"
                "tokensave will check GitHub for a newer release and apply it. "
                "No-op if you're already on the latest.\n\n"
            )
        msg += (
            "This replaces the tokensave binary on disk.  Currently-running\n"
            "MCP wrappers continue serving from the old binary until you\n"
            "restart Claude Desktop / Claude Code."
        )
        if not messagebox.askyesno("Upgrade tokensave", msg, parent=self._root):
            return
        # Clear cache so the Settings button hides until the next sync
        # reports a fresh update.  If the upgrade fails, the next sync
        # will re-populate it anyway.
        self._available_version = None
        self._on_run(
            ["upgrade"],
            cwd=os.path.dirname(self._cfg.tokensave_exe),
            label="upgrade",
        )

    # ── Internal workers ──────────────────────────────────────────────────────

    def _probe_worker(self) -> None:
        """Best-effort read of the installed tokensave version.

        Runs `tokensave --version` once at startup (background thread to avoid
        blocking the GUI). Caches the version string; failures leave it as None.
        After a successful probe, triggers a single update check immediately.
        """
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            return
        try:
            r = subprocess.run(
                [self._cfg.tokensave_exe, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        out = (r.stdout or "").strip()
        m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", out)
        if m:
            self._current_version = m.group(1)
            log.debug(f"tokensave installed version: {self._current_version}")
            # Immediate one-shot check right after we know the local version.
            # Subsequent checks fire from the hourly poller.
            self._check_updates()

    def _poll_interval(self) -> float:
        """Return the poll interval in seconds (tunable via cfg)."""
        hours = float(self._cfg.raw.get("tokensave_update_poll_hours", 1.0))
        return max(0.25, hours) * 3600.0  # never poll more than 4×/hour

    def _poll_loop(self) -> None:
        """Daemon: re-check GitHub for new tokensave releases periodically.

        Doesn't prompt the user — just refreshes the cached available_version
        so Settings reflects current state next time it's opened. The OUTPUT-
        pane hint is only logged on fresh discovery (transition from "no update
        known" → "update available"), not on every poll.
        """
        while True:
            time.sleep(self._poll_interval())
            self._check_updates()

    def _check_updates(self) -> None:
        """Single-shot check against the tokensave releases API.

        Compares against current_version (set by _probe_worker). When a
        strictly-newer version is found AND it wasn't known before, logs a
        peach hint to the OUTPUT pane.
        """
        import json as _json
        import urllib.error
        import urllib.request

        if not self._current_version:
            return  # nothing to compare against yet
        try:
            req = urllib.request.Request(
                self._RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "tokensave-manager",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            _json.JSONDecodeError,
            OSError,
        ) as e:
            log.debug(f"tokensave update check failed: {type(e).__name__}: {e}")
            return
        tag = (data.get("tag_name") or "").strip().lstrip("v")
        m = re.match(r"(\d+\.\d+\.\d+(?:\.\d+)?)", tag)
        if not m:
            return
        latest = m.group(1)
        cur = self._current_version
        if not _version_lt(cur, latest):
            return  # current is up-to-date or ahead
        prev_known = self._available_version
        self._available_version = latest
        if prev_known != latest:
            # Fresh discovery — surface to OUTPUT pane.
            self._on_log(
                f"  → tokensave {cur} → {latest} ready to install.  "
                f"Settings → 'Upgrade tokensave to v{latest}' to apply, "
                f"or run 'tokensave upgrade' from a shell.",
                C["peach"],
            )
            log.info(f"UPDATE-AVAILABLE  {cur} -> {latest}  (via GitHub API)")
