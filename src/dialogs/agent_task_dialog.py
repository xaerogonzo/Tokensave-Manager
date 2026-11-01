"""AgentTaskDialog — hand a bounded task to a coding agent, in its own copy.

The dialog's job is to make three things impossible to miss before anything
starts: WHERE the agent will work (a separate copy on its own branch, never the
person's checkout), WHAT it may do (edit files; no shell), and WHY a launch is
refused when it is (the limit, the uncommitted file, the missing agent). The
refusals come from `orchestration_run.check_launch` on a worker thread, because
it runs git and measures a disk.

Pure Tk plus callbacks: `on_start(agent_id, prompt, model)` is the only way out.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk

from constants import C
from helpers import agent_cli
from helpers import orchestration_limits as limits_mod
from helpers import orchestration_run as orun
from helpers import orchestration_worktree as wt
from theme import UiPumpMixin


def writable_agents() -> list:
    """Agent ids whose row has a measured way to run a task that edits files."""
    return [aid for aid, spec in agent_cli.AGENT_CLIS.items()
            if spec.write == agent_cli.WRITE_SUPPORTED]


class AgentTaskDialog(UiPumpMixin, tk.Toplevel):
    def __init__(self, parent, cfg, project: str, on_start) -> None:
        super().__init__(parent)
        self.title(f"Delegate a task — {os.path.basename(project)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self._cfg = cfg
        self._project = project
        self._on_start = on_start
        self._ready = None
        self._req = 0

        tk.Label(self, text="Delegate a task to an agent",
                 font=("Segoe UI", 13, "bold"), bg=C["base"], fg=C["lavender"]
                 ).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=project, font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        row = tk.Frame(self, bg=C["base"])
        row.pack(fill=tk.X, padx=20)
        tk.Label(row, text="Agent", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9), width=8, anchor=tk.W).pack(side=tk.LEFT)
        self._agent = tk.StringVar(value=(writable_agents() or [""])[0])
        combo = ttk.Combobox(row, textvariable=self._agent, values=writable_agents(),
                             state="readonly", width=14)
        combo.pack(side=tk.LEFT)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._recheck())
        tk.Label(row, text="  Model", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._model = tk.StringVar()
        ttk.Entry(row, textvariable=self._model, width=26).pack(side=tk.LEFT, padx=(4, 0))
        tk.Label(self, text="  Model left blank = the agent's own default.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]
                 ).pack(anchor=tk.W, padx=20)

        tk.Label(self, text="Task", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(8, 2))
        self._text = tk.Text(self, width=70, height=8, wrap=tk.WORD,
                             bg=C["mantle"], fg=C["text"], insertbackground=C["text"],
                             relief=tk.FLAT, font=("Segoe UI", 10))
        self._text.pack(padx=20)
        self._text.bind("<KeyRelease>", lambda _e: self._sync_start())

        self._what = tk.Label(self, justify=tk.LEFT, wraplength=520, anchor=tk.W,
                              font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"])
        self._what.pack(anchor=tk.W, padx=20, pady=(8, 2))
        self._refusal = tk.Label(self, justify=tk.LEFT, wraplength=520, anchor=tk.W,
                                 font=("Segoe UI", 9), bg=C["base"], fg=C["red"],
                                 text="Checking…")
        self._refusal.pack(anchor=tk.W, padx=20, pady=(0, 8))

        bar = tk.Frame(self, bg=C["base"])
        bar.pack(fill=tk.X, padx=20, pady=(0, 16))
        self._start = ttk.Button(bar, text="Start", command=self._go, state=tk.DISABLED)
        self._start.pack(side=tk.RIGHT)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 8))

        self._start_ui_pump()
        self._text.focus_set()
        self._recheck()

    # ── what is about to happen, and whether it can ────────────────────────────

    def _spec_exe(self):
        spec = agent_cli.AGENT_CLIS.get(self._agent.get())
        exe = getattr(self._cfg, spec.config_key_exe, "") if spec else ""
        return spec, exe

    def _limits(self):
        got, _problems = limits_mod.from_config(self._cfg.raw.get("orchestration_limits"))
        return got

    def _recheck(self) -> None:
        spec, exe = self._spec_exe()
        self._ready = None
        self._sync_start()
        if spec is None:
            self._refusal.config(text="No agent here can run a task that edits files.")
            return
        place = wt.worktree_path(self._project, "<task id>")
        self._what.config(text=(
            f"{spec.label} will work in a separate copy of this project at\n{place}\n"
            f"on its own branch, with permission to edit files (no shell). Your own "
            f"checkout is not touched and nothing is merged or pushed: you review "
            f"the result and decide."))
        self._refusal.config(text="Checking…", fg=C["overlay0"])
        self._req += 1
        req, git_exe, limits = self._req, self._cfg.git_exe, self._limits()
        project = self._project

        def work() -> None:
            ready = orun.check_launch(project, spec, exe, git_exe=git_exe, limits=limits)
            self._post(self._apply_check, req, ready)
        threading.Thread(target=work, daemon=True).start()

    def _apply_check(self, req: int, ready) -> None:
        if req != self._req or not self.winfo_exists():
            return
        if not getattr(self._cfg, "enable_agent_orchestration", False):
            ready = orun.Readiness(False, refusals=(
                "delegated agent tasks are off (Settings -> AI)",) + tuple(ready.refusals))
        self._ready = ready
        if ready.ok:
            note = (f"{ready.untracked} untracked file(s) are not in the task's copy."
                    if ready.untracked else "Ready.")
            self._refusal.config(text=note, fg=C["overlay0"])
        else:
            self._refusal.config(text="Cannot start:\n- " + "\n- ".join(ready.refusals),
                                 fg=C["red"])
        self._sync_start()

    def _sync_start(self) -> None:
        text = self._text.get("1.0", tk.END).strip()
        ok = bool(text) and self._ready is not None and self._ready.ok
        self._start.config(state=tk.NORMAL if ok else tk.DISABLED)

    def _go(self) -> None:
        prompt = self._text.get("1.0", tk.END).strip()
        if not prompt or self._ready is None or not self._ready.ok:
            return
        agent, model = self._agent.get(), self._model.get().strip()
        self.destroy()
        self._on_start(agent, prompt, model)
