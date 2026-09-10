"""dialogs/instructions_split.py — the per-project split proposal.

`helpers/instructions_split.py` computes the transformation; this shows it and
asks. The two are separate because of the rule the helper's docstring states:
**nothing is written until the complete transformation has been shown**. A
dialog that computed and wrote in one action would have no place to put that
guarantee.

**Per project, never bulk.** Moving a project's accumulated lesson log is a
large, editorial change to a file a person wrote. It is offered on the row it
applies to, one at a time, and there is deliberately no "split all".

**The section list is the control, because the boundary is a judgement.** No
mechanical discriminator survived the fleet — ALL-CAPS headings are 81% of one
project's sections and 0% of another's, and all three are logs. So the byte
budget pre-ticks a suggestion and a person moves it. On the first project the
suggestion landed mid-log, keeping 31 lesson entries loaded for no reason,
which is exactly the case that needs a human.

**What the preview shows, and what it does not.** The new `CLAUDE.md` is shown
in full — after a split it is small, which is the whole point. The moved
content is NOT re-rendered: it is byte-for-byte the original, pinned by
`test_moved_content_is_verbatim_and_in_order`, and pouring 900 KB into a Tk
text widget would be a hang rather than a review. The complete heading map is
shown instead, so every moved section is named before anything moves.

No writes happen in this module. `apply_split` is the only writer, it is
digest-guarded, and it refuses if the file changed after this dialog read it.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from helpers.instructions_split import (
    DEFAULT_TARGET, apply_split, children_of, compute_split, is_index_section,
    is_our_target, read_source,
)
from theme import UiPumpMixin, _Tooltip, bind_mousewheel


class SplitProposalDialog(UiPumpMixin, tk.Toplevel):
    """Show the whole transformation for one project, then ask."""

    def __init__(self, parent, project_path: str, project_name: str,
                 on_log=None, on_applied=None):
        super().__init__(parent)
        self.title("✂ Split instructions — %s" % project_name)
        self.configure(bg=C["base"])
        self.geometry("880x680")
        self._path = project_path
        self._name = project_name
        self._on_log = on_log or (lambda *a, **k: None)
        #: Called after a successful write so the fleet panel re-measures --
        #: the weight it is showing is now wrong by most of the file.
        self._on_applied = on_applied or (lambda: None)
        self._text = ""
        #: An existing target a previous split wrote. Passed to every
        #: recompute, because a second split ADDS to it rather than refusing.
        self._target_text = ""
        self._sections = ()
        self._vars = {}
        self._boxes = {}
        self._plan = None
        self._busy = False

        self._build_ui()
        self._start_ui_pump()
        self._load()

    # ── layout ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        tk.Label(self, text="Move the lesson log out of the file every "
                            "message loads",
                 font=("Segoe UI", 12, "bold"), bg=C["base"],
                 fg=C["blue"]).pack(anchor=tk.W, padx=18, pady=(14, 0))
        tk.Label(self,
                 text="Ticked sections move to %s, which is NOT @included. "
                      "An index of their titles stays behind." % DEFAULT_TARGET,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=18, pady=(0, 2))
        tk.Label(self,
                 text="Nothing is ticked to begin with. The byte budget "
                      "guesses a tail, and that guess has been wrong on three "
                      "of the four real files it has met — so the choice "
                      "is yours.",
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

        self._totals = tk.Label(self, text="", font=("Segoe UI", 9, "bold"),
                                bg=C["base"], fg=C["text"], anchor=tk.W)
        self._totals.pack(fill=tk.X, padx=18, pady=(2, 0))
        # A refusal is stated here rather than only disabling the button. A
        # greyed control with no explanation is the same defect as a red row
        # with none.
        self._reason = tk.Label(self, text="", font=("Segoe UI", 8),
                                bg=C["base"], fg=C["peach"], anchor=tk.W,
                                justify=tk.LEFT, wraplength=820)
        self._reason.pack(fill=tk.X, padx=18, pady=(0, 6))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._apply_btn = ttk.Button(btn_row, text="Apply…",
                                     style="Primary.TButton",
                                     command=self._apply, state=tk.DISABLED)
        self._apply_btn.pack(side=tk.LEFT)
        _Tooltip(self._apply_btn,
                 "Writes %s first, then the trimmed CLAUDE.md. Refuses if the "
                 "file changed since this dialog read it." % DEFAULT_TARGET)
        self._preview_btn = ttk.Button(btn_row, text="Preview…",
                                       command=self._preview,
                                       state=tk.DISABLED)
        self._preview_btn.pack(side=tk.LEFT, padx=(8, 0))
        _Tooltip(self._preview_btn,
                 "The new CLAUDE.md in full, plus every heading that moves.")
        self._suggest_btn = ttk.Button(btn_row, text="Suggest from budget",
                                       command=self._suggest,
                                       state=tk.DISABLED)
        self._suggest_btn.pack(side=tk.LEFT, padx=(8, 0))
        _Tooltip(self._suggest_btn,
                 "Ticks the tail the byte budget would move. A starting "
                 "point, not a recommendation: it has selected operational "
                 "sections before, because a tail is not a log.")
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    # ── reading ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Read and compute off the Tk thread; the biggest file is ~1 MB."""
        tk.Label(self._body, text="  Reading…", font=("Segoe UI", 10),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=20)
        path = self._path

        def worker():
            text = read_source(path)
            target = read_source(path, DEFAULT_TARGET)
            plan = compute_split(text, target_text=target) if text else None
            self._post(lambda: self._render(text, target, plan))

        threading.Thread(target=worker, daemon=True).start()

    def _render(self, text: str, target_text: str, plan) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        if not text:
            self._fail("Could not read CLAUDE.md in %s." % self._name)
            return

        self._text = text
        self._target_text = target_text
        # Every section, in document order. `compute_split` returns them split
        # into kept and moved; the union IS the file's section list.
        self._sections = tuple(sorted(plan.kept + plan.moved,
                                      key=lambda s: s.index))
        if not self._sections:
            self._fail(plan.blocked or "no top-level sections to move.")
            return

        # Deliberately unticked. The budget's tail has been wrong on three of
        # the four real files this has met -- once selecting an operational
        # section at the end of the document -- and a pre-ticked box reads as a
        # recommendation. The guess is one button away for anyone who wants it.
        self._vars = {}
        self._boxes = {}
        for section in self._sections:
            self._section_row(section, False)
        for button in (self._preview_btn, self._suggest_btn):
            button.configure(state=tk.NORMAL)
        self._recompute()

    def _section_row(self, section, ticked: bool) -> None:
        var = tk.BooleanVar(value=ticked)
        self._vars[section.index] = var
        row = tk.Frame(self._body, bg=C["base"])
        # Indented by level, because `##` is not the unit of a lesson: one
        # project keeps a 64 KB operational section holding twenty `###`
        # lessons, and a flat list cannot show that they belong to it.
        row.pack(fill=tk.X, padx=(8 + (section.level - 2) * 26, 8), pady=0)

        box = tk.Checkbutton(row, variable=var, bg=C["base"], fg=C["text"],
                             command=lambda i=section.index: self._on_tick(i),
                             selectcolor=C["base"],
                             activebackground=C["base"],
                             highlightthickness=0, bd=0)
        box.pack(side=tk.LEFT)
        self._boxes[section.index] = box
        if is_index_section(section):
            # Moving the index into the target loses it from the loaded file
            # AND stops the next split finding it to merge into.
            box.configure(state=tk.DISABLED)
            _Tooltip(box, "This is the index a previous split left behind. It "
                          "is rewritten in place, so it cannot be moved.")
        tk.Label(row, text="%3d" % section.index, font=("Consolas", 8),
                 bg=C["base"], fg=C["overlay0"], width=4).pack(side=tk.LEFT)
        # A parent shows its TOTAL: own bytes plus everything under it, which
        # is what a reader actually pays for keeping it.
        shown = section.total_size if section.level == 2 else section.size
        size = tk.Label(row, text="%s B" % f"{shown:,}", font=("Consolas", 8),
                        bg=C["base"], fg=self._size_colour(shown), width=11,
                        anchor=tk.E)
        size.pack(side=tk.LEFT, padx=(0, 8))
        kids = children_of(self._sections, section.index)
        if kids:
            _Tooltip(size, "%s B of its own, %s B including its %d "
                           "subsections. Ticking this moves all of them."
                           % (f"{section.size:,}", f"{section.total_size:,}",
                              len(kids)))
        tk.Label(row, text=section.title[:86], font=("Segoe UI", 9),
                 bg=C["base"],
                 fg=C["peach"] if section.has_directive else (
                     C["text"] if section.level == 2 else C["subtext"]),
                 anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        if section.entry_count >= 8 and not kids:
            note = tk.Label(row, text="%d entries" % section.entry_count,
                            font=("Segoe UI", 8), bg=C["base"],
                            fg=C["overlay0"])
            note.pack(side=tk.RIGHT, padx=(6, 0))
            _Tooltip(note,
                     "This section carries no headings, so the index can only "
                     "name the section. It records the count instead, and the "
                     "entries are found by grepping for the bold line that "
                     "opens each one.")
        if section.has_directive:
            chain = tk.Label(row, text="@include", font=("Segoe UI", 8, "bold"),
                             bg=C["base"], fg=C["peach"])
            chain.pack(side=tk.RIGHT)
            _Tooltip(chain, "This section carries the include chain. Moving it "
                            "would break what the file exists to do, so the "
                            "split is refused while it is ticked.")

    def _on_tick(self, index: int) -> None:
        """A parent carries its children, and the rows have to say so.

        `compute_split` already expands a ticked parent to its descendants.
        Without mirroring it here the screen would disagree with what is about
        to be written, which is the one thing a preview may not do.
        """
        section = self._sections[index]
        if section.level == 2:
            on = self._vars[index].get()
            for kid in children_of(self._sections, index):
                self._vars[kid.index].set(on)
                self._boxes[kid.index].configure(
                    state=tk.DISABLED if on else tk.NORMAL)
        self._recompute()

    @staticmethod
    def _size_colour(size: int) -> str:
        if size > 50_000:
            return C["peach"]
        if size > 10_000:
            return C["yellow"]
        return C["overlay0"]

    def _fail(self, message: str) -> None:
        tk.Label(self._body, text="  " + message, font=("Segoe UI", 10),
                 bg=C["base"], fg=C["peach"], anchor=tk.W,
                 wraplength=800).pack(anchor=tk.W, pady=20)
        self._totals.configure(text="")

    # ── the live proposal ────────────────────────────────────────────────

    def _selected(self) -> set:
        return {i for i, var in self._vars.items() if var.get()}

    def _suggest(self) -> None:
        """Tick what the byte budget would move. Asked for, never assumed."""
        plan = compute_split(self._text)
        suggested = {s.index for s in plan.moved}
        for index, var in self._vars.items():
            var.set(index in suggested)
        self._recompute()

    def _recompute(self) -> None:
        """Recompute the whole plan. Measured at 18 ms on the 954 KB file."""
        self._plan = compute_split(self._text, move_indices=self._selected(),
                                   target_text=self._target_text)
        plan = self._plan
        # Not `plan.appending`: that is False whenever nothing is ticked,
        # because the plan short-circuits before reaching it, and the footer
        # then said "creates" beside a file that plainly exists. The verb is a
        # fact about the project, not about the current selection.
        verb = "adds to" if is_our_target(self._target_text) else "creates"
        self._totals.configure(
            text="stays loaded: %s B (~%s tok)     ·     moves: %s B  →  %s %s"
                 % (f"{plan.kept_bytes:,}", f"{plan.kept_bytes // 4:,}",
                    f"{plan.moved_bytes:,}", verb, plan.target_rel))

        # Stated on open. `compute_split` refuses this too, but only once a
        # section is ticked -- and a person should not choose a hundred rows
        # before being told the destination is unusable.
        if self._target_text.strip() and not is_our_target(self._target_text):
            self._refuse("%s already exists and was not written by this tool, "
                         "so there is nothing safe to add to."
                         % DEFAULT_TARGET)
        elif not self._selected():
            # Not a refusal. On open this is simply the starting state, and
            # colouring it like a fault would teach people to ignore the line
            # that also carries the real ones.
            self._prompt("Tick the sections that are a log. Nothing is "
                         "written until you do.")
        elif plan.blocked:
            self._refuse(plan.blocked)
        else:
            self._reason.configure(text="")
            self._apply_btn.configure(state=tk.NORMAL)

    def _prompt(self, message: str) -> None:
        """Guidance. Same line as a refusal, deliberately not the same colour."""
        self._reason.configure(text=message, fg=C["overlay0"])
        self._apply_btn.configure(state=tk.DISABLED)

    def _refuse(self, reason: str) -> None:
        self._reason.configure(text="Cannot apply: " + reason, fg=C["peach"])
        self._apply_btn.configure(state=tk.DISABLED)

    # ── preview ──────────────────────────────────────────────────────────

    def _preview(self) -> None:
        plan = self._plan
        if plan is None:
            return
        win = tk.Toplevel(self)
        win.title("Preview — %s" % self._name)
        win.configure(bg=C["base"])
        win.geometry("900x700")
        tk.Label(win, text="The new CLAUDE.md, in full", bg=C["base"],
                 fg=C["blue"], font=("Segoe UI", 11, "bold")).pack(
            anchor=tk.W, padx=14, pady=(12, 0))
        tk.Label(win,
                 text="Moved sections are not re-shown: they are byte-for-byte "
                      "the original, and their headings are all listed below.",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "italic")).pack(anchor=tk.W, padx=14,
                                                      pady=(0, 6))
        text = tk.Text(win, bg=C["mantle"], fg=C["text"], wrap=tk.NONE,
                       font=("Consolas", 9), relief=tk.FLAT)
        vsb = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(fill=tk.BOTH, expand=True, padx=(14, 0), pady=(0, 12))
        text.insert("1.0", plan.new_source)
        text.insert(tk.END, "\n\n%s\n%d sections moving to %s (%s B)\n%s\n"
                    % ("=" * 70, len(plan.moved), plan.target_rel,
                       f"{plan.moved_bytes:,}", "=" * 70))
        for section in plan.moved:
            text.insert(tk.END, "  %s   (%s B)\n"
                        % (section.title, f"{section.size:,}"))
        text.configure(state=tk.DISABLED)

    # ── writing ──────────────────────────────────────────────────────────

    def _apply(self) -> None:
        plan = self._plan
        if plan is None or self._busy:
            return
        if not messagebox.askyesno(
                "Apply split",
                "%s\n\nWrites %s, then rewrites CLAUDE.md.\n\nThe original "
                "sections are moved verbatim. Both files are in git, so this "
                "is revertible."
                % (plan.summary(), plan.target_rel), parent=self):
            return

        self._busy = True
        self._apply_btn.configure(state=tk.DISABLED)
        path, name = self._path, self._name

        def worker():
            ok, message = apply_split(path, plan)
            self._post(lambda: self._finish(ok, message, name))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, ok: bool, message: str, name: str) -> None:
        self._busy = False
        self._on_log("[split] %s: %s" % (name, message))
        if ok:
            messagebox.showinfo("Split applied", "%s\n\n%s" % (name, message),
                                parent=self)
            self._on_applied()
            self.destroy()
            return
        # A refusal is a state, not a crash: the digest guard fires when a live
        # session appended between opening this dialog and clicking Apply.
        self._refuse(message)
        messagebox.showwarning("Nothing was written",
                               "%s\n\n%s" % (name, message), parent=self)
        self._load()
