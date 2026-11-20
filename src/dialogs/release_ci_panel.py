"""release_ci_panel -- the Release Wizard's body for a project whose workflow builds and drafts its release.

Hosted by `ReleaseWizardDialog` when the project has a drafting `release.yml` AND a
`release-flow.json` (the project saying "my PR carries the version and changelog; the tag is the
only thing left"). It builds nothing and edits no file. Four parts, top to bottom:

  1. Pre-flight  -- what is true around this tip, each line ok / warn / fail / unknown.
  2. Tag commands -- the exact commands for the current tip, each with Copy and Run. Run asks you
                    to type the tag for anything that leaves this machine, and re-reads origin
                    first: if the tip moved, nothing runs.
  3. After the build -- "Check draft" judges the drafted release (assets, checksums, tag commit).
  4. Checklist   -- what a person must try on the downloaded installer, ticks kept per tag.

There is no Publish button, on purpose: publishing is pressed on GitHub, after the checklist.
All the logic is in `helpers/release_flow.py` and `helpers/release_postcheck.py`; this file draws
it and moves work onto threads (a worker never touches Tk; it posts).
"""

from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk

from constants import C
from helpers import release_flow as rfl
from helpers import release_postcheck as rpc
from helpers.release_profile import Profile
from theme import UiPumpMixin

_GLYPH = {rfl.OK: ("✔", "green"), rfl.INFO: ("ℹ", "sky"), rfl.WARN: ("▲", "peach"),
          rfl.FAIL: ("✖", "red"), rfl.UNKNOWN: ("?", "yellow")}

_VERDICT = {rfl.READY: ("Ready: every check passed.", "green"),
            rfl.UNCONFIRMED: ("Not confirmed: something above is a warning or could not be read. "
                              "Read it before tagging.", "peach"),
            rfl.BLOCKED: ("Blocked: fix the red items first. No commands are offered.", "red")}


class ReleaseCiPanel(UiPumpMixin, tk.Frame):
    """Pre-flight, tag commands, post-build check and checklist for one project."""

    def __init__(self, parent, path: str, cfg, profile: Profile, *, opener=webbrowser.open,
                 env: "rfl.Env | None" = None):
        super().__init__(parent, bg=C["base"])
        self._path = path
        self._cfg = cfg
        self._profile = profile
        self._open = opener
        self._env = env or rfl.Env.from_config(cfg)
        self._flow, self._config_problems = rfl.load_config(path)
        self._pre: "rfl.Preflight | None" = None
        self._plan: "rfl.Plan | None" = None
        self._steps_done: "set[int]" = set()
        self._busy = False
        self._release_url = ""
        self._tick_vars: "dict[str, tk.BooleanVar]" = {}
        self._build()
        self._start_ui_pump()
        self.refresh()

    # ── layout ───────────────────────────────────────────────────────────

    def _heading(self, num: str, text: str) -> tk.Frame:
        row = tk.Frame(self, bg=C["base"])
        row.pack(fill=tk.X, padx=20, pady=(14, 4))
        tk.Label(row, text=num, font=("Segoe UI", 10, "bold"), bg=C["surface0"], fg=C["blue"],
                 width=3).pack(side=tk.LEFT)
        tk.Label(row, text=text, font=("Segoe UI", 10, "bold"), bg=C["base"], fg=C["text"]).pack(side=tk.LEFT, padx=8)
        return row

    def _build(self) -> None:
        tk.Label(self, text=self._profile.label(), font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 wraplength=620, justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 2))
        why = "; ".join(f"{e.fact}: {e.source}" for e in self._profile.evidence)
        if why:
            tk.Label(self, text="Detected from " + why, font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                     wraplength=620, justify=tk.LEFT).pack(anchor=tk.W, padx=20)
        for problem in self._config_problems:
            tk.Label(self, text="▲ " + problem, font=("Segoe UI", 8), bg=C["base"], fg=C["peach"],
                     wraplength=620, justify=tk.LEFT).pack(anchor=tk.W, padx=20)

        head = self._heading("1", "Pre-flight")
        self._refresh_btn = ttk.Button(head, text="↻ Refresh", command=self.refresh)
        self._refresh_btn.pack(side=tk.RIGHT)
        self._pre_frame = tk.Frame(self, bg=C["base"])
        self._pre_frame.pack(fill=tk.X, padx=(44, 20))
        self._verdict = tk.Label(self, text="Reading…", font=("Segoe UI", 9, "bold"), bg=C["base"],
                                 fg=C["overlay0"], wraplength=620, justify=tk.LEFT)
        self._verdict.pack(anchor=tk.W, padx=(44, 20), pady=(4, 0))

        head = self._heading("2", "Tag commands")
        self._copy_all = ttk.Button(head, text="Copy all", command=self._copy_all_commands, state=tk.DISABLED)
        self._copy_all.pack(side=tk.RIGHT)
        self._plan_frame = tk.Frame(self, bg=C["base"])
        self._plan_frame.pack(fill=tk.X, padx=(44, 20))
        self._step_log = tk.Label(self, text="", font=("Consolas", 8), bg=C["base"], fg=C["overlay0"],
                                  wraplength=620, justify=tk.LEFT)
        self._step_log.pack(anchor=tk.W, padx=(44, 20))

        head = self._heading("3", "After the build")
        self._check_btn = ttk.Button(head, text="Check draft", command=self._check_draft)
        self._check_btn.pack(side=tk.RIGHT)
        self._open_btn = ttk.Button(head, text="Open on GitHub", command=self._open_release, state=tk.DISABLED)
        self._open_btn.pack(side=tk.RIGHT, padx=6)
        self._post_frame = tk.Frame(self, bg=C["base"])
        self._post_frame.pack(fill=tk.X, padx=(44, 20))
        tk.Label(self._post_frame, text="Push the tag, wait for the workflow, then press Check draft.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W)

        self._heading("4", "Try the installer by hand")
        self._list_frame = tk.Frame(self, bg=C["base"])
        self._list_frame.pack(fill=tk.X, padx=(44, 20))
        self._tick_label = tk.Label(self, text="", font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
                                    wraplength=620, justify=tk.LEFT)
        self._tick_label.pack(anchor=tk.W, padx=(44, 20), pady=(2, 16))

    @staticmethod
    def _clear(frame: tk.Frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _rows(self, frame: tk.Frame, checks: "list[rfl.Check]") -> None:
        self._clear(frame)
        for check in checks:
            glyph, colour = _GLYPH[check.status]
            row = tk.Frame(frame, bg=C["base"])
            row.pack(fill=tk.X, anchor=tk.W)
            tk.Label(row, text=glyph, width=2, bg=C["base"], fg=C[colour], font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
            text = f"{check.label}: {check.detail}" + (f"  ({check.fix})" if check.fix and check.status != rfl.OK else "")
            tk.Label(row, text=text, bg=C["base"], fg=C["text"], font=("Segoe UI", 9), wraplength=580,
                     justify=tk.LEFT, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X)

    # ── pre-flight ───────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._refresh_btn.configure(state=tk.DISABLED)
        self._verdict.configure(text="Reading git and GitHub…", fg=C["overlay0"])
        threading.Thread(target=self._refresh_worker, daemon=True, name="release-preflight").start()

    def _refresh_worker(self) -> None:
        pre = rfl.gather(self._env, self._path, self._flow)
        plan = rfl.make_plan(pre, self._flow)
        self._post(lambda: self._show_preflight(pre, plan))

    def _show_preflight(self, pre: "rfl.Preflight", plan: "rfl.Plan") -> None:
        if not self.winfo_exists():
            return
        self._busy = False
        self._refresh_btn.configure(state=tk.NORMAL)
        self._pre, self._plan, self._steps_done = pre, plan, set()
        self._rows(self._pre_frame, pre.checks)
        text, colour = _VERDICT[pre.verdict]
        self._verdict.configure(text=text, fg=C[colour])
        self._release_url = pre.state.release_url
        self._show_plan()
        self._show_checklist()

    # ── tag commands ─────────────────────────────────────────────────────

    def _show_plan(self) -> None:
        self._clear(self._plan_frame)
        plan = self._plan
        self._copy_all.configure(state=tk.NORMAL if plan and plan.steps else tk.DISABLED)
        if not plan or not plan.steps:
            tk.Label(self._plan_frame, text=plan.message if plan else "", bg=C["base"], fg=C["peach"],
                     font=("Segoe UI", 9), wraplength=600, justify=tk.LEFT).pack(anchor=tk.W)
            return
        tk.Label(self._plan_frame, text=f"For {plan.tag} on origin/{plan.branch} = {plan.tip[:10]}. "
                 "Run these in order, here or in your own terminal. Nothing is published.",
                 bg=C["base"], fg=C["subtext"], font=("Segoe UI", 9), wraplength=600, justify=tk.LEFT).pack(anchor=tk.W)
        self._run_buttons: "list[ttk.Button]" = []
        for index, step in enumerate(plan.steps):
            box = tk.Frame(self._plan_frame, bg=C["base"])
            box.pack(fill=tk.X, pady=(6, 0))
            tk.Label(box, text=f"{index + 1}. {step.title}", bg=C["base"], fg=C["text"],
                     font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
            line = tk.Frame(box, bg=C["base"])
            line.pack(fill=tk.X)
            entry = tk.Entry(line, font=("Consolas", 9), bg=C["surface0"], fg=C["text"], relief=tk.FLAT,
                             readonlybackground=C["surface0"])
            entry.insert(0, step.display)
            entry.configure(state="readonly")
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
            ttk.Button(line, text="Copy", width=6, command=lambda s=step: self._copy(s.display)).pack(side=tk.LEFT, padx=(6, 0))
            run = ttk.Button(line, text="Run…", width=6, command=lambda i=index: self._run(i))
            run.pack(side=tk.LEFT, padx=(4, 0))
            self._run_buttons.append(run)
            if step.note:
                tk.Label(box, text=step.note, bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8),
                         wraplength=600, justify=tk.LEFT).pack(anchor=tk.W)
        self._sync_run_buttons()

    def _sync_run_buttons(self) -> None:
        """Only the next undone step can run, so the order cannot be skipped."""
        if not self._plan:
            return
        nxt = next((i for i in range(len(self._plan.steps)) if i not in self._steps_done), None)
        for i, button in enumerate(getattr(self, "_run_buttons", [])):
            if not button.winfo_exists():
                continue
            done = i in self._steps_done
            button.configure(text="Done ✔" if done else "Run…",
                             state=tk.NORMAL if (i == nxt and not self._busy) else tk.DISABLED)

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self._step_log.configure(text="Copied to the clipboard.", fg=C["overlay0"])

    def _copy_all_commands(self) -> None:
        if self._plan and self._plan.steps:
            self._copy("\n".join(s.display for s in self._plan.steps))

    def _confirm(self, step: "rfl.Step") -> str:
        """The typed confirmation, '' if declined. Shows the exact command either way."""
        plan = self._plan
        if step.kind in rfl.CONFIRM_TYPED:
            typed = simpledialog.askstring(
                "Confirm", f"This runs:\n\n    {step.display}\n\n{step.note}\n\nType {plan.tag} to confirm:",
                parent=self.winfo_toplevel())
            return typed or ""
        ok = messagebox.askyesno("Run this command?", f"{step.display}\n\n{step.note}", parent=self.winfo_toplevel())
        return plan.tag if ok else ""

    def _run(self, index: int) -> None:
        if self._busy or not self._plan:
            return
        step = self._plan.steps[index]
        typed = self._confirm(step)
        if not typed:
            return
        self._busy = True
        self._sync_run_buttons()
        self._step_log.configure(text=f"Running: {step.display}", fg=C["overlay0"])
        plan = self._plan
        threading.Thread(target=self._run_worker, args=(plan, step, index, typed), daemon=True,
                         name="release-step").start()

    def _run_worker(self, plan: "rfl.Plan", step: "rfl.Step", index: int, typed: str) -> None:
        result = rfl.run_step(self._env, self._path, plan, step, typed)
        self._post(lambda: self._step_finished(index, step, result))

    def _step_finished(self, index: int, step: "rfl.Step", result: "rfl.StepResult") -> None:
        if not self.winfo_exists():
            return
        self._busy = False
        if result.refused:
            self._step_log.configure(text=f"Not run: {result.refused}", fg=C["peach"])
        elif result.ok:
            self._steps_done.add(index)
            self._step_log.configure(text=f"✔ {step.display}\n{result.text}", fg=C["green"])
        else:
            self._step_log.configure(text=f"✖ {step.display}\n{result.text}\nLater steps stay locked.", fg=C["red"])
        self._sync_run_buttons()

    # ── after the build ──────────────────────────────────────────────────

    def _check_draft(self) -> None:
        if self._busy or not self._pre or not self._plan or not self._plan.tip:
            return
        self._busy = True
        self._check_btn.configure(state=tk.DISABLED)
        threading.Thread(target=self._check_worker, args=(self._pre, self._plan.tip), daemon=True,
                         name="release-postcheck").start()

    def _check_worker(self, pre: "rfl.Preflight", tip: str) -> None:
        checks = rpc.verify_draft(self._env, self._path, self._flow, pre, tip)
        url = next((c.fix for c in checks if c.id == "draft" and c.fix), "")
        self._post(lambda: self._show_post(checks, url))

    def _show_post(self, checks: "list[rfl.Check]", url: str) -> None:
        if not self.winfo_exists():
            return
        self._busy = False
        self._check_btn.configure(state=tk.NORMAL)
        self._rows(self._post_frame, checks)
        if url:
            self._release_url = url
        self._open_btn.configure(state=tk.NORMAL if self._release_url else tk.DISABLED)
        self._sync_run_buttons()

    def _open_release(self) -> None:
        if self._release_url:
            self._open(self._release_url)

    # ── checklist ────────────────────────────────────────────────────────

    def _show_checklist(self) -> None:
        self._clear(self._list_frame)
        self._tick_vars = {}
        if not self._pre or not self._pre.state.tag:
            return
        tag = self._pre.state.tag
        items, source = rpc.load_checklist(self._path, self._flow)
        ticked = rpc.load_ticks(self._env, self._path, tag)
        for item in items:
            var = tk.BooleanVar(value=item in ticked)
            self._tick_vars[item] = var
            tk.Checkbutton(self._list_frame, text=item, variable=var, command=self._save_ticks, bg=C["base"],
                           fg=C["text"], selectcolor=C["surface0"], activebackground=C["base"],
                           activeforeground=C["text"], wraplength=580, justify=tk.LEFT, anchor=tk.W,
                           font=("Segoe UI", 9)).pack(anchor=tk.W, fill=tk.X)
        self._update_tick_label(source)

    def _save_ticks(self) -> None:
        tag = self._pre.state.tag if self._pre else ""
        ticked = {k for k, v in self._tick_vars.items() if v.get()}
        problem = rpc.save_ticks(self._env, self._path, tag, ticked) if tag else ""
        self._update_tick_label("", problem)

    def _update_tick_label(self, source: str, problem: str = "") -> None:
        done = sum(1 for v in self._tick_vars.values() if v.get())
        text = f"{done} of {len(self._tick_vars)} ticked." + (f" ({source})" if source else "")
        text += "  Publishing is pressed on GitHub, after this list."
        if problem:
            text += f"  ▲ Ticks were not saved: {problem}"
        self._tick_label.configure(text=text)
