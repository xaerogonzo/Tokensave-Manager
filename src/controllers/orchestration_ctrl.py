"""The "Agent tasks" view of the Tasks tab (docs/ORCHESTRATION.md, stage A).

A thin controller: it lists the selected project's delegated tasks, starts one
through the dialog, and turns a person's click into the one transition that
click is allowed to make. Every decision (can it start, what happened, what is a
legal next state) lives in `helpers/orchestration_*`, which have no Tk.

What the buttons mean, because the words matter:

* **Accept** records that a person looked at the result and wants it. It merges
  NOTHING: the branch stays where it is and is merged with the Git tab like any
  other branch (docs/ORCHESTRATION.md D2).
* **Reject** records that it is not wanted. Its worktree stays until discarded.
* **Discard worktree** removes the task's separate copy. The BRANCH is kept: it
  holds the work.
* **Cancel** stops a running task by killing its whole process tree.

Workers never touch Tk; they `_post` to the pump (`theme.UiPumpMixin`).
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from constants import C, CREATE_NO_WINDOW
from helpers import agent_cli
from helpers import orchestration_limits as limits_mod
from helpers import orchestration_run as orun
from helpers import orchestration_task as ot
from helpers import orchestration_worktree as wt
from theme import UiPumpMixin, _Tooltip

_DIFF_LINES = 2000

#: status -> Treeview tag (colour). Words stay the same as the lifecycle's.
_TAGS = {ot.ST_RUNNING: "run", ot.ST_RESULT_READY: "ready", ot.ST_FAILED: "bad",
         ot.ST_LOST: "bad", ot.ST_CANCELLED: "dim", ot.ST_REJECTED: "dim",
         ot.ST_ACCEPTED: "ok"}


class AgentTasksPanel(UiPumpMixin):
    def __init__(self, parent: tk.Widget, cfg, *,
                 get_project_path: Callable[[], "str | None"]) -> None:
        self._cfg = cfg
        self._get_project_path = get_project_path
        self._frame = parent
        self._req = 0
        self._rows: dict = {}
        self._build()
        self._start_ui_pump()

    def _ui_host(self):
        return self._frame

    # ── build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        f = self._frame
        self._note = tk.Label(f, anchor="w", justify=tk.LEFT, bg=C["base"],
                              fg=C["overlay0"], wraplength=1100)
        self._note.pack(fill=tk.X, padx=8, pady=(6, 2))

        bar = tk.Frame(f, bg=C["base"])
        bar.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._buttons: dict = {}
        for key, text, cmd, tip in (
            ("new", "New task…", self.new_task,
             "Hand a task to an agent. It works in a separate copy on its own "
             "branch; your checkout is not touched."),
            ("cancel", "Cancel", self.cancel,
             "Stop the running task by ending its whole process tree."),
            ("diff", "Show changes", self.show_diff,
             "What the agent changed, measured by git (not the agent's own account)."),
            ("open", "Open folder", self.open_worktree, "Open the task's copy."),
            ("accept", "Accept", self.accept,
             "Record that you want this result. Merges nothing: merge the branch "
             "in the Git tab."),
            ("reject", "Reject", self.reject, "Record that you do not want it."),
            ("discard", "Discard copy…", self.discard,
             "Delete the task's separate copy. The branch is kept."),
        ):
            btn = ttk.Button(bar, text=text, command=cmd)
            btn.pack(side=tk.LEFT, padx=(0, 4))
            _Tooltip(btn, tip)
            self._buttons[key] = btn

        cols = ("status", "agent", "task", "changes", "when", "branch")
        self._tree = ttk.Treeview(f, columns=cols, show="headings", height=8,
                                  selectmode="browse")
        for col, text, width, stretch in (
                ("status", "Status", 110, False), ("agent", "Agent", 70, False),
                ("task", "Task", 360, True), ("changes", "Changes", 130, False),
                ("when", "Started", 130, False), ("branch", "Branch", 200, True)):
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, minwidth=50, stretch=stretch)
        for tag, colour in (("run", "yellow"), ("ready", "green"), ("bad", "red"),
                            ("dim", "overlay0"), ("ok", "blue")):
            self._tree.tag_configure(tag, foreground=C[colour])
        self._tree.pack(fill=tk.BOTH, expand=True, padx=8)
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

        self._detail = tk.Text(f, height=7, wrap=tk.WORD, state=tk.DISABLED,
                               bg=C["base"], fg=C["text"], relief=tk.FLAT,
                               font=("Consolas", 9), highlightthickness=0)
        self._detail.pack(fill=tk.X, padx=8, pady=(4, 6))

    # ── refresh ────────────────────────────────────────────────────────────────

    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "enable_agent_orchestration", False))

    def refresh(self) -> None:
        project = self._get_project_path()
        self._req += 1
        req, git_exe = self._req, self._cfg.git_exe
        if not project:
            self._apply(req, None, "Select a project on the Projects tab first.", [], 0)
            return

        def work() -> None:
            # A task recorded as running whose process is gone becomes `lost`
            # here, before the list is drawn, so it is never shown as running.
            ot.reconcile(project, orun.is_alive, git_exe)
            seen = ot.list_tasks(project, git_exe)
            self._post(self._apply, req, project, "", list(seen.tasks), seen.bad_files)
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, req: int, project, msg: str, tasks: list, bad: int) -> None:
        if req != self._req or not self._frame.winfo_exists():
            return
        if not self.enabled():
            msg = ("Delegated agent tasks are OFF. Turn them on in Settings → AI. "
                   "Nothing here starts an agent while it is off.")
        elif not msg:
            msg = (f"{os.path.basename(project)}: {len(tasks)} task(s)."
                   + (f" {bad} record(s) could not be read." if bad else "")
                   + " An agent works in a separate copy on its own branch; nothing "
                     "is merged or pushed without you.")
        self._note.config(text=msg)
        selected = self._tree.selection()
        self._tree.delete(*self._tree.get_children())
        self._rows = {}
        for rec in reversed(tasks):
            tid = rec["task_id"]
            self._rows[tid] = rec
            diff = rec.get("diff") or {}
            changes = (f"{diff.get('file_count', 0)} file(s) "
                       f"+{diff.get('insertions', 0)}/-{diff.get('deletions', 0)}"
                       if diff else "")
            self._tree.insert("", tk.END, iid=tid, tags=(_TAGS.get(rec["status"], ""),),
                              values=(rec["status"], rec["agent"],
                                      rec.get("prompt", "")[:90], changes,
                                      rec.get("started_at") or rec.get("created_at", ""),
                                      rec.get("branch", "")))
        if selected and selected[0] in self._rows:
            self._tree.selection_set(selected[0])
        self._show_detail()

    def _selected(self) -> "dict | None":
        sel = self._tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _show_detail(self) -> None:
        rec = self._selected()
        status = rec["status"] if rec else ""
        can = {
            "new": self.enabled(),
            "cancel": status == ot.ST_RUNNING,
            "diff": bool(rec and rec.get("worktree_root") and not rec.get("worktree_removed")),
            "open": bool(rec and rec.get("worktree_root") and not rec.get("worktree_removed")),
            "accept": status == ot.ST_RESULT_READY,
            "reject": status == ot.ST_RESULT_READY,
            "discard": bool(rec and rec.get("worktree_root")
                            and not rec.get("worktree_removed")
                            and status not in ot.ACTIVE),
        }
        for key, btn in self._buttons.items():
            btn.state(["!disabled"] if can[key] else ["disabled"])
        lines = []
        if rec:
            usage = rec.get("usage")
            figures = (", ".join(f"{k}={v}" for k, v in usage["figures"].items())
                       if isinstance(usage, dict) else "unknown")
            lines = [f"{rec['task_id']}  {rec['status']}  ({rec['agent']}, {rec['execution_mode']})",
                     f"task:    {rec.get('prompt', '')}",
                     f"branch:  {rec.get('branch', '')}  from {rec.get('base_commit', '')[:10]}",
                     f"folder:  {rec.get('worktree_root', '')}"
                     + ("  (discarded)" if rec.get("worktree_removed") else ""),
                     f"usage:   {figures} (agent-reported; nothing is derived)",
                     f"session: {rec.get('session_id') or '(none recorded)'}"]
            if rec.get("error"):
                lines.append(f"error:   {rec['error']}")
            diff = rec.get("diff") or {}
            if diff.get("files"):
                lines.append("changed: " + ", ".join(diff["files"][:12]))
            if rec["status"] == ot.ST_LOST:
                lines.append("The process is gone and its end was never recorded. The "
                             "copy is kept: look at it before deciding anything.")
        self._detail.config(state=tk.NORMAL)
        self._detail.delete("1.0", tk.END)
        self._detail.insert(tk.END, "\n".join(lines))
        self._detail.config(state=tk.DISABLED)

    # ── actions ────────────────────────────────────────────────────────────────

    def new_task(self) -> None:
        project = self._get_project_path()
        top = self._frame.winfo_toplevel()
        # Never a silent no-op: a button that does nothing reads as a broken app.
        if not self.enabled():
            messagebox.showinfo("Agent tasks", "Delegated agent tasks are off. Turn "
                                "them on in Settings -> AI.", parent=top)
            return
        if not project:
            messagebox.showinfo("Agent tasks", "Select a project on the Projects tab "
                                "first, then come back here.", parent=top)
            self.refresh()
            return
        from dialogs.agent_task_dialog import AgentTaskDialog
        AgentTaskDialog(self._frame.winfo_toplevel(), self._cfg, project, self._start)

    def _start(self, agent_id: str, prompt: str, model: str) -> None:
        project = self._get_project_path()
        spec = agent_cli.AGENT_CLIS.get(agent_id)
        # The one launch path, so it is where the switch is enforced: a dialog
        # opened some other way cannot start an agent while the setting is off.
        if not self.enabled() or not project or spec is None:
            return
        exe = getattr(self._cfg, spec.config_key_exe, "")
        limits, _problems = limits_mod.from_config(self._cfg.raw.get("orchestration_limits"))
        cfg_raw, git_exe, ts_exe = self._cfg.raw, self._cfg.git_exe, self._cfg.tokensave_exe

        def work() -> None:
            try:
                orun.run_task(project, spec, exe, prompt, model=model, cfg_raw=cfg_raw,
                              git_exe=git_exe, tokensave_exe=ts_exe, limits=limits,
                              on_change=lambda _r: self._post(self.refresh))
            except Exception as exc:                            # noqa: BLE001
                msg = str(exc)
                self._post(messagebox.showerror, "Agent task",
                           f"The task could not run: {msg}")
            self._post(self.refresh)
        threading.Thread(target=work, daemon=True).start()
        self._frame.after(400, self.refresh)

    def _project(self) -> "str | None":
        return self._get_project_path()

    def cancel(self) -> None:
        rec, project = self._selected(), self._project()
        if not rec or not project:
            return
        ok, why = orun.request_cancel(project, rec["task_id"], self._cfg.git_exe)
        if not ok:
            messagebox.showinfo("Cancel", why, parent=self._frame.winfo_toplevel())
        self._frame.after(800, self.refresh)

    def _resolve(self, to: str) -> None:
        rec, project = self._selected(), self._project()
        if not rec or not project:
            return
        _got, why = ot.transition(project, rec["task_id"], to, self._cfg.git_exe)
        if why:
            messagebox.showinfo("Agent task", why, parent=self._frame.winfo_toplevel())
        self.refresh()

    def accept(self) -> None:
        rec = self._selected()
        if not rec:
            return
        self._resolve(ot.ST_ACCEPTED)
        messagebox.showinfo(
            "Accepted",
            f"Recorded as accepted. Nothing was merged.\n\nThe work is on branch\n"
            f"{rec.get('branch', '')}\nMerge it from the Git tab like any other branch.",
            parent=self._frame.winfo_toplevel())

    def reject(self) -> None:
        self._resolve(ot.ST_REJECTED)

    def open_worktree(self) -> None:
        rec = self._selected()
        if rec and rec.get("worktree_root") and os.path.isdir(rec["worktree_root"]):
            os.startfile(rec["worktree_root"])                      # noqa: S606

    def discard(self) -> None:
        rec, project = self._selected(), self._project()
        if not rec or not project:
            return
        top = self._frame.winfo_toplevel()
        if not messagebox.askyesno(
                "Discard copy",
                f"Delete the task's separate copy?\n\n{rec['worktree_root']}\n\n"
                f"The branch {rec.get('branch', '')} is kept, so its work is not lost.",
                parent=top):
            return
        git_exe, task_id, path = self._cfg.git_exe, rec["task_id"], rec["worktree_root"]

        def work() -> None:
            ok, why = wt.discard(project, path, git_exe)
            if ok:
                ot.update(project, task_id, git_exe, worktree_removed=True)
            else:
                self._post(messagebox.showerror, "Discard copy", why)
            self._post(self.refresh)
        threading.Thread(target=work, daemon=True).start()

    def show_diff(self) -> None:
        rec = self._selected()
        if not rec:
            return
        git_exe, path, base = self._cfg.git_exe or "git", rec["worktree_root"], rec["base_commit"]
        top = self._frame.winfo_toplevel()

        def work() -> None:
            try:
                out = subprocess.run([git_exe, "-C", path, "diff", base], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace",
                                     timeout=60, creationflags=CREATE_NO_WINDOW).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                out = f"could not run git diff: {exc}"
            measured = wt.measure(path, base, git_exe)
            if measured["untracked"]:
                out += "\n\nNew files (not yet in git):\n  " + "\n  ".join(
                    n for n in measured["files"] if n not in out)
            lines = out.splitlines()
            if len(lines) > _DIFF_LINES:
                lines = lines[:_DIFF_LINES] + [f"... {len(lines) - _DIFF_LINES} more line(s)"]
            self._post(self._diff_window, top, rec["task_id"], "\n".join(lines) or "(no changes)")
        threading.Thread(target=work, daemon=True).start()

    def _diff_window(self, top, task_id: str, text: str) -> None:
        win = tk.Toplevel(top)
        win.title(f"Changes — {task_id}")
        win.geometry("900x600")
        box = tk.Text(win, wrap=tk.NONE, bg=C["mantle"], fg=C["text"], font=("Consolas", 9))
        box.insert(tk.END, text)
        box.config(state=tk.DISABLED)
        box.pack(fill=tk.BOTH, expand=True)

    # ── quitting ───────────────────────────────────────────────────────────────

    @staticmethod
    def running_count() -> int:
        return orun.running_total()

    def stop_all(self) -> int:
        """Cancel every running task (see `orchestration_run.cancel_all`)."""
        return orun.cancel_all(self._cfg.git_exe)
