"""ExtensionManagerDialog — the VS Code extension's lifecycle, made visible.

Opened from the Help tab header ('🧩 Extension Manager…'), beside the Tool
Manager button it is modelled on.

The problem it exists for: the extension went from 2.3.0 to 2.6.0 in source
while the installed copy stayed at 2.3.0, and nothing in the Manager could say
so. Three separate versions were involved and none of them were ever shown next
to each other:

    Source      vscode-extension/package.json
    Built       the manifest INSIDE the .vsix on disk
    Installed   what the editor reports

This dialog shows all three, plus how they relate, and offers the two actions
that fix a disagreement.

WHAT IT DOES NOT DO
-------------------
It does not build anything itself. `build-extension.ps1` is the one build
pipeline — the same one CI runs — and this dialog shells out to it. A GUI that
reimplemented the steps would drift from the release path, which is exactly the
failure mode that produced the stale extension in the first place.

There is deliberately no bare "Install the .vsix that is already there" button.
Build & Install always rebuilds first, so a stale artefact cannot reach the
editor by a single click.

FROZEN BUILDS
-------------
A Nuitka onefile Manager ships without a repository, so there is no
vscode-extension\\ and no build script. The dialog detects that, keeps showing
the installed version (which it can still ask the editor for), and disables the
build actions with the reason rather than failing when one is pressed.

THREADING
---------
Same contract as ToolManagerDialog: `_set_phase` runs synchronously on the Tk
thread BEFORE a worker starts, the worker touches no Tk at all, and its
`finally` restores state by posting through `UiPumpMixin._post`.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW, _BASE_DIR
from helpers.vscode_extension import Status, read_state, resolve_editor
from theme import UiPumpMixin

if TYPE_CHECKING:
    from state import ManagerConfig


#: Phases the dialog can be in. Button enablement is derived from this in one
#: place, so a second Build cannot start while the first is still running and
#: no handler has to remember to disable its siblings.
IDLE = "idle"
BUILDING = "building"
INSTALLING = "installing"
REFRESHING = "refreshing"

#: Colour per lifecycle state. UP_TO_DATE is the only green one; the rest are
#: distinguished from each other rather than collapsed into one red, because
#: "rebuild" and "install what you already built" are different actions.
_STATUS_COLOUR = {
    Status.UP_TO_DATE: "green",
    Status.NO_BUILD: "peach",
    Status.SOURCE_NEWER: "peach",
    Status.BUILT_DIFFERS: "peach",
    Status.NOT_INSTALLED: "peach",
    Status.INSTALLED_DIFFERS: "peach",
    Status.UNKNOWN: "subtext",
}


def _age(mtime: float) -> str:
    """'4 min ago' — the artefact's age is half the point of the panel."""
    if not mtime:
        return "unknown"
    seconds = max(0, time.time() - mtime)
    if seconds < 90:
        return f"{int(seconds)} sec ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hr ago"
    return f"{int(seconds // 86400)} days ago"


class ExtensionManagerDialog(UiPumpMixin, tk.Toplevel):
    """Source vs built vs installed, and the two actions that reconcile them."""

    def __init__(self, parent, cfg: "ManagerConfig", app=None) -> None:
        super().__init__(parent)
        self._parent = parent
        self._cfg = cfg
        # release_wizard reaches the shell through the App the same way. The
        # parent IS the App window in the normal path; the parameter exists so
        # debug_drive can pass it explicitly.
        self._app = app if app is not None else parent

        # NOT `self._root`: tk.Misc._root is a METHOD, and shadowing it with a
        # string breaks Tk's own error reporting -- the first symptom is
        # `TypeError: 'str' object is not callable` raised from deep inside
        # tkinter while REPORTING some other exception, which hides the
        # original one completely.
        self._repo_root = _BASE_DIR
        self._ext_dir = os.path.join(self._repo_root, "vscode-extension")
        self._script = os.path.join(self._repo_root, "build-extension.ps1")
        self._state = None
        self._phase = IDLE

        self.title("🧩 Extension Manager — TokenSave Manager")
        self.configure(bg=C["base"])
        self.geometry("760x560")
        self.transient(parent)

        self._build_ui()
        self._start_ui_pump()
        # Read on open rather than trusting anything cached. The user may have
        # run `code --install-extension` by hand since this last ran, which is
        # precisely the situation the panel is supposed to notice.
        self._refresh_async()

    # ── layout ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        head = tk.Frame(self, bg=C["base"])
        head.pack(fill=tk.X, padx=16, pady=(14, 6))
        tk.Label(head, text="VS Code extension", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        tk.Label(head,
                 text="Built by build-extension.ps1 — the same pipeline the "
                      "release workflow runs.",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W)

        grid = tk.Frame(self, bg=C["mantle"])
        grid.pack(fill=tk.X, padx=16, pady=(10, 6), ipady=8)

        self._rows = {}
        for key, label in (("source", "Source"), ("built", "Built"),
                           ("installed", "Installed"), ("artefact", "Artefact")):
            line = tk.Frame(grid, bg=C["mantle"])
            line.pack(fill=tk.X, padx=14, pady=2)
            tk.Label(line, text=label, width=10, anchor=tk.W, bg=C["mantle"],
                     fg=C["subtext"], font=("Segoe UI", 10)).pack(side=tk.LEFT)
            value = tk.Label(line, text="…", anchor=tk.W, bg=C["mantle"],
                             fg=C["text"], font=("Consolas", 10))
            value.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._rows[key] = value

        status_row = tk.Frame(grid, bg=C["mantle"])
        status_row.pack(fill=tk.X, padx=14, pady=(8, 2))
        tk.Label(status_row, text="Status", width=10, anchor=tk.W,
                 bg=C["mantle"], fg=C["subtext"],
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._status_lbl = tk.Label(status_row, text="reading…", anchor=tk.W,
                                    bg=C["mantle"], fg=C["subtext"],
                                    font=("Segoe UI", 10, "bold"))
        self._status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        btns = tk.Frame(self, bg=C["base"])
        btns.pack(fill=tk.X, padx=16, pady=(8, 4))
        self._build_btn = ttk.Button(btns, text="🔨  Build",
                                     command=self._on_build)
        self._build_btn.pack(side=tk.LEFT)
        self._install_btn = ttk.Button(btns, text="📦  Build & Install",
                                       command=self._on_build_install)
        self._install_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._uninstall_btn = ttk.Button(btns, text="🗑  Uninstall",
                                         command=self._on_uninstall)
        self._uninstall_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text="📂  Open folder",
                   command=self._on_open_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text="♻  Refresh",
                   command=self._refresh_async).pack(side=tk.RIGHT)

        self._note = tk.Label(self, text="", bg=C["base"], fg=C["peach"],
                              anchor=tk.W, justify=tk.LEFT, wraplength=720,
                              font=("Segoe UI", 9))
        self._note.pack(fill=tk.X, padx=16, pady=(0, 4))

        log_wrap = tk.Frame(self, bg=C["crust"])
        log_wrap.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 14))
        self._log_txt = tk.Text(log_wrap, bg=C["crust"], fg=C["subtext"],
                                font=("Consolas", 9), wrap=tk.NONE,
                                relief=tk.FLAT, height=12)
        self._log_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_wrap, command=self._log_txt.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_txt.configure(yscrollcommand=scroll.set, state=tk.DISABLED)

    # ── logging (Tk thread only; workers go through _post) ────────────────

    def _log(self, text: str) -> None:
        try:
            self._log_txt.configure(state=tk.NORMAL)
            self._log_txt.insert(tk.END, text.rstrip() + "\n")
            self._log_txt.see(tk.END)
            self._log_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass          # dialog closed mid-operation

    def _log_threadsafe(self, text: str) -> None:
        self._post(self._log, text)

    # ── the one place button state is decided ─────────────────────────────

    def _set_phase(self, phase: str) -> None:
        """Tk thread only. Called synchronously BEFORE a worker starts."""
        self._phase = phase
        busy = phase != IDLE
        buildable = self._checkout_present() and not busy

        self._build_btn.configure(
            state=tk.NORMAL if buildable else tk.DISABLED)
        self._install_btn.configure(
            state=tk.NORMAL if buildable and self._editor_ok() else tk.DISABLED)

        # Uninstall needs something installed AND an editor that answers. A
        # no-op uninstall that reports success would look like a lifecycle
        # action having happened.
        installed = bool(self._state and self._state.installed_version)
        self._uninstall_btn.configure(
            state=tk.NORMAL if installed and self._editor_ok() and not busy
            else tk.DISABLED)

    def _checkout_present(self) -> bool:
        return (os.path.isfile(os.path.join(self._ext_dir, "package.json"))
                and os.path.isfile(self._script))

    def _editor_ok(self) -> bool:
        return bool(self._state and self._state.editor_ok)

    def _editor_cmd(self) -> str:
        return self._cfg.raw.get("editor_cmd", "code") or "code"

    # ── reading state ─────────────────────────────────────────────────────

    def _refresh_async(self) -> None:
        if self._phase not in (IDLE, REFRESHING):
            return
        self._set_phase(REFRESHING)
        self._status_lbl.configure(text="reading…", fg=C["subtext"])

        editor = self._editor_cmd()
        ext_dir = self._ext_dir
        present = self._checkout_present()

        def _worker():
            state, error = None, None
            try:
                if present:
                    state = read_state(ext_dir, editor)
            except Exception as exc:            # never take the dialog down
                error = str(exc)
            finally:
                self._post(self._apply_state, state, error)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_state(self, state, error) -> None:
        """Tk thread. Render whatever was learned, including 'not much'."""
        self._state = state

        if not self._checkout_present():
            self._render_no_checkout()
        elif error or state is None:
            self._rows["source"].configure(text="—")
            self._rows["built"].configure(text="—")
            self._rows["installed"].configure(text="—")
            self._rows["artefact"].configure(text="—")
            self._status_lbl.configure(text="could not read state",
                                       fg=C["subtext"])
            self._note.configure(text=error or "")
        else:
            self._render_state(state)

        self._set_phase(IDLE)

    def _render_no_checkout(self) -> None:
        for row in self._rows.values():
            row.configure(text="—")
        self._status_lbl.configure(text="No checkout beside this Manager",
                                   fg=C["subtext"])
        self._note.configure(
            text="This Manager is running without its repository, so there is "
                 "no vscode-extension\\ to build from. Building the extension "
                 "needs a source checkout; the installed copy is unaffected.")

    def _render_state(self, state) -> None:
        self._rows["source"].configure(text=state.source_version)

        if state.built is None:
            self._rows["built"].configure(text="none", fg=C["peach"])
            self._rows["artefact"].configure(text="—")
        else:
            # Two independent things can be wrong with the artefact, so they
            # get separate marks rather than one shared red.
            marks = []
            if state.built.version != state.source_version:
                marks.append("! different version from source")
            if state.newest_input_mtime > state.built.mtime:
                newest = (os.path.basename(str(state.newest_input))
                          if state.newest_input else "a source file")
                marks.append(f"! older than {newest}")
            suffix = ("   " + "   ".join(marks)) if marks else "   ok"
            self._rows["built"].configure(
                text=f"{state.built.version}{suffix}",
                fg=C["peach"] if marks else C["green"])
            self._rows["artefact"].configure(
                text=f"{os.path.basename(str(state.built.path))}"
                     f"   ({_age(state.built.mtime)})")

        if not state.editor_ok:
            self._rows["installed"].configure(text="editor did not answer",
                                              fg=C["subtext"])
        elif state.installed_version is None:
            self._rows["installed"].configure(text="not installed",
                                              fg=C["peach"])
        else:
            behind = (state.built is not None
                      and state.installed_version != state.built.version)
            self._rows["installed"].configure(
                text=state.installed_version
                     + ("   ! not the built version" if behind else "   ok"),
                fg=C["peach"] if behind else C["green"])

        colour = _STATUS_COLOUR.get(state.status, "subtext")
        self._status_lbl.configure(text=state.status_text, fg=C[colour])

        if not state.editor_ok:
            self._note.configure(
                text=f"'{self._editor_cmd()}' did not answer "
                     "--list-extensions, so the installed version is unknown "
                     "and installing is unavailable. Settings → Paths sets "
                     "editor_cmd.")
        else:
            self._note.configure(text="")

    # ── actions ───────────────────────────────────────────────────────────

    def _on_build(self) -> None:
        self._run_script(install=False)

    def _on_build_install(self) -> None:
        self._run_script(install=True)

    def _run_script(self, install: bool) -> None:
        if self._phase != IDLE or not self._checkout_present():
            return
        self._set_phase(INSTALLING if install else BUILDING)
        self._status_lbl.configure(
            text="building…" if not install else "building and installing…",
            fg=C["peach"])
        self._log("--- " + ("build + install" if install else "build") + " ---")

        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
               "-File", self._script, "-EditorCmd", self._editor_cmd()]
        if install:
            cmd.append("-Install")
        root = self._repo_root

        def _worker():
            try:
                out, rc = self._app._shell_capture(cmd, root)
                for line in (out or "").splitlines():
                    self._log_threadsafe(line)
                self._post(self._report_outcome, rc, out or "")
            except Exception as exc:
                self._log_threadsafe(f"✗ {exc}")
                self._post(self._report_outcome, 1, str(exc))
            finally:
                self._post(self._set_phase, IDLE)
                self._post(self._refresh_async)

        threading.Thread(target=_worker, daemon=True).start()

    def _report_outcome(self, rc: int, output: str) -> None:
        """Tk thread. A failed build and a failed verification are different
        diagnoses, and collapsing them loses the one that matters more.

        "It compiled but the artefact is not trusted" means something quite
        unlike "it did not compile": the first says the package contains
        something it should not, which is the case a release must never ship.
        """
        if rc == 0:
            self._log("✓ built and verified")
            return
        if "NOT fit to release" in output:
            self._log("✗ built, but the artefact FAILED verification")
            messagebox.showerror(
                "Artefact not trusted",
                "The extension compiled and packaged, but the built .vsix did "
                "not pass verification — so it was not installed.\n\n"
                "The reasons are in the log panel, under "
                "'NOT fit to release'.",
                parent=self)
        else:
            self._log(f"✗ build failed (exit {rc})")
            messagebox.showerror(
                "Build failed",
                f"build-extension.ps1 exited {rc}. The output is in the log "
                "panel below.",
                parent=self)

    def _on_uninstall(self) -> None:
        state = self._state
        if not state or not state.installed_version:
            return
        # By publisher.name, never by a file path: the editor knows the
        # extension by its identity, and the .vsix on disk may not even be the
        # build that is installed.
        identifier = state.qualified_name
        if not messagebox.askyesno(
                "Uninstall the extension?",
                f"Remove {identifier}@{state.installed_version} from "
                f"{self._editor_cmd()}?\n\nThe built .vsix is left on disk.",
                parent=self):
            return

        # Resolved here, on the Tk thread, so an unlaunchable editor is a
        # message rather than a worker that fails invisibly. `code` is
        # `code.CMD` on Windows and subprocess cannot find it by bare name.
        editor = resolve_editor(self._editor_cmd())
        if editor is None:
            messagebox.showerror(
                "Editor not found",
                f"'{self._editor_cmd()}' could not be resolved on PATH.",
                parent=self)
            return

        self._set_phase(INSTALLING)
        self._log(f"--- uninstall {identifier} ---")

        def _worker():
            try:
                proc = subprocess.run(
                    [editor, "--uninstall-extension", identifier],
                    capture_output=True, text=True, timeout=120,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace",
                )
                for line in (proc.stdout or "").splitlines():
                    self._log_threadsafe(line)
                self._log_threadsafe(
                    "✓ uninstalled" if proc.returncode == 0
                    else f"✗ uninstall failed (exit {proc.returncode})")
            except (OSError, subprocess.SubprocessError) as exc:
                self._log_threadsafe(f"✗ {exc}")
            finally:
                self._post(self._set_phase, IDLE)
                self._post(self._refresh_async)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_open_folder(self) -> None:
        target = self._ext_dir if os.path.isdir(self._ext_dir) else self._repo_root
        try:
            os.startfile(target)            # noqa: S606 - Windows shell open
        except (OSError, AttributeError) as exc:
            messagebox.showerror("Could not open the folder", str(exc),
                                 parent=self)
