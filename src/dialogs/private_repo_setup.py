"""PrivateRepoSetupDialog — wizard for creating a local-only git repo.

Lets the user pick a destination folder and choose which files to copy
from the current project.  On confirm:
  1. Validates dest is not inside src (G4/G12)
  2. Creates the destination directory
  3. Runs `git init`
  4. Copies selected files (subdirs created automatically — G1)
  5. Runs `git add -A && git commit` with injected identity (G7)
  6. Opens the new folder in Explorer
  7. Dispatches config registration back to the main thread (G11)

No terminal is ever required.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from constants import C, CREATE_NO_WINDOW
from helpers.private_repo import _BACKUP_GIT_ENV
from theme import bind_mousewheel, themed_checkbutton

# Cap: filter + sort applied first, then this limit (G3/G13)
_FILE_CAP = 300


class PrivateRepoSetupDialog(tk.Toplevel):
    """Wizard dialog for creating a private local-only git repo."""

    def __init__(
        self,
        parent,
        src_path: str,
        gitignored_files: list,
        git_exe: str,
        on_log=None,
        on_registered=None,   # callable(dest, files) → dispatched on main thread
    ):
        super().__init__(parent)
        self._src          = src_path
        self._git_exe      = git_exe
        self._on_log       = on_log or (lambda msg, col="": None)
        self._on_registered = on_registered
        self._parent_after = parent.after   # captured before any destroy
        name = os.path.basename(src_path)

        self.title(f"Create Private Local Repo — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 440)
        self.grab_set()

        self._dest_var = tk.StringVar(value=self._default_dest(src_path, name))
        self._file_vars: list = []

        self._build_header(name)
        self._build_explanation()
        self._build_dest_row()
        self._build_file_list(gitignored_files)
        self._build_buttons()
        self._centre_on_parent(parent)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_header(self, name: str):
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🔒  Create Private Local Repo",
                 bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text=name, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(2, 0))

    def _build_explanation(self):
        expl = tk.Frame(self, bg=C["base"])
        expl.pack(fill=tk.X, padx=18, pady=(0, 10))
        tk.Label(
            expl,
            text=(
                "Creates a git repo on your computer only — GitHub never sees it.\n"
                "Selected files are copied there and committed as version history.\n"
                "Your originals stay exactly where they are."
            ),
            bg=C["base"], fg=C["text"], font=("Segoe UI", 9),
            justify=tk.LEFT, anchor=tk.W, wraplength=520,
        ).pack(anchor=tk.W)

    def _build_dest_row(self):
        sep = tk.Frame(self, bg=C["surface0"], height=1)
        sep.pack(fill=tk.X, padx=18, pady=(0, 10))

        row = tk.Frame(self, bg=C["base"])
        row.pack(fill=tk.X, padx=18, pady=(0, 12))
        tk.Label(row, text="Save repo to:", bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, pady=(0, 4))

        entry_row = tk.Frame(row, bg=C["base"])
        entry_row.pack(fill=tk.X)
        entry = ttk.Entry(entry_row, textvariable=self._dest_var, width=55)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(entry_row, text="Browse…",
                   command=self._browse_dest).pack(side=tk.LEFT)

    def _build_file_list(self, files: list):
        tk.Label(self, text="FILES TO COPY",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, padx=18, pady=(0, 2))

        # Cap warning banner (G13)
        total = len(files)
        if total > _FILE_CAP:
            files = files[:_FILE_CAP]
            warn = tk.Label(
                self,
                text=f"⚠  Showing first {_FILE_CAP} of {total} files. "
                     "Use Add File… for anything not listed.",
                bg=C["base"], fg=C["yellow"],
                font=("Segoe UI", 8), justify=tk.LEFT,
            )
            warn.pack(anchor=tk.W, padx=18, pady=(0, 4))

        list_outer = tk.Frame(self, bg=C["mantle"])
        list_outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 6))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                  highlightthickness=0, height=150)
        bind_mousewheel(self._canvas)
        vsb = ttk.Scrollbar(list_outer, orient="vertical",
                             command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(self._canvas, bg=C["mantle"])
        self._list_body_id = self._canvas.create_window(
            (0, 0), window=self._list_body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._list_body_id, width=e.width))
        self._list_body.bind("<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))

        if not files:
            tk.Label(self._list_body,
                     text="  (no gitignored files found — use Add File to pick manually)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Consolas", 9, "italic"),
                     padx=10, pady=10).pack(anchor=tk.W)
        else:
            for fname in files:
                self._add_file_row(fname, checked=False)

        # Quick-select + Add row
        sel_row = tk.Frame(self, bg=C["base"])
        sel_row.pack(fill=tk.X, padx=18, pady=(0, 10))
        ttk.Button(sel_row, text="Select All",
                   command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(sel_row, text="Select None",
                   command=lambda: self._set_all(False)).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(sel_row, text="Add File…",
                   command=self._add_extra_file).pack(side=tk.LEFT)

    def _build_buttons(self):
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._create_btn = ttk.Button(btn_row, text="Create Repo",
                                       command=self._apply)
        self._create_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _default_dest(self, src_path: str, project_name: str) -> str:
        """Include a short path hash to avoid collisions with same-named projects (G9)."""
        path_hash = hashlib.md5(
            os.path.abspath(src_path).encode("utf-8")
        ).hexdigest()[:8]
        base = os.path.join(os.path.expanduser("~"), "Private-Repos")
        return os.path.join(base, f"{project_name}-{path_hash}")

    def _add_file_row(self, fname: str, checked: bool = False):
        row = tk.Frame(self._list_body, bg=C["mantle"])
        row.pack(fill=tk.X, padx=4, pady=1)
        var = tk.BooleanVar(value=checked)
        self._file_vars.append((var, fname))
        themed_checkbutton(row, variable=var, bg=C["mantle"],
                           activebackground=C["mantle"]).pack(side=tk.LEFT)
        tk.Label(row, text=fname, anchor=tk.W,
                 font=("Consolas", 9), bg=C["mantle"],
                 fg=C["text"]).pack(side=tk.LEFT, padx=(4, 6))

    def _centre_on_parent(self, parent):
        self.update_idletasks()
        w, h = 600, 540
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    def _set_all(self, value: bool):
        for var, _ in self._file_vars:
            var.set(value)

    def _browse_dest(self):
        chosen = filedialog.askdirectory(
            title="Choose destination folder for private repo",
            initialdir=os.path.expanduser("~"),
            parent=self,
        )
        if chosen:
            self._dest_var.set(chosen)

    def _add_extra_file(self):
        paths = filedialog.askopenfilenames(
            title="Add files to copy into private repo",
            initialdir=self._src,
            parent=self,
        )
        existing = {f for _, f in self._file_vars}
        for p in paths:
            try:
                rel = os.path.relpath(p, self._src)
            except ValueError:
                # Cross-drive path on Windows — can't make relative (G10)
                continue
            if rel.startswith(".."):
                # File is outside the project tree — reject (G10)
                continue
            if rel not in existing:
                self._add_file_row(rel, checked=True)
                existing.add(rel)
        self._list_body.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    # ── Action ────────────────────────────────────────────────────────────────

    def _apply(self):
        dest = self._dest_var.get().strip()
        if not dest:
            messagebox.showwarning("No destination",
                "Choose a destination folder first.", parent=self)
            return

        # G4 / G12: reject if dest is the same as src or nested inside it
        abs_src  = os.path.abspath(self._src)
        abs_dest = os.path.abspath(dest)
        if abs_dest == abs_src or abs_dest.startswith(abs_src + os.sep):
            messagebox.showerror(
                "Invalid destination",
                "The private repo cannot be inside the project folder.\n\n"
                "Choose a folder outside the project (e.g. your home folder).",
                parent=self)
            return

        selected = [fname for var, fname in self._file_vars if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to copy.", parent=self)
            return

        self._create_btn.configure(state=tk.DISABLED)
        src         = self._src
        git_exe     = self._git_exe
        log         = self._on_log
        registered  = self._on_registered
        parent_after = self._parent_after
        self.destroy()

        def worker():
            # 1. Create dest dir
            try:
                os.makedirs(dest, exist_ok=True)
                log(f"  Created {dest}", C["green"])
            except OSError as e:
                log(f"  ✗ Error creating folder: {e}", C["red"])
                return

            # 2. git init
            proc = subprocess.run(
                [git_exe, "-C", dest, "init"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                env=_BACKUP_GIT_ENV,
                creationflags=CREATE_NO_WINDOW,
            )
            log("$ git init", C["overlay0"])
            for line in proc.stdout.strip().splitlines():
                log(f"  {line}", C["green"] if proc.returncode == 0 else C["red"])
            if proc.returncode != 0:
                log("  ✗ git init failed — aborting.", C["red"])
                return

            # 3. Copy files (G1: makedirs before copy)
            failed_copies = []
            for fname in selected:
                src_file  = os.path.join(src, fname)
                dest_file = os.path.join(dest, fname)
                try:
                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    log(f"  Copied {fname}", C["green"])
                except OSError as e:
                    log(f"  ✗ Could not copy {fname}: {e}", C["red"])
                    failed_copies.append(fname)

            if failed_copies:
                log(f"  {len(failed_copies)} file(s) could not be copied — continuing.",
                    C["yellow"])

            # 4. git add -A
            proc = subprocess.run(
                [git_exe, "-C", dest, "add", "-A"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                env=_BACKUP_GIT_ENV,
                creationflags=CREATE_NO_WINDOW,
            )
            log("$ git add -A", C["overlay0"])
            if proc.returncode != 0:
                log(f"  ✗ {proc.stderr.strip()}", C["red"])
                return

            # 5. git commit (G7: identity via env, not --author flag)
            proj_name = os.path.basename(src)
            msg = f"Initial: private backup from {proj_name}"
            proc = subprocess.run(
                [git_exe, "-C", dest, "commit", "-m", msg],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                env=_BACKUP_GIT_ENV,
                creationflags=CREATE_NO_WINDOW,
            )
            log("$ git commit", C["overlay0"])
            for line in proc.stdout.strip().splitlines()[-4:]:
                log(f"  {line}", C["green"] if proc.returncode == 0 else C["red"])

            if proc.returncode == 0:
                log(f"  ✓ Private repo ready at {dest}", C["green"])
                # G11: dispatch config save to main thread
                if registered:
                    parent_after(0, lambda: registered(dest, selected))
                os.startfile(dest)
            else:
                log(f"  ✗ commit failed: {proc.stderr.strip()}", C["red"])

        threading.Thread(target=worker, daemon=True).start()
