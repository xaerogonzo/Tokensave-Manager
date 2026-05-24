"""DoctorController — tokensave doctor command group for the Projects tab.

Extracted from ProjectsTabController (Round 5).

Dependency contract:
  • tab          — the Projects tk.Frame (after() scheduling + winfo_toplevel())
  • cfg          — read-only ManagerConfig (.tokensave_exe)
  • on_log       — thread-safe log callback  (msg: str, colour: str = "")
  • on_set_running  — (running: bool, label: str) -> None
  • on_set_proc  — (proc_or_none) -> None  updates parent's current_proc so
                   App._auto_refresh can detect when the controller is busy
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from constants import C, CREATE_NO_WINDOW, _ANSI
from helpers.runtime import log

import time

if TYPE_CHECKING:
    from state import ManagerConfig


class DoctorController:
    """Runs `tokensave doctor`, parses stale entries, and offers to purge them."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_set_running: Callable[[bool, str], None],
        on_set_proc: Callable[[object], None],
    ) -> None:
        self._tab           = tab
        self._cfg           = cfg
        self._on_log        = on_log
        self._on_set_running = on_set_running
        self._on_set_proc   = on_set_proc

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Public entry point ────────────────────────────────────────────────────

    def cmd_doctor(self, path: str) -> None:
        """Run doctor + offer purge. Call after require_tokensave guard passes."""
        self._run_with_purge_offer(path)

    # ── Worker helpers ────────────────────────────────────────────────────────

    def _run_with_purge_offer(self, path: str) -> None:
        label = os.path.basename(path)

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            log.info("RUN  tokensave doctor")
            output_lines: list[str] = []
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [self._cfg.tokensave_exe, "doctor"],
                    cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._on_set_proc(proc)
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._on_log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                else:
                    self._on_log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                stale = self._extract_stale_paths(output_lines)
                if stale and proc.returncode == 0:
                    self._tab.after(0, self._offer_purge, path, stale)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_doctor")
            finally:
                self._on_set_proc(None)
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-worker").start()

    def _run_purge(self, path: str) -> None:
        """Re-run `tokensave doctor` with `y` piped to confirm the purge prompt."""
        label = "doctor (purge)"

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            captured: list[str] = []
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [self._cfg.tokensave_exe, "doctor"],
                    cwd=path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._on_set_proc(proc)
                try:
                    proc.stdin.write("y\ny\ny\ny\ny\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                except (OSError, BrokenPipeError):
                    pass
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    captured.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                self._on_log(
                    "Done." if proc.returncode == 0
                    else f"Exited with code {proc.returncode}",
                    C["green"] if proc.returncode == 0 else C["red"])
                still_stale = self._extract_stale_paths(captured)
                if still_stale:
                    self._on_log(
                        f"  ⚠ Purge didn't take — tokensave still "
                        f"reports {len(still_stale)} stale entr"
                        f"{'y' if len(still_stale) == 1 else 'ies'}. "
                        "tokensave doctor needs a real terminal "
                        "(piped stdin doesn't trigger the prompt).",
                        C["peach"])
                    self._tab.after(0, self._offer_in_cmd, path, len(still_stale))
                else:
                    self._on_log("  ✓ Stale entries purged.", C["green"])
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in doctor purge")
            finally:
                self._on_set_proc(None)
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-purge").start()

    # ── Main-thread dialogs ───────────────────────────────────────────────────

    def _offer_purge(self, path: str, stale_paths: list[str]) -> None:
        n = len(stale_paths)
        bullets = "\n".join(f"  • {p}" for p in stale_paths)
        msg = (
            f"tokensave doctor found {n} stale project entr"
            f"{'y' if n == 1 else 'ies'} in the global DB.\n\n"
            f"{bullets}\n\n"
            "These projects were registered but their `.tokensave/` "
            "folders are gone — most likely deleted folders.\n\n"
            "Purge them now?  The manager will re-run `tokensave "
            "doctor` with `y` piped to confirm the interactive "
            "purge prompt."
        )
        if not messagebox.askyesno("Purge stale tokensave projects?", msg,
                                   parent=self._root):
            self._on_log("  (purge skipped — stale entries left in place)", C["overlay0"])
            return
        self._run_purge(path)

    def _offer_in_cmd(self, path: str, n_stale: int) -> None:
        plural = "entry" if n_stale == 1 else "entries"
        if not messagebox.askyesno(
                "Open Doctor in a new terminal?",
                f"The piped-stdin purge didn't work — tokensave needs "
                f"a real terminal for its interactive 'y/n' prompt.\n\n"
                f"Open a new cmd.exe window with `tokensave doctor` "
                f"running there?  You'll see the {n_stale} stale "
                f"{plural} listed and tokensave will ask you to "
                f"confirm — type 'y' and press Enter to purge.\n\n"
                f"The window stays open after, so you can close it "
                f"yourself when done.",
                parent=self._root):
            self._on_log(
                "  (terminal-purge skipped — stale entries still in DB)",
                C["overlay0"])
            return
        try:
            cmd_line = f'cmd.exe /k ""{self._cfg.tokensave_exe}" doctor"'
            subprocess.Popen(
                cmd_line,
                cwd=path,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self._on_log(
                "  Opened cmd.exe — type 'y' at the prompt to purge, "
                "then close the window.",
                C["sky"])
        except OSError as e:
            self._on_log(f"  ✗ Could not launch cmd.exe: {e}", C["red"])

    # ── Output parser ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_stale_paths(output_lines: list[str]) -> list[str]:
        """Parse tokensave doctor's stdout for the stale-entries section."""
        bullet_re = re.compile(r"^\s*[•*\-]\s+(.+?)\s*$")
        in_block = False
        paths: list[str] = []
        for line in output_lines:
            if "stale project" in line and "global DB" in line:
                in_block = True
                continue
            if not in_block:
                continue
            if "Re-run" in line and "tokensave doctor" in line:
                break
            m = bullet_re.match(line)
            if m:
                paths.append(m.group(1).strip())
            elif paths and not line.startswith((" ", "\t")):
                break
        return paths
