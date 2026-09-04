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
        "tokensave_files for the directory layout, then tokensave_entities "
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
    (
        "🧭  Entity outline",
        "Run tokensave_entities on [[file path]] for its structural "
        "outline — every class, function, and method in declaration "
        "order with file:line. Use it as the entry point for a guided "
        "file tour: give a one-line purpose per top-level entity, then "
        "flag the 2-3 worth reading first for a newcomer."
    ),
    (
        "🧭  Find non-code artifacts",
        "Locate specs, fixtures, schemas, and configs with tokensave_files "
        "kind=artifact. Since v7.9.0 the index tracks .feature / .json / "
        ".yaml / .sql / .toml / .proto / .graphql / .md by path — they carry "
        "no symbols, so they answer 'where is it?', not 'what calls it?'.\n\n"
        "  1. tokensave_files kind=artifact pattern=[[glob, e.g. "
        "**/*.feature]] — the artifact inventory.\n"
        "  2. tokensave_files kind=code on the same directory to pair each "
        "artifact with the code sitting beside it.\n"
        "  3. tokensave_search on a distinctive string from the artifact (a "
        "key name, a scenario title) to find its readers.\n\n"
        "Output: artifact path | size | the code file most likely to consume "
        "it. This is the supported route for path-shaped discovery — Glob, "
        "find -name, and fd --extension are redirected here by the "
        "PreToolUse hook."
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
    (
        "🔬  Symbol git history",
        "Trace how [[symbol name]] evolved over time. Step 1: "
        "tokensave_blame for per-line authorship of the current body. "
        "Step 2: tokensave_log for its commit history — it follows the "
        "symbol across file renames via structural fingerprints, not just "
        "a path match. Output a timeline (short sha + date | what changed "
        "| file:line) and flag the commit that introduced the behaviour "
        "you're investigating."
    ),
    (
        "🔬  Annotation / decorator survey",
        "Map decorator/annotation usage with tokensave_annotations. Call "
        "it with no name for a histogram of the most-used annotations "
        "across the project, then in site mode on [[annotation name]] to "
        "list every (annotation, target) pair. Output a top-10 usage "
        "table, then for [[annotation name]] every symbol it decorates "
        "with file:line — handy for auditing @property, @pytest.mark.tk, "
        "route handlers, etc."
    ),

    # ─────────────────────────── 📊 AUDITS ────────────────────────────────
    (
        "📊  Full health audit",
        "Run a comprehensive code-health audit using ALL of these "
        "tokensave tools and produce one structured report:\n"
        "  • tokensave_health(details=true)  (overall + per-dimension sub-scores)\n"
        "  • tokensave_complexity            (cyclomatic complexity, cognitive "
        "complexity, CRAP score, Halstead metrics, maintainability index)\n"
        "  • tokensave_god_class             (god classes / large files)\n"
        "  • tokensave_circular              (circular dependencies, symbol-level)\n"
        "  • tokensave_imports               (module-level import cycles — the "
        "right unit for planning a decomposition; circular counts call edges, so "
        "one 'from mod_b import X' yields zero or fifty depending on usage)\n"
        "  • tokensave_coupling              (high-coupling modules)\n"
        "  • tokensave_redundancy            (AST-level functional duplicates)\n"
        "  • tokensave_dead_code             (unused code)\n"
        "  • tokensave_unused_imports        (unused imports)\n"
        "  • tokensave_hotspots              (high-churn files)\n"
        "  • tokensave_recursion             (recursive call sites)\n"
        "  • tokensave_unsafe_patterns       (risky patterns)\n"
        "Format: 🔴 Critical / 🟡 Warning / 🔵 Info, each with file:line "
        "and a one-sentence suggested fix. End with a top-5 priority list. "
        "Calibration caveat: v7.3.0 fixed the major Python false-positive "
        "sources (closures, dict/arg value references, nested functions), and "
        "v7.9.0 closed two more — imports written inside function bodies are "
        "now indexed, and calls made outside a function body (module-level "
        "statements, const initializers) now count as uses. Remaining risk is "
        "narrower than it used to be: Tkinter dynamic dispatch "
        "(command=self._x) and attribute-only imports (constants.C, "
        "C[\"key\"]) still produce false positives — grep-verify those two "
        "classes before recommending deletion."
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
        "  • tokensave_circular         (architectural debt, symbol-level)\n"
        "  • tokensave_imports          (module-level import cycles)\n"
        "Output a Release Readiness Checklist with ✅/⚠/❌ per item. "
        "End with: 'Safe to release: yes/no, blockers: [...]'."
    ),
    (
        "📊  Dependency manifest audit",
        "Run tokensave_dependencies to introspect this project's package "
        "manifest(s) — it auto-detects the ecosystem (Cargo.toml, "
        "package.json, pyproject.toml, go.mod, …) and returns a uniform "
        "shape, one block per ecosystem in a polyglot repo. Output: each "
        "direct dependency with its declared version, any version_drift "
        "across workspace members, and a flag on every unpinned / "
        "wildcard ('*') range to tighten before release. Pass "
        "include_lockfile=true for resolved versions."
    ),
    (
        "📊  Test coverage rollup",
        "Run tokensave_test_coverage in file mode on [[file path]] for a "
        "per-symbol tested/untested classification — transitive call "
        "expansion means a helper covered only through another function "
        "still counts. Then run symbol mode on the highest-risk untested "
        "entry to confirm no test reaches it. Output a ranked 'needs a "
        "test' list (symbol | file:line | why it matters), capped at the "
        "top 10."
    ),

    # ─────────────────────────── 🪦 FINDINGS / CLEANUP ────────────────────
    (
        "🪦  Find dead code",
        "Run tokensave_dead_code and tokensave_unused_imports. For each "
        "finding, run tokensave_callers to double-check. v7.9.0 cut the "
        "false-positive rate sharply — calls made outside a function body "
        "(module-level statements, const initializers) now count as uses, "
        "and function-body imports are indexed — and v7.10.0 cut it again "
        "by excluding symbols named as ambiguity candidates, so a 'dead' "
        "hit is more likely to be genuinely dead than it was. What the "
        "indexer still can't trace: reflection and Tk dynamic dispatch "
        "(command=self._x). "
        "Output two groups: 'Safe to delete (verified zero callers)' and "
        "'Verify before deleting (might be called dynamically)' with "
        "file:line for each."
    ),
    (
        "🪦  Ambiguous, or actually dead?",
        "Use this before deleting anything tokensave_dead_code flagged, or "
        "when a symbol you can see being called reports no callers. Since "
        "v7.10.0 the resolver refuses to guess: when several candidates tie "
        "on score it emits NO call edge and records the tie in "
        "ambiguous_calls instead. So a missing caller can mean 'the graph "
        "could not tell which same-named target this hits', not 'nothing "
        "calls it'. Run tokensave_ambiguous_calls and check whether "
        "[[symbol]] appears among the candidates. If it does, the call site "
        "is real — open it and read which overload it actually reaches. "
        "Treat a symbol as dead ONLY when it is absent from BOTH "
        "tokensave_callers and the ambiguity candidates. Output: symbol | "
        "file:line | verdict (dead / ambiguous) | for ambiguous, the "
        "competing candidates and which one the call site really means."
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
    (
        "\U0001f310  Query another project",
        "Answer this about a DIFFERENT project than the one this session "
        "is serving: [[what you want to know]]\n\n"
        "The project is at [[project path]]. Pass "
        "graph_root=\"[[project path]]\" on EVERY tokensave call you "
        "make for it — tokensave_context first, then tokensave_search / "
        "tokensave_node / tokensave_callers as needed. Without "
        "graph_root you will silently answer from the pinned project "
        "instead, which looks completely normal and is wrong.\n\n"
        "Two things to know: the selected graph opens read-only, so use "
        "this for reading and reviewing, not for edits; and if the reply "
        "warns that the graph is stale, say so rather than treating the "
        "answer as current — the other project may not have been synced "
        "recently.\n\n"
        "Cite file:line for every claim, and state which project each "
        "citation came from."
    ),

    # ─────────────────────────── 🛠 WORKFLOWS ─────────────────────────────
    (
        "🛠  Pre-commit checklist",
        "I'm about to commit. Run:\n"
        "  • tokensave_commit_context   (semantic summary: changed symbols, "
        "file roles, recent commit style — richer input for the commit "
        "message than raw diff)\n"
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
    (
        "🛠  Review a change set",
        "Summarise everything that changed between [[from ref]] and "
        "[[to ref]] using tokensave_diff — one envelope that orchestrates "
        "changelog, commit_context, and diff_context. Output added / "
        "removed / modified symbols grouped by file, each with a one-line "
        "risk note, then a short 'what a reviewer should focus on' "
        "summary. For an uncommitted working tree instead of a ref range, "
        "use tokensave_diff_context."
    ),

    # ─────────────────────────── 🔗 v6-v7 NEW TOOLS ──────────────────────
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
        "FIRST run the manager's graph-trust check on this project. "
        "tokensave binds an unqualified call on an untracked receiver "
        "to the sole repo-wide symbol of that name, ignoring module "
        "scope, so production code can bind to same-named test doubles "
        "(a fake widget defines `after`; a fake logger defines `info`). "
        "Those edges feed acyclicity, and quality_signal is the "
        "geometric mean over all six dimensions, so BOTH are unsafe to "
        "optimise or compare across runs while the graph is tainted.\n\n"
        "Then run tokensave_health with details=true for the "
        "per-dimension breakdown (acyclicity, depth, equality/gini, "
        "redundancy, modularity, coverage discipline). For each "
        "sub-score below 0.9 that the trust check has NOT quarantined: "
        "explain what it measures, what's causing it (use "
        "tokensave_gini / tokensave_redundancy / tokensave_largest to "
        "drill in), and propose one concrete action.\n\n"
        "Do NOT report a quality_signal target or a before/after delta "
        "while any dimension is quarantined - that number is partly "
        "measuring name collisions in the test tree. Base every "
        "refactoring decision on a trustworthy dimension, or on direct "
        "source-level measurement instead."
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
        "DISCOUNT any dependent inside the test tree: production code "
        "cannot be imported BY a test file it does not know about, so "
        "a src -> tests dependent is an artefact of tokensave's "
        "bare-name call resolution, not a real edge. Run the manager's "
        "graph-trust check first if the list looks surprising. "
        "Output: dependency fan-in count, list of dependent files "
        "grouped by layer (same module / sibling module / top-level), "
        "and a risk rating (Low / Medium / High) for changing this file."
    ),
    (
        "🔗  Module import graph & cycle cuts",
        "Plan a decomposition with tokensave_imports — module-level "
        "dependencies, which is the right unit for the job. "
        "tokensave_circular works on symbol edges, so one 'from mod_b import "
        "X' yields zero call edges or fifty depending on how X is used.\n\n"
        "  1. tokensave_imports — every module dependency, each with the "
        "import statement's file:line. Imports inside function bodies come "
        "back flagged 'lazy'.\n"
        "  2. Locate the cycle containing [[module name]].\n"
        "  3. Re-run with the cut simulation on a candidate dependency — it "
        "recomputes the components as if that one edge were gone, which is "
        "what separates a cut that actually breaks the cycle from one that "
        "leaves everything still mutually reachable.\n"
        "  4. tokensave_impact on the symbols crossing the cut for blast "
        "radius.\n\n"
        "Output: cycle members, ranked cut candidates (dependency | "
        "file:line | components after the cut), and the one edge to break "
        "first."
    ),
    (
        "🌐  Query a sibling checkout",
        "Answer a question spanning two repos without leaving this session. "
        "Semantic tokensave tools take graph_root (plus an optional "
        "graph_branch) to read another initialized project read-only — it "
        "never initializes, syncs, or changes the served default.\n\n"
        "  1. tokensave_status — initialized siblings beside this root are "
        "named there, and in otherwise-empty search/context results.\n"
        "  2. tokensave_search graph_root=[[sibling project path]] for "
        "[[symbol name]] — an empty result now means absent, not "
        "unreachable.\n"
        "  3. tokensave_context graph_root=[[sibling project path]] for the "
        "surrounding code.\n\n"
        "Output: which repo the symbol lives in (repo + file:line), its "
        "contract, and how the two sides are coupled. Node IDs come back "
        "graph-namespaced, so follow-up traversal stays on the right graph."
    ),

    (
        "🧠  Session memory",
        "Track quality improvements across a session and persist durable "
        "decisions that survive context resets.\n\n"
        "  1. tokensave_session_start  — save a health baseline before "
        "starting work.\n"
        "  2. Work normally; at the end run tokensave_session_end for a "
        "before/after quality_signal diff showing what improved or "
        "degraded.\n"
        "  3. For any design or architecture choice worth preserving, call "
        "tokensave_record_decision (text, reason, tags, files) — decisions "
        "are stored in the per-project DB and survive context resets.\n"
        "  4. In a future session, tokensave_session_recall [[optional "
        "query]] does FTS search across decision text and reason (omit the "
        "query for recency-ordered recall) — no need to re-explain choices "
        "already made.\n\n"
        "Output: before/after metric delta + decisions matching the query."
    ),
    (
        "📖  Companion docs lookup",
        "Check whether a file has companion documentation before reading "
        "its code.\n\n"
        "  1. tokensave_entities [[file path]] — each entry now reports "
        "has_doc (bool) and doc_path. One doc file can cover many "
        "implementation files (many-to-many via an applies_to glob), so "
        "the doc may answer questions about several related files at once.\n"
        "  2. If has_doc is true: tokensave_doc [[file path]] returns the "
        "doc content, every file it covers, and a drift signal — doc_stale: "
        "true when code was committed after the doc was last updated.\n"
        "  3. Read the code only if the doc doesn't fully answer the "
        "question, or if doc_stale is true (doc may be out of date).\n\n"
        "Output: doc summary + stale warning if drift detected. Note: "
        "tokensave_doc is available when the project has a docs_dir "
        "configured or sidecar *.readme.md files alongside the code."
    ),
    (
        "🎯  Test risk ranking",
        "Find where the next test will do the most good.\n\n"
        "Risk = (cyclomatic + 1) × (fan_in + 1) × untested_multiplier — "
        "symbols that are complex, heavily called, and uncovered score "
        "highest.\n\n"
        "  1. tokensave_test_risk [[file or omit for whole project]] — "
        "top N by risk score.\n"
        "  2. For each top entry: tokensave_node to read the body, "
        "tokensave_callers to confirm real fan-in, tokensave_test_coverage "
        "to check whether any test transitively reaches it.\n"
        "  3. Draft a test scenario for the top entry.\n\n"
        "Output: ranked table — symbol | file:line | risk score | why it "
        "matters | suggested test scenario. Cap at top 10."
    ),
    (
        "🔬  Find implementations of a method",
        "Find every type that defines a method named [[method name]] using "
        "tokensave_implementations method=[[method name]]. Useful for "
        "patterns like save_into, _apply, _build_*, render where many "
        "types share the same interface. For each match:\n\n"
        "  1. tokensave_node — read the body.\n"
        "  2. tokensave_callers — see which callers drive it.\n"
        "  3. Flag diverging implementations: different return types, "
        "missing error handling, or significantly different logic.\n\n"
        "Output: table — type | file:line | signature — then a 'consistency "
        "verdict' noting any implementations that deviate from the majority "
        "pattern."
    ),
    (
        "🌿  Branch code-graph comparison",
        "Compare the code graphs of two branches — structural changes "
        "beyond what git diff shows.\n\n"
        "  1. tokensave_branch_diff base=[[base branch]] "
        "head=[[feature branch]] — symbols added, removed, and changed "
        "(signature differs).\n"
        "  2. tokensave_impact on changed symbols to estimate blast "
        "radius.\n"
        "  3. tokensave_affected to surface which tests are now "
        "implicated.\n\n"
        "Output: three groups (Added / Removed / Changed) with file:line "
        "and a one-line risk note per entry; flag any public-API change "
        "that has callers outside the changed files."
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
        "  NOTE: tokensave_changelog diffs THIS project's git refs — it\n"
        "  does NOT enumerate tokensave's own tool roster. Instead, read\n"
        "  tokensave's upstream release notes for every version after the\n"
        "  one you had installed:\n"
        "    gh release view vX.Y.Z --repo aovestdipaperino/tokensave\n"
        "  (or fetch CHANGELOG.md from that repo). List every new tool,\n"
        "  removed tool, renamed tool, and CLI/schema change. Extract tool\n"
        "  names exactly (word boundaries — tokensave_auth_login ≠\n"
        "  tokensave_auth, and tokensave_diff ≠ tokensave_diff_context).\n\n"
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
        "STEP 4.5 — Infrastructure changes (v7.2 + v7.6)\n"
        "  • v7.6.0: tokensave now writes rules to ~/.claude/rules/tokensave.md\n"
        "    instead of appending to CLAUDE.md. Verify the managed file exists;\n"
        "    confirm CLAUDE.md no longer contains a tokensave marker block.\n"
        "    Any upstream-issues doc about CLAUDE.md rule injection → STATUS: MOOT.\n"
        "  • v7.2.0: .tokensave/project.json lets users index extensionless files\n"
        "    with a language override — an alternative to shadow links.\n\n"
        "STEP 5 — Manager integration code\n"
        "  Read helpers/savings.py, agent_tools.py.\n"
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
