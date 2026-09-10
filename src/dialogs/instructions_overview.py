"""InstructionsDialog — does each project's Claude instruction chain resolve?

## What the badge means, and what it does not

The badge is read from `reach`, never from `carriage`. Those are different
questions and the whole feature exists because they were being conflated: a
project can carry a perfectly correct baseline include inside a file nothing
links, and a summary computed from `carriage` puts a green tick on it. Every
row therefore shows BOTH, so the distinction is visible rather than implied.

The wording is "chain resolves", never "loaded". The Manager reads files; it
does not observe a Claude session. Saying more than that is the mistake the
Roadmap-11 MCP dialog made when it rendered a file-content verdict as effective
scope.

## Two bulk actions, deliberately not one

Repairing a wiring line and creating agent-instruction files across a fleet are
different sizes of act. "Wire all" does reachability only. "Generate agent
rules" is a separate button, off by default, that names every file it would
create. One button doing both would hide the larger half behind the smaller.

## The executor never trusts the preview

Confirmation is built from a dry-run plan, but by the time it is clicked the
disk may have moved on. Every project is re-read immediately before it is
written and again afterwards, and one that changed in between is reported as
skipped rather than written blind.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.instructions_posture import (
    ADVISORY_CONTRADICTS,
    ADVISORY_DOUBLE_LOAD,
    ADVISORY_DUPLICATE_CONTENT,
    ADVISORY_DUPLICATE_DIRECTIVE,
    ADVISORY_INDENTED_DIRECTIVE,
    ADVISORY_NONSTANDARD_CHAIN,
    ADVISORY_PLACEHOLDER,
    REACH_ABSENT,
    REACH_ORPHANED,
    REACH_RESOLVED,
    REACH_STALE,
    REACH_UNKNOWN,
    parse_baseline_target,
    read_posture,
    read_project,
)
from helpers.instructions_wiring import apply_wiring, plan_wiring
from helpers.doctor_rules import (
    _INSTRUCTIONS_REVIEW_BYTES,
    _INSTRUCTIONS_SPLIT_BYTES,
)
from helpers.project_discovery import load_basic_instructions_template
from theme import UiPumpMixin, _Tooltip, bind_mousewheel

if TYPE_CHECKING:
    from state import ManagerConfig


#: badge, colour, one-line meaning. Ordered worst-first by `_ORDER`.
_ROWS = {
    REACH_ABSENT:   ("✗", "red",     "no baseline include anywhere"),
    REACH_ORPHANED: ("✗", "red",     "carried, but nothing links it from CLAUDE.md"),
    REACH_STALE:    ("⚠", "peach",   "resolves to a different baseline"),
    REACH_UNKNOWN:  ("?", "yellow",  "could not determine"),
    REACH_RESOLVED: ("✓", "green",   "baseline chain resolves"),
}
_ORDER = {REACH_ABSENT: 0, REACH_ORPHANED: 1, REACH_STALE: 2,
          REACH_UNKNOWN: 3, REACH_RESOLVED: 4}

_ADVISORY_TEXT = {
    ADVISORY_DOUBLE_LOAD: "baseline reachable twice — it loads twice",
    ADVISORY_DUPLICATE_DIRECTIVE: "two BASIC_INSTRUCTIONS.md includes",
    ADVISORY_DUPLICATE_CONTENT: "restates baseline sections inline as well",
    ADVISORY_NONSTANDARD_CHAIN: "resolves via its own chain — left alone",
    ADVISORY_CONTRADICTS: "instructs Grep/Glob/subagent first",
    ADVISORY_PLACEHOLDER: "BASIC_INSTRUCTIONS.md is still the template",
    ADVISORY_INDENTED_DIRECTIVE: "an indented include we cannot judge",
}


class InstructionsDialog(UiPumpMixin, tk.Toplevel):
    """Fleet view of instruction-chain reachability, with the repair."""

    def __init__(self, parent, cfg: "ManagerConfig", on_log=None):
        super().__init__(parent)
        self.title("📄 Instructions")
        self.configure(bg=C["base"])
        self.geometry("940x720")
        self._cfg = cfg
        self._on_log = on_log or (lambda *a, **k: None)
        self._fleet = None
        self._busy = False

        tk.Label(self, text="Do the shared rules actually reach each project?",
                 font=("Segoe UI", 12, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=18,
                                                  pady=(14, 0))
        tk.Label(self,
                 text="Claude Code reads CLAUDE.md. BASIC_INSTRUCTIONS.md loads "
                      "only when CLAUDE.md includes it.",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=18, pady=(0, 2))
        # The honest limit of the claim, on screen rather than only in a
        # docstring: this dialog verifies files, not a running session.
        tk.Label(self,
                 text="The Manager verifies that the documented include chain "
                      "resolves from CLAUDE.md. It cannot observe a live "
                      "Claude session.",
                 font=("Segoe UI", 8, "italic"), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=18, pady=(0, 8))

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

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._wire_all_btn = ttk.Button(btn_row, text="Wire all…",
                                        style="Primary.TButton",
                                        command=self._wire_all)
        self._wire_all_btn.pack(side=tk.LEFT)
        _Tooltip(self._wire_all_btn,
                 "Reachability only. Shows every file it would touch first.")
        self._agents_btn = ttk.Button(btn_row, text="Generate agent rules…",
                                      command=self._generate_agent_rules)
        self._agents_btn.pack(side=tk.LEFT, padx=(8, 0))
        _Tooltip(self._agents_btn,
                 "Creates AGENTS.md and .cursor/rules/tokensave.mdc for agents "
                 "that cannot follow an @include. Separate because it creates "
                 "new files rather than repairing a line.")
        ttk.Button(btn_row, text="↻ Re-scan",
                   command=self._scan).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

        self._start_ui_pump()
        self._scan()

    # ── scanning ─────────────────────────────────────────────────────────

    def _scan(self) -> None:
        """Read every project off the Tk thread, then render on it."""
        self._clear()
        tk.Label(self._body, text="  Scanning…", font=("Segoe UI", 10),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=20)

        roots = list(self._cfg.raw.get("search_roots") or [])
        cfg = self._cfg

        def worker():
            try:
                fleet = read_posture(roots, cfg)
            except Exception as exc:            # noqa: BLE001 - shown to user
                message = str(exc)
                self._post(lambda: self._render_error(message))
                return
            self._post(lambda: self._render(fleet))

        threading.Thread(target=worker, daemon=True).start()

    def _clear(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()

    def _render_error(self, message: str) -> None:
        self._clear()
        tk.Label(self._body, text="  Could not scan: %s" % message,
                 font=("Segoe UI", 10), bg=C["base"], fg=C["red"],
                 wraplength=860, justify=tk.LEFT).pack(anchor=tk.W, pady=20)

    # ── rendering ────────────────────────────────────────────────────────

    def _render(self, fleet) -> None:
        self._fleet = fleet
        self._clear()

        if not fleet.baseline_ok:
            tk.Label(self._body,
                     text="  The configured baseline include line is not a "
                          "valid include directive, so nothing below could be "
                          "decided. Settings → Template directory.",
                     font=("Segoe UI", 10, "bold"), bg=C["base"], fg=C["red"],
                     wraplength=860, justify=tk.LEFT).pack(anchor=tk.W, pady=12)
            return

        self._render_distribution(fleet)
        self._render_rows(fleet)

    def _render_distribution(self, fleet) -> None:
        """Every state, never just the resolved count.

        A move from five to sixteen that quietly produced a new "could not
        determine" is exactly what a single number would hide.
        """
        counts = fleet.counts()
        head = tk.Frame(self._body, bg=C["base"])
        head.pack(fill=tk.X, padx=4, pady=(6, 2))
        tk.Label(head, text="%d of %d projects resolve the baseline"
                            % (counts[REACH_RESOLVED], fleet.total),
                 font=("Segoe UI", 11, "bold"), bg=C["base"],
                 fg=C["text"]).pack(side=tk.LEFT)

        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(0, 10))
        for state in (REACH_RESOLVED, REACH_ORPHANED, REACH_STALE,
                      REACH_ABSENT, REACH_UNKNOWN):
            badge, colour, _meaning = _ROWS[state]
            tk.Label(strip, text="%s %d %s" % (badge, counts[state], state),
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C[colour]).pack(side=tk.LEFT, padx=(0, 14))

    def _render_rows(self, fleet) -> None:
        rows = sorted(fleet.projects,
                      key=lambda p: (_ORDER.get(p.reach, 9), p.name.lower()))
        for project in rows:
            self._render_row(project)

    def _render_row(self, project) -> None:
        badge, colour, meaning = _ROWS.get(project.reach, _ROWS[REACH_UNKNOWN])
        row = tk.Frame(self._body, bg=C["base"])
        row.pack(fill=tk.X, padx=12, pady=1)

        tk.Label(row, text=badge, font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C[colour], width=2).pack(side=tk.LEFT)
        tk.Label(row, text=project.name, font=("Segoe UI", 9),
                 bg=C["base"], fg=C["text"], width=26,
                 anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(row, text=meaning, font=("Segoe UI", 9),
                 bg=C["base"], fg=C[colour], width=38,
                 anchor=tk.W).pack(side=tk.LEFT)

        if project.weight_bytes:
            weight = tk.Label(
                row,
                text="%s B · ~%s tok" % (f"{project.weight_bytes:,}",
                                         f"{project.estimated_tokens:,}"),
                font=("Segoe UI", 8), bg=C["base"],
                fg=self._weight_colour(project), anchor=tk.E)
            weight.pack(side=tk.LEFT)
            _Tooltip(weight, self._weight_note(project))

        if project.repairable:
            ttk.Button(row, text="Wire…",
                       command=lambda p=project: self._wire_one(p)).pack(
                side=tk.RIGHT)

        # Offered per project and never in a bulk action: moving a lesson log
        # is a large editorial change to a file a person wrote, and no
        # mechanical rule decides where the log starts.
        if project.weight_bytes > _INSTRUCTIONS_REVIEW_BYTES:
            split = ttk.Button(row, text="Split…",
                               command=lambda p=project: self._split_one(p))
            split.pack(side=tk.RIGHT, padx=(0, 6))
            _Tooltip(split,
                     "Move the append-only sections into a file that is NOT "
                     "@included, leaving an index of their titles. Shows the "
                     "complete transformation before writing anything.")

        # Both facts on screen. `carriage` is what the files declare; the badge
        # above is what the chain resolves to, and the gap between them is the
        # entire point of this dialog.
        sub = "carriage: %s" % project.carriage
        if project.detail:
            sub += "   ·   %s" % project.detail
        for advisory in project.advisories:
            sub += "   ·   %s" % _ADVISORY_TEXT.get(advisory, advisory)
        tk.Label(self._body, text="        " + sub, font=("Segoe UI", 8),
                 bg=C["base"], fg=C["overlay0"], anchor=tk.W, justify=tk.LEFT,
                 wraplength=860).pack(fill=tk.X, padx=12, pady=(0, 3))

    def _weight_colour(self, project) -> str:
        if project.weight_bytes > _INSTRUCTIONS_SPLIT_BYTES:
            return C["peach"]
        if project.weight_bytes > _INSTRUCTIONS_REVIEW_BYTES:
            return C["yellow"]
        return C["overlay0"]

    @staticmethod
    def _weight_note(project) -> str:
        """Wording that names a cost, never a fault.

        A project may intentionally carry a large always-loaded file; these
        thresholds are review heuristics, not correctness verdicts.
        """
        base = ("Bytes of instruction text that load on every message.\n"
                "The token figure is a byte/4 estimate, not tokenizer output.")
        if project.weight_bytes > _INSTRUCTIONS_SPLIT_BYTES:
            return base + ("\n\nLarge enough to strongly consider moving the "
                           "append-only sections into a file that is not "
                           "included.")
        if project.weight_bytes > _INSTRUCTIONS_REVIEW_BYTES:
            return base + "\n\nLarge enough to be worth a look."
        return base

    # ── writing ──────────────────────────────────────────────────────────

    def _template_text(self) -> str:
        template_file = getattr(self._cfg, "basic_instructions_template", "")
        if not template_file or not os.path.isfile(template_file):
            return ""
        return load_basic_instructions_template(
            template_file, self._cfg.baseline_include_line)

    def _has_template(self) -> bool:
        template_file = getattr(self._cfg, "basic_instructions_template", "")
        return bool(template_file) and os.path.isfile(template_file)

    def _apply_to(self, project) -> "tuple[str, tuple]":
        """Re-read, re-plan, write, re-read. Returns (outcome, changed_files).

        The plan handed to this method is deliberately NOT reused: between the
        preview and the click the disk may have moved on, and writing a stale
        plan is how a bulk action damages a project that had already been
        fixed by hand.
        """
        baseline = parse_baseline_target(self._cfg.baseline_include_line)
        fresh = read_project(project.display_root, project.name,
                             self._cfg.template_dir, baseline)
        if fresh.reach != project.reach:
            return "skipped — state changed since preview", ()

        plan = plan_wiring(fresh, has_template=self._has_template())
        if plan.blocked:
            return "skipped — %s" % plan.blocked, ()
        if plan.is_noop:
            return "already resolved", ()

        result = apply_wiring(project.display_root, plan,
                              self._cfg.baseline_include_line, project.name,
                              self._template_text(), baseline or "")
        if not result.ok:
            return "failed — %s" % (result.error or result.skipped), ()

        after = read_project(project.display_root, project.name,
                             self._cfg.template_dir, baseline)
        if after.reach == REACH_RESOLVED:
            return "wired", result.changed_files
        return "unknown — still %s after writing" % after.reach, \
            result.changed_files

    def _wire_one(self, project) -> None:
        plan = plan_wiring(project, has_template=self._has_template())
        listing = "\n".join("    %s — %s" % (step.path, step.label)
                            for step in plan.steps)
        if not messagebox.askyesno(
                "Wire %s" % project.name,
                "%s\n\nWould write:\n\n%s\n\nNothing else in these files is "
                "touched." % (project.display_root, listing or "  (nothing)"),
                parent=self):
            return
        outcome, changed = self._apply_to(project)
        self._on_log("Instructions: %s — %s%s"
                     % (project.name, outcome,
                        (" (%s)" % ", ".join(changed)) if changed else ""),
                     C["green"] if outcome == "wired" else C["peach"])
        self._scan()

    def _split_one(self, project) -> None:
        """The oversized-chain offer, for one project.

        Lazy dialog-to-dialog import, so it stays out of module load order.
        """
        from dialogs.instructions_split import SplitProposalDialog

        dialog = SplitProposalDialog(self, project.display_root, project.name,
                                     on_log=self._on_log,
                                     on_applied=self._scan)
        dialog.transient(self)

    def _wire_all(self) -> None:
        if self._busy or not self._fleet:
            return
        candidates = [p for p in self._fleet.projects if p.repairable]
        if not candidates:
            messagebox.showinfo("Wire all",
                                "Nothing to wire — every project either "
                                "resolves already or is in a state the "
                                "Manager will not guess at.", parent=self)
            return

        plans = [(p, plan_wiring(p, has_template=self._has_template()))
                 for p in candidates]
        lines = []
        for project, plan in plans:
            lines.append("  %s" % project.name)
            for step in plan.steps:
                lines.append("      %s: %s" % (step.action.split("_")[0],
                                               step.path))
        if not messagebox.askyesno(
                "Wire %d projects" % len(plans),
                "These files would be written:\n\n%s\n\nEach project is "
                "re-read immediately before it is written, so anything that "
                "changed since this preview is skipped rather than "
                "overwritten. Nothing is deleted and no existing prose is "
                "rewritten." % "\n".join(lines[:60]), parent=self):
            return

        self._run_bulk(candidates)

    def _run_bulk(self, projects) -> None:
        self._busy = True
        self._wire_all_btn.state(["disabled"])

        def worker():
            outcomes = []
            for project in projects:
                try:
                    outcome, changed = self._apply_to(project)
                except Exception as exc:        # noqa: BLE001 - per project
                    outcome, changed = "failed — %s" % exc, ()
                outcomes.append((project.name, outcome, changed))
            self._post(lambda: self._finish_bulk(outcomes))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_bulk(self, outcomes) -> None:
        """Per-project results. Mixed outcomes never collapse into "N/N"."""
        self._busy = False
        self._wire_all_btn.state(["!disabled"])
        wired = [o for o in outcomes if o[1] == "wired"]
        other = [o for o in outcomes if o[1] != "wired"]

        report = ["%d wired:" % len(wired)]
        for name, _outcome, changed in wired:
            report.append("    %s — %s" % (name, ", ".join(changed) or "-"))
        if other:
            report.append("")
            report.append("%d not wired:" % len(other))
            for name, outcome, _changed in other:
                report.append("    %s — %s" % (name, outcome))

        for name, outcome, changed in outcomes:
            self._on_log("Instructions: %s — %s%s"
                         % (name, outcome,
                            (" (%s)" % ", ".join(changed)) if changed else ""),
                         C["green"] if outcome == "wired" else C["peach"])
        messagebox.showinfo("Wiring complete", "\n".join(report), parent=self)
        self._scan()

    # ── agent rules (separate blast radius) ──────────────────────────────

    def _generate_agent_rules(self) -> None:
        """AGENTS.md and the Cursor rule, behind their own confirmation.

        Kept apart from "Wire all" because it CREATES files rather than
        repairing a line, across every project at once. Both writers manage
        only their own marked block, so a re-run never eats authored text.
        """
        from helpers.agent_rules import (agents_md_path, cursor_rule_path,
                                         read_baseline, write_agents_md,
                                         write_cursor_rule)
        if not self._fleet:
            return
        baseline_text = read_baseline(self._cfg.template_dir)
        if not baseline_text:
            messagebox.showerror(
                "Generate agent rules",
                "project-baseline.md could not be read, and writing an empty "
                "rules file would look configured while carrying no rules.",
                parent=self)
            return

        targets = list(self._fleet.projects)
        lines = []
        for project in targets:
            root = project.display_root
            verb_agents = ("refresh" if os.path.isfile(agents_md_path(root))
                           else "create")
            verb_cursor = ("refresh" if os.path.isfile(cursor_rule_path(root))
                           else "create")
            lines.append("  %s\n      %s AGENTS.md\n      %s "
                         ".cursor/rules/tokensave.mdc"
                         % (project.name, verb_agents, verb_cursor))
        if not messagebox.askyesno(
                "Generate agent rules for %d projects" % len(targets),
                "Cursor, Codex, Gemini CLI and opencode cannot follow the "
                "@include that CLAUDE.md uses, so they get the baseline's "
                "CONTENT inlined instead.\n\n%s\n\nEach file's managed block "
                "is replaced; anything you wrote outside it is preserved."
                % "\n".join(lines[:40]), parent=self):
            return

        created = 0
        failed = 0
        for project in targets:
            for writer in (write_agents_md, write_cursor_rule):
                args = ((project.display_root, baseline_text, project.name)
                        if writer is write_agents_md
                        else (project.display_root, baseline_text))
                ok, _err, changed = writer(*args)
                if not ok:
                    failed += 1
                elif changed:
                    created += 1
        self._on_log("Instructions: agent rules — %d written, %d failed"
                     % (created, failed),
                     C["green"] if not failed else C["peach"])
        messagebox.showinfo(
            "Agent rules",
            "%d file(s) written or refreshed.%s"
            % (created, ("\n%d could not be written." % failed) if failed
               else ""), parent=self)
