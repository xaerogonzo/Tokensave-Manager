"""Help-tab topic renderers — CodeGraph, AI, checks, settings, about.

Split out of controllers/help_tab.py (Roadmap-8 god-file split).
Each function takes the HelpTabController (``ctl``) and renders
its topic via ``ctl._hw()`` / ``ctl._help_show()`` exactly as the
original method did; the controller keeps 2-line delegates.
"""

from __future__ import annotations

import os
from constants import LOG_FILE
from constants import _BASE_DIR
from constants import _CONFIG_PATH


def pyscope(ctl):
    """The PyScope topic: what it adds that the other two tools do not."""

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("PyScope")
        p("PyScope is an optional third analysis tool. It is not another code "
          "graph competing with tokensave and CodeGraph \u2014 it answers a "
          "different question about the same source tree.")
        br()

        h2("The difference, in one line")
        ins("  tokensave / CodeGraph  ", "body")
        ins("where things are, and what touches what.\n", "dim")
        ins("  PyScope                ", "body")
        ins("how much of that is actually established.\n", "dim")
        br()
        p("That distinction matters because an AI assistant reading a code "
          "graph cannot tell a proved call edge from a name that happened to "
          "match. PyScope counts the difference and reports it. On its own "
          "source tree, 4,191 of 9,906 relationships are unresolved \u2014 so "
          "roughly 43% of what a graph shows was never proved by anything.")
        br()

        h2("Three separate answers, never merged")
        ins("  Confidence    ", "body")
        ins("does this NAME refer to this source symbol?\n", "dim")
        ins("  Dispatch      ", "body")
        ins("does the call actually REACH that callable at runtime?\n", "dim")
        ins("  Completeness  ", "body")
        ins("can we list all of these, or only the ones we found?\n", "dim")
        br()
        p("These are independent. \"from foo import run; run()\" has a certain "
          "binding and a runtime target nobody can guarantee \u2014 rebinding, "
          "conditional imports and decorators all break the link. A tool whose "
          "most confident label is also its least honest one is worse than no "
          "tool, so PyScope keeps them apart and so does this manager.")
        br()

        h2("Optional, in both directions")
        p("The manager works completely without PyScope, and PyScope works "
          "completely without the manager. When it is absent, the PyScope "
          "surfaces simply do not appear \u2014 nothing errors and nothing is "
          "disabled-looking.")
        br()

        h2("Installing it")
        p("The manager does NOT install PyScope. It is a uv tool over a local "
          "checkout rather than a package this manager can fetch, so there is "
          "no Install button to press \u2014 that would be claiming a job the "
          "manager does not do. Install it yourself with:")
        ins("  uv tool install --editable <path to the PyScope checkout>\n", "body")
        p("Then Settings \u2192 PyScope \u2192 Auto-detect, or point Browse at it. "
          "The manager looks on PATH first, then in ~/.local/bin, which is "
          "where uv writes its shims.")
        br()

        h2("Configured is not installed is not working")
        p("The Settings section reports three separate things on purpose:")
        ins("  Configured  ", "body")
        ins("a path is set or was detected.\n", "dim")
        ins("  Executable  ", "body")
        ins("that path exists and can be launched.\n", "dim")
        ins("  Status      ", "body")
        ins("it ran and answered sensibly.\n", "dim")
        p("\"Installed but crashing\" and \"not installed\" send you to "
          "completely different fixes, so this manager never collapses them "
          "into one word.")
        br()

        h2("Per-project actions")
        p("Right-click a project \u2192 \U0001f52c PyScope:")
        ins("  Analyze              ", "body")
        ins("build the index and report what was established.\n", "dim")
        ins("  Status               ", "body")
        ins("binary, registration, and where the cache lives.\n", "dim")
        ins("  Register with PyScope  ", "body")
        ins("add it to PyScope's project list.\n", "dim")
        ins("  Bind to Claude Code  ", "body")
        ins("register it AND wire the MCP server.\n", "dim")
        ins("  Open in PyScope      ", "body")
        ins("launch PyScope's own desktop app here.\n", "dim")
        br()

        h2("Why binding is two things")
        p("PyScope's MCP server answers about REGISTERED projects and takes "
          "the project as an argument, so there is one server for the whole "
          "machine rather than one per project. The MCP entry makes the server "
          "reachable; registering makes a project answerable. An entry without "
          "registration replies \"unknown project\" to everything, so the "
          "manager reports both states separately and will tell you when only "
          "one of them landed.")
        warn("A user-scoped MCP entry is normally a problem in this manager "
             "\u2014 it shadows a project's own .mcp.json. For PyScope it is "
             "correct, because there is no project-scoped entry to shadow. The "
             "manager knows the difference by server name.")
        br()

        h2("The .pyscope/ folder")
        p("PyScope writes <project>/.pyscope/ ONLY when git ignores it, and "
          "otherwise keeps the analysis in per-user app data. The manager's "
          "baseline .gitignore includes .pyscope/ for exactly that reason. "
          "Doctor mentions it if a project is analysed but has no local cache "
          "\u2014 as a suggestion, not a warning. Nothing is broken either way.")
        br()

    ctl._help_show(_fill)


def vscode_extension(ctl):
    """The extension's build and install lifecycle, and why it needed one."""

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("VS Code extension")
        p("The Manager ships a VS Code extension, and for a while the copy "
          "installed in your editor was three minor versions behind the one "
          "in this repository. Nothing could say so, because the three "
          "versions involved were never shown next to each other.")
        br()

        h2("The three versions")
        ins("  Source     ", "body")
        ins("vscode-extension/package.json\n", "dim")
        ins("  Built      ", "body")
        ins("the manifest INSIDE the .vsix on disk\n", "dim")
        ins("  Installed  ", "body")
        ins("what the editor reports\n", "dim")
        br()
        p("Built is read from inside the archive, never from the filename. A "
          "filename is metadata anybody can rewrite; the manifest is what VS "
          "Code installs the thing as, so a stale build renamed to look "
          "current passes every filename check there is.")
        br()

        h2("Version parity is not freshness")
        p("A fourth signal sits beside those three: whether the artefact is "
          "older than the newest thing the build reads. That is a separate "
          "question, and it is the only one that can catch a project whose "
          "version number never moves \u2014 PyScope's extension was stale "
          "for weeks with all three of its versions reading 0.1.0.")
        br()
        p("So a version mismatch and a stale timestamp are reported as "
          "different states, because they ask you to do different things.")
        br()

        h2("Building it")
        ins("  build-extension.ps1             ", "body")
        ins("clean build + verify\n", "dim")
        ins("  build-extension.ps1 -Install    ", "body")
        ins("... and install it\n", "dim")
        ins("  build-extension.ps1 -SkipTests  ", "body")
        ins("... without the suite\n", "dim")
        br()
        p("One command, the same way every time: npm ci against the committed "
          "lockfile, out\\ deleted before tsc, the test suite, package, then "
          "the artefact verifier. Deliberately the same sequence the release "
          "workflow runs, so a local build and a released one cannot differ.")
        br()
        ok("Verification failure is a build failure. There is no path where a "
           "package is produced, judged unfit, and still reported as success.")
        br()

        h2("The Extension Manager")
        p("Help tab \u2192 Extension Manager shows all four signals and "
          "offers Build, Build & Install and Uninstall. It shells out to "
          "build-extension.ps1 rather than reimplementing the steps \u2014 a "
          "GUI with its own copy of the pipeline would drift from the release "
          "path, which is the failure this feature exists to close.")
        br()
        p("There is deliberately no bare \"install the .vsix that is already "
          "there\" button. Build & Install always rebuilds first, so a stale "
          "artefact cannot reach your editor by one click.")
        br()
        warn("Install and Uninstall need an editor that answers "
             "--list-extensions. editor_cmd is configurable and may not be VS "
             "Code at all, so the buttons are disabled with the reason rather "
             "than failing obscurely when it does not.")

    ctl._help_show(_fill)


def codegraph(ctl):
    _doc = os.path.join(_BASE_DIR, "README.md")
    _ask = ("tokensave sync finished but I still can't find a function "
            "I just added — why might that happen and how do I fix it?")

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Code Graph & CodeGraph")
        p("This section covers two things: tokensave's own code graph (the "
          ".tokensave/ index this manager builds), and CodeGraph — a separate "
          "alternative tool that does the same job differently.")
        br()

        h2("What a code graph is")
        p("A code graph is a searchable index of your project's symbols — "
          "functions, classes, methods, constants — and the relationships between "
          "them (which function calls which, which file imports which module). "
          "Nodes are symbols; edges are call/use relationships.")
        p("Claude uses the code graph to answer structural questions about your "
          "project — 'where is X defined', 'what calls this function', 'which "
          "files depend on this module' — without reading every file each time.")
        br()

        h2("tokensave init vs sync")
        ins("  tokensave init  ", "body")
        ins("Builds the index from scratch. Run once when you first add a\n"
            "                project. Creates .tokensave/ inside the project.\n", "dim")
        ins("  tokensave sync  ", "body")
        ins("Incremental update — only re-processes files changed since\n"
            "                the last sync. Much faster; run after editing code.\n", "dim")
        br()
        h2("Force Re-sync vs incremental Sync")
        p("Right-click a project → ⟳ Force Re-sync wipes the existing index "
          "and rebuilds from scratch. Use it when:")
        ins("  • Sync finished but a recently-added function can't be found\n", "body")
        ins("  • You renamed many files or did a large refactor\n", "body")
        ins("  • tokensave was upgraded to a new version\n", "body")
        ins("  • The .tokensave/ folder was manually edited\n", "body")
        br()
        p("For day-to-day edits, the incremental ↺ Sync is sufficient and much "
          "faster (seconds vs. minutes on large projects).")
        br()
        h2("Language support")
        p("tokensave extracts symbols from Python, TypeScript/JavaScript, Rust, "
          "and Go by default. Files in other languages are indexed for text "
          "search but don't contribute nodes or edges to the graph.")
        br()

        h2("How Claude uses it (MCP server)")
        p("The wrapper script `tokensave-wrapper.py` acts as an MCP server. "
          "When Claude Desktop starts with the active project pinned (★ Set as "
          "Active), the wrapper exposes the code graph as MCP tools Claude can "
          "call: tokensave_context, tokensave_search, tokensave_callers, "
          "tokensave_callees, tokensave_body, and others.")
        warn("⚠  Claude Desktop reads the active project at startup and locks to "
             "it for the session. If you change the pinned project, restart "
             "Claude Desktop to pick it up.")
        br()

        h2("CodeGraph — alternative code-graph tool")
        p("CodeGraph is a separate npm package that also builds a per-project "
          "code-graph and exposes it to Claude Code via MCP. The two don't "
          "conflict; a project can have both at once.")
        ins("  • tokensave — bundled with the manager; full-featured; manual sync\n", "body")
        ins("  • CodeGraph — auto-syncs while its MCP server is running; faster\n", "body")
        ins("                for very large codebases (e.g. 25k-file repos)\n", "body")
        br()
        warn("⚠  CodeGraph's auto-sync only runs while its MCP server is active "
             "inside an open Claude Code session. Edits made with Claude Code "
             "closed won't be picked up until the next session.")
        br()
        h2("CodeGraph — Install & Use")
        ins("  Settings → CodeGraph → Install via npm  ", "body")
        ins("(requires Node.js 18+)\n", "dim")
        ins("  Right-click any project → 🧠 CodeGraph Init  →  then 🧠 Sync / Status\n", "body")
        ins("  CG column in the Projects tab shows ✓ for initialised projects.\n", "body")
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

def ai_features(ctl):
    _doc = os.path.join(_BASE_DIR, "README.md")
    _ask = ("My commit message suggestions keep saying 'docs: update' for "
            "code-only changes — what's wrong with the suggestion strategy?")

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("AI Features")
        p("TokenSave Manager integrates AI at several points. All AI features are "
          "optional and fail open — if the AI call fails, the operation continues "
          "without it.")
        br()

        h2("Feature overview")
        ins("  Feature                 LLM used\n", "body")
        ins("  ─────────────────────── ─────────────────────────────────────\n", "dim")
        ins("  🤖 Ask tab              Configured provider (Ollama / OpenAI /\n"
            "                          Anthropic / LM Studio)\n", "body")
        ins("  🔍 Help → Explain btn   Same configured provider\n", "body")
        ins("  💡 Commit msg Suggest   Claude CLI → provider → heuristics\n", "body")
        ins("  🔍 AI Code Review       Claude CLI → provider\n", "body")
        ins("  📝 Draft CHANGELOG      Claude CLI\n", "body")
        ins("  🐙 Draft PR             Claude CLI\n", "body")
        ins("  Pre-commit hook         auto (Claude CLI → provider) or explicit\n", "body")
        ins("  AI Tasks tab            Claude CLI agent sessions\n", "body")
        br()

        h2("🤖 Ask Tab (bounded agent)")
        p("An embedded agent that answers questions about your active project. "
          "Type a question in the Ask tab, press Send. The agent runs up to 8 "
          "iterations and streams its response. Tools available to it:")
        ins("  • read_file          — read a file at a given path + line range\n", "body")
        ins("  • list_directory     — list files in a folder\n", "body")
        ins("  • git_log            — recent commit history\n", "body")
        ins("  • git_diff           — pending uncommitted diff\n", "body")
        ins("  • tokensave_search   — find defined symbols by name\n", "body")
        ins("  • tokensave_context  — subgraph for a natural-language query\n", "body")
        p("tokensave tools only work when the project has a .tokensave/ index. "
          "For general knowledge questions (git concepts, Python syntax, etc.) "
          "the agent answers directly — no tool calls needed.")
        br()

        h2("🔍 AI Code Review")
        p("Right-click a project → 🔍 AI Code Review… — streams a severity-coloured "
          "review of your staged or HEAD diff. Uses the configured LLM or Claude CLI. "
          "Results appear in a live-updating dialog:")
        ins("  ✗ critical  ⚠ warning  ℹ info  ✓ pass\n", "body")
        br()

        h2("💡 Smart commit messages")
        p("The 📝 Commit… dialog uses a multi-strategy chain to suggest a message:")
        ins("  1. CHANGELOG.md bullets (if you added an entry today)\n", "body")
        ins("  2. AI backend (Claude CLI or configured provider)\n", "body")
        ins("  3. Diff content — added/changed definitions, file types\n", "body")
        ins("  4. File-name heuristics (legacy fallback)\n", "body")
        p("Click 💡 Suggest to regenerate. Configure the backend in Settings → "
          "Commit Message AI.")
        br()

        h2("Claude CLI integration")
        p("Settings → Claude Code CLI → Exe path wires in the claude binary. "
          "Used by code review, Draft CHANGELOG, Draft PR, Run checks, and "
          "the AI Tasks tab. Configure the model (Haiku / Sonnet / Opus) in the "
          "same Settings section.")
        br()

        h2("Ollama Model Manager")
        p("Settings → Ollama → Manage Models — browse, pull, and delete local Ollama "
          "models. Models pulled here are available as a provider option for all "
          "AI features above that use the configured provider.")
        br()

        h2("When AI features are unavailable")
        ins("  No provider configured  ", "body")
        ins("AI features that need a provider show 'AI disabled' and\n"
            "                        fall through to heuristics. Configure in Settings.\n", "dim")
        ins("  No .tokensave/ index    ", "body")
        ins("tokensave_search / context return 'run tokensave init\n"
            "                        first'. Other Ask tools still work.\n", "dim")
        ins("  Claude CLI not found    ", "body")
        ins("Features that require Claude CLI show a warning and\n"
            "                        skip that step. Set Exe path in Settings.\n", "dim")
        br()

        h2("📊 Cost tracking")
        p("The 📊 Cost button shows a running tally of API tokens and estimated "
          "cost for this session — commit messages, reviews, pre-commit, and "
          "inline Explain in this Help tab.")
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

def precommit_hook(ctl):
    _ask = ("The pre-commit hook is blocking my commit — the AI review ran but I "
            "disagree with it. How do I override it for just this one commit?")

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Pre-commit AI Review Hook")

        h2("What it does")
        p("Installs a git pre-commit hook that runs an AI review of your staged diff "
          "before every commit. If the review finds critical issues it warns you — the "
          "commit still proceeds (fail-open guarantee). The review runs in the "
          "background using the configured LLM or Claude CLI backend.")
        br()

        h2("Where the hook lives")
        p("The hook file is written to:")
        ins("  <project>/.git/hooks/pre-commit\n", "body")
        p("It's a plain shell script. You can open and read it any time. "
          "Uninstalling (via the manager dialog) removes only that file — nothing "
          "else in your project is touched.")
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

        h2("Bypassing the hook in an emergency")
        p("If the hook is blocking a commit you know is fine, use:")
        ins("  git commit --no-verify -m \"your message\"\n", "body")
        p("This skips ALL hooks for that one commit. Use sparingly — the hook is "
          "there to catch real issues. The manager never force-sets --no-verify.")
        br()

        h2("If the AI provider is offline")
        p("If the AI call fails (timeout, network error, unconfigured provider), the "
          "hook exits 0 and the commit proceeds normally — this is the fail-open "
          "guarantee. A warning message is printed to the terminal. Your workflow "
          "is never hard-blocked by an AI service failure.")
        br()

        warn("⚠  The hook only fires for projects where it is installed. Right-click "
             "each project individually to install.")
    ctl._help_show(_fill, ask_text=_ask)

def run_checks(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill)

def integration_check(ctl):
    _doc = os.path.join(_BASE_DIR, "docs", "UPGRADE_INTEGRATION.md")
    _ask = "What steps should I follow when tokensave releases a new version?"

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

def settings_reference(ctl):
    _doc = os.path.join(_BASE_DIR, "README.md")
    _ask = "Explain each setting in the Settings dialog for TokenSave Manager"

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

def file_locations(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("File Locations")
        ins("Active project pin:  ", "body")
        ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
        ins("Baseline rules:      ", "body")
        ins(os.path.join(ctl._cfg.template_dir, "project-baseline.md") + "\n", "code")
        ins("Project template:    ", "body")
        ins(os.path.join(ctl._cfg.template_dir, "claude-md-template.md") + "\n", "code")
        ins("Nuitka templates:    ", "body")
        ins(os.path.join(ctl._cfg.template_dir, "nuitka-build.ps1.template") + "\n", "code")
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
    ctl._help_show(_fill)

def relocating(ctl):
    _doc = os.path.join(_BASE_DIR, "docs", "RELOCATING.md")
    _ask = ("I moved TokenSave Manager to a new folder - what do I need to "
            "check in my projects?")

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Moving the Manager")
        p("Move the folder, start the Manager, and press the button it offers. "
          "The rest of this explains why there is a button at all.")
        br()
        h2("Why the location matters")
        p("Every wired project's BASIC_INSTRUCTIONS.md holds an ABSOLUTE path "
          "to the shared baseline. That is what makes one edit to "
          "project-baseline.md reach every project at once - and it is why "
          "moving this folder matters to all of them.")
        br()
        h2("What gets repaired")
        ins("  template_dir in manager-config.json\n", "body")
        ins("  the baseline include in every project\n", "body")
        ins("  MCP wrapper paths - Settings -> MCP integration\n", "body")
        br()
        p("Paths to software installed elsewhere - tokensave.exe, git, your "
          "editor, the analyzers - are never rewritten by a move. Neither is "
          "anything under search_roots, even if it lives inside this folder.")
        br()
        h2("When it refuses")
        p("If your projects point at two different baselines, or some reach "
          "none at all, there is no single thing to repoint and the Manager "
          "says so rather than fixing half the fleet.")
        br()
        warn("Two installations - a source checkout and a release build - each "
             "carry their own templates folder. Whichever ran last owns the "
             "projects, and the other will report them all as stale. That is "
             "correct, not a bug.")
        br()
        dim("Full detail: docs/RELOCATING.md")
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask)


def about(ctl):
    _doc = os.path.join(_BASE_DIR, "README.md")
    _ask = "Give me a high-level overview of what TokenSave Manager does and all its features"

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
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
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)
