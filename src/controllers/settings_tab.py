"""SettingsTabController -- Settings as a first-class tab, in five pages.

Replaces `dialogs/settings.py`. There is now exactly ONE settings surface:
a second one is the confusion this change exists to remove, and every caller
that used to open the modal gets a better interaction as a tab -- notably
"CodeGraph is not installed", which can now land the user on the page that
configures it instead of at the top of a 700-pixel scroll.

WHAT THIS CONTROLLER OWNS: composition, page selection, dirty state, and the
save transaction. It does NOT own section semantics -- those stay in
`dialogs/settings_*.py`, which document the contract in
`dialogs/settings_section.py`.

SAVE IS A STAGED TRANSACTION, and that is a fix rather than a refactor. The
dialog walked the sections mutating `cfg.raw` in place and only wrote to disk
once all of them returned True, so an abort in section three left sections one
and two already applied to the live config object the whole application reads,
while disk still held the old values. A modal mostly hid it: the user fixed
the path and saved again within seconds. A persistent tab can sit in that
state indefinitely, and any unrelated `cfg.save()` elsewhere would quietly
persist the half-change.

Two details decide whether the staging actually works:

  * `copy.deepcopy`, never `dict(...)`. `AISection.save_into` does
    `existing_llm = raw.get("commit_message_llm") or {}` and then
    `existing_llm.update(...)` -- an in-place mutation of a NESTED dict. A
    shallow copy shares that object, so the write goes straight through the
    staging area and the transaction silently does nothing.
  * Commit by mutating `cfg.raw` IN PLACE. Modules hold references to that
    dict; rebinding it would leave them reading the old one, which is the
    stale-snapshot failure the ManagerConfig contract already forbids.

The pre-commit hook runs AFTER the commit and its failure does not roll the
configuration back. It is a file in someone's `.git/hooks/`, not a config
value, and undoing saved settings because a hook could not be written would be
the more surprising behaviour.

DIRTY IS A COMPARISON, NOT A TRACE. Two sections fill their path entry from
auto-detection on a build timer (`settings_codegraph` at 200 ms,
`settings_pyscope`), so a trace-only implementation shows "unsaved changes" on
a page nobody has touched. Instead the controller snapshots every variable
once detection has settled and compares against it -- which also means typing
a change and undoing it returns the page to clean, as a user expects.
"""

from __future__ import annotations

import copy
import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import bind_mousewheel
from dialogs.settings_paths import PathsSection
from dialogs.settings_projects import ProjectsSection
from dialogs.settings_git import GitPolicySection
from dialogs.settings_ai import AISection
from dialogs.settings_codegraph import CodegraphSection
from dialogs.settings_pyscope import PyScopeSection
from dialogs.settings_mcp import McpStatusSection

if TYPE_CHECKING:
    from state import ManagerConfig


#: Page keys, in tab order. `open_settings(page=...)` takes one of these, so
#: they are a small public vocabulary rather than an internal detail.
PAGES = (
    ("paths",        "  Paths & Tools  "),
    ("projects",     "  Projects  "),
    ("git",          "  Git & Policy  "),
    ("ai",           "  AI  "),
    ("integrations", "  Integrations  "),
)

#: How long to wait before the page's variables become the clean baseline.
#: Must outlast every section's own build timer -- the longest is
#: `settings_codegraph`'s 200 ms status check, which auto-fills the path
#: entry. Anything shorter and opening Settings reports unsaved changes.
DETECTION_SETTLE_MS = 800

#: Stands in for a variable whose value cannot currently be read -- an
#: `IntVar` over an entry holding letters, say. A module constant so that two
#: `_values()` calls compare equal when nothing has actually changed.
_UNREADABLE = object()


def _scrollable(parent):
    """``(outer, body)`` -- a vertically scrolling page.

    Same Canvas + inner Frame shape the dialog used, once per page instead of
    once for all seven sections. Sections pack onto `body`.
    """
    outer = tk.Frame(parent, bg=C["base"])
    canvas = tk.Canvas(outer, bg=C["base"], highlightthickness=0, bd=0)
    bind_mousewheel(canvas)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    body = tk.Frame(canvas, bg=C["base"])
    window = canvas.create_window((0, 0), window=body, anchor="nw")
    body.bind("<Configure>",
              lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfigure(window, width=e.width))

    def _wheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind("<MouseWheel>", _wheel)
    body.bind("<MouseWheel>", _wheel)
    return outer, body


class SettingsTabController:
    """The Settings tab: five pages, one save transaction."""

    def __init__(self, notebook: "ttk.Notebook", cfg: "ManagerConfig", *,
                 host, save_fn, on_saved,
                 on_upgrade_tokensave, on_integration_check,
                 get_tokensave_versions) -> None:
        self._cfg = cfg
        self._host = host
        self._save_fn = save_fn
        self._on_saved = on_saved
        self._on_upgrade_tokensave = on_upgrade_tokensave
        self._on_integration_check = on_integration_check
        self._get_tokensave_versions = get_tokensave_versions

        self._sections: list = []
        self._pages: dict = {}
        self._baseline: list = []
        self._hydrating = True

        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  ⚙ Settings  ")
        self._notebook = notebook

        self._note = tk.Label(
            self._tab, text="", bg=C["red"], fg=C["mantle"],
            font=("Segoe UI", 9, "bold"), justify=tk.LEFT,
            padx=14, pady=8, wraplength=700)

        self._bar = tk.Frame(self._tab, bg=C["surface1"])
        tk.Label(self._bar, text="Unsaved changes", bg=C["surface1"],
                 fg=C["text"], font=("Segoe UI", 9, "bold"),
                 padx=12, pady=6).pack(side=tk.LEFT)
        ttk.Button(self._bar, text="Revert",
                   command=self._revert).pack(side=tk.RIGHT, padx=(0, 12),
                                              pady=4)
        ttk.Button(self._bar, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.RIGHT, padx=(0, 8), pady=4)

        self._inner = ttk.Notebook(self._tab)
        self._inner.pack(fill=tk.BOTH, expand=True)
        self._build_pages()

    # ── Public surface ───────────────────────────────────────────────────

    def select_page(self, page: str = "") -> None:
        """Show `page` (a key from `PAGES`); empty keeps the current one."""
        frame = self._pages.get(page)
        if frame is not None:
            self._inner.select(frame)

    def show_tab(self) -> None:
        """Bring the Settings tab itself to the front."""
        self._notebook.select(self._tab)

    def show_note(self, text: str) -> None:
        """Display a startup problem above the pages, or clear it."""
        if text:
            self._note.configure(text=text)
            self._note.pack(side=tk.TOP, fill=tk.X, before=self._inner)
        else:
            self._note.pack_forget()

    def is_dirty(self) -> bool:
        """True when the page differs from the last saved/reverted state.

        False while hydrating, by definition: nothing the user did produced
        those values. Without that, Revert would rebuild the pages and leave
        its own bar showing until the settle timer fired.
        """
        if self._hydrating:
            return False
        return self._values() != self._baseline

    # ── Composition ──────────────────────────────────────────────────────

    def _build_pages(self) -> None:
        frames = {}
        bodies = {}
        for key, title in PAGES:
            outer, body = _scrollable(self._inner)
            self._inner.add(outer, text=title)
            frames[key] = outer
            bodies[key] = body
        self._pages = frames

        host, cfg = self._host, self._cfg
        self._paths = PathsSection(
            host, bodies["paths"], cfg,
            open_tool_manager=self._open_tool_manager,
            on_upgrade_tokensave=self._on_upgrade_tokensave,
            on_integration_check=self._on_integration_check,
            get_tokensave_versions=self._get_tokensave_versions)
        self._build_tool_buttons(bodies["paths"])
        self._projects = ProjectsSection(host, bodies["projects"], cfg)
        self._git = GitPolicySection(host, bodies["git"], cfg)
        self._ai = AISection(host, bodies["ai"], cfg)
        self._codegraph = CodegraphSection(
            host, bodies["integrations"], cfg,
            open_tool_manager=self._open_tool_manager)
        self._pyscope = PyScopeSection(host, bodies["integrations"], cfg)
        self._mcp = McpStatusSection(host, bodies["integrations"], cfg)

        # Order is load-bearing: five pages now write into ONE staging dict,
        # and `tests/test_settings_tab.py` pins this sequence.
        self._sections = [self._paths, self._projects, self._git, self._ai,
                          self._codegraph, self._pyscope, self._mcp]
        for section in self._sections:
            section.bind_dirty(self._mark_dirty)

        self._hydrating = True
        self._host.after(DETECTION_SETTLE_MS, self._settle)

    def _build_tool_buttons(self, body) -> None:
        """The three manager dialogs, beside the paths that configure them.

        They used to live in the Help tab's left nav, which made Help the
        discovery surface for three actions that are not help.
        """
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20,
                                                      pady=(12, 8))
        tk.Label(body, text="Managers", font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20,
                                                  pady=(0, 4))
        row = tk.Frame(body, bg=C["base"])
        row.pack(anchor=tk.W, padx=20, pady=(0, 8))
        ttk.Button(row, text="\U0001f4be  Tool Manager…",
                   command=self._open_tool_manager).pack(side=tk.LEFT,
                                                         padx=(0, 6))
        ttk.Button(row, text="\U0001f9e9  Extension Manager…",
                   command=self._open_extension_manager).pack(side=tk.LEFT,
                                                              padx=(0, 6))
        ttk.Button(row, text="\U0001f9ea  Test Manager…",
                   command=self._open_test_manager).pack(side=tk.LEFT)

    # ── Dirty state ──────────────────────────────────────────────────────

    def _values(self) -> list:
        """Every section variable's current value, in a stable order.

        An unreadable value (an `IntVar` over an entry holding letters) is
        reported as a sentinel rather than raising: it is certainly not equal
        to the baseline, which is the honest answer.
        """
        out = []
        for index, section in enumerate(self._sections):
            for name, value in sorted(vars(section).items()):
                if isinstance(value, tk.Variable):
                    try:
                        out.append((index, name, value.get()))
                    except tk.TclError:
                        out.append((index, name, _UNREADABLE))
            # Optional extra state a section holds outside Tk variables --
            # ProjectsSection's root rows are the only case, and without this
            # adding a search root would leave the Save bar hidden.
            extra = getattr(section, "snapshot", None)
            if extra is not None:
                out.append((index, "snapshot", extra()))
        return out

    def _settle(self) -> None:
        """Adopt what the page shows as the clean baseline."""
        self._hydrating = False
        self._baseline = self._values()
        self._refresh_bar()

    def _mark_dirty(self) -> None:
        if self._hydrating:
            return
        self._refresh_bar()

    def _refresh_bar(self) -> None:
        if self.is_dirty():
            self._bar.pack(side=tk.BOTTOM, fill=tk.X, before=self._inner)
        else:
            self._bar.pack_forget()

    # ── Save / Revert ────────────────────────────────────────────────────

    def _save(self) -> None:
        staged = copy.deepcopy(self._cfg.raw)
        for section in self._sections:
            if not section.save_into(staged):
                return          # cfg.raw never touched; the page stays open
        raw = self._cfg.raw
        raw.clear()
        raw.update(staged)      # in place: other modules hold this dict

        # Filesystem side effect, deliberately after the commit and unable to
        # roll it back. See the module docstring.
        self._git.apply_hook()

        self._save_fn()
        self._baseline = self._values()
        self._refresh_bar()
        self._on_saved()

    def _revert(self) -> None:
        """Rebuild every page from `cfg.raw`, discarding pending edits."""
        self._hydrating = True
        current = ""
        try:
            selected = self._inner.select()
            for key, frame in self._pages.items():
                if str(frame) == str(selected):
                    current = key
                    break
        except tk.TclError:
            pass
        for frame in list(self._pages.values()):
            frame.destroy()
        self._build_pages()
        self.select_page(current)
        self._refresh_bar()

    # ── Dialog launchers (lazy imports, per the project rule) ────────────

    def _open_tool_manager(self) -> None:
        from dialogs.tool_manager import ToolManagerDialog
        ToolManagerDialog(self._host, self._cfg)

    def _open_extension_manager(self) -> None:
        from dialogs.extension_manager import ExtensionManagerDialog
        ExtensionManagerDialog(self._host, self._cfg)

    def _open_test_manager(self) -> None:
        from dialogs.test_manager import TestManagerDialog

        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        project_root = (raw.get("projects") or [{}])[0].get("path") or ""
        if not project_root or not os.path.isdir(project_root):
            from constants import _BASE_DIR
            project_root = _BASE_DIR
        TestManagerDialog(self._host, project_root, self._cfg)



def confirm_discard(parent) -> bool:
    """Ask whether to leave unsaved settings behind. True = go ahead."""
    return bool(messagebox.askyesno(
        "Unsaved settings",
        "Settings has unsaved changes.\n\nClose anyway and discard them?",
        parent=parent))
