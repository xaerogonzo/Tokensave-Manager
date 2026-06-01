"""PrivateRepoManagerDialog — manage an existing private local git repo.

Opens when a project already has a private repo configured.  Reads from
config (not git ls-files), so the file list reflects exactly what is being
synced.  Lets the user add/remove tracked files, change the destination
path, sync on demand, or remove the link entirely.

Thread-safety contract:
  • All subprocess.run calls happen in daemon threads.
  • Widget writes from threads always go through self.after(0, callback).
  • Every such callback starts with: if not self.winfo_exists(): return
  • Workers never touch _pending_private_sync / _active_private_syncs —
    those live in App and are managed by App's callbacks only.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from constants import C, CREATE_NO_WINDOW
from helpers.private_repo import _BACKUP_GIT_ENV, sync_private_repo


class PrivateRepoManagerDialog(tk.Toplevel):
    """Manage a configured private local git repo for a project."""

    def __init__(
        self,
        parent,
        src_path: str,
        cfg_entry: dict,
        cfg,
        git_exe: str,
        on_log,
        on_refresh,
    ):
        super().__init__(parent)
        self._src       = src_path
        self._cfg_entry = cfg_entry          # live dict from cfg.raw
        self._cfg       = cfg
        self._git_exe   = git_exe
        self._on_log    = on_log
        self._on_refresh = on_refresh

        name = os.path.basename(src_path)
        self.title(f"Private Repo Manager — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 500)
        self.grab_set()

        # Timer ID for the "✓ Saved." → health-revert after-call (M9)
        self._status_timer_id: str | None = None

        self._dest_var = tk.StringVar(value=cfg_entry.get("dest", ""))

        self._build_header(name)
        self._build_dest_row()
        self._build_file_list(cfg_entry.get("files", []))
        self._build_log_section(cfg_entry.get("dest", ""))
        self._build_action_row()
        self._build_status_row()
        self._centre_on_parent(parent)

        # Kick background git-log load
        self._reload_git_log(cfg_entry.get("dest", ""))

        # Determine initial health status
        self._refresh_health_status()

    # ── Build helpers ─────────────────────────────────────────────────────────

    def _build_header(self, name: str) -> None:
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🔒  Private Repo Manager",
                 bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text=name, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(2, 0))

    def _build_dest_row(self) -> None:
        sep = tk.Frame(self, bg=C["surface0"], height=1)
        sep.pack(fill=tk.X, padx=18, pady=(0, 10))

        row = tk.Frame(self, bg=C["base"])
        row.pack(fill=tk.X, padx=18, pady=(0, 12))
        tk.Label(row, text="Repo location:", bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, pady=(0, 4))

        entry_row = tk.Frame(row, bg=C["base"])
        entry_row.pack(fill=tk.X)
        self._dest_entry = ttk.Entry(entry_row, textvariable=self._dest_var, width=55)
        self._dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._browse_btn = ttk.Button(entry_row, text="Browse…",
                                      command=self._browse_dest)
        self._browse_btn.pack(side=tk.LEFT)

    def _build_file_list(self, files: list) -> None:
        lbl_row = tk.Frame(self, bg=C["base"])
        lbl_row.pack(fill=tk.X, padx=18, pady=(0, 4))
        tk.Label(lbl_row,
                 text="TRACKED FILES  (synced on every commit)",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT)

        # Treeview
        tv_frame = tk.Frame(self, bg=C["mantle"])
        tv_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 6))
        vsb = ttk.Scrollbar(tv_frame, orient="vertical")
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv = ttk.Treeview(tv_frame, columns=("file",), show="",
                                 selectmode="extended",
                                 yscrollcommand=vsb.set, height=7)
        self._tv.column("file", width=480, anchor=tk.W)
        vsb.configure(command=self._tv.yview)
        self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for fname in files:
            self._tv.insert("", tk.END, values=(fname,))

        # Buttons below the list
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 12))
        self._add_btn = ttk.Button(btn_row, text="Add File…",
                                   command=self._add_file)
        self._add_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._remove_btn = ttk.Button(btn_row, text="Remove Selected",
                                      command=self._remove_selected)
        self._remove_btn.pack(side=tk.LEFT)

    def _build_log_section(self, dest: str) -> None:
        sep = tk.Frame(self, bg=C["surface0"], height=1)
        sep.pack(fill=tk.X, padx=18, pady=(0, 8))

        tk.Label(self, text="RECENT COMMITS IN PRIVATE REPO",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, padx=18)

        # Error banner (hidden by default)
        self._log_banner = tk.Label(self, text="", bg=C["base"],
                                    fg=C["yellow"], font=("Segoe UI", 8),
                                    justify=tk.LEFT, anchor=tk.W)
        self._log_banner.pack(fill=tk.X, padx=18)

        txt_frame = tk.Frame(self, bg=C["mantle"])
        txt_frame.pack(fill=tk.X, padx=18, pady=(2, 12))
        self._log_txt = tk.Text(txt_frame, height=5, state="disabled",
                                font=("Consolas", 9), bg=C["mantle"],
                                fg=C["text"], relief="flat",
                                wrap=tk.NONE)
        self._log_txt.pack(fill=tk.X)

    def _build_action_row(self) -> None:
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 6))
        self._sync_btn = ttk.Button(btn_row, text="Sync Now",
                                    command=self._cmd_sync)
        self._sync_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Open Folder",
                   command=self._cmd_open_folder).pack(side=tk.LEFT, padx=(0, 6))
        self._save_btn = ttk.Button(btn_row, text="Save Changes",
                                    command=self._cmd_save)
        self._save_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._remove_link_btn = ttk.Button(btn_row, text="Remove Link…",
                                           command=self._cmd_remove_link)
        self._remove_link_btn.pack(side=tk.LEFT)

    def _build_status_row(self) -> None:
        self._status_lbl = tk.Label(self, text="", bg=C["base"],
                                    fg=C["green"], font=("Segoe UI", 9))
        self._status_lbl.pack(anchor=tk.W, padx=18, pady=(0, 12))

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 620, 560
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_status(self, msg: str, colour: str,
                    revert_to: tuple | None = None,
                    revert_delay_ms: int = 0) -> None:
        """Update status label, cancelling any pending revert timer first (M9)."""
        if self._status_timer_id is not None:
            self.after_cancel(self._status_timer_id)
            self._status_timer_id = None
        self._status_lbl.config(text=msg, fg=colour)
        if revert_to and revert_delay_ms:
            self._status_timer_id = self.after(
                revert_delay_ms, lambda: self._set_status(*revert_to))

    def _health_status(self) -> tuple[str, str]:
        """Return (msg, colour) reflecting current repo health."""
        dest = self._dest_var.get().strip()
        if os.path.isdir(dest):
            return ("✓ Repo healthy", C["green"])
        return ("⚠ Repo folder not found", C["red"])

    def _refresh_health_status(self) -> None:
        msg, colour = self._health_status()
        self._set_status(msg, colour)

    # ── UI locking (M8) ───────────────────────────────────────────────────────

    def _set_ui_locked(self, locked: bool) -> None:
        """Enable or disable all action buttons and dest entry."""
        state = tk.DISABLED if locked else tk.NORMAL
        for widget in (self._sync_btn, self._save_btn,
                       self._remove_link_btn, self._add_btn,
                       self._remove_btn, self._browse_btn,
                       self._dest_entry):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

    # ── Git log loading ───────────────────────────────────────────────────────

    def _reload_git_log(self, dest: str) -> None:
        """Kick a background thread to refresh the recent-commits Text widget."""
        def worker():
            try:
                proc = subprocess.run(
                    [self._git_exe, "-C", dest, "log", "--oneline", "-8"],
                    capture_output=True, text=True, timeout=8,
                    encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                err_msg = f"git log failed: {exc}"
                self.after(0, lambda m=err_msg: self._write_log_text("", error=m))
                return
            self.after(0, lambda: self._write_log_text(
                proc.stdout.strip(),
                error=proc.stderr.strip() if proc.returncode != 0 else ""))

        threading.Thread(target=worker, daemon=True).start()

    def _write_log_text(self, text: str, error: str = "") -> None:
        """Write git-log output to the Text widget (must run on main thread)."""
        if not self.winfo_exists():
            return
        # Error banner
        if error:
            self._log_banner.config(text=f"⚠ Could not read git log: {error}")
        else:
            self._log_banner.config(text="")

        body = text if text else "(no commits yet)"
        self._log_txt.configure(state="normal")
        self._log_txt.delete("1.0", tk.END)
        self._log_txt.insert(tk.END, body)
        self._log_txt.configure(state="disabled")

    # ── Pre-flight validation (M2 / M10) ─────────────────────────────────────

    def _validate_dest(self, dest: str) -> bool:
        """Show a warning and return False if dest is invalid."""
        if not dest:
            messagebox.showwarning("No destination",
                                   "Repo location is empty.", parent=self)
            return False
        abs_src  = os.path.abspath(self._src)
        abs_dest = os.path.abspath(dest)
        if abs_dest == abs_src or abs_dest.startswith(abs_src + os.sep):
            messagebox.showerror(
                "Invalid destination",
                "The private repo cannot be inside the project folder.\n\n"
                "Choose a folder outside the project.",
                parent=self)
            return False
        # Cross-drive guard (M10)
        try:
            os.path.relpath(dest, self._src)
        except ValueError:
            messagebox.showerror(
                "Invalid destination",
                "The repo location is on a different drive from the project.\n\n"
                "Choose a destination on the same drive, or use an absolute path "
                "that os.path.relpath can handle.",
                parent=self)
            return False
        return True

    # ── Browse ────────────────────────────────────────────────────────────────

    def _browse_dest(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose destination folder for private repo",
            initialdir=os.path.expanduser("~"),
            parent=self,
        )
        if chosen:
            self._dest_var.set(chosen)

    # ── Add / Remove files ────────────────────────────────────────────────────

    def _add_file(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add files to private repo tracking",
            initialdir=self._src,
            parent=self,
        )
        existing = {self._tv.item(iid)["values"][0] for iid in self._tv.get_children()}
        for p in paths:
            try:
                rel = os.path.relpath(p, self._src)
            except ValueError:
                continue  # cross-drive — skip (G10)
            if rel.startswith(".."):
                continue  # outside project tree — skip (G10)
            if rel not in existing:
                self._tv.insert("", tk.END, values=(rel,))
                existing.add(rel)

    def _remove_selected(self) -> None:
        sel = self._tv.selection()
        if not sel:
            messagebox.showwarning("Nothing selected",
                                   "Select one or more files to remove.",
                                   parent=self)
            return
        fnames = [self._tv.item(iid)["values"][0] for iid in sel]

        delete_from_repo = messagebox.askyesno(
            "Delete from repo?",
            f"Also delete {len(fnames)} file(s) from the private repo on disk?\n\n"
            "Your original copies are NEVER touched — only the files inside "
            "the private repo folder are removed.",
            parent=self,
        )

        # Remove from Treeview immediately
        for iid in sel:
            self._tv.delete(iid)

        if delete_from_repo:
            dest = self._dest_var.get().strip()
            self._set_ui_locked(True)
            self._set_status("Updating repo files…", C["yellow"])
            self._do_ghost_cleanup(dest, fnames)

    def _do_ghost_cleanup(self, dest: str, fnames: list[str]) -> None:
        """Background thread: delete files from private repo and commit (M3/M8)."""
        def worker():
            abs_dest = os.path.abspath(dest)
            for fname in fnames:
                fp = os.path.join(dest, fname)
                if os.path.isfile(fp):
                    try:
                        os.remove(fp)
                        # Prune empty parent dirs — never repo root
                        parent = os.path.abspath(os.path.dirname(fp))
                        if parent != abs_dest:
                            try:
                                os.removedirs(parent)
                            except OSError:
                                pass
                    except OSError as exc:
                        self._on_log(f"  ✗ Could not remove {fname}: {exc}", C["red"])

            # Commit the removal if repo exists
            if os.path.isdir(dest):
                short = ", ".join(fnames[:3])
                if len(fnames) > 3:
                    short += f" (+{len(fnames) - 3} more)"
                msg = f"remove: {short}"
                try:
                    subprocess.run(
                        [self._git_exe, "-C", dest, "add", "-A"],
                        capture_output=True, timeout=15,
                        env=_BACKUP_GIT_ENV,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    subprocess.run(
                        [self._git_exe, "-C", dest, "commit", "-m", msg],
                        capture_output=True, timeout=15,
                        env=_BACKUP_GIT_ENV,
                        creationflags=CREATE_NO_WINDOW,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                    self._on_log(f"  ✗ Ghost cleanup commit failed: {exc}", C["red"])

            self.after(0, self._on_ghost_cleanup_done, dest)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ghost_cleanup_done(self, dest: str) -> None:
        if not self.winfo_exists():
            return
        self._set_ui_locked(False)
        self._reload_git_log(dest)
        health = self._health_status()
        self._set_status(
            "✓ Files removed from repo.", C["green"],
            revert_to=health,
            revert_delay_ms=3000,
        )

    # ── Sync Now ──────────────────────────────────────────────────────────────

    def _cmd_sync(self) -> None:
        # --- Pre-flight on main thread (M2 / M10) ---
        dest  = self._dest_var.get().strip()
        files = [self._tv.item(iid)["values"][0]
                 for iid in self._tv.get_children()]

        if not self._validate_dest(dest):
            return

        if not files:
            messagebox.showwarning("No files tracked",
                                   "Add at least one file before syncing.",
                                   parent=self)
            return

        self._set_ui_locked(True)
        self._set_status("Syncing…", C["peach"])

        src = self._src
        git_exe = self._git_exe
        on_log = self._on_log

        def worker():
            result = sync_private_repo(git_exe, src, dest, files,
                                       on_log=on_log, commit_msg="")
            if not result and result.reason:
                on_log(f"  Detail: {result.reason}", "red")
            self.after(0, self._on_sync_complete, dest)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_complete(self, dest: str) -> None:
        if not self.winfo_exists():
            return
        self._set_ui_locked(False)
        self._reload_git_log(dest)
        self._on_refresh()
        self._refresh_health_status()

    # ── Open Folder ───────────────────────────────────────────────────────────

    def _cmd_open_folder(self) -> None:
        dest = self._dest_var.get().strip()
        if not dest or not os.path.isdir(dest):
            messagebox.showwarning("Folder not found",
                                   f"The repo folder does not exist:\n{dest}",
                                   parent=self)
            return
        os.startfile(dest)

    # ── Save Changes ──────────────────────────────────────────────────────────

    def _cmd_save(self) -> None:
        dest = self._dest_var.get().strip()
        if not self._validate_dest(dest):
            return

        files = [self._tv.item(iid)["values"][0]
                 for iid in self._tv.get_children()]
        src_path = self._src
        self._cfg.raw.setdefault("private_repos", {})[src_path] = {
            "dest":  dest,
            "files": files,
        }
        self._cfg.save()

        health = self._health_status()
        self._set_status(
            "✓ Saved.", C["green"],
            revert_to=health,
            revert_delay_ms=3000,
        )

    # ── Remove Link ───────────────────────────────────────────────────────────

    def _cmd_remove_link(self) -> None:
        if not messagebox.askyesno(
            "Remove private repo link?",
            "This will stop auto-sync for this project.\n\n"
            "The private repo folder itself is NOT deleted — "
            "only the link in manager-config.json is removed.\n\n"
            "Continue?",
            parent=self,
        ):
            return
        repos = self._cfg.raw.get("private_repos", {})
        repos.pop(self._src, None)
        self._cfg.save()
        self._on_refresh()
        self.destroy()
