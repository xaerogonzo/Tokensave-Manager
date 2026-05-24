"""FileOpsController — open / copy / remove commands for the Projects tab.

Extracted from ProjectsTabController (Round 5).

Dependency contract:
  • tab       — the Projects tk.Frame (winfo_toplevel() for dialog parenting)
  • cfg       — read-only ManagerConfig (.raw["editor_cmd"])
  • on_log    — thread-safe log callback  (msg: str, colour: str = "")
  • on_refresh — () -> None
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from tkinter import messagebox
from typing import TYPE_CHECKING, Callable

import tkinter as tk

from constants import C, CREATE_NO_WINDOW
from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


class FileOpsController:
    """Handles open-folder, open-editor, copy-path, and remove-index commands."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_refresh: Callable[[], None],
    ) -> None:
        self._tab        = tab
        self._cfg        = cfg
        self._on_log     = on_log
        self._on_refresh = on_refresh

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Commands ──────────────────────────────────────────────────────────────

    def cmd_open_folder(self, path: str) -> None:
        os.startfile(path)

    def cmd_open_editor(self, path: str) -> None:
        editor_str = self._cfg.raw.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self._root,
            )

    def cmd_copy_path(self, path: str) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(path)
        self._on_log(f"Copied: {path}", C["sky"])

    def cmd_remove(self, path: str) -> None:
        name   = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self._root)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self._root,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._on_log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self._on_refresh()
        except Exception as e:
            self._on_log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self._root)
