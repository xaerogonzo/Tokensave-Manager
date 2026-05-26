"""Built-in Claude prompt snippets for the Reference tab.

Module-level constant only — no imports beyond stdlib. Loaded by
`src/app.py` and passed into `SnippetsController` at construction time.

Placeholder syntax: `[[double brackets]]`. Single `[brackets]` are
intentionally NOT placeholders — they're reserved for markdown links
(`[doc](url)`) and footnote refs (`[1]`) so the user can write real
markdown inside custom snippets without triggering spurious input
fields. The `_PLACEHOLDER_RE` in `src/controllers/snippets.py` matches
`\\[\\[token\\]\\]` only.

Defaults are immutable (ROM). User edits land in
`manager-config.json` → `builtin_snippet_overrides` (RAM overlay).
The override system NEVER mutates this file at runtime — Reset just
pops the override key and the next render falls through to the
default below.

To add a new built-in: append a `(title, body)` tuple. Keep the emoji
prefix (load-bearing for visual scan + grouping). To remove one: just
delete the tuple — any user override for that title becomes an orphan
that the controller harmlessly ignores.
"""

from __future__ import annotations

PROMPT_SNIPPETS: list[tuple[str, str]] = [
    # ─────────────────────────── 🧭 EXPLORATION ───────────────────────────
    (
        "🧭  Codebase overview",
        "Give me a structural overview of this project. Run "
        "tokensave_context with 'overall architecture', then "
        "tokensave_files for the directory layout, then tokensave_outline "
        "on the entry-point file. Produce: a 2-sentence elevator pitch, a "
        "list of the top-level modules with one-line purpose each, and a "
        "guided 5-step tour with file:line jumps that a new contributor "
        "could follow in 15 minutes."
    ),
    (
        "🧭  Read this function",
        "Find and read [[symbol name]]. Step 1: tokensave_search to locate "
        "the exact file:line. Step 2: tokensave_node to fetch the body. "
        "Step 3: explain what it does, when it's called, and any "
        "non-obvious behaviour. Cite file:line for every claim."
    ),
    (
        "🧭  Module public API",
        "Run tokensave_module_api on [[module or file]]. For each exported "
        "symbol: list its signature (tokensave_signature) and one example "
        "call site (tokensave_callers, take the first result). Output a "
        "compact reference table — name | signature | example file:line."
    ),

    # ─────────────────────────── 🔬 ANALYSIS / TRACING ────────────────────
    (
        "🔬  Symbol deep-dive",
        "Deep-dive on [[symbol name]]. Run in this order:\n"
        "  1. tokensave_search to confirm the file:line\n"
        "  2. tokensave_node to read the body\n"
        "  3. tokensave_callers — who depends on it\n"
        "  4. tokensave_callees — what it depends on\n"
        "  5. tokensave_impact — full downstream blast radius\n"
        "  6. tokensave_affected — which tests would re-run if changed\n"
        "Output: purpose (1 sentence), call graph as a two-column tree "
        "(callers ← symbol → callees), and a risk note flagging any "
        "cross-module ripples or public-API exposure."
    ),
    (
        "🔬  Trace a bug",
        "Bug symptom: [[describe what's broken]].\n"
        "Workflow: tokensave_search for the suspect symbol → "
        "tokensave_callers to see entry points → tokensave_node to read "
        "the body → tokensave_callees to see what it calls → "
        "tokensave_diff_context if the bug is recent. Cite file:line for "
        "every claim. End with: root cause hypothesis + the minimal fix."
    ),
    (
        "🔬  Why does this fail?",
        "I'm getting this error / wrong output from [[symbol or feature]]:\n\n"
        "[[paste error or describe wrong behaviour]]\n\n"
        "Workflow: tokensave_search the symbol → tokensave_node to read "
        "it → tokensave_diff_context to see recent changes to it → "
        "git_diff if the change is uncommitted. Identify what changed, "
        "what broke, and propose a minimal fix with file:line."
    ),

    # ─────────────────────────── 📊 AUDITS ────────────────────────────────
    (
        "📊  Full health audit",
        "Run a comprehensive code-health audit using ALL of these "
        "tokensave tools and produce one structured report:\n"
        "  • tokensave_health(details=true)  (overall + per-dimension sub-scores)\n"
        "  • tokensave_complexity            (high-complexity functions)\n"
        "  • tokensave_god_class             (god classes / large files)\n"
        "  • tokensave_circular              (circular dependencies)\n"
        "  • tokensave_coupling              (high-coupling modules)\n"
        "  • tokensave_redundancy            (AST-level functional duplicates)\n"
        "  • tokensave_dead_code             (unused code)\n"
        "  • tokensave_unused_imports        (unused imports)\n"
        "  • tokensave_hotspots              (high-churn files)\n"
        "  • tokensave_recursion             (recursive call sites)\n"
        "  • tokensave_unsafe_patterns       (risky patterns)\n"
        "Format: 🔴 Critical / 🟡 Warning / 🔵 Info, each with file:line "
        "and a one-sentence suggested fix. End with a top-5 priority list. "
        "Calibration caveat: for Python/Tk projects, dead-code and "
        "unused-imports false-positive heavily on tkinter callbacks "
        "(command=self._x) and attribute imports (constants.C) — grep-"
        "verify each finding before recommending deletion."
    ),
    (
        "📊  Architecture report",
        "Produce a docs/ARCHITECTURE.md draft for this project:\n"
        "  • tokensave_dsm              (dependency structure matrix)\n"
        "  • tokensave_dependency_depth (longest module chains)\n"
        "  • tokensave_inheritance_depth (deepest class hierarchies)\n"
        "  • tokensave_coupling          (most-imported modules)\n"
        "  • tokensave_module_api        (each top-level subpackage)\n"
        "Format: Overview → Layer Diagram (ascii) → Module Reference "
        "Table → Hot Spots → Recommended reading order for a new "
        "contributor. Cite file:line throughout."
    ),
    (
        "📊  Hotspot risk scan",
        "Cross-reference tokensave_hotspots (high-churn) × "
        "tokensave_complexity (high CC) × tokensave_coupling (high "
        "fan-in/out). Files high on multiple axes are most bug-prone. "
        "Output a ranked Risk Heatmap with the top 10 entries: file | "
        "churn | complexity | coupling | one-line refactor suggestion."
    ),
    (
        "📊  Pre-release readiness",
        "Check whether this project is ready to release:\n"
        "  • tokensave_diff_context     (recent changes summary)\n"
        "  • tokensave_changelog        (CHANGELOG draft from commits)\n"
        "  • tokensave_health           (overall metrics)\n"
        "  • tokensave_dead_code        (cruft that shouldn't ship)\n"
        "  • tokensave_circular         (architectural debt)\n"
        "Output a Release Readiness Checklist with ✅/⚠/❌ per item. "
        "End with: 'Safe to release: yes/no, blockers: [...]'."
    ),

    # ─────────────────────────── 🪦 FINDINGS / CLEANUP ────────────────────
    (
        "🪦  Find dead code",
        "Run tokensave_dead_code and tokensave_unused_imports. For each "
        "finding, run tokensave_callers to double-check — many static-"
        "analysis 'dead' hits are reflection / dynamic dispatch / Tk "
        "callbacks the indexer can't trace. Output two groups: 'Safe "
        "to delete (verified zero callers)' and 'Verify before deleting "
        "(might be called dynamically)' with file:line for each."
    ),
    (
        "🪦  List TODOs / FIXMEs",
        "Run tokensave_todos to list every TODO/FIXME/HACK/XXX comment. "
        "Group by file. For each item, use git_log on the file to "
        "estimate when the comment was added — flag any older than 6 "
        "months as stale. Output: file | line | type | age | text."
    ),
    (
        "🪦  Largest / most complex files",
        "Cross-reference tokensave_largest (by LOC), tokensave_complexity "
        "(by CC), and tokensave_god_class (by method count). The "
        "intersection of all three is your top refactor priority. Output "
        "a ranked table with all three scores, plus a 'split candidate' "
        "suggestion for each top-5 entry (e.g. extract method group X "
        "into separate file)."
    ),
    (
        "🪦  Redundancy hunt",
        "Find functional code duplication using tokensave_redundancy "
        "(AST-level: blends AST shape, control-flow, call-sequence, and "
        "token-shingle similarity into one composite score).\n\n"
        "Workflow:\n"
        "  1. tokensave_redundancy — returns definite / likely / "
        "naming_only buckets ranked by similarity score.\n"
        "  2. For each 'definite' or 'likely' pair: tokensave_node on "
        "both bodies to read side-by-side. Confirm the algorithm is "
        "the same, not just structurally similar boilerplate "
        "(__init__, _build_*, event handlers share structure by "
        "convention — skip these).\n"
        "  3. tokensave_callers on each to find the merge target: keep "
        "the one with more callers, delete/delegate the other.\n"
        "  4. tokensave_impact to estimate blast radius of the merge.\n\n"
        "Output: ranked table — score | file:line A | file:line B | "
        "verdict | proposed action. Cap at 10 pairs."
    ),

    # ─────────────────────────── 🛠 WORKFLOWS ─────────────────────────────
    (
        "🛠  Pre-commit checklist",
        "I'm about to commit. Run:\n"
        "  • tokensave_diff_context     (what's actually changed)\n"
        "  • tokensave_affected         (which tests would be affected)\n"
        "  • tokensave_impact           (downstream impact of changes)\n"
        "  • tokensave_run_affected_tests (run only the relevant tests)\n"
        "Then review for: missing tests, missing docstrings, leftover "
        "TODOs, secrets/keys. Output: 'Ready to commit: yes/no, "
        "issues: [...]'."
    ),
    (
        "🛠  PR review prep",
        "I'm opening a PR. Run tokensave_pr_context for the branch-level "
        "summary, tokensave_diff_context for per-file diffs, "
        "tokensave_impact on the most-changed symbols, and "
        "tokensave_affected for test coverage. Draft a PR description "
        "with sections: Summary (2-3 sentences), Changes (bulleted by "
        "file), Testing (what was run), Review Questions (what a "
        "reviewer should pay attention to)."
    ),
    (
        "🛠  Plan a refactor",
        "I want to refactor [[target]]. Workflow:\n"
        "  1. tokensave_coupling — what is the target bound to\n"
        "  2. tokensave_dependency_depth — its layer-cake position\n"
        "  3. tokensave_callers + tokensave_callees — blast radius\n"
        "  4. tokensave_rename_preview — if any renames involved\n"
        "  5. tokensave_test_map — what tests cover it\n"
        "Output a step-by-step plan with file:line per step, ordered "
        "least-coupled first so the diff can ship in small reviewable "
        "chunks. Flag any step that requires updating >5 call sites."
    ),
    (
        "🛠  Generate CHANGELOG entry",
        "Run tokensave_changelog to draft a CHANGELOG entry from recent "
        "commits. Group by ### Added / ### Changed / ### Fixed / "
        "### Removed (Keep-a-Changelog format). Use the existing "
        "CHANGELOG.md tone as reference: bolded lead-in sentence, "
        "em-dash, then prose. Cite file:line for any structural changes."
    ),

    # ─────────────────────────── 🔗 v6 NEW TOOLS ─────────────────────────
    (
        "🔗  Call chain between two symbols",
        "Find the shortest directed call chain from [[source symbol]] "
        "to [[target symbol]] using tokensave_call_chain. If a direct "
        "path exists, show it as A → B → … → Z with file:line for each "
        "hop. If no path, explain why (different subsystem, async "
        "dispatch, etc.) and suggest the most likely indirect route via "
        "tokensave_callers + tokensave_callees."
    ),
    (
        "🔗  Health sub-score breakdown",
        "Run tokensave_health with details=true to get the full "
        "per-dimension breakdown (acyclicity, depth, equality/gini, "
        "redundancy, modularity, coverage discipline). For each "
        "sub-score below 0.9: explain what it measures, what's causing "
        "the low score (use tokensave_circular / tokensave_gini / "
        "tokensave_redundancy as needed to drill in), and propose one "
        "concrete action to improve it. End with the composite "
        "quality_signal and what moving it from current to 8000+ would "
        "require."
    ),
    (
        "🔗  Exact symbol lookup",
        "Look up [[exact symbol name]] using tokensave_find_exact_symbol "
        "(O(log n) index probe — use this when you already know the "
        "name, not for discovery). Then tokensave_node for the body, "
        "tokensave_callers for who uses it, tokensave_callees for its "
        "dependencies. Produce a one-page reference card: purpose, "
        "signature, callers (top 5), callees (top 5), file:line."
    ),
    (
        "🔗  File dependency map",
        "For [[file path]], run tokensave_file_dependents to see every "
        "file that imports or depends on it. Then tokensave_impact on "
        "the file's exported symbols to confirm the blast radius. "
        "Output: dependency fan-in count, list of dependent files "
        "grouped by layer (same module / sibling module / top-level), "
        "and a risk rating (Low / Medium / High) for changing this file."
    ),

    # ─────────────────────── 🔄 UPGRADE INTEGRATION ──────────────────────────
    (
        "🔄  Integration audit (after upgrade)",
        "IMPORTANT — run this AFTER upgrading tokensave AND pulling the\n"
        "latest manager repo. Running before those steps produces a\n"
        "false-clean report because the analysis reads locally present\n"
        "files (CHANGELOG.md, docs/upstream-issues/, src/prompts.py).\n\n"
        "You are auditing a tokensave upgrade for TokenSave Manager.\n\n"
        "STEP 1 — What changed in tokensave?\n"
        "  Call tokensave_changelog. List every new tool, removed tool,\n"
        "  and CLI/schema change. Extract tool names exactly (word\n"
        "  boundaries — tokensave_auth_login ≠ tokensave_auth).\n\n"
        "STEP 2 — Snippet coverage\n"
        "  Read src/prompts.py. For each NEW tool from Step 1, search the\n"
        "  snippet bodies for that exact tool name. List any that have no\n"
        "  covering snippet.\n\n"
        "STEP 3 — Stale snippets\n"
        "  For each REMOVED tool from Step 1, check if any snippet body\n"
        "  still calls it. List any stale snippet titles.\n\n"
        "STEP 4 — Upstream issues\n"
        "  Read docs/upstream-issues/*.md. For each file whose first 8\n"
        "  lines lack 'STATUS: FIXED', 'STATUS: SHIPPED', or\n"
        "  'STATUS: MOOT': check if the changelog change resolves it and\n"
        "  propose the correct STATUS line to add.\n\n"
        "STEP 5 — Manager integration code\n"
        "  Read helpers/daemon_cost.py, ai_tasks_ctrl.py, agent_tools.py.\n"
        "  Flag any function that wraps a tokensave CLI command or MCP\n"
        "  tool that was removed or renamed in Step 1.\n\n"
        "STEP 6 — Output a structured action list:\n"
        "  ## Integration report — tokensave v[[new version]]\n"
        "  ### New snippets needed  (title + 2-sentence prompt body)\n"
        "  ### Stale snippets to update  (title → what to change)\n"
        "  ### Upstream issues to update  (filename → new STATUS line)\n"
        "  ### Manager code changes  (file:line → what to change)\n"
        "  ### Confirmed no-action  (anything verified as fine)"
    ),
    (
        "🔄  Generate snippet for [[new tool name]]",
        "Write a new src/prompts.py snippet for the tokensave MCP tool\n"
        "[[new tool name]].\n\n"
        "Rules:\n"
        "  • Title: emoji prefix matching the section (🔗 for new graph\n"
        "    tools, 🛠 for workflow tools, 📊 for audit tools) + 2-4 word\n"
        "    label. Match the style of existing titles.\n"
        "  • Body: 3-5 numbered steps. Each step calls [[new tool name]]\n"
        "    plus 1-2 supporting tokensave tools for context. End with a\n"
        "    prescribed output format (e.g. 'Output: X, Y, Z').\n"
        "  • Use [[double bracket]] placeholders for any user-supplied\n"
        "    arguments (symbol names, file paths, thresholds).\n"
        "  • Length: 60-120 words — match existing snippet density.\n"
        "  • Exact tool name required in the body (word boundary, not\n"
        "    as a substring of another tool call).\n\n"
        "After writing the snippet, also check: does any EXISTING snippet\n"
        "reference a tool that [[new tool name]] supersedes? If so,\n"
        "propose the minimal edit to mention the new tool alongside it."
    ),

    # ─────────────────────────── 📝 DOCUMENTATION ─────────────────────────
    # Doc-update prompts — paste into Claude session to offload markdown
    # revision work from this conversation. Used in lieu of (or alongside)
    # the manager's '📝 Doc Updates…' dialog when you want the change drafted
    # in the active session instead of by the local Ollama / Claude CLI.
    (
        "📝  README feature bullet",
        "Draft ONE README 'Features' section bullet for this change:\n\n"
        "[[describe the change]]\n\n"
        "Match the existing README bullet style: verb-first, technical\n"
        "detail in parens (file paths, helper names, threshold values),\n"
        "no marketing fluff, no emoji unless the change adds a UI element\n"
        "with one. Read 3-5 surrounding bullets from README.md first to\n"
        "anchor the voice. Output the bullet line only — no leading dash,\n"
        "no surrounding section context."
    ),
    (
        "📝  CHANGELOG [Unreleased] entry",
        "Draft CHANGELOG.md [Unreleased] entries from these commits:\n\n"
        "[[paste git log --oneline output]]\n\n"
        "Use Keep-a-Changelog headings (### Added / ### Changed / ### Fixed\n"
        "/ ### Removed). Each bullet uses conventional-commit scope prefix\n"
        "in parens, e.g. '(gitignore-dialog) 🤖 AI Suggest button — one\n"
        "click AI-powered pattern recommendations'. Output bullets only,\n"
        "NO '## [Unreleased]' header line. Read the existing [Unreleased]\n"
        "block first so you don't repeat anything already there."
    ),
    (
        "📝  Architecture doc section update",
        "Update the section titled '[[section name]]' in [[doc path]] to\n"
        "reflect this change:\n\n"
        "[[describe the architectural change]]\n\n"
        "Read the surrounding two sections first so the voice, depth, and\n"
        "code-example density match exactly. If the change adds a new\n"
        "pattern, document the pattern + the file:line that exemplifies it.\n"
        "If it moves/renames something, update every reference inside the\n"
        "section. Output the FULL replacement section including its '##' or\n"
        "'###' header line — nothing outside the section."
    ),
    (
        "📝  Memory file entry",
        "Draft a Claude memory file entry summarising:\n\n"
        "[[Roadmap-N work / session topic / lesson learned]]\n\n"
        "Format: YAML frontmatter then markdown body. Frontmatter keys:\n"
        "  name: kebab-case-id\n"
        "  description: \"one-line summary for the MEMORY.md index\"\n"
        "  metadata:\n"
        "    node_type: memory\n"
        "    type: project-knowledge   # or 'project' or 'lesson-learned'\n\n"
        "Body: ~150-400 words. Focus on what future-me needs to know — file\n"
        "paths, locked decisions, gotchas, reusable patterns. NOT a play-\n"
        "by-play of the work, just the durable knowledge worth keeping."
    ),
    (
        "📝  Cross-doc consistency check",
        "Compare README.md + CHANGELOG.md + docs/ROADMAP.md against the\n"
        "actual git log for the last [[N]] commits.\n\n"
        "Find inconsistencies:\n"
        "  • Feature mentioned in CHANGELOG but missing from README\n"
        "    Features section\n"
        "  • ROADMAP item still marked 🟡/🔮 but actually shipped per\n"
        "    git log\n"
        "  • README claim about behaviour that no longer matches the code\n"
        "    (cite file:line via tokensave_search to verify)\n"
        "  • CHANGELOG attribution drift (scope prefix doesn't match the\n"
        "    actual directory changed)\n\n"
        "Output a per-file action list — exact line to change + suggested\n"
        "replacement text."
    ),
    (
        "📝  Migration / breaking-change note",
        "Draft a 'Migration notes' section for version [[X.Y.Z]] from\n"
        "these breaking commits:\n\n"
        "[[commits or describe the breaking changes]]\n\n"
        "Format per breaking change:\n"
        "  ### What broke: [one-line description]\n"
        "  **Before:** [code/config sample showing the old way]\n"
        "  **After:**  [code/config sample showing the new way]\n"
        "  **Why:** [one-sentence rationale]\n\n"
        "Cite file:line for the exact symbol(s) that changed. Order from\n"
        "most likely to hit a user → least likely."
    ),
    (
        "📝  PR description from CHANGELOG slice",
        "Read the current CHANGELOG.md [Unreleased] section. Build a PR\n"
        "description from its contents:\n\n"
        "## Summary\n"
        "  1-3 bullets — the WHY of the change set, not a verbatim copy of\n"
        "  the changelog bullets. Group related items.\n\n"
        "## Test plan\n"
        "  Bulleted markdown checklist of TODOs for verifying the PR. For\n"
        "  each item that touches AI / network / external services, note\n"
        "  the manual setup needed.\n\n"
        "## Migration notes (if any)\n"
        "  Only include if the [Unreleased] section has breaking changes.\n\n"
        "Output the markdown body only — no PR-title line, no leading\n"
        "metadata. Caller pipes this to `gh pr create --body-file -`."
    ),
]
