"""TrayManager — system-tray icon lifecycle for the TokenSave Manager window.

Owns the pystray icon, hide/show/quit orchestration, and the last-geometry
cache. App delegates all tray concerns to this class via callbacks, keeping
pystray contained in one module.

Dependency contract:
  root     — the tk.Tk App window (geometry, after, withdraw, deiconify, …)
  cfg      — ManagerConfig instance (saves window_geometry on hide)
  on_quit  — called before window.destroy(); should cancel in-flight workers
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

import pystray

from helpers.runtime import _make_tray_icon, log

if TYPE_CHECKING:
    import tkinter as tk
    from state import ManagerConfig


class TrayManager:
    """Manages the pystray icon and window hide / show / quit lifecycle."""

    def __init__(
        self,
        root: "tk.Tk",
        cfg: "ManagerConfig",
        on_quit: "Callable[[], None]",
    ) -> None:
        self._root = root
        self._cfg  = cfg
        self._on_quit = on_quit
        self._icon: "pystray.Icon | None" = None
        self._last_geometry: str = ""

    def setup(self) -> None:
        """Create the pystray icon and start its background thread."""
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon(
            "TokenSaveManager",
            _make_tray_icon(),
            "TokenSave Manager",
            menu,
        )
        threading.Thread(target=self._icon.run, daemon=True).start()

    def hide(self) -> None:
        """Save window geometry and withdraw the window to the tray."""
        self._last_geometry = self._root.geometry()
        self._cfg.raw["window_geometry"] = self._last_geometry
        self._cfg.save()
        self._root.withdraw()
        log.debug("Window hidden to tray (geometry saved: %s)", self._last_geometry)

    def show(self) -> None:
        """Schedule window restore on the Tk main thread (safe from any thread)."""
        self._root.after(0, self._do_show)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _do_show(self) -> None:
        if self._last_geometry:
            self._root.geometry(self._last_geometry)
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def _show_from_tray(self, icon=None, item=None) -> None:
        self.show()

    def _quit(self, icon=None, item=None) -> None:
        log.info("Quit requested from tray")
        self._on_quit()
        if self._icon:
            self._icon.stop()
        self._root.after(0, self._root.destroy)
