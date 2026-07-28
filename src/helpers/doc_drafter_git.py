"""Commit-range resolution + git plumbing for the doc drafter.

Split out of helpers/doc_drafter.py (Roadmap-8 god-file split).
Import via the ``helpers.doc_drafter`` facade — it re-exports
every name, so call sites and tests are unchanged.
"""

from __future__ import annotations

import os
import subprocess
from constants import CREATE_NO_WINDOW
from helpers.release import _commits_since
from helpers.release import _last_release_tag




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
