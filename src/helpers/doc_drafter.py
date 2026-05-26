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
import subprocess

from constants import CREATE_NO_WINDOW
from helpers.release import (
    _commits_since, _last_release_tag,
)


# Doc files whose modifications mark a commit as "documented".  Used by
# "since last doc commit" mode.
_DOC_PATHSPECS = ["CHANGELOG.md", "README.md", "docs/"]

# Average commit-subject character length below which we trigger the
# sparse-commit safety net (extra prompt hint + changed-file path list).
_SPARSE_AVG_THRESHOLD = 15


# ── Commit-range resolution ──────────────────────────────────────────────────

def _last_doc_commit_sha(project_path, git_exe):
    """Return the SHA of the most recent commit that touched docs, or None.

    Uses ``git log --diff-filter=AM --name-only -- CHANGELOG.md README.md docs/``
    to find the newest commit that modified or added any doc file.  If no
    such commit exists, returns None.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "log", "-n", "1",
             "--pretty=format:%H",
             "--diff-filter=AM", "--", *_DOC_PATHSPECS],
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


def resolve_commit_range(project_path, mode, custom_ref, git_exe):
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
        sha = _last_doc_commit_sha(project_path, git_exe)
        if not sha:
            # No prior doc commit at all — fall back to "since beginning"
            commits = _commits_since(project_path, None, git_exe)
            return {
                "range_label":    "All commits (no prior doc commit found)",
                "range_spec":     "HEAD",
                "commits":        commits,
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
        commits = _commits_since(project_path, sha if not mixed
                                 else f"{sha}^", git_exe)
        return {
            "range_label":    f"Since last doc commit ({sha[:8]})",
            "range_spec":     range_spec,
            "commits":        commits,
            "boundary_mixed": mixed,
            "boundary_note":  note,
        }

    if mode == "since_last_commit":
        commits = _commits_since(project_path, "HEAD~1", git_exe)
        return {
            "range_label":    "Since last commit (HEAD~1..HEAD)",
            "range_spec":     "HEAD~1..HEAD",
            "commits":        commits,
            "boundary_mixed": False,
            "boundary_note":  None,
        }

    if mode == "since_last_tag":
        tag = _last_release_tag(project_path, git_exe)
        if not tag:
            commits = _commits_since(project_path, None, git_exe)
            return {
                "range_label":    "All commits (no release tag found)",
                "range_spec":     "HEAD",
                "commits":        commits,
                "boundary_mixed": False,
                "boundary_note":  None,
            }
        commits = _commits_since(project_path, tag, git_exe)
        return {
            "range_label":    f"Since last release tag ({tag})",
            "range_spec":     f"{tag}..HEAD",
            "commits":        commits,
            "boundary_mixed": False,
            "boundary_note":  None,
        }

    if mode == "custom":
        ref = (custom_ref or "").strip()
        if not ref:
            return {
                "range_label":    "Custom range (empty)",
                "range_spec":     "",
                "commits":        [],
                "boundary_mixed": False,
                "boundary_note":  None,
            }
        # If the user gave a single ref, treat it as `<ref>..HEAD`.  If
        # they already wrote `A..B`, pass through.
        if ".." not in ref:
            range_spec = f"{ref}..HEAD"
            commits = _commits_since(project_path, ref, git_exe)
        else:
            range_spec = ref
            # Parse "A..B" — _commits_since only supports ref..HEAD style,
            # so we shell out directly for arbitrary ranges.
            commits = _commits_in_range(project_path, range_spec, git_exe)
        return {
            "range_label":    f"Custom range ({range_spec})",
            "range_spec":     range_spec,
            "commits":        commits,
            "boundary_mixed": False,
            "boundary_note":  None,
        }

    return {
        "range_label":    f"Unknown mode: {mode!r}",
        "range_spec":     "",
        "commits":        [],
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
    "- Cite file paths and helper names in backticks.  No marketing fluff."
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
    "- Output exactly ONE sub-section.  No surrounding anchor line, no "
    "`---` separator, no other sub-sections, no prose preamble, no "
    "explanations, no markdown code fences.\n"
    "- The first line MUST be the bold header in the form "
    "`**Roadmap-N — short topic**` (or, if not roadmap-scoped, "
    "`**Feature area — short topic**`).\n"
    "- Bullets are verb-first, technical detail in parens (file paths, "
    "helper names, threshold values).  No marketing fluff.  Match the "
    "voice of bullets in the 'Current highlights body' below.\n"
    "- Use the project's existing emoji prefix when obvious (🤖 for AI, "
    "🔍 for review, 🔌 for MCP, 🦙 for Ollama, ⚙ for settings, etc.).\n"
    "- If a matching sub-section already exists in the current body, "
    "include ALL of its existing bullets PLUS the new ones — the patcher "
    "REPLACES the whole sub-section, so omitting a bullet deletes it."
)


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
    parts.append("Commits classified by conventional-commit prefix:")
    parts.append("")
    parts.append(_render_commit_list(classified) or "(no commits classified)")
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
    parts.append("Commits classified by conventional-commit prefix:")
    parts.append("")
    parts.append(_render_commit_list(classified) or "(no commits classified)")
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
            "actual change scope from the changed file paths below.  Do NOT "
            "echo a terse commit subject verbatim — describe what changed."
        )
        if changed_files:
            parts.append("")
            parts.append("[Changed files]")
            for p in changed_files:
                parts.append(f"- {p}")

    return system, "\n".join(parts)


# ── LLM dispatch ────────────────────────────────────────────────────────────

def dispatch_llm(llm_cfg, system_prompt, user_prompt,
                 claude_cli_exe, cwd, timeout=120):
    """Call the configured LLM and return ``(text, error)``.

    Routes to ``call_claude_cli_print`` when ``llm_cfg["provider"] ==
    "claude_cli"``, otherwise to ``_call_llm`` (anthropic / openai /
    openai_compatible / ollama).  Returns ``(text, None)`` on success or
    ``(None, error_string)`` on failure.

    Pure dispatch — does NOT handle threading, cancellation, or UI.
    Caller wraps in a worker thread + stop_event check.
    """
    provider = (llm_cfg or {}).get("provider", "")

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
            result = call_claude_cli_print(
                exe, user_prompt,
                system_prompt=system_prompt,
                timeout=timeout,
                model=model,
                cwd=cwd,
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
