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
        "  • tokensave_health          (overall metrics)\n"
        "  • tokensave_complexity      (high-complexity functions)\n"
        "  • tokensave_god_class       (god classes / large files)\n"
        "  • tokensave_circular        (circular dependencies)\n"
        "  • tokensave_coupling        (high-coupling modules)\n"
        "  • tokensave_dead_code       (unused code)\n"
        "  • tokensave_unused_imports  (unused imports)\n"
        "  • tokensave_hotspots        (high-churn files)\n"
        "  • tokensave_recursion       (recursive call sites)\n"
        "  • tokensave_unsafe_patterns (risky patterns)\n"
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
        "Find functional code duplication in this project. Don't rely on "
        "name similarity alone — chain these tools:\n"
        "  1. tokensave_similar on the top 5 from tokensave_complexity "
        "(naming overlap is a weak first-pass signal). "
        "SKIP names that are pure Tkinter boilerplate: __init__, _build, "
        "_build_*, _on_* — shared name is convention, not shared logic.\n"
        "  2. tokensave_signature_search for the 3 most common signature "
        "shapes (e.g. (str) -> bool). Same-shape functions across "
        "modules are dupe candidates regardless of name. "
        "SKIP event-handler stubs: (self), (self, *_), (self, _evt=None) "
        "— no logic to share.\n"
        "  3. For every candidate pair: tokensave_node to fetch both "
        "bodies. Compare side-by-side for: same algorithm with renamed "
        "vars, same control flow with different error handling, same "
        "data transform with different field names.\n"
        "  4. Cross-reference with tokensave_dead_code — true duplicates "
        "often manifest as one copy being half-dead. Ignore dead-code "
        "hits on tkinter callbacks (command=self._x), attribute imports "
        "(constants.C, tkinter.ttk), and TYPE_CHECKING guards — these "
        "are confirmed false positives in Python/Tk projects.\n"
        "  5. Bucket findings: 🔴 Definite duplicate (>80% body "
        "overlap) | 🟡 Likely refactor target (same algorithm, "
        "different shape) | 🔵 Naming overlap only (false positive). "
        "For each 🔴/🟡 pair, cite both file:line locations and propose "
        "a single canonical helper to keep + the deletion path for the "
        "rest. Cap output at 10 pairs."
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
]
