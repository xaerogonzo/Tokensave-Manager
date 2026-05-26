"""Doc-update drafter logic — pure functions called by the doc-drafter dialog.

Responsibilities:
  - **Commit-range resolution**: turn a high-level mode ("since last doc
    commit", "since last release", "custom") into a concrete git range +
    list of commits.  Handles the mixed-commit edge case (a commit that
    touched BOTH code and docs in the same commit).
  - **Prompt building**: per-target (CHANGELOG / README) system + user
    prompts that include the commit list, the existing target-section
    content, optional CLAUDE.md blueprint context, and sparse-commit
    hints when the user's commit messages are too short to be useful.
  - **LLM dispatch**: routes to ``call_claude_cli_print`` or
    ``_call_llm`` based on the provider in ``llm_cfg``.

Pure-function module — no Tkinter.  Safe to call from any thread.
"""

from __future__ import annotations

import os
import re
import subprocess

from constants import CREATE_NO_WINDOW
from helpers.release import (
    _commits_since, _last_release_tag,
)


# Doc files whose modifications mark a commit as "documented".  Used by
# "since last doc commit" mode.  Deliberately narrow: only CHANGELOG.md and
# README.md count as "doc anchors" so that adding new docs/ files (e.g.
# AGENT_BACKENDS.md, ARCHITECTURE.md) doesn't accidentally mask unrecorded
# code commits by moving the anchor forward to HEAD.
_DOC_PATHSPECS = ["CHANGELOG.md", "README.md"]

# Average commit-subject character length below which we trigger the
# sparse-commit safety net (extra prompt hint + changed-file path list).
_SPARSE_AVG_THRESHOLD = 15


# ── Commit-range resolution ──────────────────────────────────────────────────

def _last_doc_commit_sha(project_path, git_exe, pathspecs=None):
    """Return the SHA of the most recent commit that touched the given paths.

    ``pathspecs`` overrides the module-level ``_DOC_PATHSPECS`` so each
    DocType can use its own anchor file(s) instead of the shared fallback.
    """
    specs = pathspecs if pathspecs else _DOC_PATHSPECS
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "log", "-n", "1",
             "--pretty=format:%H",
             "--diff-filter=AM", "--", *specs],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _commit_touches_code(project_path, sha, git_exe):
    """Return True if a commit touched any file OUTSIDE the doc set.

    A commit that only touched CHANGELOG / README / docs/ is pure-doc.
    A commit that ALSO touched code files is "mixed" — we want to INCLUDE
    it in the range so its code changes are visible to the drafter, even
    though its doc changes are already documented.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "show", "--name-only",
             "--pretty=format:", sha],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    files = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    for f in files:
        if f == "CHANGELOG.md" or f == "README.md":
            continue
        if f.startswith("docs/"):
            continue
        return True
    return False


def resolve_commit_range(project_path, mode, custom_ref, git_exe, pathspecs=None):
    """Resolve a commit range from a high-level mode to a concrete spec + commits.

    Args:
        project_path: Absolute path to the project repo.
        mode:         One of ``"since_last_doc"``, ``"since_last_commit"``,
                      ``"since_last_tag"``, ``"custom"``.
        custom_ref:   Used only when ``mode == "custom"``.  May be a single
                      ref (treated as ``<ref>..HEAD``) or a full ``A..B`` range.
        git_exe:      Path to git executable.

    Returns dict::

        {
            "range_label":     "Since last doc commit (abc123)",  # for UI / prompt
            "range_spec":      "abc123..HEAD",                    # git rev range
            "commits":         [{"hash": ..., "subject": ..., "body": ...}],
            "boundary_mixed":  True/False,
            "boundary_note":   str | None,
        }

    On any failure (no commits, malformed mode, git failure) returns dict
    with empty ``commits`` and a ``range_label`` describing the issue.
    """
    if mode == "since_last_doc":
        return _resolve_since_last_doc(project_path, git_exe, pathspecs)
    if mode == "since_last_commit":
        return {
            "range_label":    "Since last commit (HEAD~1..HEAD)",
            "range_spec":     "HEAD~1..HEAD",
            "commits":        _commits_since(project_path, "HEAD~1", git_exe),
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    if mode == "since_last_tag":
        return _resolve_since_last_tag(project_path, git_exe)
    if mode == "custom":
        return _resolve_custom(project_path, custom_ref, git_exe)
    return {
        "range_label":    f"Unknown mode: {mode!r}",
        "range_spec":     "",
        "commits":        [],
        "boundary_mixed": False,
        "boundary_note":  None,
    }


def _resolve_since_last_doc(project_path, git_exe, pathspecs=None) -> dict:
    """Range resolver for mode='since_last_doc'."""
    sha = _last_doc_commit_sha(project_path, git_exe, pathspecs)
    if not sha:
        return {
            "range_label":    "All commits (no prior doc commit found)",
            "range_spec":     "HEAD",
            "commits":        _commits_since(project_path, None, git_exe),
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    # Mixed-commit handling: if the boundary commit touched code as well
    # as docs, INCLUDE it in the range using ``<sha>^..HEAD`` so its
    # code changes don't get lost.  Pure-doc boundary just uses ``..``.
    mixed = _commit_touches_code(project_path, sha, git_exe)
    if mixed:
        range_spec = f"{sha}^..HEAD"
        note = (
            f"Note: commit {sha[:8]} is a boundary commit with mixed "
            "content — it already includes its own doc updates.  "
            "Reference its CODE changes only when drafting; do NOT "
            "re-describe what its doc diff already said."
        )
    else:
        range_spec = f"{sha}..HEAD"
        note = None
    return {
        "range_label":    f"Since last doc commit ({sha[:8]})",
        "range_spec":     range_spec,
        "commits":        _commits_since(project_path,
                                         sha if not mixed else f"{sha}^",
                                         git_exe),
        "boundary_mixed": mixed,
        "boundary_note":  note,
    }


def _resolve_since_last_tag(project_path, git_exe) -> dict:
    """Range resolver for mode='since_last_tag'."""
    tag = _last_release_tag(project_path, git_exe)
    if not tag:
        return {
            "range_label":    "All commits (no release tag found)",
            "range_spec":     "HEAD",
            "commits":        _commits_since(project_path, None, git_exe),
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    return {
        "range_label":    f"Since last release tag ({tag})",
        "range_spec":     f"{tag}..HEAD",
        "commits":        _commits_since(project_path, tag, git_exe),
        "boundary_mixed": False,
        "boundary_note":  None,
    }


def _resolve_custom(project_path, custom_ref, git_exe) -> dict:
    """Range resolver for mode='custom'."""
    ref = (custom_ref or "").strip()
    if not ref:
        return {
            "range_label":    "Custom range (empty)",
            "range_spec":     "",
            "commits":        [],
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    # Single ref → treat as <ref>..HEAD; already A..B → pass through.
    if ".." not in ref:
        range_spec = f"{ref}..HEAD"
        commits = _commits_since(project_path, ref, git_exe)
    else:
        range_spec = ref
        commits = _commits_in_range(project_path, range_spec, git_exe)
    return {
        "range_label":    f"Custom range ({range_spec})",
        "range_spec":     range_spec,
        "commits":        commits,
        "boundary_mixed": False,
        "boundary_note":  None,
    }


def _commits_in_range(project_path, range_spec, git_exe):
    """Variant of `_commits_since` that takes an arbitrary `A..B` range.

    Needed for the "custom range" mode where the user may pass a
    non-HEAD endpoint (e.g. ``main..feature/foo``).
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "log", range_spec,
             "--pretty=format:%H%x09%s%x09%b%x1f"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    commits = []
    for record in proc.stdout.split("\x1f"):
        if not record.strip():
            continue
        record = record.lstrip("\n")
        parts = record.split("\x09", 2)
        if len(parts) < 2:
            continue
        commits.append({
            "hash":    parts[0],
            "subject": parts[1] if len(parts) >= 2 else "",
            "body":    parts[2] if len(parts) >= 3 else "",
        })
    return commits


# ── Context gathering ───────────────────────────────────────────────────────

def changed_file_paths(project_path, range_spec, git_exe):
    """Return the deduped list of file paths changed in ``range_spec``.

    Used when sparse-commit mode triggers, so the LLM gets explicit
    structural signal beyond just commit subjects.  Capped at 60 paths.
    """
    if not range_spec:
        return []
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "diff", "--name-only",
             range_spec],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    seen = []
    for ln in proc.stdout.splitlines():
        p = ln.strip()
        if p and p not in seen:
            seen.append(p)
        if len(seen) >= 60:
            break
    return seen


def read_blueprint_context(project_path, max_chars=1500):
    """Return the first ``max_chars`` of CLAUDE.md or BASIC_INSTRUCTIONS.md.

    Tries ``CLAUDE.md`` first (Claude Code convention), then
    ``BASIC_INSTRUCTIONS.md`` (this manager's convention).  Returns the
    empty string if neither exists or both are unreadable.

    Capped at ``max_chars`` so the prompt doesn't bloat — the goal is a
    structural map for the model, not the full document.
    """
    for name in ("CLAUDE.md", "BASIC_INSTRUCTIONS.md"):
        p = os.path.join(project_path, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read(max_chars + 1)
        except OSError:
            continue
        if text:
            return text[:max_chars]
    return ""


def is_sparse(commits, threshold=_SPARSE_AVG_THRESHOLD):
    """Return True if the average commit-subject length is under ``threshold``.

    Used by the prompt builders to inject the sparse-commit safety hint
    + explicit changed-file list.
    """
    if not commits:
        return False
    subjects = [c.get("subject", "").strip() for c in commits]
    subjects = [s for s in subjects if s]
    if not subjects:
        return False
    avg = sum(len(s) for s in subjects) / len(subjects)
    return avg < threshold


# ── Prompt builders ─────────────────────────────────────────────────────────

_CHANGELOG_SYSTEM = (
    "You are a CHANGELOG.md maintainer.  Draft NEW Keep-a-Changelog "
    "bullets for the provided commit range.  The patcher will APPEND "
    "these bullets to the existing [Unreleased] sub-sections — your "
    "output ADDS to what's already there; it does NOT replace it.\n\n"
    "Output format (exactly this — nothing else):\n\n"
    "### Added\n"
    "- bullet 1\n"
    "- bullet 2\n\n"
    "### Fixed\n"
    "- bullet 3\n\n"
    "### Changed\n"
    "- bullet 4\n\n"
    "Rules:\n"
    "- Output ONLY ### Added / ### Changed / ### Fixed / ### Removed "
    "headers followed by bullets.  Omit any section with no new bullets.\n"
    "- If a section has NO new bullets to add, OMIT THE ENTIRE `### Section` "
    "HEADER.  Do NOT write `- None`, `- N/A`, `- TBD`, `- nothing to add`, "
    "or any other placeholder under an empty header.  An ABSENT section is "
    "the correct signal for 'nothing changed here' — a placeholder bullet "
    "is NOT.\n"
    "- No '## [Unreleased]' line, no prose preamble, no markdown code "
    "fences, no trailing explanation.\n"
    "- Output ONLY bullets describing the provided commit range.  Do NOT "
    "echo, summarise, restate, or re-describe bullets already in the "
    "'Current [Unreleased] content' below — the patcher keeps all of "
    "those VERBATIM.  Repeating them creates duplicates.\n"
    "- If the provided commits don't introduce anything not already in "
    "the current [Unreleased] content, output nothing.\n"
    "- Each bullet uses conventional-commit scope prefix in parens, e.g. "
    "'(gitignore-dialog) 🤖 AI Suggest button — one-click pattern recs'.\n"
    "- Cite file paths and helper names in backticks.  No marketing fluff.\n"
    "- AIM for ONE bullet per distinct code change.  A commit that touches "
    "three subsystems likely warrants three bullets, not one summary.  If "
    "the commit range spans N commits, target 3-8 bullets per ### section "
    "unless the changes are genuinely trivial.  HIDING multiple shipped "
    "changes behind one generic bullet ('add filtering and sanitisation') "
    "is a documentation failure.\n"
    "- CITE specific file paths and helper/class names in backticks. "
    "'add bullet-quality filters for truncation' is too generic. "
    "'`_looks_truncated` stop-word heuristic in `dialogs/doc_drafter.py`' "
    "is the correct level of detail.\n"
    "- When the commit range spans MULTIPLE distinct features (e.g. a "
    "truncation fix AND a redundancy filter AND a CI cleanup), produce "
    "separate bullets for each feature — do NOT collapse them into one."
)

_README_SYSTEM = (
    "You are a README.md maintainer.  Draft ONE bold sub-section for the "
    "'Recent highlights (Unreleased)' block, describing only what the "
    "provided commit range actually changes.\n\n"
    "Output format (exactly this — nothing else):\n"
    "  **<sub-section header>**\n"
    "  - bullet 1\n"
    "  - bullet 2\n"
    "  - …\n\n"
    "The patcher will SPLICE this sub-section into the highlights block.  "
    "If a sub-section with the same header already exists, its bullets "
    "are REPLACED.  Otherwise the new sub-section is inserted at the top "
    "of the highlights block.\n\n"
    "Rules:\n"
    "- ⚠ LOAD-BEARING RULE — preserve all existing sub-section bullets "
    "VERBATIM.  The patcher REPLACES the entire sub-section, so omitting "
    "a bullet DELETES it from the file.  If a matching sub-section "
    "already exists in the current body, ALL of its existing bullets "
    "MUST appear in your output verbatim, BEFORE the new bullets you "
    "add.  A code-side safety check (mirror-contract enforcement) "
    "hard-rejects any draft that drops more than 25% of existing "
    "bullets — your draft will NOT be applied if you break this rule.\n"
    "- Output exactly ONE sub-section.  No surrounding anchor line, no "
    "`---` separator, no other sub-sections, no prose preamble, no "
    "explanations, no markdown code fences.\n"
    "- The first line MUST be the bold header.  Inspect the 'Candidate "
    "sub-section headers' list in the user prompt below.  If any "
    "candidate thematically matches your new bullets (e.g. continuing "
    "an ongoing roadmap effort), output that candidate VERBATIM — "
    "copy it character-for-character including emoji and dash style.  "
    "When no candidate fits, create a NEW header using a CONCRETE "
    "roadmap number from context (e.g. `**Roadmap-6 — short topic**` "
    "or `**Roadmap-7 — short topic**`), or `**Feature area — short "
    "topic**` for non-roadmap work.\n"
    "- Bullets are verb-first, technical detail in parens (file paths, "
    "helper names, threshold values).  No marketing fluff.  Match the "
    "voice of bullets in the 'Current highlights body' below.\n"
    "- Use the project's existing emoji prefix when obvious (🤖 for AI, "
    "🔍 for review, 🔌 for MCP, 🦙 for Ollama, ⚙ for settings, etc.).\n"
    "- For the NEW bullets you ADD beyond the existing sub-section "
    "content, AIM for ONE bullet per distinct shipped feature.  A commit "
    "range that spans three features likely warrants three new bullets, "
    "not one summary mega-bullet ('updates — feature A, feature B, "
    "feature C').  If the commit range spans N distinct features, target "
    "N new bullets.\n"
    "- CITE specific file paths and helper/class names in backticks. "
    "'doc-drafter updates — backend dropdown, noop filtering, dedup' is "
    "too generic.  'backend override dropdown (`_backend_override_var` "
    "in `dialogs/doc_drafter.py`)' is the correct level of detail.\n"
    "- When the commit range spans MULTIPLE distinct features (a UI "
    "dropdown AND a new filter AND a CI cleanup), produce SEPARATE new "
    "bullets for each — do NOT collapse them into one comma-separated "
    "mega-bullet.  Existing bullets stay verbatim; new bullets get the "
    "per-feature treatment."
)


# Phase 2.0 — sub-section header detection for the prompt's candidate-list
# block.  Whitespace tolerance before the optional colon catches manually-
# edited `**Header** :` forms.  Indented `  **Header**  ` also matches.
# Mid-line bold inside a paragraph does NOT match (entire line must be
# the bold span — anchored with `^\s*` and `\s*:?\s*$`).
#
# Used ONLY for prompt enumeration.  The apply-time matching stays with
# `_normalise_subheader` in `helpers/readme_patch.py` — that's the single
# source of truth for "does this header match an existing sub-section".
_SUBSECTION_HEADER_RE = re.compile(
    r"^\s*(\*\*[^\n*][^\n]*\*\*)\s*:?\s*$"
)


def _extract_subsection_headers(highlights_body):
    """Return list of bold-header lines from a highlights body, verbatim.

    Used by `build_readme_prompt` to give the model an explicit menu of
    headers it can reuse character-for-character — small models follow
    enumerated candidate lists far better than they follow "look at the
    body and infer" instructions.

    Returns the headers with their ``**`` markers intact.  ATX
    ``### Header`` style is NOT supported (project convention is bold
    sub-headers); if a future project mixes formats, that's a Roadmap-7
    cross-format support item.
    """
    if not highlights_body:
        return []
    headers = []
    for line in highlights_body.splitlines():
        m = _SUBSECTION_HEADER_RE.match(line)
        if m:
            headers.append(m.group(1))
    return headers


def _render_commit_list(classified):
    """Render the classified-commits dict as a markdown list for the prompt."""
    lines = []
    for section, items in classified.items():
        if not items:
            continue
        lines.append(f"### {section}")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip()


def build_changelog_prompt(commits, classified, existing_unreleased,
                           project_name, project_desc,
                           changed_files, boundary_note):
    """Return (system_prompt, user_prompt) for the CHANGELOG tab.

    ``existing_unreleased`` is the current body of the [Unreleased] block
    (output of ``helpers.changelog_patch.read_unreleased``).  Empty string
    is fine.
    """
    system = _CHANGELOG_SYSTEM
    parts = [
        f"Project: {project_name}",
    ]
    if project_desc:
        parts.append(f"Project description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    # Per-commit subject summary — gives the model a quick view of how
    # MANY commits there are and what each one was about, pushing it
    # toward per-commit granularity in the output bullets.
    if commits:
        parts.append("Commits in range (use these to infer per-change "
                     "granularity — produce roughly one bullet per "
                     "distinct change, not one summary per topic):")
        for c in commits:
            sha = (c.get("hash") or "")[:8]
            subj = (c.get("subject") or "").strip()
            parts.append(f"  {sha}  {subj}")
        parts.append("")
    parts.append("Commits classified by conventional-commit prefix:")
    parts.append("")
    parts.append(_render_commit_list(classified) or "(no commits classified)")
    parts.append("")
    # ALWAYS include the changed-file list for CHANGELOG mode (not just
    # in sparse-commit mode like README).  Knowing exactly which files
    # were touched lets the model produce per-file granular bullets even
    # when commit subjects are detailed.
    if changed_files:
        parts.append("[Changed files] (repo-root-relative paths):")
        for p in changed_files:
            parts.append(f"  {p}")
        parts.append("")
    parts.append("Current [Unreleased] content (DO NOT repeat, restate, or "
                 "consolidate any of these — the patcher keeps them VERBATIM; "
                 "your job is only to ADD new bullets for the commit range "
                 "above):")
    parts.append("")
    parts.append(existing_unreleased or "(currently empty)")

    if is_sparse(commits):
        parts.append("")
        parts.append(
            "The provided commit subjects are unusually concise.  Infer the "
            "actual change scope from the changed file paths below "
            "(e.g. .py files in src/dialogs/ imply UI dialog logic).  Do NOT "
            "echo a terse commit subject verbatim — describe what changed."
        )
        if changed_files:
            parts.append("")
            parts.append("[Changed files]")
            for p in changed_files:
                parts.append(f"- {p}")

    return system, "\n".join(parts)


def build_readme_prompt(commits, classified, existing_highlights,
                        project_name, project_desc,
                        changed_files, boundary_note):
    """Return (system_prompt, user_prompt) for the README tab.

    ``existing_highlights`` is the current body of the 'Recent highlights'
    block (output of ``helpers.readme_patch.read_highlights``).
    """
    system = _README_SYSTEM
    parts = [
        f"Project: {project_name}",
    ]
    if project_desc:
        parts.append(f"Project description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    # Per-commit subject summary — mirrors the CHANGELOG prompt.  Gives the
    # model a per-commit view so it produces per-feature new bullets instead
    # of a single mega-bullet collapsing the whole range.
    if commits:
        parts.append("Commits in range (each represents a distinct feature "
                     "or fix — produce roughly one NEW bullet per commit, "
                     "do NOT collapse multiple commits into a single "
                     "comma-separated summary bullet):")
        for c in commits:
            sha = (c.get("hash") or "")[:8]
            subj = (c.get("subject") or "").strip()
            parts.append(f"  {sha}  {subj}")
        parts.append("")
    parts.append("Commits classified by conventional-commit prefix:")
    parts.append("")
    parts.append(_render_commit_list(classified) or "(no commits classified)")
    parts.append("")
    # ALWAYS include the changed-file list (Phase 1.8 — mirrors CHANGELOG).
    # Knowing exactly which files were touched lets the model produce
    # per-file granular new bullets even when commit subjects are detailed.
    if changed_files:
        parts.append("[Changed files] (repo-root-relative paths):")
        for p in changed_files:
            parts.append(f"  {p}")
        parts.append("")
    # Phase 2.0 — explicit candidate-header menu.  Small models follow
    # enumerated lists FAR better than they follow "look at the body and
    # infer the right header" instructions.  Reduces frequency of the
    # `Roadmap-N` literal-placeholder hallucination by giving the model a
    # concrete set of headers to choose from.  Hint only — the apply path
    # does NOT consult this list; `_normalise_subheader` in readme_patch.py
    # remains the single source of truth for header matching.
    existing_headers = _extract_subsection_headers(existing_highlights)
    if existing_headers:
        parts.append("Candidate sub-section headers from current highlights "
                     "body (REUSE one character-for-character if your bullets "
                     "thematically belong; otherwise create a new header "
                     "with a concrete roadmap number like Roadmap-6 or "
                     "Roadmap-7):")
        for h in existing_headers:
            parts.append(f"  {h}")
        parts.append("")
    parts.append("Current README 'Recent highlights (Unreleased)' block body "
                 "(preserve old sub-sections, only add/update what this "
                 "commit range actually changes):")
    parts.append("")
    parts.append(existing_highlights or "(currently empty)")

    if is_sparse(commits):
        parts.append("")
        parts.append(
            "The provided commit subjects are unusually concise.  Infer the "
            "actual change scope from the changed file paths above.  Do NOT "
            "echo a terse commit subject verbatim — describe what changed."
        )

    return system, "\n".join(parts)


# ── LLM dispatch ────────────────────────────────────────────────────────────

def _dispatch_agentic(llm_cfg, system_prompt, user_prompt,
                      project_path, tokensave_exe, timeout, stop_event):
    """Run a one-shot LocalAgent loop with tokensave tools and return (text, error).

    Used by dispatch_llm when enable_tokensave_tools=True and provider is
    ollama/openai_compatible.  The agent may call tokensave_search and
    tokensave_context before producing its final answer, which becomes the
    draft text returned to the dialog.
    """
    import threading

    try:
        from agent import LocalAgent
        from agent_tools import (
            make_tokensave_search_tool,
            make_tokensave_context_tool,
        )
    except ImportError:
        return None, "Agentic mode unavailable: agent module not found."

    tools = {}
    if tokensave_exe and project_path:
        ts_search = make_tokensave_search_tool(project_path, tokensave_exe)
        ts_ctx = make_tokensave_context_tool(project_path, tokensave_exe)
        tools[ts_search.name] = ts_search
        tools[ts_ctx.name] = ts_ctx

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    result_holder: list[str | None] = [None]
    error_holder:  list[str | None] = [None]
    done_event = threading.Event()

    def _on_done(text):
        result_holder[0] = text
        done_event.set()

    def _on_error(msg):
        error_holder[0] = msg
        done_event.set()

    agent = LocalAgent(cfg=llm_cfg, project_path=project_path, tools=tools,
                       max_iterations=6)
    agent_thread = threading.Thread(
        target=agent.run,
        kwargs=dict(
            messages=messages,
            on_done=_on_done,
            on_error=_on_error,
            stop_event=stop_event,
        ),
        daemon=True,
    )
    agent_thread.start()
    done_event.wait(timeout=timeout)
    if not done_event.is_set():
        return None, "Agentic call timed out."
    if error_holder[0]:
        return None, error_holder[0]
    text = result_holder[0]
    if not text:
        return None, "Agentic call returned no output."
    return text, None


def dispatch_llm(llm_cfg, system_prompt, user_prompt,
                 claude_cli_exe, cwd, timeout=120,
                 gen_params=None, examples=None,
                 enable_tokensave_tools=False, tokensave_exe="",
                 stop_event=None):
    """Call the configured LLM and return ``(text, error)``.

    Routes to ``call_claude_cli_print`` when ``llm_cfg["provider"] ==
    "claude_cli"``, otherwise to ``_call_llm`` (anthropic / openai /
    openai_compatible / ollama).  Returns ``(text, None)`` on success or
    ``(None, error_string)`` on failure.

    gen_params: dict of per-DocType overrides merged into llm_cfg
      (temperature, top_p, top_k, num_ctx).  Values in gen_params win.
    examples: list of (input_text, output_text) few-shot pairs spliced
      into user_prompt for ollama / openai_compatible providers.
    enable_tokensave_tools: when True and provider is ollama/openai_compatible,
      use a one-shot LocalAgent loop with tokensave_search + tokensave_context
      tools instead of a plain completion (Theme B2).  Claude CLI stays on the
      B1-injection path (documented asymmetry).
    tokensave_exe: path to tokensave binary; required when enable_tokensave_tools=True.
    stop_event: threading.Event for cooperative cancellation in agentic path.

    Pure dispatch — does NOT handle threading, cancellation, or UI.
    Caller wraps in a worker thread + stop_event check.
    """
    if gen_params:
        llm_cfg = {**(llm_cfg or {}), **gen_params}
    provider = (llm_cfg or {}).get("provider", "")

    # C4: splice few-shot examples for local providers (ollama / openai_compat)
    if examples and provider.lower() in ("ollama", "openai_compatible"):
        ex_parts = []
        for inp, out in examples[:2]:
            ex_parts.append(f"Example input:\n{inp}\n\nExpected output:\n{out}")
        ex_block = "\n\n---\n\n".join(ex_parts)
        user_prompt = ex_block + "\n\n---\n\n" + user_prompt

    # Theme B2: agentic path for local providers when tokensave tools requested.
    if enable_tokensave_tools and provider.lower() in ("ollama", "openai_compatible"):
        return _dispatch_agentic(
            llm_cfg, system_prompt, user_prompt,
            project_path=cwd or "",
            tokensave_exe=tokensave_exe or "",
            timeout=timeout,
            stop_event=stop_event,
        )

    try:
        if provider == "claude_cli":
            from helpers.claude_cli import call_claude_cli_print
            exe = (claude_cli_exe or "").strip()
            if not exe:
                return None, (
                    "Claude CLI not configured — set path in "
                    "Settings → Claude Code CLI."
                )
            model = (llm_cfg.get("model") or "").strip()
            # cwd=$HOME: Claude Code loads <cwd>/CLAUDE.md as project
            # context, which puts smaller models (Haiku) into "assistant
            # mode" — they reply conversationally to the prompt instead of
            # producing the requested draft. Pointing cwd at $HOME isolates
            # this call from any project CLAUDE.md. Project context that
            # actually matters (existing section text, commit list,
            # tokensave grounding) is already embedded in the prompt by
            # the caller, so the CLI process does not need to be in the
            # project directory. Same pattern used in
            # helpers.commit_messages._strat_claude_cli.
            result = call_claude_cli_print(
                exe, user_prompt,
                system_prompt=system_prompt,
                timeout=timeout,
                model=model,
                cwd=os.path.expanduser("~"),
            )
            if not result:
                return None, "Claude CLI returned no output (timeout or auth)."
            return result, None

        from helpers.llm import _call_llm
        # Adaptive token cap — _call_llm defaults to 1500 which is fine for
        # commit messages but cramped for grouped CHANGELOG bullets where the
        # model needs to emit multiple sub-section headers + several bullets
        # each. Bump for larger prompts; throttle DOWN to 1000 for tiny ranges
        # so Ollama on constrained hardware doesn't allocate context it won't
        # use. Truncation guard in _parse_grouped_bullets catches cases where
        # even 2500 isn't enough.
        prompt_chars = len(system_prompt or "") + len(user_prompt or "")
        if prompt_chars < 1500:
            max_tokens = 1000           # tiny range → speed mode
        elif prompt_chars < 6000:
            max_tokens = 2000           # typical
        else:
            max_tokens = 2500           # large existing-content context
        result = _call_llm(llm_cfg, system_prompt, user_prompt,
                           max_tokens=max_tokens)
        if not result:
            return None, f"{provider or 'LLM'} returned empty result."
        return result, None
    except Exception as exc:
        return None, f"{provider or 'LLM'} call failed: {exc}"


# ── Bullet-quality filter helpers (moved from dialogs/doc_drafter.py) ────────
#
# These run on the AI's output BEFORE the patcher applies it.  Catch the two
# Ollama failure modes: truncation (bullet ends mid-clause) and redundancy
# (bullet restates an existing entry).  Moved to helpers so DocType callables
# can reference them without circular imports.

_TRUNCATION_TRAILING = {
    "for", "the", "to", "with", "and", "or", "of",
    "in", "on", "at", "by", "as", "is", "a", "an",
}

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than",
    "of", "to", "in", "on", "at", "by", "as", "for", "with", "from", "into",
    "via", "per", "between", "through", "across", "over", "under",
    "after", "before", "during", "while", "when",
    "be", "is", "are", "was", "were", "it", "its", "this", "that",
    "also", "now", "still", "even", "just", "only",
}

_NOOP_BULLET_PATTERNS = [
    re.compile(
        r"^-?\s*(none|n/?a|nothing|tbd|no\s+changes?"
        r"|nothing\s+to\s+(add|report|note|do)"
        r"|n\.?a\.?|empty|placeholder)\s*\.?$",
        re.IGNORECASE,
    ),
]

# Phase 2.0 — literal template placeholder detector.
_LITERAL_PLACEHOLDER_RE = re.compile(
    r"\bRoadmap[-\s](?:"
    r"[A-Za-z](?![A-Za-z0-9])"
    r"|<[^>]+>"
    r"|TODO\b|TBD\b|PLACEHOLDER\b|PENDING\b|NUM\b|NUMBER\b"
    r")|<sub-section[^>]*>",
    re.IGNORECASE,
)

_PRESERVATION_THRESHOLD = 0.40


def _is_noop_bullet(bullet):
    s = (bullet or "").strip().lstrip("-*").strip().strip("()").strip()
    return any(p.match(s) for p in _NOOP_BULLET_PATTERNS)


def _looks_truncated(bullet):
    s = (bullet or "").rstrip()
    if not s:
        return False
    for _ in range(3):
        before = s
        m = re.search(r"\s+\([^()]*\)\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        m = re.search(r"\s+\[[^\]]*\]\([^)]*\)\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        m = re.search(r"\s+`[^`]*`\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        if s == before:
            break
    if not s:
        return False
    if s.endswith((".", ":", "!", "?", ")", "`", '"', "'")):
        return False
    last_word = s.split()[-1].lower().rstrip(",;:")
    return last_word in _TRUNCATION_TRAILING


def _token_set(bullet):
    s = re.sub(r"[^\w\s]", " ", (bullet or "").lower())
    return {t for t in s.split() if t not in _STOP_WORDS and len(t) > 2}


def _is_duplicate(new_bullet, existing_bullet):
    a = _token_set(new_bullet)
    b = _token_set(existing_bullet)
    if not a or not b:
        return False
    union = a | b
    jaccard = len(a & b) / len(union) if union else 0.0
    overlap = len(a & b) / min(len(a), len(b))
    return jaccard >= 0.6 or overlap >= 0.65


def _sanitise_raw_draft(text):
    lines = text.splitlines()
    while lines:
        last = lines[-1].strip()
        if (not last
                or last.startswith("```")
                or last.startswith("<!--")
                or last in {"---", "***", "___"}
                or last.lower().startswith(("*generated", "_generated",
                                            "*draft", "*ai-generated"))):
            lines.pop()
            continue
        break
    while lines:
        first = lines[0].strip()
        if not first or first.startswith("```"):
            lines.pop(0)
            continue
        break
    return "\n".join(lines)


def _preserve_score(a_set, b_set):
    if not a_set or not b_set:
        return 0.0
    intersection = a_set & b_set
    if len(intersection) < 2:
        return 0.0
    union = a_set | b_set
    jaccard = len(intersection) / len(union)
    overlap = len(intersection) / min(len(a_set), len(b_set))
    return max(jaccard, overlap)


def _mirror_contract_check(draft_bullets, existing_bullets,
                            min_preservation=0.75):
    if not existing_bullets:
        return True, 0, 0, []
    existing_compiled = [(eb, _token_set(eb)) for eb in existing_bullets]
    draft_compiled   = [(db, _token_set(db)) for db in draft_bullets]
    triples = []
    for ei, (_, a) in enumerate(existing_compiled):
        for di, (_, b) in enumerate(draft_compiled):
            s = _preserve_score(a, b)
            if s >= _PRESERVATION_THRESHOLD:
                triples.append((s, ei, di))
    triples.sort(key=lambda t: -t[0])
    claimed_e, claimed_d = set(), set()
    for _score, ei, di in triples:
        if ei in claimed_e or di in claimed_d:
            continue
        claimed_e.add(ei)
        claimed_d.add(di)
    matched = len(claimed_e)
    missing_list = [eb for ei, (eb, _) in enumerate(existing_compiled)
                    if ei not in claimed_e]
    missing = len(missing_list)
    ratio = matched / len(existing_bullets)
    if ratio < min_preservation and missing > 1:
        missing_examples = [
            (eb[:80] + "…" if len(eb) > 80 else eb)
            for eb in missing_list
        ][:3]
        return False, matched, missing, missing_examples
    return True, matched, missing, []


def _filter_bullets(bullets_md, existing_bullets, *,
                    dedup_against_existing=True):
    kept = []
    truncated_n = 0
    duplicate_n = 0
    noop_n = 0
    for line in bullets_md.splitlines():
        stripped = line.lstrip()
        if not (stripped.startswith("- ") or stripped.startswith("* ")):
            kept.append(line)
            continue
        if _looks_truncated(stripped):
            truncated_n += 1
            continue
        if _is_noop_bullet(stripped):
            noop_n += 1
            continue
        if dedup_against_existing:
            if any(_is_duplicate(stripped, eb) for eb in existing_bullets):
                duplicate_n += 1
                continue
            kept.append(line)
        else:
            is_dup = False
            for idx, kb in enumerate(kept):
                kb_stripped = kb.lstrip()
                if not kb_stripped.startswith(("- ", "* ")):
                    continue
                if _is_duplicate(stripped, kb_stripped):
                    is_dup = True
                    if len(stripped) > len(kb_stripped) + 8:
                        kept[idx] = line
                    duplicate_n += 1
                    break
            if not is_dup:
                kept.append(line)
    return "\n".join(kept).strip(), truncated_n, duplicate_n, noop_n


def parse_grouped_bullets(draft_text):
    """Parse '### Header / - bullet ...' grouped output into (header, body) pairs."""
    pairs = []
    current_section = None
    current_lines = []
    for ln in draft_text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("### "):
            if current_section is not None:
                body = "\n".join(current_lines).strip()
                if body:
                    pairs.append((current_section, body))
            current_section = stripped[4:].strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(ln)
    if current_section is not None:
        body = "\n".join(current_lines).strip()
        if body:
            pairs.append((current_section, body))
    return pairs


def split_readme_subsection(draft_text):
    """Extract first ``**Header**`` line + remaining bullets. Returns (header, bullets) or None."""
    lines = draft_text.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if (stripped.startswith("**") and stripped.endswith("**")
                and len(stripped) > 4):
            header_idx = i
            break
        if (stripped.startswith("**") and "**" in stripped[2:]
                and stripped.endswith(":")):
            header_idx = i
            break
    if header_idx is None:
        return None
    header_line = lines[header_idx].strip()
    bullets = "\n".join(lines[header_idx + 1:]).strip("\n")
    return header_line, bullets


# ── DocType callable implementations ─────────────────────────────────────────
#
# These are the io_apply / compute_apply / filter_draft callables stored in
# the DocType registry.  Defined here (helpers) so they can be imported by
# helpers/doc_types.py without circular imports.

def changelog_filter_draft(raw_text, target_path):
    """Filter changelog draft. Returns (filtered_text, trunc_n, dup_n, noop_n)."""
    from helpers.changelog_patch import read_section_bullets
    sanitised = _sanitise_raw_draft(raw_text or "")
    total_trunc = total_dup = total_noop = 0
    pairs = parse_grouped_bullets(sanitised)
    kept_pairs = []
    for section, bullets in pairs:
        existing = read_section_bullets(target_path, section)
        filtered, t_n, d_n, n_n = _filter_bullets(bullets, existing)
        total_trunc += t_n
        total_dup += d_n
        total_noop += n_n
        if filtered.strip():
            kept_pairs.append((section, filtered.strip()))
    chunks = [f"### {sec}\n{body}" for sec, body in kept_pairs]
    return "\n\n".join(chunks), total_trunc, total_dup, total_noop


def readme_filter_draft(raw_text, target_path):
    """Filter readme draft. Returns (filtered_text, trunc_n, dup_n, noop_n)."""
    from helpers.readme_patch import read_subsection_bullets
    sanitised = _sanitise_raw_draft(raw_text or "")
    parsed = split_readme_subsection(sanitised)
    if parsed is None:
        return sanitised, 0, 0, 0
    header_line, bullets = parsed
    existing = read_subsection_bullets(target_path, header_line)
    filtered, t_n, d_n, n_n = _filter_bullets(
        bullets, existing, dedup_against_existing=False)
    if filtered.strip():
        return (f"{header_line}\n{filtered.strip()}", t_n, d_n, n_n)
    return "", t_n, d_n, n_n


def changelog_compute_apply(full_text, pairs_list):
    """PURE: apply grouped bullet pairs to full file text. Returns (new_text, ok, msg)."""
    from helpers.changelog_patch import _compute_insert_unreleased_bullets
    if not pairs_list:
        return full_text, False, "No bullet pairs to apply."
    simulated = full_text
    for section, bullets in pairs_list:
        simulated, ok, msg = _compute_insert_unreleased_bullets(
            simulated, section, bullets)
        if not ok:
            return full_text, False, f"{section}: {msg}"
    return simulated, True, "ok"


def readme_compute_apply(full_text, header_line, bullets):
    """PURE: splice readme sub-section into full file text. Returns (new_text, ok, msg)."""
    from helpers.readme_patch import _compute_insert_readme_highlights_subsection
    return _compute_insert_readme_highlights_subsection(
        full_text, header_line, bullets)


def changelog_io_apply(target_path, draft_text):
    """Apply changelog draft. Returns (ok, msg, stats_dict)."""
    from helpers.changelog_patch import read_section_bullets, insert_unreleased_bullets
    pairs = parse_grouped_bullets(draft_text)
    if not pairs:
        return False, (
            "Draft is missing '### Section' headers — CHANGELOG mode requires "
            "bullets grouped under ### Added / ### Fixed / ### Changed / "
            "### Removed."
        ), {}
    applied = []
    total_truncated = total_duplicates = total_noop = 0
    empty_after_filter = 0
    for section, bullets in pairs:
        existing = read_section_bullets(target_path, section)
        filtered, trunc_n, dup_n, noop_n = _filter_bullets(bullets, existing)
        total_truncated += trunc_n
        total_duplicates += dup_n
        total_noop += noop_n
        if not filtered.strip():
            empty_after_filter += 1
            continue
        ok, msg = insert_unreleased_bullets(target_path, section, filtered)
        if not ok:
            stats = {"truncated": total_truncated,
                     "duplicates": total_duplicates, "noop": total_noop}
            return False, f"{section}: {msg} (applied so far: {applied})", stats
        applied.append(section)
    stats = {"truncated": total_truncated,
             "duplicates": total_duplicates, "noop": total_noop}
    if not applied:
        return False, (
            f"All bullets filtered out ({total_truncated} truncated, "
            f"{total_duplicates} duplicates, {total_noop} placeholders).  "
            "Click Regenerate to retry."
        ), stats
    summary = f"appended to {len(applied)} section(s): {', '.join(applied)}"
    if empty_after_filter:
        summary += f" ({empty_after_filter} section(s) emptied by filter)"
    return True, summary, stats


def readme_io_apply(target_path, draft_text):
    """Apply readme sub-section draft. Returns (ok, msg, stats_dict)."""
    from helpers.readme_patch import (
        read_subsection_bullets, insert_readme_highlights_subsection,
        read_highlights,
    )
    parsed = split_readme_subsection(draft_text)
    if parsed is None:
        return False, (
            "Draft is missing a `**Bold header**` line — README mode requires "
            "a sub-section header as the first non-blank line."
        ), {}
    header_line, bullets = parsed
    if _LITERAL_PLACEHOLDER_RE.search(header_line):
        candidates = _extract_subsection_headers(read_highlights(target_path))
        candidates_msg = "\n  ".join("• " + h for h in candidates[:5])
        return False, (
            f"Header contains a literal template placeholder: "
            f"{header_line!r}.  Edit the draft to use a real header, "
            f"or click Regenerate.  Existing candidates to REUSE if "
            f"your bullets belong:\n  "
            f"{candidates_msg if candidates else '(none — your sub-section is brand new; use a concrete number like Roadmap-6 or Roadmap-7)'}"
        ), {}
    existing = read_subsection_bullets(target_path, header_line)
    filtered, trunc_n, dup_n, noop_n = _filter_bullets(
        bullets, existing, dedup_against_existing=False)
    stats = {"truncated": trunc_n, "duplicates": dup_n, "noop": noop_n}
    if not filtered.strip():
        return False, (
            f"All bullets filtered out ({trunc_n} truncated, {dup_n} "
            f"self-duplicates, {noop_n} placeholders).  README's REPLACE "
            "patcher would DELETE the existing sub-section if applied with "
            "no bullets.  Click Regenerate."
        ), stats
    draft_bullets = [ln.lstrip() for ln in filtered.splitlines()
                     if ln.lstrip().startswith(("- ", "* "))]
    ok, kept, missing, examples = _mirror_contract_check(
        draft_bullets, existing)
    if not ok:
        bullet_list = "\n  ".join("• " + ex for ex in examples)
        extra = (f" (showing {len(examples)} of {missing})"
                 if missing > len(examples) else "")
        return False, (
            f"Mirror-contract safety abort: draft preserves only "
            f"{kept}/{kept + missing} existing bullets "
            f"({int(100 * kept / max(1, kept + missing))}%).  "
            f"README's REPLACE patcher would DELETE {missing} "
            f"preserved bullet(s){extra}:\n  {bullet_list}\n\n"
            f"Click Regenerate to retry, or manually edit the draft "
            f"to add the missing bullets back before Apply."
        ), stats
    ok2, msg2 = insert_readme_highlights_subsection(
        target_path, header_line,
        filtered + "\n" if filtered else "\n")
    return ok2, msg2, stats


# ── Phase 2 DocType system prompts ────────────────────────────────────────────

_ARCHITECTURE_SYSTEM = (
    "You are an architecture documentation maintainer.\n\n"
    "CRITICAL: Output ONLY the updated section content — raw markdown starting "
    "with a ``## `` heading.  NO preamble, NO explanation, NO questions, NO "
    "permission requests, NO code fences.  Your entire response must begin with "
    "``## `` and contain nothing before it.\n\n"
    "Task: update ONE named section in ARCHITECTURE.md based on the provided commits.\n\n"
    "Output format:\n"
    "  ## Section Name\n"
    "  <updated section body>\n\n"
    "Rules:\n"
    "- Preserve all factual content in the existing section that is still "
    "accurate.  Only ADD or UPDATE what the commits actually change.\n"
    "- Use the same style as the existing document: tree-formatted file "
    "listings, one-liner descriptions per module, Key exports annotations.\n"
    "- New helpers get an entry under ``helpers/`` sorted alphabetically. "
    "New dialogs get an entry under ``dialogs/`` sorted alphabetically.\n"
    "- Cite exported symbol names in backtick format, e.g. "
    "``read_unreleased``, ``_compute_insert_*``.\n"
    "- Do NOT invent details not supported by the commits or existing text.\n"
    "- Do NOT ask for permission or confirmation.  Just output the section."
)

_ROADMAP_SYSTEM = (
    "You are a roadmap document maintainer.\n\n"
    "CRITICAL: Output ONLY raw markdown — no preamble, no explanation, no "
    "questions, no permission requests.  Your response must begin with "
    "``## Roadmap`` and contain nothing before it.\n\n"
    "Task: draft content for a roadmap section based on the provided commits.\n\n"
    "Output format:\n"
    "  ## Roadmap N — Theme Title\n"
    "  ### ✅ Item title\n"
    "  Description.\n"
    "  ### 🟡 Another item\n"
    "  Description.\n\n"
    "Status emoji meanings: ✅ shipped  🟡 in-progress  🔮 planned  "
    "💭 idea  💤 stale.\n\n"
    "Rules:\n"
    "- The first line MUST be ``## Roadmap N — Theme``.  Use the roadmap "
    "number from the user prompt.\n"
    "- Each entry starts with ``### <emoji> <title>``.\n"
    "- Body text per entry: 1–3 short sentences.  Technical, not marketing.\n"
    "- Only describe work evidenced by the provided commits.\n"
    "- Do NOT ask for permission or confirmation.  Just output the section."
)

_MEMORY_SYSTEM = (
    "You are a persistent-memory file author.\n\n"
    "CRITICAL: Output ONLY the body text — no YAML frontmatter, no code "
    "fences, no preamble, no explanation, no questions.  Start writing the "
    "memory content immediately.\n\n"
    "Task: write the body of a memory file based on the provided commits.\n\n"
    "Format: lead with the key fact, then a **Why:** line (reason / "
    "motivation), then a **How to apply:** line (when this guidance kicks "
    "in).  Link related memories with ``[[slug-name]]``.\n\n"
    "Rules:\n"
    "- Be specific and actionable.  'Always use X when Y' beats 'Consider X'.\n"
    "- No filler, no hedging.\n"
    "- Do NOT ask for permission or confirmation.  Just output the content."
)

_GENERIC_DOC_SYSTEM = (
    "You are a markdown documentation maintainer.\n\n"
    "CRITICAL: Output ONLY the updated section content — raw markdown "
    "starting with a ``## `` heading.  NO preamble, NO explanation, NO "
    "questions, NO permission requests.  Begin with ``## `` immediately.\n\n"
    "Task: update ONE named section in the target document based on the "
    "provided commits.\n\n"
    "Output format:\n"
    "  ## Section Name\n"
    "  <updated section body>\n\n"
    "Rules:\n"
    "- Preserve all still-accurate content from the existing section.  "
    "Only ADD or UPDATE what the commits actually change.\n"
    "- Match the voice and style of the existing document.\n"
    "- Do NOT ask for permission or confirmation.  Just output the section."
)


# ── Phase 2 build_prompt functions ───────────────────────────────────────────

def _render_commit_summary(commits, classified):
    """Shared commit block used by all Phase 2 prompt builders."""
    parts = []
    if commits:
        parts.append("Recent commits:")
        for c in commits[:40]:
            parts.append(f"  {c}")
    if classified:
        parts.append("")
        parts.append("Classified changes:")
        parts.append(_render_commit_list(classified))
    return "\n".join(parts)


def build_architecture_prompt(commits, classified, existing_full,
                               project_name, project_desc,
                               changed_files, boundary_note,
                               grounding_block=""):
    """Return (system, user) for the architecture tab."""
    system = _ARCHITECTURE_SYSTEM
    parts = [f"Project: {project_name}"]
    if project_desc:
        parts.append(f"Description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    parts.append(_render_commit_summary(commits, classified))
    if changed_files:
        parts.append("")
        parts.append("Changed files:")
        for f in changed_files[:60]:
            parts.append(f"  {f}")
    if grounding_block:
        parts.append("")
        parts.append(grounding_block)
    parts.append("")
    parts.append(
        "Current ARCHITECTURE.md (update the section most affected by "
        "these commits; output exactly that section with ## heading):"
    )
    parts.append("")
    parts.append(existing_full or "(currently empty)")
    return system, "\n".join(parts)


def build_roadmap_prompt(commits, classified, existing_full,
                          project_name, project_desc,
                          changed_files, boundary_note,
                          grounding_block=""):
    """Return (system, user) for the roadmap tab."""
    system = _ROADMAP_SYSTEM
    parts = [f"Project: {project_name}"]
    if project_desc:
        parts.append(f"Description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    parts.append(_render_commit_summary(commits, classified))
    if grounding_block:
        parts.append("")
        parts.append(grounding_block)
    parts.append("")
    parts.append(
        "Current ROADMAP.md (identify the active roadmap number from "
        "context; output the updated ## Roadmap N section):"
    )
    parts.append("")
    parts.append(existing_full or "(currently empty)")
    return system, "\n".join(parts)


def build_memory_prompt(commits, classified, existing_body,
                         project_name, project_desc,
                         changed_files, boundary_note,
                         grounding_block=""):
    """Return (system, user) for the memory tab."""
    system = _MEMORY_SYSTEM
    parts = [f"Project: {project_name}"]
    if project_desc:
        parts.append(f"Description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    parts.append(_render_commit_summary(commits, classified))
    if grounding_block:
        parts.append("")
        parts.append(grounding_block)
    parts.append("")
    parts.append("Current memory body (update or extend based on commits):")
    parts.append("")
    parts.append(existing_body or "(currently empty)")
    return system, "\n".join(parts)


def build_generic_doc_prompt(commits, classified, existing_full,
                              project_name, project_desc,
                              changed_files, boundary_note,
                              grounding_block=""):
    """Return (system, user) for the docs_generic / tokensave_guide tab."""
    system = _GENERIC_DOC_SYSTEM
    parts = [f"Project: {project_name}"]
    if project_desc:
        parts.append(f"Description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    parts.append(_render_commit_summary(commits, classified))
    if changed_files:
        parts.append("")
        parts.append("Changed files:")
        for f in changed_files[:60]:
            parts.append(f"  {f}")
    if grounding_block:
        parts.append("")
        parts.append(grounding_block)
    parts.append("")
    parts.append(
        "Current document (update the section most affected by these "
        "commits; output exactly that section with ## heading):"
    )
    parts.append("")
    parts.append(existing_full or "(currently empty)")
    return system, "\n".join(parts)


# ── Phase 2 parse_draft functions ────────────────────────────────────────────
#
# Each returns a tuple that gets splatted: compute_apply(full_text, *parse_draft(draft))
# Return (None, ...) on failure so _simulate_body can detect it.

_SECTION_HEADING_RE = re.compile(r"(?m)^## ([^\n]+)\n(.*)", re.DOTALL)
_ROADMAP_HEADING_RE = re.compile(r"(?m)^## Roadmap (\d+)([^\n]*)\n(.*)", re.DOTALL)


def architecture_parse_draft(draft_text):
    """Extract (section_header, content) from draft. Returns (None, '') on failure."""
    m = _SECTION_HEADING_RE.search((draft_text or "").strip())
    if not m:
        return (None, "")
    return (m.group(1).strip(), m.group(2).strip())


def roadmap_parse_draft(draft_text):
    """Extract (roadmap_n, theme_title, content) from draft. Returns (None, '', '') on failure."""
    m = _ROADMAP_HEADING_RE.search((draft_text or "").strip())
    if not m:
        return (None, "", "")
    n = int(m.group(1))
    theme_rest = m.group(2).strip().lstrip("—").strip()
    theme = theme_rest or f"Roadmap {n}"
    return (n, theme, m.group(3).strip())


def memory_parse_draft(draft_text):
    """Return (body_text,) — the entire draft is the memory body."""
    body = (draft_text or "").strip()
    if not body:
        return (None,)
    return (body,)


def generic_parse_draft(draft_text):
    """Extract (section_header, content) from draft. Returns (None, '') on failure."""
    m = _SECTION_HEADING_RE.search((draft_text or "").strip())
    if not m:
        return (None, "")
    return (m.group(1).strip(), m.group(2).strip())


# ── Phase 2 filter_draft functions ────────────────────────────────────────────

_CODE_FENCE_RE = re.compile(
    r"(?m)^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
    re.DOTALL,
)

# A line that looks like "real section content": tree chars, headings,
# list markers, numbered items, or horizontal rules.
_CONTENT_LINE_RE = re.compile(
    r"^(?:[│├└─#\-\*>]|\d+\.|\s+[│├└─]|={3,}|-{3,})"
)


def _strip_trailing_prose(text: str) -> str:
    """Discard trailing prose paragraphs after the last real content line.

    "Real content" means tree-formatted lines, headings, list items, or
    horizontal rules.  A trailing paragraph like Haiku's '**Change summary:**'
    blob is dropped.
    """
    lines = text.splitlines()
    last_content = -1
    for i, ln in enumerate(lines):
        if _CONTENT_LINE_RE.match(ln):
            last_content = i
    if last_content < 0:
        return text
    return "\n".join(lines[:last_content + 1])


def _strip_preamble_and_fences(text: str) -> str:
    """Remove prose before the first '## ' heading, unwrap code fences,
    and strip trailing prose paragraphs after the last content line.

    Models sometimes wrap output in a prose preamble ("Based on my analysis…"),
    surround content with ```markdown fences, and/or add a trailing summary
    paragraph.  All three are stripped here so only the bare section content
    reaches the patcher.
    """
    # 1. Drop prose before the first ## heading.
    m = re.search(r"(?m)^## ", text)
    if m:
        text = text[m.start():]

    # 2. Unwrap code fences — keep only the body inside the backticks.
    text = _CODE_FENCE_RE.sub(lambda mo: mo.group(1), text)

    # 3. Drop trailing prose after the last real content line.
    text = _strip_trailing_prose(text)

    return text


def _filter_freeform(raw_text):
    """Lightweight filter for non-bullet drafts: strip preamble, fences, placeholders."""
    sanitised = _sanitise_raw_draft(
        _strip_preamble_and_fences(raw_text or "")
    )
    lines = sanitised.splitlines()
    noop_n = 0
    kept = []
    for ln in lines:
        stripped = ln.strip()
        if _LITERAL_PLACEHOLDER_RE.search(stripped):
            noop_n += 1
            continue
        kept.append(ln)
    return "\n".join(kept), 0, 0, noop_n


def architecture_filter_draft(raw_text, target_path):
    """Filter architecture draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


def roadmap_filter_draft(raw_text, target_path):
    """Filter roadmap draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


def memory_filter_draft(raw_text, target_path):
    """Filter memory draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


def generic_filter_draft(raw_text, target_path):
    """Filter generic doc draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


# ── Phase 2 compute_apply (PURE) functions ────────────────────────────────────

def architecture_compute_apply(full_text, section_header, content):
    """PURE: apply architecture section to full file. Returns (new_text, ok, msg)."""
    from helpers.architecture_patch import _compute_insert_architecture_section
    if section_header is None:
        return full_text, False, "Could not parse section header from draft."
    return _compute_insert_architecture_section(full_text, section_header, content)


def roadmap_compute_apply(full_text, roadmap_n, theme_title, content):
    """PURE: apply roadmap section to full file. Returns (new_text, ok, msg)."""
    from helpers.roadmap_patch import _compute_insert_roadmap_section
    if roadmap_n is None:
        return full_text, False, "Could not parse roadmap number from draft."
    return _compute_insert_roadmap_section(full_text, roadmap_n, theme_title, content)


def memory_compute_apply(full_text, new_body):
    """PURE: replace memory body. Returns (new_text, ok, msg)."""
    from helpers.memory_patch import _compute_insert_memory_body
    if new_body is None:
        return full_text, False, "Draft body is empty."
    return _compute_insert_memory_body(full_text, new_body)


def generic_compute_apply(full_text, section_header, content):
    """PURE: apply generic section to full file. Returns (new_text, ok, msg)."""
    from helpers.generic_doc_patch import _compute_insert_generic_section
    if section_header is None:
        return full_text, False, "Could not parse section header from draft."
    return _compute_insert_generic_section(full_text, section_header, content)


# ── Phase 2 io_apply functions ────────────────────────────────────────────────

def architecture_io_apply(target_path, draft_text):
    """Apply architecture draft. Returns (ok, msg, stats_dict)."""
    from helpers.architecture_patch import insert_architecture_section
    parsed = architecture_parse_draft(draft_text)
    section_header, content = parsed
    if section_header is None:
        return False, (
            "Draft is missing a ``## Section Name`` heading — architecture "
            "mode requires the output to start with a level-2 heading."
        ), {}
    ok, msg = insert_architecture_section(target_path, section_header, content)
    return ok, msg, {}


def roadmap_io_apply(target_path, draft_text):
    """Apply roadmap draft. Returns (ok, msg, stats_dict)."""
    from helpers.roadmap_patch import insert_roadmap_section
    parsed = roadmap_parse_draft(draft_text)
    roadmap_n, theme_title, content = parsed
    if roadmap_n is None:
        return False, (
            "Draft is missing a ``## Roadmap N`` heading — roadmap mode "
            "requires the output to start with '## Roadmap <number>'."
        ), {}
    ok, msg = insert_roadmap_section(target_path, roadmap_n, theme_title, content)
    return ok, msg, {}


def memory_io_apply(target_path, draft_text):
    """Apply memory draft. Returns (ok, msg, stats_dict)."""
    from helpers.memory_patch import insert_memory_body
    body = (draft_text or "").strip()
    if not body:
        return False, "Draft is empty.", {}
    ok, msg = insert_memory_body(target_path, body)
    return ok, msg, {}


def generic_io_apply(target_path, draft_text):
    """Apply generic doc draft. Returns (ok, msg, stats_dict)."""
    from helpers.generic_doc_patch import insert_generic_section
    parsed = generic_parse_draft(draft_text)
    section_header, content = parsed
    if section_header is None:
        return False, (
            "Draft is missing a ``## Section Name`` heading — generic doc "
            "mode requires the output to start with a level-2 heading."
        ), {}
    ok, msg = insert_generic_section(target_path, section_header, content)
    return ok, msg, {}
