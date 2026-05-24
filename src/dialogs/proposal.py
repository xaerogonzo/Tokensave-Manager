"""ProposalDialog — secure write-verification sandbox for AI-proposed file edits.

Presents a dual-pane diff view (original left, proposed right) with a draggable
sash so the user can adjust the split to their preference. The proposed pane is
editable — the user can correct the AI's suggestion before accepting. Scrollbars
on both axes prevent long lines from wrapping and obscuring code indentation.

The WriteProposal dataclass is also defined here (it's tiny and used only with
this dialog). In Roadmap-2, agent_tools.py will import WriteProposal and call
ProposalDialog via root.after(0, ...) from a background thread.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from constants import C


@dataclass
class WriteProposal:
    """Data contract for an AI-proposed file write."""
    filepath:         str
    original_content: str
    proposed_content: str
    rationale:        str


class ProposalDialog(tk.Toplevel):
    """Show an AI-proposed file edit and let the user accept, edit, or reject it."""

    def __init__(self, parent, proposal: WriteProposal, on_accept_callback):
        """
        Args:
            parent:             The root Tk window (or any Toplevel).
            proposal:           WriteProposal describing what changed and why.
            on_accept_callback: Called with (filepath: str, final_content: str)
                                when the user accepts (possibly after editing the
                                proposed pane). NOT called on rejection.
        """
        super().__init__(parent)
        self._proposal  = proposal
        self._on_accept = on_accept_callback

        self.title("🛡️  Secure Write Verification Required")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(700, 500)
        self.transient(parent)
        self.grab_set()

        self._build_ui()

        # Centre on parent
        self.update_idletasks()
        w, h = 960, 640
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header — file path + rationale
        hdr = tk.Frame(self, bg=C["base"], padx=20, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr,
            text=f"The AI proposes editing:  {self._proposal.filepath}",
            fg=C["text"], bg=C["base"],
            font=("Segoe UI", 10, "bold"),
            justify=tk.LEFT,
        ).pack(anchor="w")
        tk.Label(
            hdr,
            text=f"Reason: {self._proposal.rationale}",
            fg=C["subtext0"], bg=C["base"],
            font=("Segoe UI", 9, "italic"),
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))

        # Draggable split pane
        paned = tk.PanedWindow(
            self, orient=tk.HORIZONTAL,
            sashrelief=tk.FLAT, sashwidth=6,
            bg=C["surface0"],
        )
        paned.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        # ── Left pane: Original (read-only) ──────────────────────────────
        left_frame = tk.LabelFrame(
            paned, text="Original Content",
            fg=C["red"], bg=C["base"],
            labelanchor="n",
            padx=4, pady=4,
        )
        self.orig_text = self._make_text_pane(left_frame, editable=False)
        self.orig_text.insert(tk.END, self._proposal.original_content)
        self.orig_text.configure(state=tk.DISABLED)
        paned.add(left_frame, stretch="always")

        # ── Right pane: Proposed (editable) ──────────────────────────────
        right_frame = tk.LabelFrame(
            paned, text="Proposed Edit  (you may edit before accepting)",
            fg=C["green"], bg=C["base"],
            labelanchor="n",
            padx=4, pady=4,
        )
        self.prop_text = self._make_text_pane(right_frame, editable=True)
        self.prop_text.insert(tk.END, self._proposal.proposed_content)
        paned.add(right_frame, stretch="always")

        # Action buttons
        btn_row = tk.Frame(self, bg=C["base"], padx=20, pady=12)
        btn_row.pack(fill=tk.X)

        tk.Button(
            btn_row, text="❌  Reject Changes",
            fg=C["crust"], bg=C["red"],
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=10, pady=4,
            command=self._on_reject,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Button(
            btn_row, text="🛡️  Accept & Apply Changes",
            fg=C["crust"], bg=C["green"],
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=10, pady=4,
            command=self._on_accept_press,
        ).pack(side=tk.RIGHT, padx=(6, 0))

    def _make_text_pane(self, parent: tk.Frame, editable: bool) -> tk.Text:
        """Build a Text widget with both scrollbars and no line wrapping."""
        wrapper = tk.Frame(parent, bg=C["mantle"])
        wrapper.pack(fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(wrapper, orient="vertical")
        hsb = ttk.Scrollbar(wrapper, orient="horizontal")

        txt = tk.Text(
            wrapper,
            wrap=tk.NONE,
            bg=C["mantle"], fg=C["text"],
            insertbackground=C["text"],
            font=("Consolas", 9),
            highlightthickness=0,
            relief=tk.FLAT,
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )

        vsb.configure(command=txt.yview)
        hsb.configure(command=txt.xview)

        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        txt.pack(side=tk.LEFT,   fill=tk.BOTH, expand=True)

        return txt

    # ── Handlers ──────────────────────────────────────────────────────────

    def _on_reject(self):
        self.destroy()

    def _on_accept_press(self):
        user_edited = self.prop_text.get("1.0", tk.END).rstrip("\n")
        self.destroy()
        self._on_accept(self._proposal.filepath, user_edited)


# ── Standalone test harness ───────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()   # flush window manager — do NOT use time.sleep()

    dummy = WriteProposal(
        filepath="src/helpers/example.py",
        original_content=(
            "def foo():\n"
            "    # old implementation\n"
            "    return 42\n"
        ),
        proposed_content=(
            "def foo():\n"
            "    # improved implementation with a very long comment "
            "that tests horizontal scrolling behaviour across the pane\n"
            "    result = sum(range(100))\n"
            "    return result\n"
        ),
        rationale="Replaced magic number with computed sum for clarity.",
    )

    def _accepted(path, content):
        print(f"ACCEPTED: {path}\n---\n{content}\n---")
        root.destroy()

    ProposalDialog(root, dummy, _accepted)
    root.mainloop()
