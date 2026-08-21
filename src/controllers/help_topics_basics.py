"""Help-tab topic renderers — projects, scaffold, tray basics.

Split out of controllers/help_tab.py (Roadmap-8 god-file split).
Each function takes the HelpTabController (``ctl``) and renders
its topic via ``ctl._hw()`` / ``ctl._help_show()`` exactly as the
original method did; the controller keeps 2-line delegates.
"""

from __future__ import annotations



def switching(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Switching Projects")
        ok("✔  You usually do NOT need to restart Claude Desktop to work on "
           "another project.")
        br()
        p("The pin (★ Set as Active) chooses the DEFAULT project — the one "
          "tokensave answers about when nothing says otherwise. Claude Desktop "
          "reads it once, when it starts its tokensave server, so moving that "
          "default is the one thing that really does need a restart.")
        br()
        p("Reading a different project does not. Every tokensave tool takes a "
          "graph_root argument, which opens any indexed project on demand — "
          "including one in a completely unrelated folder tree:")
        ins('  tokensave_context(task="…", graph_root="D:\\Projects\\Other")\n',
            "dim")
        br()
        p("The Reference tab has this ready to paste — the "
          "“🌐  Query another project” snippet. Fill in the path, copy, "
          "paste.")
        br()

        h2("Two things to watch")
        ins("  •  The selected graph opens read-only", "body")
        ins(" — use it for reading and reviewing, not for edits.\n", "dim")
        ins("  •  Claude has to be told", "body")
        ins(" — without graph_root it answers from the pinned project, and a "
            "wrong-project answer looks completely normal.\n", "dim")
        br()
        warn("Turn on strict_tree (right-click a project → 🗂 Index → "
             "🛡 Enable strict_tree…) to make that second case fail loudly "
             "instead of quietly answering from the wrong checkout. The same "
             "entry reads Disable once it is on, so you can turn it back off "
             "if it ever refuses something legitimate.")
        br()

        h2("When you do want to move the default")
        ins("  1. Select the new project in the list\n", "body")
        ins("  2. Click ★ Set as Active\n", "body")
        ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
        ins("  4. Relaunch Claude Desktop\n", "body")
        br()
        dim("A manager feature that tried to skip step 3 by restarting Desktop's "
            "server for you was removed in Roadmap-10. Desktop does not start a "
            "replacement when its MCP server dies — it left you with no "
            "tokensave at all. See docs/MCP_INTEGRATION_GOTCHAS.md.")
        br()
        p("Tip: to go back to whichever project you last synced automatically, "
          "click Auto-detect instead of pinning a specific project.")
    ctl._help_show(_fill)

def window_tray(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def context_menu(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
        ins("  ★  Set as Active  ", "body"); ins("Pin the DEFAULT project for Claude Desktop (see Switching Projects)\n", "dim")
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
        ins("  🔗  Shadow Links…     ", "body"); ins("Hardlink non-standard file types under a known extension so tokensave can index them. New in v7.2+: .tokensave/project.json can index extensionless files directly (with a language override), avoiding shadow links in many projects.\n", "dim")
        ins("  📁  Assign Category…  ", "body"); ins("Override this project's group label and sub-category\n", "dim")
        ins("  🗑  Remove Index…     ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
        ins("  Auto-detect           ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
    ctl._help_show(_fill)

def scaffold(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def retrofit(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def nuitka(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def scaffold_column(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def autodetect(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def init_vs_sync(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def categories(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)
