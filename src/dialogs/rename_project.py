"""dialogs/rename_project.py — rename a project folder as a coordinated migration.

See helpers/project_rename.py for why a folder rename is treated as a
migration with independently-reportable phases rather than an atomic OS
rename: tokensave, Claude Code's project record and session store, and git
worktree metadata each reference the project's path independently.

Preflight runs twice: once to populate this dialog, once again, fresh,
immediately before the mutation. Findings gathered the first time do not
authorize a click against a state that has since changed — if the second
scan disagrees with the first on anything hazard-relevant, the dialog re-
renders the new findings and requires another click rather than proceeding.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from theme import UiPumpMixin, bind_mousewheel
from helpers import project_rename as pr


def _fmt_age(epoch: float) -> str:
    secs = max(0.0, time.time() - epoch)
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


def _wrapper_selections():
    from helpers.mcp_shadow import WRAPPER_SELECTIONS
    return WRAPPER_SELECTIONS


class RenameProjectDialog(UiPumpMixin, tk.Toplevel):
    """Rename a tokensave project folder, repointing what the Manager knows
    how to repoint and reporting plainly on what it could not.

    `on_done(new_path, ok)` fires after the dialog closes following an
    attempted migration — never for a plain Cancel. `ok` is False for a
    `PARTIAL_FAILURE`; the caller should still refresh, since the filesystem
    move may have happened even when a later phase did not.
    """

    def __init__(self, parent, cfg, path: str, on_done=None, on_log=None):
        super().__init__(parent)
        self._start_ui_pump()
        self._cfg = cfg
        self._old_path = path
        self._on_done = on_done
        self._on_log = on_log or (lambda *a, **k: None)

        self._latest_preflight = None
        self._busy = False
        self._typed_var: "tk.StringVar | None" = None
        self._force_worktree_var: "tk.BooleanVar | None" = None
        self._move_history_var: "tk.BooleanVar | None" = None

        self.title("Rename Project")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(600, 560)
        self.grab_set()

        self._build_header()
        self._build_name_field()
        self._build_findings_section()
        self._build_confirm_section()
        self._build_action_bar()

        self._centre_on_parent(parent)
        self._refresh_preflight()

    # ── layout ───────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="✏️  Rename Project", bg=C["surface0"],
                 fg=C["text"], font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text=self._old_path, bg=C["surface0"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(2, 0))

    def _build_name_field(self) -> None:
        row = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        row.pack(fill=tk.X)
        tk.Label(row, text="New folder name:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W)
        self._name_var = tk.StringVar(value=os.path.basename(self._old_path))
        entry = ttk.Entry(row, textvariable=self._name_var, width=48)
        entry.pack(anchor=tk.W, pady=(2, 0), fill=tk.X)
        entry.selection_range(0, tk.END)
        entry.focus_set()
        self._name_var.trace_add("write", lambda *_: self._on_name_changed())
        self._name_error = tk.Label(row, text="", bg=C["base"], fg=C["red"],
                                    font=("Segoe UI", 8), wraplength=540,
                                    justify=tk.LEFT)
        self._name_error.pack(anchor=tk.W, pady=(2, 0))

    def _build_findings_section(self) -> None:
        wrap = tk.LabelFrame(self, text="Findings", fg=C["subtext"], bg=C["base"],
                             font=("Segoe UI", 9, "bold"))
        wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(4, 4))
        canvas = tk.Canvas(wrap, bg=C["base"], highlightthickness=0)
        bind_mousewheel(canvas)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._findings_body = tk.Frame(canvas, bg=C["base"])
        body_id = canvas.create_window((0, 0), window=self._findings_body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_id, width=e.width))
        self._findings_body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    def _build_confirm_section(self) -> None:
        self._confirm_frame = tk.Frame(self, bg=C["base"], padx=18)
        self._confirm_frame.pack(fill=tk.X, pady=(0, 4))

    def _build_action_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        self._rename_btn = ttk.Button(bar, text="Rename", style="Primary.TButton",
                                      command=self._on_rename_clicked,
                                      state=tk.DISABLED)
        self._rename_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self._status_label = tk.Label(bar, text="Scanning…", bg=C["base"],
                                      fg=C["subtext"], font=("Segoe UI", 9))
        self._status_label.pack(side=tk.LEFT)

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 640, 640
        try:
            px = parent.winfo_x() + (parent.winfo_width() - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── preflight ────────────────────────────────────────────────────────

    def _refresh_preflight(self) -> None:
        self._status_label.configure(text="Scanning…")
        self._rename_btn.configure(state=tk.DISABLED)
        cfg, old_path = self._cfg, self._old_path

        def _worker() -> None:
            found = pr.preflight(old_path, cfg, cfg.git_exe)
            self._post(lambda: self._on_preflight_done(found))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_preflight_done(self, found: "pr.RenamePreflight") -> None:
        self._latest_preflight = found
        self._status_label.configure(text="")
        self._render_findings(found)
        self._on_name_changed()

    @staticmethod
    def _hazard_signature(found: "pr.RenamePreflight") -> tuple:
        return (
            len(found.tokensave_servers),
            len(found.active_sessions),
            bool(found.worktree_locked),
            found.nested_worktree_conflict,
            found.manager_cwd_inside_project,
        )

    # ── findings rendering ───────────────────────────────────────────────

    def _line(self, text: str, colour=None) -> None:
        tk.Label(self._findings_body, text=text, bg=C["base"],
                 fg=colour or C["text"], font=("Segoe UI", 9),
                 wraplength=560, justify=tk.LEFT, anchor="w",
                ).pack(fill=tk.X, anchor=tk.W, pady=(2, 0))

    def _render_findings(self, found: "pr.RenamePreflight") -> None:
        for child in self._findings_body.winfo_children():
            child.destroy()
        for child in self._confirm_frame.winfo_children():
            child.destroy()
        self._typed_var = None
        self._force_worktree_var = None
        self._move_history_var = None

        if found.manager_cwd_inside_project:
            self._line(
                "⛔  The Manager's own working directory is inside this "
                "project. It cannot safely move out of the way from a "
                "background thread. Close and reopen the Manager from a "
                "different folder, then retry.", C["red"])

        self._render_server_findings(found)
        self._render_worktree_findings(found)
        self._render_session_findings(found)

        if not any((found.tokensave_servers, found.active_sessions,
                   found.is_linked_worktree, found.linked_worktrees,
                   found.nested_worktree_conflict,
                   found.manager_cwd_inside_project)):
            self._line(
                "✓  No running tokensave server, no worktree "
                "complications, no Claude Code session observed for this "
                "project.", C["green"])

    def _render_server_findings(self, found: "pr.RenamePreflight") -> None:
        for srv in found.tokensave_servers:
            self._line(
                f"⚠  A tokensave server (pid {srv.pid}) will be stopped "
                f"before the rename — attribution: {srv.attribution}.",
                C["yellow"])
            if srv.selection in _wrapper_selections():
                self._line(
                    "    Claude Desktop does not restart a server it did not "
                    "expect to die — it will show “disconnected” "
                    "until Desktop itself is restarted.", C["yellow"])

    def _render_worktree_findings(self, found: "pr.RenamePreflight") -> None:
        if found.nested_worktree_conflict:
            self._line(
                "⛔  A linked worktree of this project lives INSIDE the "
                "folder being renamed. Moving it would silently move that "
                "worktree too. Move the worktree out first.", C["red"])
            return

        if found.is_linked_worktree:
            if found.worktree_locked:
                self._line(
                    "\U0001f512  This folder is a LOCKED linked git worktree. "
                    "Git will refuse to move it unless forced.", C["yellow"])
                self._force_worktree_var = tk.BooleanVar(value=False)
                tk.Checkbutton(
                    self._findings_body,
                    text="Force the move anyway (git worktree move --force)",
                    variable=self._force_worktree_var, bg=C["base"], fg=C["text"],
                    selectcolor=C["surface0"], font=("Segoe UI", 9),
                    command=self._on_name_changed,
                ).pack(anchor=tk.W, pady=(0, 4))
            else:
                self._line(
                    "ℹ  This folder is a linked git worktree — "
                    "'git worktree move' will be used instead of an ordinary "
                    "rename.", C["blue"])
        elif found.linked_worktrees:
            self._line(
                f"ℹ  {len(found.linked_worktrees)} linked worktree(s) exist "
                "for this repo. They will not move; 'git worktree repair' will "
                "run afterward to keep their back-references correct.", C["blue"])

    def _render_session_findings(self, found: "pr.RenamePreflight") -> None:
        if found.session_store_exists:
            self._line(
                f"ℹ  {found.session_store_file_count} Claude Code session "
                "transcript(s) are stored under this project's current path.",
                C["blue"])
            self._move_history_var = tk.BooleanVar(value=True)
            tk.Checkbutton(
                self._findings_body,
                text="Move this session history to the new path too",
                variable=self._move_history_var, bg=C["base"], fg=C["text"],
                selectcolor=C["surface0"], font=("Segoe UI", 9),
            ).pack(anchor=tk.W, pady=(0, 4))

        if not found.active_sessions:
            return
        newest = max(found.active_sessions, key=lambda s: s["last_activity"])
        age = _fmt_age(newest["last_activity"])
        self._line(
            f"⚠  A Claude Code session was OBSERVED for this project "
            f"(last activity {age} ago). This is evidence, not proof it still "
            "holds the folder open — but if it does, renaming will not be "
            "visible to that session: it keeps seeing the old, now-moved "
            "folder. Claude may also be running and could overwrite "
            "~/.claude.json's trust record after this dialog writes it; the "
            "Manager verifies the write afterward and reports if it did not "
            "stick.", C["yellow"])
        tk.Label(
            self._confirm_frame,
            text=("Type the current folder name (%s) to confirm:"
                 % os.path.basename(self._old_path)),
            bg=C["base"], fg=C["subtext"], font=("Segoe UI", 9),
        ).pack(anchor=tk.W)
        self._typed_var = tk.StringVar()
        ttk.Entry(self._confirm_frame, textvariable=self._typed_var, width=40
                 ).pack(anchor=tk.W, pady=(2, 4))
        self._typed_var.trace_add("write", lambda *_: self._on_name_changed())

    # ── validation / gating ──────────────────────────────────────────────

    def _on_name_changed(self) -> None:
        found = self._latest_preflight
        if found is None:
            self._rename_btn.configure(state=tk.DISABLED)
            return

        new_name = self._name_var.get().strip()
        new_path = os.path.join(os.path.dirname(self._old_path), new_name)
        check = pr.validate_destination(self._old_path, new_path)
        if not check.ok:
            self._name_error.configure(text=check.reason)
            self._rename_btn.configure(state=tk.DISABLED)
            return
        self._name_error.configure(text="")

        if found.manager_cwd_inside_project or found.nested_worktree_conflict:
            self._rename_btn.configure(state=tk.DISABLED)
            return
        if found.worktree_locked and not (
                self._force_worktree_var and self._force_worktree_var.get()):
            self._rename_btn.configure(state=tk.DISABLED)
            return
        if found.active_sessions:
            typed = self._typed_var.get().strip() if self._typed_var else ""
            if typed != os.path.basename(self._old_path):
                self._rename_btn.configure(state=tk.DISABLED)
                return
        self._rename_btn.configure(state=tk.NORMAL)

    # ── the migration itself ─────────────────────────────────────────────

    def _on_rename_clicked(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._rename_btn.configure(state=tk.DISABLED, text="Checking…")
        cfg, old_path = self._cfg, self._old_path
        prior_signature = self._hazard_signature(self._latest_preflight)

        def _worker() -> None:
            fresh = pr.preflight(old_path, cfg, cfg.git_exe)
            self._post(lambda: self._after_recheck(fresh, prior_signature))

        threading.Thread(target=_worker, daemon=True).start()

    def _after_recheck(self, fresh: "pr.RenamePreflight", prior_signature: tuple) -> None:
        self._busy = False
        self._rename_btn.configure(text="Rename")
        self._render_findings(fresh)
        self._latest_preflight = fresh
        self._on_name_changed()
        if self._hazard_signature(fresh) != prior_signature:
            messagebox.showwarning(
                "Findings changed",
                "What this rename would affect has changed since you last "
                "reviewed it. Review the updated findings below and click "
                "Rename again to proceed.", parent=self)
            return
        self._run_migration(fresh)

    def _run_migration(self, found: "pr.RenamePreflight") -> None:
        new_name = self._name_var.get().strip()
        new_path = os.path.join(os.path.dirname(self._old_path), new_name)
        old_path = self._old_path
        move_history = bool(self._move_history_var and self._move_history_var.get())
        force_worktree = bool(self._force_worktree_var and self._force_worktree_var.get())
        git_exe = self._cfg.git_exe
        cfg = self._cfg

        self._rename_btn.configure(state=tk.DISABLED)
        self._status_label.configure(text="Renaming…")

        def _worker() -> None:
            lines: list = []
            self._migrate(found, old_path, new_path, cfg, git_exe,
                          move_history, force_worktree, lines)
            ok = not any(l.startswith("PARTIAL_FAILURE") for l in lines)
            self._post(lambda: self._finish(lines, ok, new_path))

        threading.Thread(target=_worker, daemon=True).start()

    def _migrate(self, found, old_path, new_path, cfg, git_exe,
                move_history, force_worktree, lines: list) -> None:
        """Runs on the worker thread. Appends one report line per phase —
        never raises past a phase, since a later phase's outcome must still
        be reported even when an earlier one only partly succeeded."""
        from helpers.tokensave_daemon import stop_tokensave_server

        for srv in found.tokensave_servers:
            ok, detail = stop_tokensave_server(srv, confirmed=True)
            lines.append(f"tokensave pid {srv.pid}: "
                        f"{'stopped' if ok else 'FAILED — ' + detail}")
        remaining = pr.servers_for_project(old_path, cfg.tokensave_exe)
        if remaining:
            lines.append(
                f"PARTIAL_FAILURE: {len(remaining)} server(s) are still "
                "attributed to the old path — rename aborted before "
                "touching the filesystem.")
            return

        if found.is_linked_worktree:
            move = pr.move_linked_worktree(old_path, new_path, git_exe,
                                           force=force_worktree)
        else:
            move = pr.move_directory(old_path, new_path)
        if not move.ok:
            hint = (" A process appears to be using this folder — see "
                    "docs/gotchas/renaming-a-project-folder.md."
                    if pr.is_in_use_failure(move) else "")
            lines.append(
                f"PARTIAL_FAILURE: filesystem move failed "
                f"({move.rename_failed_reason or move.reason}). Nothing else "
                f"was changed.{hint}")
            return
        lines.append(f"Filesystem: moved via '{move.strategy}'.")

        touched = pr.repoint_manager_config(cfg, old_path, new_path)
        lines.append("Manager config repointed: " + ", ".join(touched.keys())
                    if touched else "Manager config: nothing to repoint.")

        cj = pr.repoint_claude_json(old_path, new_path)
        lines.append(self._describe_claude_repoint(cj))
        if cj.outcome == "blocked" or cj.write_ok is False or cj.verified is False:
            lines.append(
                "PARTIAL_FAILURE: the folder moved, but the Claude Code "
                "project record was not fully repointed — see the line "
                "above for what to fix by hand.")

        if move_history:
            lines.append(f"Claude session history: "
                        f"{pr.repoint_session_store(old_path, new_path)}")

        if not found.is_linked_worktree and found.linked_worktrees:
            repaired = pr.repair_main_worktree_links(new_path, git_exe)
            lines.append("Linked worktree back-references: "
                        + ("repaired" if repaired else "repair FAILED"))

        report = pr.verify(new_path, old_path, cfg.tokensave_exe, git_exe,
                           was_linked_worktree=found.is_linked_worktree,
                           expected_linked_worktrees=found.linked_worktrees)
        lines.append(
            f"Verification: servers_gone={report.servers_gone}, "
            f"tokensave_dir_present={report.tokensave_dir_present}, "
            f"git_dirs_resolve={report.git_dirs_resolve}")
        if report.git_dirs_resolve is False:
            lines.append(
                "PARTIAL_FAILURE: git no longer resolves cleanly at the new "
                "path — investigate before treating this project as normal.")

    @staticmethod
    def _describe_claude_repoint(cj: "pr.RepointResult") -> str:
        text = f"Claude project record: {cj.outcome}"
        if cj.outcome == "blocked":
            text += f" (conflicting fields: {', '.join(cj.conflicting_fields)})"
        elif cj.write_ok is False:
            text += " — write failed"
        elif cj.verified is False:
            text += " — written but NOT verified afterward (Claude may have overwritten it)"
        elif cj.verified:
            text += " — verified"
        return text

    def _finish(self, lines: list, ok: bool, path: str) -> None:
        self._busy = False
        self._status_label.configure(text="")
        summary = "\n".join(f"• {line}" for line in lines)
        if ok:
            messagebox.showinfo("Rename complete", summary, parent=self)
        else:
            messagebox.showerror("Rename incomplete", summary, parent=self)
        for line in lines:
            self._on_log("  " + line, C["blue"] if ok else C["red"])
        on_done = self._on_done
        self.destroy()
        if on_done:
            on_done(path, ok)
