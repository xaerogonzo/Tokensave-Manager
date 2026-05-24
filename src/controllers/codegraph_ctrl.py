"""CodeGraphController — all CodeGraph commands for the Projects tab.

Extracted from ProjectsTabController (Round 5) so that the four
cmd_codegraph_* methods live in a focused class with an explicit,
minimal dependency surface.

Dependency contract:
  • cfg       — read-only ManagerConfig (needs .codegraph_exe)
  • tab       — the Projects tk.Frame; used for after() scheduling and
                winfo_toplevel() dialog parenting
  • on_log    — thread-safe log callback  (msg: str, colour: str)
  • on_shell  — synchronous shell runner  (args, cwd) -> (out: str, rc: int)
  • on_refresh           — () -> None, requests a full tree rebuild
  • on_commit_offer      — (path, label) -> None, offered after init
  • on_settings          — () -> None, opened when codegraph is not installed
"""

from __future__ import annotations

import os
import shutil
import threading
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from helpers.detection import _detect_codegraph, _is_codegraph_project
from theme import C

if TYPE_CHECKING:
    from state import ManagerConfig


class CodeGraphController:
    """Handles CodeGraph init / sync / status / remove for a selected project."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable[[str, str], None],
        on_shell: Callable,
        on_refresh: Callable[[], None],
        on_commit_offer: Callable[[str, str], None],
        on_settings: Callable[[], None],
    ) -> None:
        self._tab             = tab
        self._cfg             = cfg
        self._on_log          = on_log
        self._on_shell        = on_shell
        self._on_refresh      = on_refresh
        self._on_commit_offer = on_commit_offer
        self._on_settings     = on_settings

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Guard ─────────────────────────────────────────────────────────────────

    def _require_installed(self) -> bool:
        """Return True if codegraph_exe is found; otherwise prompt to open Settings."""
        if self._cfg.codegraph_exe and os.path.isfile(self._cfg.codegraph_exe):
            return True
        if messagebox.askyesno(
                "CodeGraph is not installed",
                "CodeGraph is not installed on this machine.\n\n"
                "CodeGraph is an alternative code-graph tool that builds a "
                "per-project SQLite index for Claude Code to query. It complements "
                "tokensave — both can be enabled on the same project.\n\n"
                "Open Settings now to install it?",
                parent=self._root):
            self._on_settings()
        return False

    # ── Commands (path acquired by ProjectsTabController before calling) ───────

    def cmd_init(self, path: str) -> None:
        if not self._require_installed():
            return
        if _is_codegraph_project(path):
            messagebox.showinfo("Already initialised",
                f"{os.path.basename(path)} already has CodeGraph initialised.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph init in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell(
                [self._cfg.codegraph_exe, "init", "--index", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._on_log(f"  {line}", col)
            self._tab.after(0, self._on_refresh)
            self._tab.after(0, lambda: self._on_commit_offer(path, "CodeGraph init files"))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_sync(self, path: str) -> None:
        if not self._require_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph sync in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell(
                [self._cfg.codegraph_exe, "sync", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._on_log(f"  {line}", col)
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self, path: str) -> None:
        if not self._require_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph status in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell(
                [self._cfg.codegraph_exe, "status", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines():
                self._on_log(f"  {line}", col)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_remove(self, path: str) -> None:
        name   = os.path.basename(path)
        cg_dir = os.path.join(path, ".codegraph")
        if not os.path.isdir(cg_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no CodeGraph index.", parent=self._root)
            return
        if not messagebox.askyesno(
                "Remove CodeGraph index?",
                f"Delete the CodeGraph index for {name}?\n\n"
                "This removes .codegraph/ from the project folder. Your "
                "source files are not touched. You can re-create the index "
                "later via 🧠 CodeGraph Init.",
                parent=self._root):
            return
        try:
            shutil.rmtree(cg_dir)
            self._on_log(f"  Removed .codegraph/ from {name}", C["green"])
        except OSError as e:
            self._on_log(f"  Could not remove .codegraph/: {e}", C["red"])
        self._on_refresh()
