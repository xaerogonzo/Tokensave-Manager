"""RefactorScoutDialog — show deterministic code-health findings.

Modal Toplevel that renders the result of `helpers.refactor_scout.run_scout`.
Each finding card has:
  - kind badge (colour-coded by severity)
  - file:line + symbol header
  - one-line metric ("CC=22", "methods=47")
  - evidence snippet (verbatim from source, capped at EVIDENCE_MAX_LINES)
  - Investigate button → pre-seeds Ask tab with structured context
  - Ignore button → persists finding ID to cfg.raw["refactor_scout_ignored"]

The scout itself is deterministic and grounded in BASIC_INSTRUCTIONS.md
thresholds — no LLM call happens to PRODUCE the findings (so they can't
hallucinate). The LLM only enters via Investigate, where it explains a
specific finding using the structured context the scout already built.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from constants import C
from helpers.refactor_scout import EVIDENCE_MAX_LINES, Finding, kind_label


_KIND_COLOUR = {
    "complexity": "peach",
    "god_class":  "red",
    "god_file":   "red",
    "dead_code":  "yellow",
}


def _looks_like_path(part: str) -> bool:
    """True when a `::`-separated qualified-name segment looks like a file path."""
    return "/" in part or "\\" in part or "." in part


def _clean_symbol(sym: str) -> str:
    """Strip tokensave's duplicated file-path prefix from qualified names.

    tokensave emits qualified_names like `src/foo.py::src/foo.py::Class::method`
    or `build.ps1::build.ps1::Some-Function` — the first segment is redundant.
    Drop leading path-shaped parts until at most one remains, then drop that
    one too if the next part is still a symbol (non-path). Falls back to the
    original string if the heuristic empties everything out."""
    if "::" not in sym:
        return sym
    parts = [p for p in sym.split("::") if p]
    # Drop duplicate leading path segments
    while len(parts) >= 2 and _looks_like_path(parts[0]) and _looks_like_path(parts[1]):
        parts.pop(0)
    # If a single path part remains and there's a real symbol after it, drop it
    if len(parts) >= 2 and _looks_like_path(parts[0]) and not _looks_like_path(parts[1]):
        parts = parts[1:]
    return "::".join(parts) if parts else sym


class RefactorScoutDialog(tk.Toplevel):
    """Modal findings panel. Construct on the Tk main thread."""

    def __init__(
        self,
        parent: tk.Misc,
        project_path: str,
        findings: dict[str, list[Finding]],
        suppressed_count: int,
        on_investigate: Callable[[Finding], None] | None,
        on_save_ignored: Callable[[set[str]], None],
        currently_ignored: set[str],
        on_investigate_cli: Callable[[Finding], None] | None = None,
        on_export_all_cli: Callable[[], None] | None = None,
        on_batch_clipboard: Callable[[list[Finding]], None] | None = None,
        on_batch_cli: Callable[[list[Finding]], None] | None = None,
        on_batch_ask: Callable[[list[Finding]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("🔬 Refactor scout")
        self.configure(bg=C["base"])
        self.geometry("960x780")
        self.transient(parent)

        self._project_path = project_path
        self._findings = findings
        self._on_investigate = on_investigate
        self._on_investigate_cli = on_investigate_cli
        self._on_export_all_cli = on_export_all_cli
        self._on_batch_clipboard = on_batch_clipboard
        self._on_batch_cli = on_batch_cli
        self._on_batch_ask = on_batch_ask
        self._on_save_ignored = on_save_ignored
        # Local mutable copy — we push the updated set back on close (or
        # on every Ignore click for crash safety).
        self._ignored: set[str] = set(currently_ignored)
        # finding-id → card frame (so Ignore can remove the card live)
        self._cards: dict[str, tk.Widget] = {}
        # finding-id → BooleanVar (the per-card selection checkbox)
        self._selected: dict[str, tk.BooleanVar] = {}
        # finding-id → Finding (for fast lookup when batching)
        self._all_findings: dict[str, Finding] = {
            f.id: f for items in findings.values() for f in items
        }
        # Running counts for the header label.
        self._suppressed_count = suppressed_count

        self._build_header(project_path)
        self._build_selection_toolbar()
        self._build_body()
        self._build_footer()

        # Centre over parent
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + parent.winfo_width() // 2
            py = parent.winfo_rooty() + parent.winfo_height() // 2
            self.geometry(f"+{px - self.winfo_width() // 2}"
                          f"+{py - self.winfo_height() // 2}")
        except tk.TclError:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.grab_set()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_header(self, project_path: str) -> None:
        head = tk.Frame(self, bg=C["mantle"])
        head.pack(fill=tk.X, padx=0, pady=0)
        tk.Label(head, text="🔬  Refactor scout",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["mantle"], fg=C["text"]).pack(anchor=tk.W, padx=14, pady=(10, 0))
        tk.Label(head, text=project_path,
                 font=("Consolas", 9),
                 bg=C["mantle"], fg=C["subtext"]).pack(anchor=tk.W, padx=14, pady=(0, 4))

        total = sum(len(v) for v in self._findings.values())
        self._count_lbl = tk.Label(
            head,
            text=self._count_text(total),
            font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["overlay0"])
        self._count_lbl.pack(anchor=tk.W, padx=14, pady=(0, 10))

    def _count_text(self, total: int) -> str:
        return (f"{total} finding{'s' if total != 1 else ''} "
                f"({self._suppressed_count} suppressed). "
                f"Suppressions persist until a symbol is renamed or moved.")

    def _build_selection_toolbar(self) -> None:
        """Selection-aware toolbar: select-all/clear + per-kind toggles + counter.

        Lives between the header strip and the scrollable body so it's
        always visible regardless of scroll position — the user needs to
        see "5 selected" while scrolling through 31 cards.
        """
        bar = tk.Frame(self, bg=C["surface0"])
        bar.pack(fill=tk.X, padx=0, pady=0)

        tk.Label(bar, text="Select:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["surface0"], fg=C["subtext"]).pack(
            side=tk.LEFT, padx=(14, 6), pady=6)

        ttk.Button(bar, text="All",
                   command=self._select_all).pack(side=tk.LEFT, padx=2, pady=4)
        ttk.Button(bar, text="None",
                   command=self._select_none).pack(side=tk.LEFT, padx=2, pady=4)

        # Per-kind select buttons (only render kinds that have findings)
        for kind, items in self._findings.items():
            if not items:
                continue
            short = {
                "complexity": "Complexity",
                "god_class":  "God classes",
                "god_file":   "Big files",
                "dead_code":  "Dead code",
            }.get(kind, kind)
            ttk.Button(bar, text=short,
                       command=lambda k=kind: self._select_kind(k)).pack(
                side=tk.LEFT, padx=2, pady=4)

        self._selection_lbl = tk.Label(
            bar, text="0 selected",
            font=("Segoe UI", 9),
            bg=C["surface0"], fg=C["overlay0"])
        self._selection_lbl.pack(side=tk.RIGHT, padx=14, pady=6)

    def _build_body(self) -> None:
        # Scrollable canvas wrapper — findings can be long.
        outer = tk.Frame(self, bg=C["base"])
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, bg=C["base"], highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = tk.Frame(canvas, bg=C["base"])
        canvas_window = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        body.bind("<Configure>", _on_body_configure)

        def _on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling (Windows convention). bind_all is global, so
        # we unbind on dialog close — otherwise the closure keeps the dead
        # canvas reference alive and breaks wheel scrolling in other widgets.
        def _on_wheel(e):
            try:
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except tk.TclError:
                pass  # canvas was destroyed under us
        canvas.bind_all("<MouseWheel>", _on_wheel)
        self._wheel_canvas = canvas  # _on_close uses this to unbind cleanly

        # One section per kind, only if it has findings
        any_rendered = False
        for kind, items in self._findings.items():
            if not items:
                continue
            any_rendered = True
            self._build_kind_section(body, kind, items)

        if not any_rendered:
            tk.Label(body,
                     text="No findings — code health looks clean.",
                     font=("Segoe UI", 10, "italic"),
                     bg=C["base"], fg=C["green"],
                     pady=40).pack()

    def _build_kind_section(self, parent, kind: str,
                             items: list[Finding]) -> None:
        sec = tk.LabelFrame(
            parent, text=f"  {kind_label(kind)}  ({len(items)})  ",
            bg=C["base"], fg=C["text"],
            font=("Segoe UI", 10, "bold"),
            bd=1, relief=tk.GROOVE)
        sec.pack(fill=tk.X, padx=8, pady=(8, 4))

        for f in items:
            self._build_finding_card(sec, f)

    def _build_finding_card(self, parent, f: Finding) -> None:
        card = tk.Frame(parent, bg=C["mantle"], bd=1, relief=tk.FLAT)
        card.pack(fill=tk.X, padx=6, pady=4)
        self._cards[f.id] = card

        # Top row: checkbox + badge + file:line
        head = tk.Frame(card, bg=C["mantle"])
        head.pack(fill=tk.X, padx=8, pady=(6, 2))

        # Per-card selection checkbox (drives the batch actions)
        sel_var = tk.BooleanVar(value=False)
        self._selected[f.id] = sel_var
        tk.Checkbutton(
            head, variable=sel_var,
            bg=C["mantle"], fg=C["text"],
            activebackground=C["mantle"], selectcolor=C["surface1"],
            bd=0, highlightthickness=0,
            command=self._refresh_selection_label,
        ).pack(side=tk.LEFT, padx=(0, 4))

        badge_colour = C.get(_KIND_COLOUR.get(f.kind, "subtext"), C["subtext"])
        tk.Label(head, text=f.metric or f.kind,
                 font=("Consolas", 9, "bold"),
                 bg=C["mantle"], fg=badge_colour).pack(side=tk.LEFT)
        tk.Label(head, text=f"  {f.file}:{f.line}",
                 font=("Consolas", 9),
                 bg=C["mantle"], fg=C["subtext"]).pack(side=tk.LEFT)

        # Symbol + message
        tk.Label(card, text=_clean_symbol(f.symbol),
                 font=("Segoe UI", 10, "bold"),
                 bg=C["mantle"], fg=C["text"],
                 anchor=tk.W).pack(fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(card, text=f.message,
                 font=("Segoe UI", 9),
                 bg=C["mantle"], fg=C["overlay0"],
                 anchor=tk.W).pack(fill=tk.X, padx=8, pady=(0, 4))

        # Evidence snippet
        if f.evidence:
            ev_box = tk.Text(card, height=min(EVIDENCE_MAX_LINES,
                                                f.evidence.count("\n") + 1),
                             font=("Consolas", 9),
                             bg=C["base"], fg=C["text"],
                             relief=tk.FLAT, padx=8, pady=4, wrap=tk.NONE)
            ev_box.insert("1.0", f.evidence)
            ev_box.configure(state=tk.DISABLED)
            ev_box.pack(fill=tk.X, padx=8, pady=(0, 4))

        # Actions
        actions = tk.Frame(card, bg=C["mantle"])
        actions.pack(fill=tk.X, padx=8, pady=(2, 6))
        if self._on_investigate is not None:
            ttk.Button(actions, text="🔍  Investigate in Ask tab",
                       command=lambda fid=f: self._investigate(fid)).pack(
                side=tk.LEFT)
        if self._on_investigate_cli is not None:
            ttk.Button(actions, text="🚀  Investigate in Claude Code",
                       command=lambda fid=f: self._investigate_cli(fid)).pack(
                side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="🚫  Ignore (don't show again)",
                   command=lambda fid=f.id: self._ignore(fid)).pack(
            side=tk.LEFT, padx=(8, 0))

    def _build_footer(self) -> None:
        # Two-row footer: batch-action row on top, utility row on bottom.
        # Splitting them keeps the batch buttons (the headline feature)
        # visually grouped instead of competing with Close / Clear-all.
        outer = tk.Frame(self, bg=C["mantle"])
        outer.pack(fill=tk.X, side=tk.BOTTOM)

        batch = tk.Frame(outer, bg=C["mantle"])
        batch.pack(fill=tk.X, padx=12, pady=(8, 2))
        tk.Label(batch, text="Selected →",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["mantle"], fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 8))
        if self._on_batch_clipboard is not None:
            ttk.Button(batch, text="📋  Copy to clipboard",
                       command=self._batch_clipboard).pack(side=tk.LEFT, padx=2)
        if self._on_batch_cli is not None:
            ttk.Button(batch, text="🚀  Open in Claude Code",
                       command=self._batch_cli).pack(side=tk.LEFT, padx=2)
        if self._on_batch_ask is not None:
            ttk.Button(batch, text="🤖  Send to Ask tab",
                       command=self._batch_ask).pack(side=tk.LEFT, padx=2)

        util = tk.Frame(outer, bg=C["mantle"])
        util.pack(fill=tk.X, padx=12, pady=(2, 8))
        ttk.Button(util, text="Close",
                   command=self._on_close).pack(side=tk.RIGHT)
        if self._on_export_all_cli is not None:
            ttk.Button(util, text="📤  Export ALL findings to Claude Code",
                       command=self._export_all_cli).pack(
                side=tk.RIGHT, padx=(0, 8))
        ttk.Button(util, text="↺  Clear all suppressions",
                   command=self._clear_suppressions).pack(side=tk.LEFT)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _investigate(self, finding: Finding) -> None:
        if self._on_investigate is None:
            return
        try:
            self._on_investigate(finding)
        except Exception:
            from helpers.runtime import log
            log.exception("on_investigate callback raised")
            return
        # The Ask tab is now seeded. Close the scout so the user can see it.
        self._on_close()

    def _investigate_cli(self, finding: Finding) -> None:
        if self._on_investigate_cli is None:
            return
        try:
            self._on_investigate_cli(finding)
        except Exception:
            from helpers.runtime import log
            log.exception("on_investigate_cli callback raised")
            return
        # CC opens in its own terminal — leave the scout open so the user
        # can investigate multiple findings without re-running the scout.

    def _export_all_cli(self) -> None:
        if self._on_export_all_cli is None:
            return
        try:
            self._on_export_all_cli()
        except Exception:
            from helpers.runtime import log
            log.exception("on_export_all_cli callback raised")

    # ── Selection helpers ──────────────────────────────────────────────────

    def _selected_findings(self) -> list[Finding]:
        """Return Findings whose checkbox is currently ticked, in display order."""
        out: list[Finding] = []
        for items in self._findings.values():
            for f in items:
                var = self._selected.get(f.id)
                if var is not None and var.get():
                    out.append(f)
        return out

    def _refresh_selection_label(self) -> None:
        n = sum(1 for v in self._selected.values() if v.get())
        self._selection_lbl.configure(
            text=f"{n} selected" if n else "0 selected",
            fg=C["green"] if n else C["overlay0"])

    def _select_all(self) -> None:
        for var in self._selected.values():
            var.set(True)
        self._refresh_selection_label()

    def _select_none(self) -> None:
        for var in self._selected.values():
            var.set(False)
        self._refresh_selection_label()

    def _select_kind(self, kind: str) -> None:
        """Select every (non-suppressed) finding of one kind. Does NOT deselect
        other kinds — lets the user build a heterogeneous batch ("all god
        classes + the top-3 complexity hotspots") with successive clicks."""
        for f in self._findings.get(kind, []):
            var = self._selected.get(f.id)
            if var is not None:
                var.set(True)
        self._refresh_selection_label()

    # ── Batch actions ──────────────────────────────────────────────────────

    def _require_selection(self) -> list[Finding] | None:
        """Common preamble for batch actions: ensure at least one selected."""
        items = self._selected_findings()
        if not items:
            from tkinter import messagebox
            messagebox.showinfo(
                "Nothing selected",
                "Tick at least one finding's checkbox (or use the Select "
                "buttons in the toolbar) before running a batch action.",
                parent=self,
            )
            return None
        return items

    def _batch_clipboard(self) -> None:
        items = self._require_selection()
        if items is None or self._on_batch_clipboard is None:
            return
        try:
            self._on_batch_clipboard(items)
        except Exception:
            from helpers.runtime import log
            log.exception("on_batch_clipboard callback raised")

    def _batch_cli(self) -> None:
        items = self._require_selection()
        if items is None or self._on_batch_cli is None:
            return
        try:
            self._on_batch_cli(items)
        except Exception:
            from helpers.runtime import log
            log.exception("on_batch_cli callback raised")

    def _batch_ask(self) -> None:
        items = self._require_selection()
        if items is None or self._on_batch_ask is None:
            return
        # Large batches into a local LLM can exceed context — warn at 8+.
        if len(items) >= 8:
            from tkinter import messagebox
            if not messagebox.askyesno(
                "Large batch",
                f"You're sending {len(items)} findings to the Ask tab. "
                f"Local models (Ollama, LM Studio) may exceed their context "
                f"window with this much. Continue?",
                parent=self,
            ):
                return
        try:
            self._on_batch_ask(items)
        except Exception:
            from helpers.runtime import log
            log.exception("on_batch_ask callback raised")
        # Ask tab auto-sends — close the scout so the chat is visible.
        self._on_close()

    def _ignore(self, finding_id: str) -> None:
        self._ignored.add(finding_id)
        # Persist immediately — crash safety, and the next scout run will
        # see the new suppression even if the user force-quits.
        try:
            self._on_save_ignored(self._ignored)
        except Exception:
            from helpers.runtime import log
            log.exception("on_save_ignored raised")
        # Remove the card from the UI + drop its selection var.
        card = self._cards.pop(finding_id, None)
        if card is not None:
            card.destroy()
        self._selected.pop(finding_id, None)
        self._refresh_selection_label()
        # Update header counter.
        self._suppressed_count += 1
        total = sum(len(v) for v in self._findings.values()) - sum(
            1 for kind_items in self._findings.values()
            for ff in kind_items if ff.id in self._ignored)
        self._count_lbl.configure(text=self._count_text(total))

    def _clear_suppressions(self) -> None:
        if not self._ignored:
            return
        self._ignored.clear()
        try:
            self._on_save_ignored(self._ignored)
        except Exception:
            from helpers.runtime import log
            log.exception("on_save_ignored raised")
        # Close + ask user to re-run for fresh results — re-rendering with
        # un-ignored items live would require re-running the scout, which
        # belongs in the controller, not the dialog.
        from tkinter import messagebox
        messagebox.showinfo(
            "Suppressions cleared",
            "All ignored findings cleared. Run the scout again to see them.",
            parent=self,
        )
        self._on_close()

    def _on_close(self) -> None:
        try:
            self._wheel_canvas.unbind_all("<MouseWheel>")
        except (AttributeError, tk.TclError):
            pass
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
