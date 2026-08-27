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
import shutil
from tkinter import messagebox
from typing import TYPE_CHECKING, Callable

import tkinter as tk

from constants import C
from helpers.runtime import log
from helpers.vscode_tasks import open_in_editor

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

    def cmd_open_editor(self, path: str, line: "int | None" = None,
                        column: "int | None" = None) -> None:
        """Open *path* in the configured editor, optionally at a line.

        The line argument exists so findings that already know a location can
        land the cursor on it rather than dropping the user at the top of a
        1,400-line file. Refactor-scout findings carry `file` and `line` and
        are wired to it; **Doctor violations are plain strings with no line
        number** ("_render_projects_section() complexity 20 (cap 10)"), so they
        cannot use this without first resolving the symbol.

        This is the Manager driving an editor from OUTSIDE. Inside a VS Code
        extension the right call is `vscode.window.showTextDocument`, which
        avoids spawning a second process; see docs/vscode-mcp-matrix.md.
        """
        editor_str = self._cfg.raw.get("editor_cmd", "code")
        ok, error = open_in_editor(editor_str, path, line, column)
        if not ok:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n{error}\n\n"
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
