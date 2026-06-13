"""ShadowLinksController — shadow-link generation command for the Projects tab.

Extracted from ProjectsTabController (Round 5).

Dependency contract:
  • tab              — the Projects tk.Frame (after() + winfo_toplevel())
  • cfg              — read-only ManagerConfig (.tokensave_exe)
  • on_log           — thread-safe log callback  (msg: str, colour: str = "")
  • on_refresh       — () -> None
  • on_run_capture   — (args: list, cwd: str, label: str) -> (raw, rc, elapsed)
  • on_commit_offer  — (path: str, label: str) -> None
"""

from __future__ import annotations

import os
import threading
from tkinter import messagebox
from typing import TYPE_CHECKING, Callable

import tkinter as tk

from constants import C, _ANSI
from helpers.runtime import log
from helpers.shadow_links import (
    generate_shadow_links,
    supports_hardlinks,
    update_gitignore_for_shadows,
)
from dialogs.shadow_links import ShadowLinksDialog

if TYPE_CHECKING:
    from state import ManagerConfig


class ShadowLinksController:
    """Opens the ShadowLinksDialog and runs shadow-link generation in a thread."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_refresh: Callable[[], None],
        on_run_capture: Callable,
        on_commit_offer: Callable[[str, str], None],
    ) -> None:
        self._tab             = tab
        self._cfg             = cfg
        self._on_log          = on_log
        self._on_refresh      = on_refresh
        self._on_run_capture  = on_run_capture
        self._on_commit_offer = on_commit_offer

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Command ───────────────────────────────────────────────────────────────

    def cmd_shadow_links(self, path: str) -> None:
        # R9-SL4: probe once up front. Without this, os.link fails per-file
        # on FAT32/exFAT/network volumes and the user only sees opaque
        # "0 created, N failed" counts after a full walk.
        if not supports_hardlinks(path):
            messagebox.showerror(
                "Shadow Links unavailable",
                "This volume doesn't support hardlinks (NTFS required).\n\n"
                "Shadow links can't be created on FAT32/exFAT or most "
                "network drives. Move the project to an NTFS volume to "
                "use this feature.",
                parent=self._root)
            return
        ShadowLinksDialog(self._root, path, self._do_shadow_links,
                          cfg=self._cfg)

    # ── Worker ────────────────────────────────────────────────────────────────

    def _do_shadow_links(self, path: str, ext_map: dict, run_sync: bool = True) -> None:
        name = os.path.basename(path)

        def worker():
            rc = 0
            try:
                self._on_log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._on_log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and self._cfg.tokensave_exe and created > 0:
                    self._on_log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._on_run_capture(["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._on_log(f"    {line}", col)

                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}."
                       if run_sync and created > 0 else ""),
                    parent=self._root))
                self._tab.after(0, lambda: self._on_commit_offer(
                    path, "shadow links + .gitignore"))
            except Exception as e:
                err_msg = str(e)
                log.exception(f"SHADOW LINKS failed: {path}")
                self._on_log(f"  Error: {err_msg}", C["red"])
                self._tab.after(0, lambda m=err_msg: messagebox.showerror(
                    "Shadow Links failed", m, parent=self._root))

        threading.Thread(target=worker, daemon=True).start()
