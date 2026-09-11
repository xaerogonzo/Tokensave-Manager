"""dialogs/relocate.py — one action for "this installation moved".

What it replaces is a four-surface scavenger hunt: fix `template_dir` in
Settings, then Instructions -> Wire all, then Settings -> MCP integration, then
Doctor. Every one of those worked; the problem was that nothing said to do them,
in that order, and two of the four were silent in the configuration the release
ships with.

**It writes nothing of its own.** Config values go through `cfg`, and every
project goes through `instructions_wiring.apply_to_project`, which is the one
implementation of re-read / re-plan / write / verify. A second writer here is
exactly the drift rule D1c exists to prevent.

**Order is load-bearing.** `baseline_include_line` is DERIVED from
`template_dir`, so the config update and `refresh_derived()` must land before a
single project is repointed. Repointing first would write the old baseline path
into every project, very convincingly.

**MCP wrapper paths are deliberately not here.** `App._check_config` already
classifies them at startup and opens `MCPConfigDialog`, which owns
`_apply_mcp_fix`. Duplicating that would be a second surface for one repair.
This dialog names it instead.

**Nothing is applied to the fleet automatically.** Two installations disagreeing
is a live condition, not a hypothetical, and a silent repoint would have them
rewriting the same repositories against each other on alternate launches.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from helpers.install_identity import RELOCATABLE_KEYS
from helpers.instructions_posture import parse_baseline_target
from helpers.instructions_wiring import apply_to_project
from helpers.project_discovery import load_basic_instructions_template
from theme import UiPumpMixin, _Tooltip, bind_mousewheel


class RelocateDialog(UiPumpMixin, tk.Toplevel):
    """Show the whole relocation, then ask once."""

    def __init__(self, parent, cfg, plan, on_log=None):
        super().__init__(parent)
        self.title("📦 This installation moved")
        self.configure(bg=C["base"])
        self.geometry("900x640")
        self._cfg = cfg
        self._plan = plan
        self._on_log = on_log or (lambda *a, **k: None)
        self._busy = False
        self._acknowledged = tk.BooleanVar(value=False)

        self._build_ui()
        self._start_ui_pump()
        self._render()

    # ── layout ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        tk.Label(self, text="Repoint this installation and the projects that "
                            "follow it",
                 font=("Segoe UI", 12, "bold"), bg=C["base"],
                 fg=C["blue"]).pack(anchor=tk.W, padx=18, pady=(14, 0))
        self._headline = tk.Label(self, text="", font=("Segoe UI", 9),
                                  bg=C["base"], fg=C["overlay0"], anchor=tk.W,
                                  justify=tk.LEFT, wraplength=840)
        self._headline.pack(fill=tk.X, padx=18, pady=(2, 8))

        wrap = tk.Frame(self, bg=C["base"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(2, 4))
        self._canvas = tk.Canvas(wrap, bg=C["base"], highlightthickness=0)
        bind_mousewheel(self._canvas)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._body = tk.Frame(self._canvas, bg=C["base"])
        self._body_win = self._canvas.create_window((0, 0), window=self._body,
                                                    anchor="nw")
        self._body.bind("<Configure>",
                        lambda e: self._canvas.configure(
                            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfigure(
                              self._body_win, width=e.width))
        for widget in (self._canvas, self._body):
            widget.bind("<MouseWheel>",
                        lambda e: self._canvas.yview_scroll(
                            int(-1 * (e.delta / 120)), "units"))

        self._warning = tk.Label(self, text="", font=("Segoe UI", 8),
                                 bg=C["base"], fg=C["peach"], anchor=tk.W,
                                 justify=tk.LEFT, wraplength=840)
        self._warning.pack(fill=tk.X, padx=18, pady=(0, 2))
        self._tick = tk.Checkbutton(
            self, text="", variable=self._acknowledged,
            command=self._sync_apply, bg=C["base"], fg=C["text"],
            selectcolor=C["base"], activebackground=C["base"],
            highlightthickness=0, bd=0, font=("Segoe UI", 8))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(4, 14))
        self._apply_btn = ttk.Button(btn_row, text="Apply…",
                                     style="Primary.TButton",
                                     command=self._apply, state=tk.DISABLED)
        self._apply_btn.pack(side=tk.LEFT)
        _Tooltip(self._apply_btn,
                 "Updates the config first, then repoints each project — the "
                 "baseline include line is derived from template_dir, so the "
                 "order is not cosmetic.")
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    # ── what it would do ─────────────────────────────────────────────────

    def _row(self, text, colour="text", indent=0, font=("Consolas", 8)):
        tk.Label(self._body, text=("    " * indent) + text, font=font,
                 bg=C["base"], fg=C[colour], anchor=tk.W, justify=tk.LEFT,
                 wraplength=800).pack(fill=tk.X, padx=10, pady=0)

    def _heading(self, text):
        tk.Label(self._body, text=text, font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["blue"], anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(10, 2))

    def _render(self) -> None:
        plan = self._plan
        self._headline.configure(
            text="%s\nNothing is written until you press Apply."
                 % self._describe(plan))

        if plan.config_updates:
            self._heading("manager-config.json")
            for key, old, new in plan.config_updates:
                self._row("%s" % key, "text", 0, ("Segoe UI", 9))
                self._row("from  %s" % old, "overlay0", 1)
                self._row("to    %s" % new, "green", 1)
            untouched = [k for k in RELOCATABLE_KEYS
                         if k not in {u[0] for u in plan.config_updates}]
            if untouched:
                self._row("unchanged: %s" % ", ".join(untouched), "overlay0", 1)
        self._row("Every other path in the config points at software installed "
                  "elsewhere and is left alone.", "overlay0", 0,
                  ("Segoe UI", 8, "italic"))

        if plan.projects:
            self._heading("%d project(s) — BASIC_INSTRUCTIONS.md baseline "
                          "include" % len(plan.projects))
            for name in plan.projects:
                self._row(name, "text", 1, ("Segoe UI", 9))
            self._row("Each is re-read and re-planned at the moment it is "
                      "written, so one that changed meanwhile is skipped "
                      "rather than overwritten.", "overlay0", 1,
                      ("Segoe UI", 8, "italic"))

        self._heading("Not handled here")
        self._row("MCP wrapper paths — Settings → MCP integration owns those, "
                  "and the Manager already checks them at startup.",
                  "overlay0", 1, ("Segoe UI", 8))

        self._render_baselines()
        self._sync_apply()

    @staticmethod
    def _describe(plan) -> str:
        identity = plan.identity
        if identity.moved:
            return ("This installation moved from %s to %s, and %d project(s) "
                    "still point at where it was."
                    % (identity.recorded_display, identity.current_display,
                       len(plan.projects)))
        return ("%d project(s) point at a baseline that is not this "
                "installation's." % len(plan.projects))

    def _render_baselines(self) -> None:
        """Both baselines, as evidence. No version comparison is invented."""
        plan = self._plan
        if not plan.downgrades:
            self._warning.configure(text="")
            return
        shipped, reached = plan.shipped_baseline, plan.reached_baseline
        self._heading("The two baselines differ")
        for label, facts in (("they point at now", reached),
                             ("this installation ships", shipped)):
            self._row("%-24s %s" % (label, facts.path), "text", 1)
            self._row("%-24s %s B · %s · %s" % ("", f"{facts.size:,}",
                                                _stamp(facts.mtime),
                                                facts.content_hash[:12]),
                      "overlay0", 1)
        self._warning.configure(
            text="These are different files. Repointing moves every project "
                 "onto the one this installation ships, which may be older. "
                 "Nothing here decides which is newer — compare them.")
        self._tick.configure(
            text="I have compared the two baselines and want to repoint anyway")
        self._tick.pack(fill=tk.X, padx=18, pady=(0, 4), before=self._warning)
        self._tick.pack_configure(after=self._warning)

    def _sync_apply(self) -> None:
        plan = self._plan
        allowed = plan.offers_bulk and (
            not plan.downgrades or self._acknowledged.get())
        self._apply_btn.configure(
            state=tk.NORMAL if allowed and not self._busy else tk.DISABLED)

    # ── writing ──────────────────────────────────────────────────────────

    def _apply(self) -> None:
        plan = self._plan
        if self._busy or not plan.offers_bulk:
            return
        if not messagebox.askyesno(
                "Apply relocation",
                "Update %d config value(s) and repoint %d project(s)?\n\n"
                "Both files in each project are tracked by git, so this is "
                "revertible."
                % (len(plan.config_updates), len(plan.projects)),
                parent=self):
            return
        self._busy = True
        self._sync_apply()

        # Config FIRST: `baseline_include_line` is derived from `template_dir`,
        # so repointing before this would write the old path into every project.
        for key, _old, new in plan.config_updates:
            self._cfg.raw[key] = new
        self._cfg.raw["install_dir"] = plan.identity.current_display
        self._cfg.save()
        self._cfg.refresh_derived()
        self._on_log("Relocate: config updated, install_dir recorded as %s"
                     % plan.identity.current_display, C["green"])

        cfg = self._cfg
        names = set(plan.projects)
        roots = list(cfg.raw.get("search_roots") or [])

        def worker():
            from helpers.instructions_posture import read_posture
            baseline = parse_baseline_target(cfg.baseline_include_line) or ""
            template_text = _template_text(cfg)
            has_template = bool(template_text)
            outcomes = []
            for project in read_posture(roots, cfg).projects:
                if project.name not in names:
                    continue
                try:
                    result = apply_to_project(
                        project, baseline, cfg.template_dir,
                        cfg.baseline_include_line, template_text, has_template)
                except Exception as exc:    # noqa: BLE001 — per project
                    from helpers.instructions_wiring import (
                        OUTCOME_FAILED, ApplyOutcome,
                    )
                    result = ApplyOutcome(OUTCOME_FAILED, str(exc))
                outcomes.append((project.name, result))
            self._post(lambda: self._finish(outcomes))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, outcomes) -> None:
        """Per-project results. Mixed outcomes never collapse into "N/N"."""
        self._busy = False
        wrote = [o for o in outcomes if o[1].wrote]
        rest = [o for o in outcomes if not o[1].wrote]
        for name, result in outcomes:
            self._on_log("Relocate: %s — %s" % (name, result.render()),
                         C["green"] if result.wrote else C["peach"])
        report = ["%d repointed." % len(wrote)]
        if rest:
            report.append("")
            report.append("%d not repointed:" % len(rest))
            for name, result in rest:
                report.append("    %s — %s" % (name, result.render()))
        messagebox.showinfo("Relocation complete", "\n".join(report),
                            parent=self)
        self.destroy()


def _template_text(cfg) -> str:
    import os
    template_file = getattr(cfg, "basic_instructions_template", "")
    if not template_file or not os.path.isfile(template_file):
        return ""
    return load_basic_instructions_template(template_file,
                                            cfg.baseline_include_line)


def _stamp(mtime: float) -> str:
    import datetime
    if not mtime:
        return "—"
    return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
