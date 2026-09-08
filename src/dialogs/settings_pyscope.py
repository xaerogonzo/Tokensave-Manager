"""PyScopeSection — the PyScope block of the Settings dialog.

Follows the house pattern established by ``settings_codegraph.py``: the section
receives the dialog handle for Tk plumbing only (``after()`` scheduling and
messagebox / dialog parenting), holds no back-references to dialog attributes,
and exposes its slice of the Save contract as ``save_into(raw)``.

Two deliberate differences from the CodeGraph section, both of which the row
states out loud rather than implying by greyed-out buttons:

**No install action.** PyScope is a uv tool over a local editable checkout, not
an npm package the Manager can fetch. Offering an Install button would mean the
Manager owning a build workflow that belongs to PyScope's own repository, so the
section shows the exact command instead and lets the user run it.

**Three status rows, not one.** A configured path proves only that a path is
configured. Whether it launches, and whether PyScope answers sanely, are two
further questions — and "installed but crashing" must never render as "not
installed", because the whole reason someone opens this row is to find out which
of those they are looking at. See ``helpers/pyscope.status``.

The probe shells a subprocess, so it runs on a worker thread and hands its
result back through ``UiPumpMixin._post`` — a synchronous probe would hitch the
dialog every time it opened, and a worker calling ``after()`` itself blocks
silently on Linux (see ``tests/test_no_cross_thread_tk.py``).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from typing import TYPE_CHECKING

from constants import C
from theme import _Tooltip, UiPumpMixin
from helpers.detection import _detect_pyscope

if TYPE_CHECKING:
    from state import ManagerConfig


#: What to run to get PyScope onto PATH. Shown, never executed — the Manager
#: does not install PyScope (see the module docstring).
INSTALL_HINT = "uv tool install --editable <path to the PyScope checkout>"


class PyScopeSection(UiPumpMixin):
    """PyScope executable path, three-state status, and an install hint.

    Mixes in ``UiPumpMixin`` because the status probe is a subprocess and must
    run off the Tk thread. A worker calling ``after()`` directly is the failure
    shape `tests/test_no_cross_thread_tk.py` exists to catch: it usually works
    on Windows, and on Linux it blocks silently rather than raising.
    """

    def __init__(self, dialog: tk.Toplevel, body: tk.Frame,
                 cfg: "ManagerConfig") -> None:
        self._dlg = dialog
        self._cfg = cfg
        self._build(body, cfg.raw)
        self._start_ui_pump()

    def save_into(self, raw: dict) -> bool:
        """Write this section's fields into raw. Always succeeds."""
        raw["pyscope_exe"] = self._exe_var.get().strip()
        return True

    def focus_path_entry(self) -> None:
        """Pull the PyScope path entry into focus (see CodegraphSection)."""
        try:
            self._exe_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    # ── Section construction ─────────────────────────────────────────────

    def _build(self, body, raw):
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._section = tk.Frame(body, bg=C["base"])
        self._section.pack(fill=tk.X)

        header = tk.Frame(self._section, bg=C["base"])
        header.pack(fill=tk.X, padx=20)
        tk.Label(header,
                 text="PyScope (pyscope)  —  optional code-comprehension tool",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Path entry + Browse / Auto-detect
        path_row = tk.Frame(self._section, bg=C["base"])
        path_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._exe_var = tk.StringVar(value=raw.get("pyscope_exe", ""))
        self._exe_entry = ttk.Entry(path_row, textvariable=self._exe_var, width=44)
        self._exe_entry.pack(side=tk.LEFT, padx=(0, 6))
        browse = ttk.Button(path_row, text="Browse…", command=self._browse)
        browse.pack(side=tk.LEFT, padx=(0, 6))
        detect = ttk.Button(path_row, text="Auto-detect", command=self._autodetect)
        detect.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(browse, "Pick the pyscope executable yourself.")
        _Tooltip(detect, "Look for pyscope on PATH, then in ~/.local/bin where "
                         "`uv tool install` puts its shims.")

        # Three status rows. Separate labels rather than one multi-line string:
        # the point of the section is that these three answers are independent,
        # and a single label invites collapsing them back into one verdict.
        status_box = tk.Frame(self._section, bg=C["base"])
        status_box.pack(fill=tk.X, padx=20, pady=(6, 0))
        self._row_configured = self._status_row(status_box, "Configured")
        self._row_executable = self._status_row(status_box, "Executable")
        self._row_status = self._status_row(status_box, "Status")

        btn_row = tk.Frame(self._section, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        check = ttk.Button(btn_row, text="Check again", command=self.check_status)
        check.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(check, "Re-run `pyscope version` and refresh all three rows.")

        # Install hint — shown only when PyScope is absent. Packed on demand so
        # a healthy install is not permanently advised to reinstall itself.
        self._hint_frame = tk.Frame(self._section, bg=C["base"])
        tk.Label(self._hint_frame,
                 text="  PyScope is not bundled or installed by this manager. "
                      "To install it, run:",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W)
        hint_row = tk.Frame(self._hint_frame, bg=C["base"])
        hint_row.pack(fill=tk.X, pady=(2, 0))
        hint_entry = ttk.Entry(hint_row, width=52)
        hint_entry.insert(0, INSTALL_HINT)
        hint_entry.configure(state="readonly")
        hint_entry.pack(side=tk.LEFT, padx=(0, 6))
        copy_btn = ttk.Button(hint_row, text="Copy", command=self._copy_hint)
        copy_btn.pack(side=tk.LEFT)
        _Tooltip(copy_btn, "Copy the install command to the clipboard.")

        tk.Label(self._section,
                 text="  Per-project actions live in the right-click menu "
                      "(🔬 PyScope …).",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(4, 0))

        self._dlg.after(200, self.check_status)

    def _ui_host(self):
        """UiPumpMixin drives this frame: a section is not a widget itself."""
        return self._section

    def _status_row(self, parent, caption: str) -> tk.Label:
        """One `caption: value` line; returns the value label to update later."""
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X)
        tk.Label(row, text=f"{caption}:", width=11, anchor=tk.W,
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        value = tk.Label(row, text="Checking…", anchor=tk.W, justify=tk.LEFT,
                         bg=C["base"], fg=C["overlay0"],
                         font=("Segoe UI", 8), wraplength=400)
        value.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return value

    # ── Handlers ─────────────────────────────────────────────────────────

    def _browse(self):
        found = _detect_pyscope()
        initial = os.path.dirname(found) if found else os.path.join(
            os.path.expanduser("~"), ".local", "bin")
        path = filedialog.askopenfilename(
            title="Select pyscope executable",
            filetypes=[("Executable", "*.exe;*.cmd;*.bat"), ("All", "*.*")],
            initialdir=initial, parent=self._dlg)
        if path:
            self._exe_var.set(path)
            self.check_status()

    def _autodetect(self):
        found = _detect_pyscope()
        if found:
            self._exe_var.set(found)
        self.check_status()

    def _copy_hint(self):
        try:
            self._dlg.clipboard_clear()
            self._dlg.clipboard_append(INSTALL_HINT)
        except tk.TclError:
            pass

    # ── Status probe ─────────────────────────────────────────────────────

    def check_status(self) -> None:
        """Probe PyScope on a worker thread and repaint the three rows.

        The configured path is resolved the same way ``ManagerConfig`` resolves
        it — an explicit entry wins, detection fills in otherwise — so the rows
        describe what the Manager will actually use, not what is merely typed.
        """
        configured = self._exe_var.get().strip() or _detect_pyscope()
        for label in (self._row_configured, self._row_executable, self._row_status):
            label.config(text="Checking…", fg=C["overlay0"])

        def worker():
            from helpers.pyscope import status as probe
            self._post(self._apply, probe(configured))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, result) -> None:
        """Main-thread repaint. Guarded — the dialog may have closed mid-probe."""
        try:
            if not self._section.winfo_exists():
                return
        except tk.TclError:
            return

        from helpers.pyscope import STATE_OK, STATE_ABSENT

        if result.configured:
            self._row_configured.config(text=result.configured, fg=C["green"])
        else:
            self._row_configured.config(text="not configured or detected", fg=C["red"])

        if result.executable:
            self._row_executable.config(text="yes", fg=C["green"])
        else:
            self._row_executable.config(text="no", fg=C["red"])

        if result.state == STATE_OK:
            colour = C["green"]
        elif result.state == STATE_ABSENT:
            colour = C["red"]
        else:
            # Launchable but not answering. Yellow, never red: the distinction
            # between "absent" and "broken" is the one this row exists to make.
            colour = C["yellow"]
        self._row_status.config(text=f"{result.state} — {result.detail}", fg=colour)

        # Advise installation only when there is nothing to talk to.
        absent = result.state == STATE_ABSENT
        try:
            packed = bool(self._hint_frame.winfo_manager())
        except tk.TclError:
            packed = False
        if absent and not packed:
            self._hint_frame.pack(fill=tk.X, padx=20, pady=(6, 0))
        elif not absent and packed:
            self._hint_frame.pack_forget()
