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
