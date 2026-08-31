"""SavingsDialog — what tokensave saved you, and what your traffic list-prices at.

This replaces a panel that was confidently wrong. It scraped `tokensave cost`'s
human table for the **Cost** column — money *spent*, at API list price — and
rendered it in the "Value Recouped" card. On the reference machine that showed
**$4132.75** where the ledger which actually records savings said **$0.14**. The
"Saved Tokens" card was no better: a lifetime, all-projects counter displayed
under a subtitle reading "past 7 days".

Three separations carry the fix, and each one is load-bearing rather than
cosmetic:

**Savings and spend are different quantities.** Savings come from
`tokensave gain`; spend from `tokensave cost`. They are never adjacent without a
label saying which is which.

**They also have different scopes.** `gain` is per-project (with an explicit
all-projects toggle); `cost` is machine-global across every project and agent,
because tokensave offers no project filter for it. The Spend heading says so
inline — a subtitle is too easy to miss when the surrounding dialog was opened
from a project row.

**They have different ranges, and the ranges move independently.** The range
selector drives Savings only. `cost` writes to tokensave's global ledger, so
wiring it to a control a user flicks through would turn a display into a
repeated ingestion trigger. Spend holds a snapshot, states the range and age it
was captured at, and refreshes only when asked.

Two rules the old panel broke, applied everywhere here:

* **Unknown is never zero.** Every section renders one of five states —
  loading, loaded, stale, unavailable, error — and `0` is only ever a measured
  zero. `unavailable` carries the reason.
* **Nothing is derived that upstream did not report.** Cache reads are shown as
  "not reported" rather than computed, because the only available derivation
  yields exactly zero on every payload. The spend footnote says outright that
  the cost cannot be reconciled from the token counts beside it, so a user doing
  the arithmetic blames the right party.
"""

from __future__ import annotations

import datetime
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.savings import (
    RANGES,
    Gain,
    fetch_discover,
    fetch_gain,
    fetch_gain_history,
    fetch_spend,
)
from theme import UiPumpMixin, bind_mousewheel

if TYPE_CHECKING:
    from state import ManagerConfig


def _utc_day(epoch: int) -> str:
    """Format a `gain --history` day, in UTC, because that is what it is.

    Every observed value is midnight-aligned UTC. Rendering it in local time
    would move rows across day boundaries for anyone not on UTC — a silent
    one-day error in a table whose whole purpose is attribution by day.
    """
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%d")


def _age(since: float) -> str:
    """"just now" / "3m ago" / "2h ago" — how stale a snapshot is."""
    seconds = max(0, int(time.time() - since))
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _thousands(n: int) -> str:
    return f"{n:,}"


class _Section:
    """A titled block with one of five mutually exclusive bodies.

    Exists so the five states are impossible to skip. Each section is rendered
    by swapping a single child frame, so there is no path where a section is
    left blank — and blank is the state the old panel used for "we could not
    find out", which read as zero.
    """

    def __init__(self, parent: tk.Frame, title: str, subtitle: str = "",
                 title_colour: str = "") -> None:
        self.frame = tk.Frame(parent, bg=C["base"])
        self.frame.pack(fill=tk.X, padx=20, pady=(0, 18))

        header = tk.Frame(self.frame, bg=C["base"])
        header.pack(fill=tk.X)
        self._title_lbl = tk.Label(
            header, text=title, fg=title_colour or C["text"], bg=C["base"],
            font=("Segoe UI", 12, "bold"), anchor="w")
        self._title_lbl.pack(side=tk.LEFT)
        self._header = header

        self._subtitle_lbl = tk.Label(
            self.frame, text=subtitle, fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8), anchor="w", justify=tk.LEFT, wraplength=560)
        if subtitle:
            self._subtitle_lbl.pack(fill=tk.X, pady=(1, 0))

        self._body = tk.Frame(self.frame, bg=C["base"])
        self._body.pack(fill=tk.X, pady=(8, 0))

    def header(self) -> tk.Frame:
        """For controls that belong beside the title (range, refresh)."""
        return self._header

    def set_subtitle(self, text: str, colour: str = "") -> None:
        self._subtitle_lbl.configure(text=text, fg=colour or C["overlay0"])
        if text and not self._subtitle_lbl.winfo_ismapped():
            self._subtitle_lbl.pack(fill=tk.X, pady=(1, 0),
                                    before=self._body)

    def clear(self) -> tk.Frame:
        """Empty the body and hand back the frame to build into."""
        for child in self._body.winfo_children():
            child.destroy()
        return self._body

    # ── The five states ──────────────────────────────────────────────

    def loading(self) -> None:
        self._message("Loading…", C["overlay0"])

    def unavailable(self, reason: str) -> None:
        """Could not read it. Never rendered as zero."""
        self._message(f"⚠  Unavailable — {reason}", C["yellow"])

    def error(self, detail: str) -> None:
        self._message(f"✖  {detail}", C["red"])

    def _message(self, text: str, colour: str) -> None:
        body = self.clear()
        tk.Label(body, text=text, fg=colour, bg=C["base"],
                 font=("Segoe UI", 9), anchor="w", justify=tk.LEFT,
                 wraplength=560).pack(anchor="w")


class SavingsDialog(UiPumpMixin, tk.Toplevel):
    """Savings, spend and opportunity — each with its own scope and range."""

    def __init__(self, parent, cfg: "ManagerConfig", project_path: str = ""):
        super().__init__(parent)
        # Before anything can post to it.
        self._start_ui_pump()

        self._cfg = cfg
        self._project = project_path or ""

        # Generation guards, one PER SECTION. The range selector can start a
        # `30d` fetch, be switched to `today`, and have the slower `30d` worker
        # land last — silently replacing the newer selection with older
        # numbers. Workers capture their section's counter at launch and
        # `_apply_*` drops anything stale.
        #
        # They are separate because a single shared counter is wrong in a way
        # that only shows up in the real window: refreshing Spend would bump
        # it, and the still-in-flight Savings fetch would then look stale and
        # be discarded, leaving Savings stuck on "Loading…" forever. The
        # sections are independent, so their staleness is too.
        self._savings_gen = 0
        self._spend_gen = 0
        # Spend is refreshed explicitly and WRITES to tokensave's ledger, so a
        # second click while one is in flight must not start a second ingest.
        self._spend_busy = False
        self._spend_at = 0.0
        self._spend_range = ""

        self._range = tk.StringVar(value="30d")
        self._all_projects = tk.BooleanVar(value=False)

        self.title("Savings & Spend")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(620, 460)
        self.grab_set()

        self._build_scroller()
        self._build_ui()

        self._refresh_savings()
        self._refresh_spend()

        self.update_idletasks()
        w, h = 720, 640
        try:
            px = parent.winfo_x() + (parent.winfo_width() - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Scrollable shell (unchanged shape from the old dialog) ───────────

    def _build_scroller(self) -> None:
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
        bind_mousewheel(self._canvas)
        self._vsb = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self.body = tk.Frame(self._canvas, bg=C["base"])

        self.body.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._body_id = self._canvas.create_window(
            (0, 0), window=self.body, anchor="nw")
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._body_id, width=e.width))
        self._canvas.configure(yscrollcommand=self._vsb.set)

        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Layout ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        tk.Label(
            self.body, text="📊  Savings & Spend",
            fg=C["peach"], bg=C["base"], font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", padx=20, pady=(18, 2))

        tk.Label(
            self.body,
            text="Savings and spend are different numbers with different "
                 "scopes. Each section says which it is showing.",
            fg=C["overlay0"], bg=C["base"], font=("Segoe UI", 9),
            anchor="w", justify=tk.LEFT, wraplength=620,
        ).pack(anchor="w", padx=20, pady=(0, 16))

        self._build_savings_section()
        self._build_spend_section()
        self._build_opportunity_section()

        footer = tk.Frame(self.body, bg=C["base"])
        footer.pack(fill=tk.X, padx=20, pady=(4, 20))
        ttk.Button(footer, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _build_savings_section(self) -> None:
        scope = self._project or "no project selected"
        self._savings = _Section(
            self.body, "💰  Savings",
            subtitle=f"From `tokensave gain` — {scope}",
            title_colour=C["green"])

        controls = tk.Frame(self._savings.header(), bg=C["base"])
        controls.pack(side=tk.RIGHT)

        # The range selector belongs to Savings ALONE. Spend does not follow
        # it — see the module docstring on why that would be an ingest trigger.
        for value in RANGES:
            ttk.Radiobutton(
                controls, text=value, value=value, variable=self._range,
                command=self._refresh_savings,
            ).pack(side=tk.LEFT, padx=(0, 4))

        ttk.Checkbutton(
            controls, text="All projects", variable=self._all_projects,
            command=self._refresh_savings,
        ).pack(side=tk.LEFT, padx=(10, 0))

    def _build_spend_section(self) -> None:
        # Scope in the heading, not the subtitle: this dialog opens from a
        # project row, so "all projects on this machine" has to be impossible
        # to skim past.
        self._spend = _Section(
            self.body, "🧾  Estimated API spend — this machine, all projects",
            subtitle="API list price · not your subscription bill",
            title_colour=C["peach"])

        right = tk.Frame(self._spend.header(), bg=C["base"])
        right.pack(side=tk.RIGHT)
        self._spend_stamp = tk.Label(
            right, text="", fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8))
        self._spend_stamp.pack(side=tk.LEFT, padx=(0, 8))
        self._spend_btn = ttk.Button(
            right, text="↻ Refresh spend", command=self._refresh_spend)
        self._spend_btn.pack(side=tk.LEFT)

    def _build_opportunity_section(self) -> None:
        self._opportunity = _Section(
            self.body, "🔍  Opportunity",
            subtitle="From `tokensave discover` — navigation turns a tokensave "
                     "query could have served.",
            title_colour=C["blue"])

    # ── Small render helpers ─────────────────────────────────────────────

    def _stat(self, parent: tk.Frame, col: int, title: str, value: str,
              colour: str, note: str = "") -> None:
        card = tk.Frame(parent, bg=C["surface0"], padx=14, pady=12)
        card.grid(row=0, column=col, padx=(0, 8), pady=0, sticky="nsew")
        parent.grid_columnconfigure(col, weight=1)

        tk.Label(card, text=title, fg=C["subtext"], bg=C["surface0"],
                 font=("Segoe UI", 9)).pack(anchor="w")
        tk.Label(card, text=value, fg=colour, bg=C["surface0"],
                 font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(4, 0))
        if note:
            tk.Label(card, text=note, fg=C["overlay0"], bg=C["surface0"],
                     font=("Segoe UI", 8), wraplength=170,
                     justify=tk.LEFT).pack(anchor="w", pady=(2, 0))

    def _table(self, parent: tk.Frame, headers: list, rows: list,
               note: str = "") -> None:
        """A small grid table. The data here is tens of rows, not thousands."""
        grid = tk.Frame(parent, bg=C["base"])
        grid.pack(fill=tk.X, pady=(10, 0))

        for col, head in enumerate(headers):
            tk.Label(grid, text=head, fg=C["subtext"], bg=C["base"],
                     font=("Segoe UI", 9, "bold"),
                     anchor="w" if col == 0 else "e").grid(
                row=0, column=col, sticky="ew", padx=(0, 12), pady=(0, 3))
            grid.grid_columnconfigure(col, weight=1 if col == 0 else 0)

        for r, row in enumerate(rows, start=1):
            for col, cell in enumerate(row):
                tk.Label(grid, text=str(cell), fg=C["text"], bg=C["base"],
                         font=("Segoe UI", 9),
                         anchor="w" if col == 0 else "e").grid(
                    row=r, column=col, sticky="ew", padx=(0, 12))

        if note:
            tk.Label(parent, text=note, fg=C["overlay0"], bg=C["base"],
                     font=("Segoe UI", 8), anchor="w", justify=tk.LEFT,
                     wraplength=620).pack(anchor="w", pady=(6, 0))

    # ── Savings ──────────────────────────────────────────────────────────

    def _refresh_savings(self) -> None:
        """Re-read `gain` and `gain --history`. Never touches `cost`."""
        self._savings_gen += 1
        generation = self._savings_gen
        range_ = self._range.get()
        every = self._all_projects.get()

        self._savings.loading()
        self._opportunity.loading()

        exe = getattr(self._cfg, "tokensave_exe", "")
        project = self._project

        def _work():
            gain = fetch_gain(exe, project, range_, all_projects=every)
            history = fetch_gain_history(exe, project, range_)
            self._post(lambda: self._apply_savings(generation, gain, history,
                                                   range_))
            # `discover` writes to the ledger, like `cost`. It rides the
            # savings refresh rather than getting its own control, so the
            # ingest happens on a deliberate range change and nowhere else.
            found = fetch_discover(exe, project, range_)
            self._post(lambda: self._apply_opportunity(generation, found))

        threading.Thread(target=_work, daemon=True).start()

    def _stale(self, generation: int) -> bool:
        """True when a worker's result has been overtaken by a newer one."""
        return generation != self._savings_gen or not self.winfo_exists()

    def _apply_savings(self, generation: int, gain, history,
                       range_: str) -> None:
        if self._stale(generation):
            return
        if not gain:
            self._savings.unavailable(gain.reason)
            return

        value: Gain = gain.value
        scope = ("all projects" if value.all_projects
                 else (self._project or "this project"))
        self._savings.set_subtitle(
            f"From `tokensave gain` — {scope} · {range_}", C["overlay0"])

        body = self._savings.clear()
        cards = tk.Frame(body, bg=C["base"])
        cards.pack(fill=tk.X)

        self._stat(cards, 0, "Tokens saved",
                   _thousands(value.saved_tokens), C["green"])
        self._stat(cards, 1, "Tool calls",
                   _thousands(value.calls), C["subtext"])
        # The valuation basis travels with the number. A bare `$` with no
        # basis is exactly how the old panel became untrustworthy.
        self._stat(cards, 2, "USD saved", f"${value.usd:,.2f}", C["green"],
                   note=f"valued at {Gain.USD_BASIS}")

        if not history:
            tk.Label(body, text=f"Daily history unavailable — {history.reason}",
                     fg=C["yellow"], bg=C["base"], font=("Segoe UI", 8),
                     anchor="w").pack(anchor="w", pady=(10, 0))
            return

        days = history.value
        if not days:
            tk.Label(body, text="No recorded activity in this range.",
                     fg=C["overlay0"], bg=C["base"], font=("Segoe UI", 9),
                     anchor="w").pack(anchor="w", pady=(10, 0))
            return

        self._table(
            body,
            # Labelled UTC because the underlying value is a UTC midnight
            # epoch; converting it to local dates would shift rows.
            ["Day (UTC)", "Tokens", "Calls", "USD"],
            [(_utc_day(d.day), _thousands(d.saved_tokens), d.calls,
              f"${d.usd:,.2f}") for d in days],
            note="Only days with recorded tool calls appear — a missing day "
                 "means no calls, not zero savings.")

    # ── Spend ────────────────────────────────────────────────────────────

    def _refresh_spend(self) -> None:
        """Explicit, guarded, and the only thing that re-runs `cost`.

        `cost` ingests accounting rows into tokensave's global ledger, so a
        double-click must not start a second ingest — the button disables for
        the duration and the generation check drops a late result.
        """
        if self._spend_busy:
            return
        self._spend_busy = True
        self._spend_btn.configure(state=tk.DISABLED)

        self._spend_gen += 1
        generation = self._spend_gen
        range_ = self._range.get()

        self._spend.loading()
        self._spend_stamp.configure(text="refreshing…")

        exe = getattr(self._cfg, "tokensave_exe", "")
        project = self._project

        def _work():
            spend = fetch_spend(exe, range_, project)
            self._post(lambda: self._apply_spend(generation, spend, range_))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_spend(self, generation: int, spend, range_: str) -> None:
        # The in-flight guard clears even for a stale result: the subprocess
        # has finished either way, and leaving the button disabled would strand
        # the only control that can refresh this section.
        self._spend_busy = False
        if not self.winfo_exists():
            return
        self._spend_btn.configure(state=tk.NORMAL)
        if generation != self._spend_gen:
            return

        if not spend:
            # A failed refresh must not leave the previous snapshot looking
            # current. Either it is unavailable, or it is explicitly stale.
            if self._spend_at:
                self._spend_stamp.configure(
                    text=f"⚠ stale — {self._spend_range} · "
                         f"{_age(self._spend_at)}", fg=C["yellow"])
            else:
                self._spend_stamp.configure(text="")
                self._spend.unavailable(spend.reason)
            return

        value = spend.value
        self._spend_at = time.time()
        self._spend_range = range_
        self._spend_stamp.configure(
            text=f"snapshot: {range_} · {_age(self._spend_at)}",
            fg=C["overlay0"])

        body = self._spend.clear()
        cards = tk.Frame(body, bg=C["base"])
        cards.pack(fill=tk.X)

        self._stat(cards, 0, "Estimated spend",
                   f"${value.total_cost_usd:,.2f}", C["peach"])
        self._stat(cards, 1, "Input tokens",
                   _thousands(value.total_input_tokens), C["subtext"])
        self._stat(cards, 2, "Output tokens",
                   _thousands(value.total_output_tokens), C["subtext"])
        # Read from the export on tokensave 7.11+ (#472), never derived.
        # `None` is "this binary did not report it", which is not zero — the
        # distinction the whole module exists to preserve.
        if value.cache_read_tokens is None:
            self._stat(cards, 3, "Cache reads", "not reported", C["overlay0"],
                       note="needs tokensave 7.11+")
        else:
            self._stat(cards, 3, "Cache reads",
                       _thousands(value.cache_read_tokens), C["subtext"],
                       note="usually the dominant category")

        if value.by_model:
            self._table(body, ["Model", "Cost", "Tokens"],
                        [(m.model, f"${m.cost:,.2f}", _thousands(m.tokens))
                         for m in value.by_model])
        if value.by_category:
            self._table(body, ["Category", "Cost", "Turns"],
                        [(c.category, f"${c.cost:,.2f}", _thousands(c.turns))
                         for c in value.by_category])

        # The basis travels with the rate. Over all four token categories the
        # arithmetic closes (~$0.61/Mtok measured); over input+output alone —
        # all an older export can offer — it reads in the hundreds, because
        # three quarters of the tokens are missing from the denominator, not
        # because tokensave priced anything strangely.
        implied = value.implied_usd_per_mtok()
        note = "These are tokensave's reported figures"
        if implied is None:
            note += " (no tokens in this range)"
        elif implied[1] == value.BASIS_TOTAL:
            note += (f" — ${implied[0]:,.2f} per million tokens "
                     f"across all four token categories")
        else:
            note += (f" — ${implied[0]:,.2f} per million tokens, but counting "
                     f"only input and output: this tokensave does not export "
                     f"cache tokens, so the denominator is incomplete")
        note += ". Savings are not shown here; see the Savings section."
        tk.Label(body, text=note, fg=C["overlay0"], bg=C["base"],
                 font=("Segoe UI", 8), anchor="w", justify=tk.LEFT,
                 wraplength=620).pack(anchor="w", pady=(10, 0))

    # ── Opportunity ──────────────────────────────────────────────────────

    def _apply_opportunity(self, generation: int, found) -> None:
        if self._stale(generation):
            return
        if not found:
            self._opportunity.unavailable(found.reason)
            return

        value = found.value
        body = self._opportunity.clear()

        summary = (f"{_thousands(value.replaceable_turns)} of "
                   f"{_thousands(value.total_turns)} turns could have been "
                   f"served by a tokensave query.")
        tk.Label(body, text=summary, fg=C["text"], bg=C["base"],
                 font=("Segoe UI", 10), anchor="w").pack(anchor="w")

        if value.buckets:
            self._table(body, ["Tool", "Turns", "Suggested instead"],
                        [(b.tool, _thousands(b.turns), b.suggestion)
                         for b in value.buckets])

        # The note is ALWAYS shown, because token estimates are never on the
        # typed surface — turns are the authoritative figure and tokens are
        # not displayed at any range. Gating the explanation on the evidence
        # check left the other case silent, which is the "withheld" versus
        # "no such estimate exists" ambiguity this section exists to avoid:
        # a reader saw turns, no tokens, and no reason.
        #
        # The evidence only makes the note more specific. It is a recorded
        # observation rather than a plausibility rule, so a range where the
        # degenerate identity does not hold still gets an honest note instead
        # of an implied endorsement of numbers nobody is showing.
        if value.tokens_trustworthy:
            note = ("Turn counts are the authoritative figure here. "
                    "tokensave's token-recovery estimates are not shown.")
            colour = C["overlay0"]
        else:
            note = ("Token-recovery estimates are withheld: tokensave's "
                    f"reported accounting failed validation "
                    f"({value.token_evidence}).")
            colour = C["yellow"]
        tk.Label(body, text=note, fg=colour, bg=C["base"],
                 font=("Segoe UI", 8), anchor="w", justify=tk.LEFT,
                 wraplength=620).pack(anchor="w", pady=(8, 0))


#: The dialog was called `CostViewerDialog` while it was showing cost as though
#: it were savings. The name went with the behaviour; this alias keeps any
#: stale import working rather than failing at a lazy import inside a handler.
CostViewerDialog = SavingsDialog
