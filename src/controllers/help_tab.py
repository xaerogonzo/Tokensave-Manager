"""HelpTabController — builds and manages the Help tab.

Extracted from App (Round 5 / App decomposition).

Dependency contract:
  • notebook — ttk.Notebook to add the Help tab to
  • cfg      — read-only ManagerConfig (.template_dir, read at execution time)

All help content is static text.  The only cfg usage is in _help_file_locations,
which reads .template_dir at display time (not at __init__ time) so a
Settings save propagates without restarting.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C, LOG_FILE, _BASE_DIR, _CONFIG_PATH

if TYPE_CHECKING:
    from state import ManagerConfig


class HelpTabController:
    """Owns the Help tab: topic list + rich-text content pane."""

    def __init__(self, notebook: ttk.Notebook, cfg: "ManagerConfig") -> None:
        self._cfg = cfg
        self._build(notebook)

    # ── Construction ──────────────────────────────────────────────────────────

    def _build(self, notebook: ttk.Notebook) -> None:
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

        # ── Right: content ────────────────────────────────────────────────────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(fill=tk.BOTH, expand=True)

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

    def _on_help_select(self, _event=None):
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    # ── Rendering helpers ─────────────────────────────────────────────────────

    def _help_show(self, fn):
        """Clear the content pane, call fn() to fill it, then lock + scroll to top."""
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

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
            ins("  Tray icon → Quit      ", "body"); ins("Fully exits the app (stops the daemon connection too).\n", "dim")
            br()
            p("The window position and size are saved automatically when you hide to tray "
              "and restored the next time you click Show.")
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
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect       ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
            br()

            h2("Git")
            ins("  📜  Git Log        ", "body")
            ins("Show last 20 commits + working-tree status from the project's own repo.\n", "dim")
            ins("                    ", "body")
            ins("Nothing is stored in the manager — purely a read-only view.\n", "dim")
            ins("                    ", "body")
            ins("Shows a friendly message if the folder is not a git repo or git is not on PATH.\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup")
            ins("  ⚙  Retrofit…       ", "body")
            ins("Open the Retrofit dialog for the selected project without re-navigating\n", "dim")
            ins("                     ", "body")
            ins("to the folder manually. Same as the toolbar button but pre-filled.\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
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
        self._help_show(_fill)

    def _help_git_workflow(self):
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
        self._help_show(_fill)

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
        self._help_show(_fill)

    def _help_codegraph(self):
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
        self._help_show(_fill)

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
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "git log / status, folder/editor navigation, and clipboard shortcuts.")
            br()
            h2("What it doesn't do (yet)")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Daemon start/stop/status\n", "dim")
            ins("  • Cost tracking (tokensave cost)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            ins("  • Inline git diff / commit details\n", "dim")
        self._help_show(_fill)
