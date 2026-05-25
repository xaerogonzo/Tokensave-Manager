"""CostViewerDialog — display token savings and cost metrics from tokensave cost.

Opens as a modal Toplevel. Metrics are fetched in a background thread (the
tokensave subprocess can take 500ms+) so the dialog opens instantly with
"Loading…" placeholders, then updates when the data arrives.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.daemon_cost import parse_tokensave_cost

if TYPE_CHECKING:
    from state import ManagerConfig


class CostViewerDialog(tk.Toplevel):
    """Show token savings and estimated cost recouped from tokensave."""

    def __init__(self, parent, cfg: "ManagerConfig"):
        super().__init__(parent)
        self._cfg = cfg

        self.title("Token Cost Savings")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(500, 380)
        self.transient(parent)
        self.grab_set()

        # ── Scrollable wrapper ─────────────────────────────────────────────
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
        self._vsb    = ttk.Scrollbar(self, orient="vertical",
                                     command=self._canvas.yview)
        self.body    = tk.Frame(self._canvas, bg=C["base"])

        self.body.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._body_id = self._canvas.create_window(
            (0, 0), window=self.body, anchor="nw")
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(
                self._body_id, width=e.width))
        self._canvas.configure(yscrollcommand=self._vsb.set)

        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._build_ui()
        self._refresh_metrics()

        # Centre on parent
        self.update_idletasks()
        w, h = 580, 420
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        tk.Label(
            self.body, text="📊  Token Cost Savings",
            fg=C["peach"], bg=C["base"],
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", padx=20, pady=(20, 4))

        tk.Label(
            self.body,
            text="Metrics fetched from `tokensave cost` — past 7 days.",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=20, pady=(0, 14))

        # 2×2 metric grid
        card_frame = tk.Frame(self.body, bg=C["base"])
        card_frame.pack(fill=tk.X, padx=20, pady=(0, 16))

        self._saved_lbl  = self._create_card(card_frame, "Saved Tokens",   "Loading…", 0, 0, C["green"])
        self._value_lbl  = self._create_card(card_frame, "Value Recouped", "Loading…", 0, 1, C["peach"])
        self._input_lbl  = self._create_card(card_frame, "Input Tokens",   "Loading…", 1, 0, C["subtext0"])
        self._output_lbl = self._create_card(card_frame, "Output Tokens",  "Loading…", 1, 1, C["subtext0"])

        # Refresh button
        btn_row = tk.Frame(self.body, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 20))
        ttk.Button(btn_row, text="↻  Refresh",
                   command=self._refresh_metrics).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _create_card(
        self,
        parent: tk.Frame,
        title: str,
        initial_val: str,
        row: int,
        col: int,
        val_color: str,
    ) -> tk.Label:
        card = tk.Frame(parent, bg=C["surface0"], padx=15, pady=15)
        card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
        parent.grid_columnconfigure(col, weight=1)

        tk.Label(card, text=title, fg=C["subtext0"], bg=C["surface0"],
                 font=("Segoe UI", 10)).pack(anchor="center")

        val_lbl = tk.Label(card, text=initial_val,
                           fg=val_color, bg=C["surface0"],
                           font=("Segoe UI", 18, "bold"))
        val_lbl.pack(anchor="center", pady=(6, 0))
        return val_lbl

    # ── Metrics fetch ──────────────────────────────────────────────────────

    def _refresh_metrics(self):
        """Spawn a background thread to fetch metrics; update labels when done."""
        for lbl in (self._saved_lbl, self._value_lbl,
                    self._input_lbl, self._output_lbl):
            lbl.configure(text="Loading…", fg=C["overlay0"])

        def _fetch():
            m = parse_tokensave_cost(self._cfg.tokensave_exe, scope="7day")
            self.after(0, lambda metrics=m: self._apply_metrics(metrics))

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_metrics(self, m: dict):
        if not self.winfo_exists():
            return
        self._saved_lbl.configure( text=m["saved"],        fg=C["green"])
        self._value_lbl.configure( text=f"${m['value']}",  fg=C["peach"])
        self._input_lbl.configure( text=m["input"],         fg=C["subtext0"])
        self._output_lbl.configure(text=m["output"],        fg=C["subtext0"])
