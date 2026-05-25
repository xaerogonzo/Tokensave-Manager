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

from constants import C, LOG_FILE, _BASE_DIR, _CONFIG_PATH

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
        list_wrap = tk.Frame(pane, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

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
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Switching Projects")
            warn("⚠  You must restart Claude Desktop after switching the active project.")
            br()
            p("The tokensave wrapper script runs once when Claude Desktop launches. It "
              "reads the active project at startup and stays locked to it for that "
              "session. Changing the pin (★ Set as Active) writes to a config file, "
              "but the already-running server won't pick it up until Claude Desktop "
              "restarts.")
            br()
            p("Workflow for switching:")
            ins("  1. Select the new project in the list\n", "body")
            ins("  2. Click ★ Set as Active\n", "body")
            ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
            ins("  4. Relaunch Claude Desktop\n", "body")
            br()
            p("Tip: to go back to whichever project you last synced automatically, "
              "click Auto-detect instead of pinning a specific project.")
        self._help_show(_fill)

    def _help_window_tray(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Window & Tray")
            p("TokenSave Manager runs in the system tray so it can stay alive between "
              "sessions without cluttering the taskbar.")
            br()
            h2("Window controls")
            ins("  ╳  Close (X button)  ", "body"); ins("Hides the window to the system tray — the app keeps running.\n", "dim")
            ins("  _  Minimize           ", "body"); ins("Minimizes normally to the taskbar.\n", "dim")
            ins("  Tray icon → Show      ", "body"); ins("Restores the window to its last position and size.\n", "dim")
            ins("  Tray icon → Quit      ", "body"); ins("Fully exits the app.\n", "dim")
            br()
            p("The window position and size are saved automatically when you hide to tray "
              "and restored the next time you click Show.")
            br()
            h2("Claude CLI model")
            p("Settings → Claude Code CLI → Model controls which Claude model the manager "
              "uses for its automated background calls: pre-commit AI review, the Suggest "
              "button's Claude CLI strategy, and Draft PR via CLI.")
            br()
            ins("  Haiku 4.5 (default)  ", "body"); ins("Fast (3–5 s), cheap, sufficient for code review and commit messages.\n", "dim")
            ins("  Sonnet 4.6           ", "body"); ins("Balanced — slower but catches more nuance in reviews.\n", "dim")
            ins("  Opus 4.7             ", "body"); ins("Slow (20–40 s on large diffs) but deepest analysis.\n", "dim")
            ins("  (empty)              ", "body"); ins("Use whatever ~/.claude/settings.json defaults to.\n", "dim")
            br()
            warn("This setting does NOT affect interactive 'claude' sessions you launch "
                 "from the terminal or the Reference tab — those still use your global default.")
        self._help_show(_fill)

    def _help_context_menu(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Right-click Menu")
            p("Right-click any row in the project list for per-project actions. "
              "Global actions are in the toolbar at the bottom.")
            br()

            h2("Toolbar buttons")
            ins("  ＋  Scaffold          ", "body"); ins("Open the scaffold dialog for a folder\n", "dim")
            ins("  ⚙  Retrofit Existing  ", "body"); ins("Add tokensave rules to an existing project\n", "dim")
            ins("  ↺↺ Sync All           ", "body"); ins("Sync every indexed project sequentially\n", "dim")
            ins("  ⟳  Refresh            ", "body"); ins("Manually refresh the list (auto-refreshes every 60 s)\n", "dim")
            br()

            h2("Index management")
            ins("  ★  Set as Active  ", "body"); ins("Pin this project for Claude Desktop (restart Claude to apply)\n", "dim")
            ins("  ↺  Sync           ", "body"); ins("Incrementally re-index changed files\n", "dim")
            ins("  📊  Status         ", "body"); ins("Show node/edge/file counts and last sync time in a popup\n", "dim")
            ins("  ⟳  Force Re-sync  ", "body"); ins("Rebuild the entire code graph from scratch\n", "dim")
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health for this project\n", "dim")
            br()

            h2("CodeGraph")
            ins("  🧠  CodeGraph Init          ", "body"); ins("Build the CodeGraph index for this project\n", "dim")
            ins("  🧠  CodeGraph Sync          ", "body"); ins("Incrementally update the CodeGraph index\n", "dim")
            ins("  🧠  CodeGraph Status        ", "body"); ins("Show CodeGraph node and edge counts\n", "dim")
            ins("  🧠  Remove CodeGraph Index… ", "body"); ins("Delete the CodeGraph index (project files untouched)\n", "dim")
            br()

            h2("Git & AI")
            ins("  📜  Git Log                    ", "body"); ins("Show last 20 commits + working-tree status (read-only view)\n", "dim")
            ins("  📝  Git Commit…                ", "body"); ins("Open the commit dialog with AI-suggested message\n", "dim")
            ins("  🔍  AI Code Review…            ", "body"); ins("Stream a severity-coloured AI review of your staged diff\n", "dim")
            ins("  🔧  Git Init                   ", "body"); ins("Initialise a new git repository in the project folder\n", "dim")
            ins("  📋  Manage .gitignore…         ", "body"); ins("Add, remove, or view .gitignore rules interactively\n", "dim")
            ins("  🧹  Untrack Ignored Files…     ", "body"); ins("Remove already-tracked files that .gitignore now covers\n", "dim")
            ins("  🔍  Pre-commit AI Review hook… ", "body"); ins("Install or remove the AI review hook for this project\n", "dim")
            ins("  📝  Draft CHANGELOG entry…     ", "body"); ins("Use AI to generate a CHANGELOG bullet from recent changes\n", "dim")
            ins("  🔬  Refactor scout…            ", "body"); ins("AI-powered complexity and duplication analysis\n", "dim")
            ins("  ✓  Run checks…                ", "body"); ins("Run syntax, pyflakes, Doctor, and optional Claude review\n", "dim")
            ins("  🔄  Integration check          ", "body"); ins("Run the tokensave integration audit (version + snippets)\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup & organisation")
            ins("  ⚙  Retrofit…          ", "body"); ins("Open the Retrofit dialog pre-filled with this project\n", "dim")
            ins("  🔗  Shadow Links…     ", "body"); ins("Manage symlink mirrors for sharing files between projects\n", "dim")
            ins("  📁  Assign Category…  ", "body"); ins("Override this project's group label and sub-category\n", "dim")
            ins("  🗑  Remove Index…     ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect           ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
        self._help_show(_fill)

    def _help_scaffold(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("＋  Scaffold")
            p("Pick any folder — empty or existing — and choose what to create:")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md  ", "body"); ins("— project template for Claude\n", "dim")
            ins("  Run tokensave init             ", "body"); ins("— build the code graph (~10–30 s)\n", "dim")
            ins("  Add Nuitka build files         ", "body"); ins("— copies build.ps1 + build.bat\n", "dim")
            br()
            p("While init runs the project appears in the list immediately as '(indexing…)'. "
              "Claude reads BASIC_INSTRUCTIONS.md on first session and adapts to whatever "
              "structure already exists.")
            br()
            p("If the folder already has a tokensave index, 'Run tokensave init' is "
              "unchecked by default. If BASIC_INSTRUCTIONS.md already exists, the "
              "checkbox notes it will be overwritten.")
        self._help_show(_fill)

    def _help_retrofit(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("⚙  Retrofit Existing")
            p("Add tokensave wiring to a project that already exists — without "
              "touching any of its current files destructively.")
            br()
            ins("  Add tokensave rules to CLAUDE.md  ", "body")
            ins("— prepends a single @include line.\n", "dim")
            ins("                                   ", "body")
            ins("  Non-destructive: all existing content is kept.\n", "dim")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md      ", "body")
            ins("— optional project template for Claude.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if the file already exists.\n", "dim")
            br()
            ins("  Add Nuitka build files            ", "body")
            ins("— copies build.ps1 + build.bat.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if build.ps1 already exists.\n", "dim")
            br()
            p("After applying, a summary popup lists exactly what was created or skipped.")
        self._help_show(_fill)

    def _help_nuitka(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Nuitka Build Files")
            p("Both Scaffold and Retrofit Existing have an 'Add Nuitka build files' "
              "checkbox. When ticked, two files are copied from the templates folder "
              "into the target project:")
            br()
            ins("  build.ps1  ", "body"); ins("— full Nuitka build script (PowerShell)\n", "dim")
            ins("  build.bat  ", "body"); ins("— one-line launcher that calls build.ps1\n", "dim")
            br()
            p("After applying, open build.ps1 and fill in the two remaining placeholders:")
            br()
            ins("  [ENTRY_SCRIPT]  ", "code"); ins("— path to your main .py file (relative to build.ps1)\n", "dim")
            ins("  [OUTPUT_NAME]   ", "code"); ins("— the desired .exe filename\n", "dim")
            ins("  [PROJECT_NAME]  ", "code"); ins("— already filled in from your folder name\n", "dim")
            br()
            p("Then double-click build.bat to compile. Read NUITKA_GOTCHAS.md (in the "
              "templates folder) for known pitfalls before your first build.")
            br()
            warn("Tip (Claude Code users):  ")
            ins("if you already have a project open in Claude Code you can skip the "
                "button entirely — just tell Claude: 'Set up a Nuitka build pipeline. "
                "Entry script is src/main.py, output name my-tool.exe.'\n"
                "Claude reads the Nuitka instructions from project-baseline.md via "
                "@include and will copy + fill in the templates automatically.", "body")
        self._help_show(_fill)

    def _help_scaffold_column(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Scaffold Column")
            p("The 'Scaffold' column in the project list shows whether "
              "BASIC_INSTRUCTIONS.md has been created for each project.")
            br()
            ok("✔  BASIC_INSTRUCTIONS.md exists")
            br()
            ins("—  ", "warn"); ins("Not yet scaffolded — use ＋ Scaffold or ⚙ Retrofit Existing\n", "body")
            br()
            p("The column only checks for BASIC_INSTRUCTIONS.md. It does not indicate "
              "whether CLAUDE.md has the @include line or whether Nuitka build files "
              "are present.")
        self._help_show(_fill)

    def _help_autodetect(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("How Auto-detect Works")
            p("The wrapper script (tokensave-wrapper.py / tokensave-wrapper.exe) "
              "runs at Claude Desktop startup and decides which project to serve:")
            br()
            ins("  1. ", "body"); ins("Checks desktop-project.txt — uses that path if present and valid\n", "dim")
            ins("  2. ", "body"); ins("Otherwise scans project roots for .tokensave/tokensave.db files\n", "dim")
            ins("  3. ", "body"); ins("Picks the one with the most recent modification time\n", "dim")
            ins("  4. ", "body"); ins("Starts: tokensave.exe serve -p <chosen path>\n", "dim")
            br()
            p("Running ↺ Sync on a project updates its database timestamp, so the next "
              "Auto-detect restart will naturally pick it up.")
            br()
            p("'Auto-detect' in the right-click menu clears the pin file, switching "
              "back to automatic selection on the next Claude Desktop restart.")
        self._help_show(_fill)

    def _help_init_vs_sync(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("init vs sync")
            h2("tokensave init")
            p("Full first-time index of a project. Run once when setting up a new "
              "project. Builds the complete code graph from scratch. Can take a few "
              "minutes for large codebases.")
            br()
            h2("tokensave sync")
            p("Incremental update — only re-indexes files that changed since the last "
              "run. Fast. Run this any time you want to update the index after making "
              "code changes, or to make Auto-detect pick this project on the next "
              "Claude Desktop restart.")
            br()
            p("The ↺ Sync button in the right-click menu runs 'sync'. If the project "
              "has no index yet, it asks whether to run 'init' instead.")
        self._help_show(_fill)

    def _help_categories(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Project Categories")
            p("Projects are automatically grouped under the label of the search root "
              "folder they belong to. You can override any project's category — and add "
              "an optional sub-category — without moving any files.")
            br()
            h2("How root labels work")
            p("Each entry in Settings → Search Roots has a Label. That label becomes "
              "the category header for all projects found inside that folder. Edit the "
              "label in Settings to rename the whole group at once.")
            br()
            h2("Overriding a single project")
            ins("  1. Right-click the project row\n", "body")
            ins("  2. Choose  📁 Assign Category…\n", "body")
            ins("  3. Pick or type a Category (and optional Sub-category)\n", "body")
            ins("  4. Click OK — the project moves to the new group immediately\n", "body")
            br()
            p("To remove an override and return the project to its root's group, "
              "open Assign Category… and click Clear Override.")
            br()
            h2("Sub-categories")
            p("Sub-categories appear indented under their parent category (shown as "
              "↳ Sub-category). They work like folders-within-folders. Right-click "
              "any project at any time to move it between groups.")
            br()
            warn("⚠  Category headers and sub-category rows are not selectable — "
                 "right-click and action buttons only work on project rows.")
        self._help_show(_fill)

    def _help_git_concepts(self):
        _doc = os.path.join(_BASE_DIR, "docs", "GITHUB_GUIDE.md")
        _ask = "Explain git fundamentals — commit, branch, push, pull — for someone who has never used git"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: What & Why")
            p("Git is a tool that remembers the history of every change you make to "
              "your project. Think of it like infinite undo — but smarter. You decide "
              "when to save a checkpoint, and you can always go back.")
            br()
            h2("Commit — a save point")
            p("A commit is a snapshot of your project at a moment in time. Each one "
              "has a short message you write, like 'fix: typo in README' or "
              "'feat: add dark mode'. Over time, these build up into a history "
              "you can scroll through.")
            br()
            h2("Repository (repo) — the project folder + its history")
            p("When you run Git Init on a project, git creates a hidden .git folder "
              "inside it. That folder stores every commit ever made. The whole thing "
              "— your files plus that history — is called a repository.")
            br()
            h2("Branch — a parallel version")
            p("Imagine photocopying your project so you can experiment on the copy "
              "without touching the original. That's a branch. When you're happy "
              "with the experiment, you can merge it back. The default branch is "
              "usually called 'master' or 'main'.")
            br()
            h2("Remote — a copy on GitHub")
            p("A remote is a second home for your repository, stored on GitHub's "
              "servers. It acts as a backup and lets others see your work. The "
              "remote is usually called 'origin'.")
            br()
            h2("Push — upload to GitHub")
            p("After making commits on your machine, Push sends them to GitHub. "
              "Nothing leaves your computer until you Push — commits are purely local "
              "until then.")
            br()
            h2("Pull — download from GitHub")
            p("Pull fetches any commits from GitHub that you don't have yet and "
              "adds them to your local history. Useful if you work on multiple "
              "machines, or if a collaborator pushed something new.")
            br()
            h2("Working tree — uncommitted changes")
            p("The working tree is the current state of your files right now, before "
              "you've committed them. The Git tab shows a list of files that have "
              "changed since your last commit. An 'M' means modified, '?' means "
              "a new file git hasn't seen before, 'D' means deleted.")
            br()
            h2("Staging — choosing what to commit")
            p("Git lets you pick exactly which changes to include in a commit. "
              "The 'Stage all changes' checkbox in the Commit dialog does this "
              "automatically — it stages everything in the working tree, which is "
              "almost always what you want.")
            br()
            ok("Bottom line: commit often, push when you're done for the day.")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    def _help_git_workflow(self):
        _doc = os.path.join(_BASE_DIR, "docs", "GITHUB_GUIDE.md")
        _ask = "Walk me through the daily git workflow in this manager step by step"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: Daily Workflow")
            p("Here's how a typical coding session looks when using the Git tab.")
            br()
            h2("Starting a session")
            ins("  1. Switch to the Git tab\n", "body")
            ins("  2. Click ⟳ Refresh to see the current state\n", "body")
            ins("  3. If there's a remote set, click ⬇ Pull first — picks up any\n"
                "     changes from GitHub before you start editing\n", "body")
            br()
            h2("While you're working")
            p("Edit your files normally. The Working Tree list updates whenever "
              "you Refresh. Click any file in the list to see exactly what changed "
              "(green = added, red = removed).")
            br()
            h2("Saving your work (committing)")
            ins("  1. Click  📝 Commit…\n", "body")
            ins("  2. The dialog shows what files changed and suggests a message\n", "body")
            ins("  3. Edit the message if you like — keep it short and descriptive\n", "body")
            ins("  4. Click Commit\n", "body")
            br()
            p("There's no rule for how often to commit. A good rule of thumb: "
              "commit whenever you finish one thing. Small commits are better than "
              "one huge commit at the end of the day.")
            br()
            h2("Uploading to GitHub (pushing)")
            ins("  1. Click  ⬆ Push\n", "body")
            ins("  2. The output log shows whether it succeeded\n", "body")
            ins("  3. Your commits are now on GitHub — backed up and shareable\n", "body")
            br()
            h2("Trying out an idea safely (branching)")
            ins("  1. Click  🌿 New Branch  and give it a name (e.g. 'try-new-ui')\n", "body")
            ins("  2. Check 'Switch to this branch immediately'\n", "body")
            ins("  3. Make your changes and commit as normal\n", "body")
            ins("  4. If you don't like it: 🔀 Switch Branch back to master — the\n"
                "     experiment branch stays there but your main code is untouched\n", "body")
            br()
            h2("Finishing a feature branch (merge & cleanup)")
            p("Once your branch is tested and ready to bring back into master:")
            ins("  1.  🔀 Switch Branch  → master\n", "body")
            ins("  2.  ⬇ Pull            — pick up any new master commits first\n", "body")
            ins("  3.  ⇄ Merge…          → pick your feature branch\n", "body")
            ins("                          Confirmation says 'Merge X INTO master?' — yes\n", "body")
            ins("  4.  ⬆ Push            — master with the merged commits goes to GitHub\n", "body")
            ins("  5.  🗑 Delete Branch  → pick your feature branch → Yes (local)\n", "body")
            ins("                          Then: 'Also delete from GitHub?' → Yes\n", "body")
            br()
            p("If the merge produces conflicts, the manager pops a dialog telling "
              "you what to do (resolve in editor + commit, or run "
              "'git merge --abort' to undo). Conflicts only happen when both "
              "branches changed the same lines.")
            br()
            h2("Undoing mistakes")
            p("Made a bad commit? Click  ↩ Undo Last Commit. Your changes come back "
              "as uncommitted edits — you can fix them and recommit, or just discard.")
            br()
            warn("⚠  Undo Last Commit only removes the last commit. To undo older "
                 "commits, use the terminal.")
            br()
            h2("Typical day in one line")
            dim("  Pull → Edit → Commit → Edit → Commit → Push")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    def _help_git_tab(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git Tab")
            p("The Git tab shows live status for whichever project is selected in the "
              "Projects tab. It updates automatically when you switch projects or switch "
              "to this tab.")
            br()
            h2("Working Tree & Diff")
            p("The Working Tree panel lists every modified, added, or deleted file. "
              "Click any file to see its diff below — added lines are green, removed "
              "lines are red.")
            br()
            h2("Committing changes")
            ins("  1. Make your edits (in your editor, or via Claude)\n", "body")
            ins("  2. Click  📝 Commit… — the dialog opens with a suggested message\n", "body")
            ins("  3. Edit the message if you like, then click Commit\n", "body")
            br()
            p("The suggested message is generated from your staged changes, using "
              "a chain of strategies — highest-quality first:")
            ins("    1. CHANGELOG.md bullets (if you've added an entry)\n", "body")
            ins("    2. Diff content — added Python defs/classes, file kinds\n", "body")
            ins("    3. File-name patterns (legacy fallback)\n", "body")
            p("Each result is sanitised (subject ≤ 72 chars, imperative mood, "
              "no filename listings). When AI is enabled in Settings, an "
              "Anthropic / OpenAI / LM Studio / Ollama call runs first — silent "
              "fallback to heuristics on any failure. Click 💡 Suggest at any "
              "time to regenerate.")
            br()
            h2("Undo Last Commit")
            p("Removes the most recent commit but keeps all your changes staged — "
              "nothing is deleted. Safe to use if you committed too early or with "
              "the wrong message.")
            br()
            h2("Branches")
            ins("  🌿 New Branch    — create a branch and optionally switch to it\n", "body")
            ins("  🔀 Switch Branch — pick a branch from the list to check out\n", "body")
            ins("  ⇄ Merge…         — merge another branch INTO the current one\n", "body")
            ins("                     (use after switching to master to pull a finished feature back in)\n", "body")
            ins("  🗑 Delete Branch — safe-delete locally; then offers to also delete from GitHub\n", "body")
            ins("                     (only prompts about GitHub if a remote copy actually exists)\n", "body")
            br()
            warn("⚠  Switching branches with uncommitted changes will fail. "
                 "Commit or undo first.")
            br()
            warn("⚠  Merging with uncommitted changes also fails. Same fix — commit, "
                 "stash, or undo first.")
            br()
            h2("Push & Pull")
            p("Push and Pull are only enabled once a remote (GitHub URL) is set. "
              "Use  Set Remote  or the  🐙 GitHub…  wizard to connect to GitHub first.")
        self._help_show(_fill)

    def _help_github_setup(self):
        _doc = os.path.join(_BASE_DIR, "docs", "GITHUB_GUIDE.md")
        _ask = "How do I connect a project to GitHub and create my first release?"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("GitHub Setup")
            p("The  🐙 GitHub…  button in the Git tab header opens a step-by-step "
              "wizard for getting your project onto GitHub — even if you've never used "
              "GitHub before.")
            br()
            h2("Step 1 — Git identity")
            p("Every commit is stamped with your name and email. The wizard shows your "
              "current global settings and lets you update them. These are stored in "
              "your global git config and apply to every project on this machine.")
            br()
            h2("Step 2 — Create a GitHub account")
            p("Free at github.com. The wizard has a button to open the sign-up page.")
            br()
            h2("Step 3 — Create a repository")
            p("Go to github.com/new. Give it a name, leave it Public. "
              "Do NOT check 'Add README' or 'Add .gitignore' — you already have those. "
              "Copy the HTTPS URL shown after creation (e.g. "
              "https://github.com/you/my-project.git).")
            br()
            h2("Step 4 — Paste the URL")
            p("Paste the URL into the wizard and click Set. This tells git where to "
              "send your code. The Git tab's Remote label will update immediately.")
            br()
            h2("Step 5 — Push")
            p("Click ⬆ Push to GitHub. The first time, a browser window opens asking "
              "you to log in to GitHub — this is Git Credential Manager doing its job. "
              "Log in once and future pushes happen silently.")
            br()
            warn("⚠  If Push fails with an authentication error, open a terminal in "
                 "the project folder and run:  git push\n"
                 "This triggers the browser login. After that, the Push button works normally.")
            br()
            h2("📦 GitHub Releases")
            p("A Release lets anyone download your .exe without needing Python "
              "installed. To create one:")
            ins("  1. Run build.bat to compile dist\\tokensave-manager.exe\n", "body")
            ins("  2. Open  🐙 GitHub…  and scroll to the Releases section\n", "body")
            ins("  3. Enter a version tag (e.g. v1.0.0) and a title\n", "body")
            ins("  4. Click  📦 Create Release — the .exe files are uploaded automatically\n", "body")
            br()
            p("Releases require the GitHub CLI (gh). If it's not installed, "
              "the wizard shows a link to cli.github.com.")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    def _help_codegraph(self):
        _doc = os.path.join(_BASE_DIR, "README.md")
        _ask = "Explain CodeGraph: what it does, how it compares to tokensave, when to use each"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("CodeGraph (alternative code-graph tool)")
            p("CodeGraph is a separate MCP server that does what tokensave does — "
              "builds a per-project code-graph index and exposes it to Claude Code. "
              "The two don't conflict; a project can have both at once.")
            br()
            h2("When to use which")
            ins("  • tokensave — bundled with the manager; full-featured; manual sync\n", "body")
            ins("  • CodeGraph — auto-syncs while its MCP server is running; faster\n", "body")
            ins("                for very large codebases (e.g. 25k-file repos)\n", "body")
            br()
            warn("⚠  About CodeGraph's auto-sync: the file watcher only runs while "
                 "CodeGraph's MCP server is active inside an open Claude Code session. "
                 "If you edit code with Claude Code closed, those edits won't be "
                 "picked up automatically until the next session — at which point "
                 "the watcher catches up. You can also right-click → 🧠 CodeGraph Sync "
                 "to force an incremental update manually.")
            br()
            h2("Install")
            ins("  Settings → CodeGraph → Install via npm  ", "body")
            ins("(requires Node.js 18+)\n", "dim")
            br()
            h2("Use")
            ins("  Right-click any project → 🧠 CodeGraph Init  →  then 🧠 Sync / Status\n", "body")
            ins("  CG column in the Projects tab shows ✓ for initialised projects.\n", "body")
            br()
            h2("Why the manager doesn't run `codegraph install`")
            p("CodeGraph registers itself with Claude Code (and Cursor / Codex / "
              "opencode if you use them) via its own one-time installer: "
              "`npx @colbymchenry/codegraph`. The TokenSave Manager intentionally "
              "stays out of that flow — we handle per-project lifecycle only "
              "(init / sync / status / remove). This means tokensave and CodeGraph "
              "can both write their own sections into your global ~/.claude.json "
              "without fighting each other.")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    # ── New sections ───────────────────────────────────────────────────────────

    def _help_ai_features(self):
        _doc = os.path.join(_BASE_DIR, "README.md")
        _ask = "Explain all the AI features in TokenSave Manager — code review, Ask tab, Ollama, smart commit messages, Claude CLI integration"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("AI Features")
            p("TokenSave Manager integrates AI at several points. All AI features are "
              "optional and fail open — if the AI call fails, the operation continues "
              "without it.")
            br()

            h2("🔍 AI Code Review")
            p("Right-click a project → 🔍 AI Code Review… — streams a severity-coloured "
              "review of your staged or HEAD diff. Uses the configured LLM or Claude CLI. "
              "Results appear in a live-updating dialog with icons:")
            ins("  ✗ critical  ⚠ warning  ℹ info  ✓ pass\n", "body")
            br()

            h2("Ask Tab (bounded agent)")
            p("An embedded 8-iteration agent that answers questions about your codebase. "
              "Type a question, pick a project, click Ask. The agent has six read-only "
              "tokensave tools (context, search, callers, callees, body, outline) and "
              "stays bounded to prevent runaway token spend. Results stream line-by-line.")
            br()

            h2("Ollama Model Manager")
            p("Settings → Ollama → Manage Models — browse, pull, and delete local Ollama "
              "models without leaving the manager. Models listed here are available for AI "
              "commit messages, AI code review, pre-commit hook review, and the inline "
              "Explain button in this Help tab.")
            br()

            h2("💡 Smart commit messages")
            p("The 📝 Commit… dialog uses a multi-strategy chain to suggest a message:")
            ins("  1. CHANGELOG.md bullets (if you added an entry today)\n", "body")
            ins("  2. Diff content — added/changed definitions, file types\n", "body")
            ins("  3. File-name heuristics (legacy fallback)\n", "body")
            p("When an AI backend is configured, an AI step runs first and the chain "
              "falls back to heuristics silently on any failure. Click 💡 Suggest to "
              "regenerate at any time. Configure the backend in Settings → Commit Message.")
            br()

            h2("Claude CLI integration")
            p("Settings → Claude Code CLI → Exe path wires in the claude binary. "
              "The manager uses it for:")
            ins("  • 💡 Suggest strategy in the commit dialog\n", "body")
            ins("  • 🔍 AI Code Review streaming\n", "body")
            ins("  • 📝 Draft CHANGELOG entry\n", "body")
            ins("  • 🐙 Draft PR description\n", "body")
            ins("  • ✓ Run checks → Claude review step\n", "body")
            br()

            h2("📊 Cost tracking")
            p("The 📊 Cost button (bottom of the main window) shows a running tally of "
              "API tokens and estimated cost for this session. Covers all LLM calls made "
              "through the manager's own LLM helper — commit message suggestions, code "
              "reviews, pre-commit hook, and inline Explain in this Help tab.")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    def _help_precommit_hook(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Pre-commit AI Review Hook")

            h2("What it does")
            p("Installs a git pre-commit hook that runs an AI review of your staged diff "
              "before every commit. If the review finds critical issues it warns you — the "
              "commit still proceeds (fail-open guarantee). The review runs in the "
              "background using the configured LLM or Claude CLI backend.")
            br()

            h2("Installing the hook")
            p("Right-click any project → 🔍 Pre-commit AI Review hook… → choose Install. "
              "The dialog shows the exact hook script that will be written. You can also "
              "choose Remove to uninstall without touching anything else.")
            br()

            h2("Backends (auto-selected by priority)")
            ins("  1. auto (default)  ", "body")
            ins("— tries Claude CLI first, falls back to LLM, then skips if neither\n", "dim")
            ins("  2. claude_cli      ", "body")
            ins("— always uses the claude binary (fastest if configured)\n", "dim")
            ins("  3. llm             ", "body")
            ins("— always uses the configured LLM provider (Anthropic, OpenAI, LM Studio, Ollama)\n", "dim")
            br()
            p("Change the backend in Settings → Pre-commit section.")
            br()

            h2("Fail-open guarantee")
            p("If the AI call fails (timeout, network error, unconfigured provider), the "
              "hook exits 0 and the commit proceeds normally. A warning message is printed "
              "to the terminal. Your workflow is never blocked by an AI service failure.")
            br()

            warn("⚠  The hook only fires for projects where it is installed. Right-click "
                 "each project individually to install. The hook file lives at "
                 ".git/hooks/pre-commit inside the project folder.")
        self._help_show(_fill)

    def _help_run_checks(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Run Checks")

            h2("Overview")
            p("Right-click any project → ✓ Run checks… opens a dialog that runs four "
              "quality checks against the project. All enabled checks run concurrently.")
            br()

            h2("Four checks")
            ins("  [✓] Python syntax        ", "body")
            ins("— python -m compileall src/ -q   (free, instant)\n", "dim")
            ins("  [✓] pyflakes             ", "body")
            ins("— detects unused imports, undefined names   (free, ~1 s)\n", "dim")
            ins("  [✓] Doctor audit         ", "body")
            ins("— tokensave health check (same as the Doctor button)   (free)\n", "dim")
            ins("  [ ] Claude Code review   ", "body")
            ins("— AI review of your PR diff against the base branch   (uses tokens, off by default)\n", "dim")
            br()

            h2("Reading results")
            ins("  ✓  ", "ok"); ins("— passed (summary: 0 errors / 0 warnings)\n", "body")
            ins("  ✗  ", "warn"); ins("— failed (summary: file:line error message)\n", "body")
            ins("  ⏳  — running…\n", "body")
            ins("  —   — skipped (check was unchecked)\n", "body")
            br()

            h2("Large-diff warning")
            p("If the Claude review is enabled and your diff exceeds ~10 k characters, "
              "the dialog asks for confirmation before sending it. This prevents accidental "
              "token spend on huge diffs.")
            br()

            h2("Claude review scope")
            p("The Claude review runs against git diff <base>...HEAD (triple-dot, PR scope) "
              "— the same range GitHub would show in a pull request. The base branch is "
              "auto-detected from your git history.")
            br()

            h2("Preferences persist")
            p("Your checkbox selections are remembered between sessions. Uncheck Claude "
              "review once and it stays off until you turn it back on.")
        self._help_show(_fill)

    def _help_integration_check(self):
        _doc = os.path.join(_BASE_DIR, "docs", "UPGRADE_INTEGRATION.md")
        _ask = "What steps should I follow when tokensave releases a new version?"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Integration Check")

            h2("Overview")
            p("When tokensave releases a new version it may add MCP tools, remove "
              "commands, or change tool schemas. The integration check surfaces those "
              "gaps quickly without burning tokens on the routine parts.")
            br()

            h2("4-step workflow (order matters)")
            ins("  1. tokensave upgrade            ", "body")
            ins("— update the binary\n", "dim")
            ins("  2. git pull (this repo)         ", "body")
            ins("— update CHANGELOG.md and docs/\n", "dim")
            ins("  3. Settings → 🔍 Check integration  ", "body")
            ins("(or right-click → 🔄 Integration check)\n", "dim")
            ins("     ", "body")
            ins("Free deterministic audit: installed version, upstream issue status,\n", "dim")
            ins("     ", "body")
            ins("new tools without snippets, stale snippet references to removed tools.\n", "dim")
            ins("  4. Reference tab → '🔄 Integration audit' snippet  ", "body")
            ins("→ paste into Claude Code CLI\n", "dim")
            ins("     ", "body")
            ins("LLM-powered analysis of what snippets to add, update, or remove.\n", "dim")
            br()
            warn("⚠  Always upgrade tokensave and git pull BEFORE running the check. "
                 "Running the script before updating local files produces a false-clean report.")
            br()

            h2("Step 3 report shows")
            ins("  • Installed version vs. CHANGELOG latest documented version\n", "body")
            ins("  • Upstream issue files — STATUS: FIXED / SHIPPED / MOOT / OPEN\n", "body")
            ins("  • New tools (### Added) without snippet coverage in src/prompts.py\n", "body")
            ins("  • Stale snippets that call tools listed under ### Removed\n", "body")
            br()

            h2("Step 4 — LLM audit")
            p("The '🔄 Integration audit' snippet in the Reference tab instructs Claude "
              "to call tokensave_changelog, cross-reference tool names against snippet "
              "bodies, and output a structured action list. Run it in a Claude Code CLI "
              "session opened in this project's directory.")
            br()

            ok("See 📄 Open docs for the full UPGRADE_INTEGRATION.md guide.")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    def _help_settings_reference(self):
        _doc = os.path.join(_BASE_DIR, "README.md")
        _ask = "Explain each setting in the Settings dialog for TokenSave Manager"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Settings Reference")

            h2("Core paths")
            ins("  tokensave_exe    ", "code")
            ins("— path to the tokensave binary (auto-detected at startup)\n", "body")
            ins("  template_dir     ", "code")
            ins("— folder containing BASIC_INSTRUCTIONS.md and Nuitka templates\n", "body")
            ins("  git_exe          ", "code")
            ins("— path to git.exe (default: git, uses PATH)\n", "body")
            ins("  editor_cmd       ", "code")
            ins("— command to open files in your editor (e.g. code, notepad++)\n", "body")
            br()

            h2("Project discovery")
            ins("  search_roots     ", "code")
            ins("— list of root folders to scan for .tokensave/ directories.\n", "body")
            ins("                   ", "body")
            ins("  Each entry has a Label (the group header) and a Path.\n", "dim")
            br()

            h2("AI / Commit messages")
            ins("  commit_message_backend  ", "code")
            ins("— auto / llm_first / claude_cli / llm\n", "body")
            ins("  commit_message_llm      ", "code")
            ins("— provider config: enabled, provider, model, api_key, base_url\n", "body")
            ins("                          ", "body")
            ins("  Providers: anthropic, openai, lm_studio, ollama\n", "dim")
            br()

            h2("Pre-commit hook")
            ins("  precommit_backend  ", "code")
            ins("— auto / claude_cli / llm\n", "body")
            br()

            h2("Claude CLI")
            ins("  claude_cli_exe    ", "code")
            ins("— path to the claude binary (auto-detected or set manually)\n", "body")
            ins("  claude_cli_model  ", "code")
            ins("— model for managed calls: haiku-4-5, sonnet-4-6, opus-4-7, or empty\n", "body")
            ins("                    ", "body")
            ins("  Leave empty to use your global ~/.claude/settings.json default.\n", "dim")
            br()

            h2("Update polling")
            ins("  update_poll_hours  ", "code")
            ins("— how often to check GitHub for a new tokensave release (default: 1.0)\n", "body")
            ins("                     ", "body")
            ins("  Set to 0 to disable polling.\n", "dim")
            br()

            h2("CodeGraph")
            ins("  codegraph_enabled   ", "code")
            ins("— show CodeGraph menu items and CG column in the project list\n", "body")
            ins("  codegraph_npm_path  ", "code")
            ins("— path to npm (for running npx @colbymchenry/codegraph)\n", "body")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

    # ── Existing sections (continued) ─────────────────────────────────────────

    def _help_file_locations(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("File Locations")
            ins("Active project pin:  ", "body")
            ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
            ins("Baseline rules:      ", "body")
            ins(os.path.join(self._cfg.template_dir, "project-baseline.md") + "\n", "code")
            ins("Project template:    ", "body")
            ins(os.path.join(self._cfg.template_dir, "claude-md-template.md") + "\n", "code")
            ins("Nuitka templates:    ", "body")
            ins(os.path.join(self._cfg.template_dir, "nuitka-build.ps1.template") + "\n", "code")
            ins("Wrapper script:      ", "body")
            if os.environ.get("NUITKA_ONEFILE_PARENT"):
                _wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
            else:
                _wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
            ins(_wrapper + "\n", "code")
            ins("Manager log:         ", "body")
            ins(LOG_FILE + "\n", "code")
            ins("Manager config:      ", "body")
            ins(_CONFIG_PATH + "\n", "code")
        self._help_show(_fill)

    def _help_about(self):
        _doc = os.path.join(_BASE_DIR, "README.md")
        _ask = "Give me a high-level overview of what TokenSave Manager does and all its features"

        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop and Claude Code. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "full git workflow (commit, branch, merge, push, pull, GitHub releases), "
              "AI code review, smart commit messages, pre-commit hook, Ask tab agent, "
              "run checks dialog, CodeGraph lifecycle, and integration monitoring.")
            br()
            h2("What it doesn't do")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            br()
            h2("Version history")
            p("See CHANGELOG.md in the project root for the full release history. "
              "The 📄 Open docs button opens the project README.")
        self._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)
