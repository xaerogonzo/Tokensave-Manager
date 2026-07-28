"""HelpTabController — builds and manages the Help tab.

Extracted from App (Round 5 / App decomposition).

Dependency contract:
  • notebook      — ttk.Notebook to add the Help tab to
  • cfg           — read-only ManagerConfig (.template_dir, read at execution time)
  • on_seed_ask   — optional Callable[[str, str], None] — seeds a question into the Ask tab
  • on_llm_cfg    — optional Callable[[], dict]         — returns the current LLM config dict

All help content is static text.  The only cfg usage is in _help_file_locations,
which reads .template_dir at display time (not at __init__ time) so a
Settings save propagates without restarting.

Inline "🔍 Explain" streams a 3-5 sentence LLM summary via helpers/llm._call_llm.
"🤖 Ask" pre-fills a question in the Ask tab and fires the agent.
"📄 Open docs" opens the relevant markdown file in the system default viewer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Optional

from constants import C, _BASE_DIR
from controllers import (
    help_topics_basics,
    help_topics_git,
    help_topics_tools,
)

if TYPE_CHECKING:
    from state import ManagerConfig


# ── Module-level helpers ───────────────────────────────────────────────────────

def _open_doc(path: str) -> None:
    """Open a documentation file in the system default viewer (cross-platform)."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        try:
            subprocess.run(["open", path], check=True)
        except Exception:
            pass
    else:
        try:
            subprocess.run(["xdg-open", path], check=True)
        except Exception:
            from tkinter import messagebox
            messagebox.showwarning(
                "Cannot open file",
                f"Could not open the document automatically.\nPath: {path}",
            )


class HelpTabController:
    """Owns the Help tab: topic list + rich-text content pane."""

    def __init__(
        self,
        notebook: "ttk.Notebook",
        cfg: "ManagerConfig",
        *,
        on_seed_ask: Optional[Callable[[str, str], None]] = None,
        on_llm_cfg: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._cfg = cfg
        self._on_seed_ask = on_seed_ask
        self._on_llm_cfg = on_llm_cfg

        # Streaming explain state — monotonic request ID prevents zombie tokens
        self._explain_req_id: int = 0
        self._explain_running: bool = False
        self._current_explain_text: Optional[str] = None

        self._build(notebook)

    # ── Construction ──────────────────────────────────────────────────────────

    def _build(self, notebook: "ttk.Notebook") -> None:
        tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ── Left: topic list ──────────────────────────────────────────────────
        left_wrap = tk.Frame(pane, bg=C["base"])
        left_wrap.pack(side=tk.LEFT, fill=tk.Y)

        # v4.8: Tool Manager shortcut at the top of the left nav so it's
        # always discoverable regardless of which help section is open.
        ttk.Button(
            left_wrap, text="💾  Tool Manager…",
            command=self._open_tool_manager,
        ).pack(side=tk.TOP, fill=tk.X, padx=(0, 0), pady=(0, 6))

        # v4.13: Test Manager dialog (replaces the v4.12 "Run Smoke Tests"
        # button). Four tabs: Run+View, Coverage Gaps, Stale Tests,
        # Scaffold Generator. The old smoke-tests dialog still works
        # internally (it's reused by the V-E shared background helper),
        # but novice users get the full test-lifecycle UI here.
        ttk.Button(
            left_wrap, text="🧪  Test Manager…",
            command=self._open_test_manager,
        ).pack(side=tk.TOP, fill=tk.X, padx=(0, 0), pady=(0, 6))

        list_wrap = tk.Frame(left_wrap, bg=C["mantle"])
        list_wrap.pack(side=tk.TOP, fill=tk.Y, expand=True)

        self._help_lb = tk.Listbox(
            list_wrap, width=20, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Right: footer (pack BOTTOM first so content fills the rest) ───────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        self._footer = tk.Frame(right, bg=C["base"])
        self._footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

        self._doc_btn = ttk.Button(self._footer, text="📄 Open docs")
        self._explain_btn = ttk.Button(
            self._footer, text="🔍 Explain", command=self._help_explain_clicked
        )
        self._ask_btn = ttk.Button(self._footer, text="🤖 Ask")
        # Buttons are shown/hidden per section by _help_show(); don't pack here

        # ── Right: content ────────────────────────────────────────────────────
        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
            yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Text tags (shared across all sections) ────────────────────────────
        self._help_txt.tag_configure("h1",   font=("Segoe UI", 13, "bold"), foreground=C["blue"],
                                     spacing1=14, spacing3=6)
        self._help_txt.tag_configure("h2",   font=("Segoe UI", 10, "bold"), foreground=C["lavender"],
                                     spacing1=10, spacing3=2)
        self._help_txt.tag_configure("warn", font=("Segoe UI", 10, "bold"), foreground=C["yellow"])
        self._help_txt.tag_configure("ok",   font=("Segoe UI", 10, "bold"), foreground=C["green"])
        self._help_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._help_txt.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        self._help_txt.tag_configure("body", foreground=C["text"], spacing3=3)

        # ── Sections ──────────────────────────────────────────────────────────
        self._help_sections = [
            ("  Switching Projects",  self._help_switching),
            ("  Window & Tray",       self._help_window_tray),
            ("  Right-click Menu",    self._help_context_menu),
            ("  Scaffold",            self._help_scaffold),
            ("  Retrofit Existing",   self._help_retrofit),
            ("  Nuitka Builds",       self._help_nuitka),
            ("  Scaffold Column",     self._help_scaffold_column),
            ("  Auto-detect",         self._help_autodetect),
            ("  init vs sync",        self._help_init_vs_sync),
            ("  Project Categories",  self._help_categories),
            ("  Git: What & Why",     self._help_git_concepts),
            ("  Git: Daily Workflow", self._help_git_workflow),
            ("  Git Tab Buttons",     self._help_git_tab),
            ("  GitHub Setup",        self._help_github_setup),
            ("  CodeGraph",           self._help_codegraph),
            ("  AI Features",         self._help_ai_features),
            ("  Pre-commit Hook",     self._help_precommit_hook),
            ("  Run checks",          self._help_run_checks),
            ("  Integration check",   self._help_integration_check),
            ("  Settings reference",  self._help_settings_reference),
            ("  File Locations",      self._help_file_locations),
            ("  About",               self._help_about),
        ]
        for title, _ in self._help_sections:
            self._help_lb.insert(tk.END, title)

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)

        # Show first section on open
        self._help_lb.selection_set(0)
        self._help_sections[0][1]()

    # ── Event handling ────────────────────────────────────────────────────────

    def _on_help_select(self, _event=None) -> None:
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    # ── Rendering helpers ─────────────────────────────────────────────────────

    def _help_show(
        self,
        fn: Callable,
        *,
        doc_path: Optional[str] = None,
        ask_text: Optional[str] = None,
        explain_text: Optional[str] = None,
    ) -> None:
        """Clear the content pane, call fn() to fill it, lock + scroll to top. Wire footer."""
        # Invalidate any in-flight Explain stream for the previous section
        self._explain_req_id += 1
        self._current_explain_text = explain_text

        # Fill content
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        # Anchor mark so consecutive Explain runs can cleanly replace LLM output
        self._help_txt.mark_set("baseline_end", "end-1c")
        self._help_txt.mark_gravity("baseline_end", tk.LEFT)
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

        # ── "📄 Open docs" button ──────────────────────────────────────────────
        if doc_path and os.path.isfile(doc_path):
            self._doc_btn.configure(command=lambda p=doc_path: _open_doc(p))
            self._doc_btn.pack(side=tk.LEFT, padx=(0, 6))
        else:
            self._doc_btn.pack_forget()

        # ── "🔍 Explain" button ───────────────────────────────────────────────
        if explain_text:
            # Always reset to default label; DISABLED if a stream is still running
            btn_state = tk.DISABLED if self._explain_running else tk.NORMAL
            self._explain_btn.configure(text="🔍 Explain", state=btn_state)
            self._explain_btn.pack(side=tk.LEFT, padx=(0, 6))
        else:
            self._explain_btn.pack_forget()

        # ── "🤖 Ask" button ───────────────────────────────────────────────────
        if ask_text and self._on_seed_ask:
            self._ask_btn.configure(
                command=lambda t=ask_text: self._on_seed_ask(t, _BASE_DIR)  # type: ignore[misc]
            )
            self._ask_btn.pack(side=tk.LEFT)
        else:
            self._ask_btn.pack_forget()

    def _open_tool_manager(self) -> None:
        """v4.8: open the Tool Manager dialog from the Help tab nav.

        Lazy import so help_tab.py's import graph stays light. Uses
        the manager's top-level Tk window as parent so the dialog
        renders modally above all tabs.
        """
        from dialogs.tool_manager import ToolManagerDialog
        # The help_lb's toplevel is the App window — same root as Settings uses.
        try:
            root = self._help_lb.winfo_toplevel()
        except (tk.TclError, AttributeError):
            return
        ToolManagerDialog(root, self._cfg)

    def _open_test_manager(self) -> None:
        """Open the v4.13 Test Manager dialog.

        Resolves the project root from the manager's currently-active
        project. Lazy-imports the dialog so help_tab.py stays light.
        """
        import os
        from dialogs.test_manager import TestManagerDialog

        try:
            root = self._help_lb.winfo_toplevel()
        except (tk.TclError, AttributeError):
            return

        # Resolve the active project root from cfg. Same heuristic as
        # the old _run_smoke_tests handler.
        project_root = ""
        try:
            project_root = self._cfg.raw.get("projects", [{}])[0].get("path", "") or ""
        except Exception:
            project_root = ""
        if not project_root or not os.path.isdir(project_root):
            # Fall back to the manager-source dir so the dialog has
            # SOMETHING to scan (better than crashing on an empty
            # project_root).
            project_root = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )

        TestManagerDialog(root, project_root, self._cfg)

    def _help_explain_clicked(self) -> None:
        """Handle the Explain button click — runs on the main thread."""
        explain_text = self._current_explain_text
        if not explain_text:
            return

        llm_cfg: dict = self._on_llm_cfg() if self._on_llm_cfg else {}

        if not llm_cfg.get("enabled"):
            # No LLM configured — show dim hint in the content pane
            self._help_txt.configure(state=tk.NORMAL)
            try:
                self._help_txt.delete("baseline_end", tk.END)
            except tk.TclError:
                pass
            self._help_txt.insert(
                tk.END,
                "\n\nConfigure an LLM in Settings → AI / Commit to use inline explanations.",
                "dim",
            )
            self._help_txt.configure(state=tk.DISABLED)
            return

        # Increment ID to invalidate any previous stream
        self._explain_req_id += 1
        my_id = self._explain_req_id
        # Snapshot context on the main thread (up to 800 chars of static section text)
        ctx = self._help_txt.get("1.0", "baseline_end")[:800]

        self._explain_running = True
        self._explain_btn.configure(state=tk.DISABLED)

        t = threading.Thread(
            target=self._help_explain_worker,
            args=(my_id, ctx, explain_text, llm_cfg),
            daemon=True,
        )
        t.start()

    def _help_explain_worker(
        self,
        req_id: int,
        ctx: str,
        explain_text: str,
        llm_cfg: dict,
    ) -> None:
        """Background thread: stream an LLM explanation into the content pane."""
        from helpers.llm import _call_llm  # lazy import — avoids circular at startup

        system = (
            "You are a concise help assistant for TokenSave Manager. "
            "Explain this topic clearly with a practical example. 3-5 sentences."
        )
        user = f"Topic: {explain_text}\n\nContext:\n{ctx}"

        stream_state = {"first": True}

        def on_token(tok: str) -> None:
            is_first = stream_state["first"]
            stream_state["first"] = False

            def _put(t: str = tok, req: int = req_id, first: bool = is_first) -> None:
                if self._explain_req_id != req:
                    return  # zombie token from abandoned stream — discard
                self._help_txt.configure(state=tk.NORMAL)
                if first:
                    try:
                        self._help_txt.delete("baseline_end", tk.END)
                    except tk.TclError:
                        pass
                    self._help_txt.insert(tk.END, "\n\n", "body")
                self._help_txt.insert(tk.END, t, "dim")
                self._help_txt.see(tk.END)
                self._help_txt.configure(state=tk.DISABLED)

            self._help_txt.after(0, _put)

        try:
            result = _call_llm(
                llm_cfg, system, user,
                max_tokens=300, timeout=30, on_token=on_token,
            )

            # Non-streaming fallback: provider completed without calling on_token
            if stream_state["first"]:
                if result:
                    def _put_result(r: str = result, req: int = req_id) -> None:
                        if self._explain_req_id != req:
                            return
                        self._help_txt.configure(state=tk.NORMAL)
                        try:
                            self._help_txt.delete("baseline_end", tk.END)
                        except tk.TclError:
                            pass
                        self._help_txt.insert(tk.END, "\n\n" + r, "dim")
                        self._help_txt.see(tk.END)
                        self._help_txt.configure(state=tk.DISABLED)
                    self._help_txt.after(0, _put_result)
                else:
                    def _put_none(req: int = req_id) -> None:
                        if self._explain_req_id != req:
                            return
                        self._help_txt.configure(state=tk.NORMAL)
                        try:
                            self._help_txt.delete("baseline_end", tk.END)
                        except tk.TclError:
                            pass
                        self._help_txt.insert(
                            tk.END,
                            "\n\nLLM did not return a response. Check Settings → AI / Commit.",
                            "dim",
                        )
                        self._help_txt.configure(state=tk.DISABLED)
                    self._help_txt.after(0, _put_none)

        except Exception as e:
            err = str(e)

            def _show_err(m: str = err, req: int = req_id) -> None:
                if self._explain_req_id != req:
                    return  # error from abandoned stream — discard silently
                self._help_txt.configure(state=tk.NORMAL)
                try:
                    self._help_txt.delete("baseline_end", tk.END)
                except tk.TclError:
                    pass
                self._help_txt.insert(tk.END, f"\n\nError: {m}", "warn")
                self._help_txt.configure(state=tk.DISABLED)

            self._help_txt.after(0, _show_err)

        finally:
            def _cleanup(req: int = req_id) -> None:
                self._explain_running = False
                try:
                    if self._explain_btn.winfo_ismapped():
                        self._explain_btn.configure(state=tk.NORMAL)
                except tk.TclError:
                    pass

            self._help_txt.after(0, _cleanup)

    def _hw(self):
        """Return (h1, h2, p, warn, ok, dim, br, ins) writer helpers for _help_txt."""
        t = self._help_txt
        def h1(s):       t.insert(tk.END, s + "\n", "h1")
        def h2(s):       t.insert(tk.END, s + "\n", "h2")
        def p(s):        t.insert(tk.END, s + "\n", "body")
        def warn(s):     t.insert(tk.END, s + "\n", "warn")
        def ok(s):       t.insert(tk.END, s + "\n", "ok")
        def dim(s):      t.insert(tk.END, s + "\n", "dim")
        def br():        t.insert(tk.END, "\n")
        def ins(s, tag): t.insert(tk.END, s, tag)
        return h1, h2, p, warn, ok, dim, br, ins

    # ── Help sections ─────────────────────────────────────────────────────────

    def _help_switching(self):
        help_topics_basics.switching(self)

    def _help_window_tray(self):
        help_topics_basics.window_tray(self)

    def _help_context_menu(self):
        help_topics_basics.context_menu(self)

    def _help_scaffold(self):
        help_topics_basics.scaffold(self)

    def _help_retrofit(self):
        help_topics_basics.retrofit(self)

    def _help_nuitka(self):
        help_topics_basics.nuitka(self)

    def _help_scaffold_column(self):
        help_topics_basics.scaffold_column(self)

    def _help_autodetect(self):
        help_topics_basics.autodetect(self)

    def _help_init_vs_sync(self):
        help_topics_basics.init_vs_sync(self)

    def _help_categories(self):
        help_topics_basics.categories(self)

    def _help_git_concepts(self):
        help_topics_git.git_concepts(self)

    def _help_git_workflow(self):
        help_topics_git.git_workflow(self)

    def _help_git_tab(self):
        help_topics_git.git_tab(self)

    def _help_github_setup(self):
        help_topics_git.github_setup(self)

    def _help_codegraph(self):
        help_topics_tools.codegraph(self)

    def _help_ai_features(self):
        help_topics_tools.ai_features(self)

    def _help_precommit_hook(self):
        help_topics_tools.precommit_hook(self)

    def _help_run_checks(self):
        help_topics_tools.run_checks(self)

    def _help_integration_check(self):
        help_topics_tools.integration_check(self)

    def _help_settings_reference(self):
        help_topics_tools.settings_reference(self)

    def _help_file_locations(self):
        help_topics_tools.file_locations(self)

    def _help_about(self):
        help_topics_tools.about(self)
