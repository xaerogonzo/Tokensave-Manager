"""pr_checklist — Sync the open PR's '## Testing checklist' from local tests (v4.13).

Powers the Test Manager Tab 1 ``🔁 Sync PR Checklist`` button.  Reads
the open PR's body via ``gh pr view``, locates the manager-generated
``## Testing checklist`` section, re-renders ONLY its ``### Automated``
subsection from a fresh test-run snapshot, and writes the new body back
via ``gh api ... -X PATCH --input -`` (V-A: stdin-piped JSON, NOT
``-f body=...`` which URL-encodes and mangles markdown specials).

Critically: this helper ONLY edits a checklist section that the manager
itself emitted via :func:`format_automated_section` (we identify it by
the leading marker comment).  Hand-written or third-party PR bodies are
left untouched, so a user can safely click Sync without worrying about
clobbering their own notes.

All ``gh`` subprocesses are bounded with timeouts; failures are
surfaced as ``(False, error_message)`` tuples — never raised.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

try:
    from constants import CREATE_NO_WINDOW
except ImportError:
    CREATE_NO_WINDOW = 0


# ── Marker that identifies a manager-generated checklist section ─────────

# Embedded as an HTML comment so it's invisible in GitHub's rendered
# markdown but trivially parseable. Matching is exact — refuses to
# touch hand-written ``## Testing checklist`` sections that lack it.
_MARKER = "<!-- tokensave-manager:testing-checklist v1 -->"

_AUTOMATED_HEADING = "### Automated (verified by `pytest -m \"not tk\"`)"
_MANUAL_HEADING    = "### Manual (please verify before merge)"


# ── gh CLI wrappers (V-A: stdin-piped for body writes) ───────────────────

def _run_gh(gh_exe: str, args: list[str], project_root: str,
              timeout_s: int = 30,
              input_text: Optional[str] = None) -> tuple[int, str, str]:
    """Shared gh runner. Returns (rc, stdout, stderr).

    Subprocess kwargs we always pass:
      * cwd=project_root   so ``gh pr view`` resolves the right repo
      * encoding=utf-8, errors=replace   for safe unicode
      * CREATE_NO_WINDOW   to hide the console window on Windows

    For body writes use ``input_text`` (will be piped to gh's stdin)
    rather than embedding the body in an argv slot.
    """
    if not gh_exe:
        return 127, "", "gh CLI not configured"
    try:
        proc = subprocess.run(
            [gh_exe] + args,
            input=input_text,
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, "", f"gh executable not found: {gh_exe}"
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {timeout_s} s"
    except OSError as exc:
        return 1, "", f"gh launch failed: {exc}"
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def get_open_pr(gh_exe: str, project_root: str) -> Optional[dict]:
    """Return ``{"number": N, "body": "...", "title": "..."}`` for the
    currently-checked-out branch's open PR, or ``None`` if no PR exists.

    Uses ``gh pr view --json number,body,title``. gh returns rc=0 + a
    JSON object on success, rc=1 + a "no pull requests found" message
    when no PR matches the branch.
    """
    rc, stdout, _stderr = _run_gh(
        gh_exe, ["pr", "view", "--json", "number,body,title"],
        project_root, timeout_s=15,
    )
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_owner_repo(gh_exe: str, project_root: str
                          ) -> Optional[tuple[str, str]]:
    """Return (owner, name) for the project's GitHub remote, or None.

    Resolved via ``gh repo view --json owner,name`` so we don't have to
    parse remote URLs ourselves. Required for the ``gh api repos/.../
    pulls/N`` URL path.
    """
    rc, stdout, _stderr = _run_gh(
        gh_exe, ["repo", "view", "--json", "owner,name"],
        project_root, timeout_s=15,
    )
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    owner_obj = data.get("owner") or {}
    owner = owner_obj.get("login") if isinstance(owner_obj, dict) else None
    name  = data.get("name")
    if not (isinstance(owner, str) and isinstance(name, str)):
        return None
    return owner, name


def update_pr_body(gh_exe: str, project_root: str,
                    pr_number: int, new_body: str) -> tuple[bool, str]:
    """Write *new_body* to the PR via ``gh api ... -X PATCH --input -``.

    V-A: uses stdin-piped JSON, NOT ``-f body=<new>``. The ``-f`` flag
    URL-encodes form fields, which mangles markdown specials (``&``,
    ``=``, ``#``, multiline) in PR bodies. The stdin-piped path treats
    the body as a single opaque string.
    """
    owner_repo = _resolve_owner_repo(gh_exe, project_root)
    if owner_repo is None:
        return False, "could not resolve owner/repo via `gh repo view`"
    owner, repo = owner_repo
    payload = json.dumps({"body": new_body})
    rc, stdout, stderr = _run_gh(
        gh_exe,
        [
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}",
            "-X", "PATCH",
            "--input", "-",
        ],
        project_root,
        timeout_s=30,
        input_text=payload,
    )
    if rc != 0:
        return False, (stderr or stdout or "gh api PATCH failed").strip()
    return True, "PR body updated"


# ── Checklist rendering + sync ───────────────────────────────────────────

def format_automated_section(test_results: dict) -> str:
    """Render the ``### Automated`` subsection markdown.

    ``test_results`` schema mirrors ``test_discovery.save_last_run_results``::

        {
            "passed": int,
            "total":  int,
            "ran_at": <iso timestamp string>,
        }

    The subsection's ticked items reflect what we can verify from the
    local pytest run. Items the manager can't verify itself (e.g. "CI
    test-gate passes on this PR") stay unticked.
    """
    passed = int(test_results.get("passed", 0))
    total  = int(test_results.get("total", 0))
    ran_at = test_results.get("ran_at", "unknown time")
    all_pass = total > 0 and passed == total
    ts_tick = "x" if all_pass else " "
    return (
        f"{_AUTOMATED_HEADING}\n"
        f"- [{ts_tick}] Test suite passes locally "
        f"({passed}/{total} passed as of {ran_at})\n"
        "- [ ] CI test-gate job passes on this PR (check GitHub Actions tab)\n"
    )


def sync_checklist_section(current_body: str,
                            test_results: dict) -> tuple[str, bool]:
    """Re-render the ``### Automated`` subsection inside *current_body*.

    Returns ``(new_body, changed)``. ``changed`` is True only when
    the body was actually modified (we found a manager-marked checklist
    section AND its Automated subsection differs from the rendered one).

    Refuses to touch bodies that:
      * Don't contain the ``_MARKER`` HTML comment (third-party / hand-
        written checklists).
      * Lack a ``### Automated`` heading inside the checklist section.

    The ``### Manual`` subsection (and any other text outside the
    Automated heading) is byte-identical pre- and post-sync.
    """
    if _MARKER not in current_body:
        return current_body, False

    new_automated = format_automated_section(test_results)

    # Locate the Automated heading and the boundary that ends it (next
    # ``### `` heading, ``## `` heading, or end of body).
    auto_idx = current_body.find(_AUTOMATED_HEADING)
    if auto_idx == -1:
        return current_body, False

    # Find the next sibling/parent heading after the Automated heading.
    # Use a regex anchored at line starts.
    tail = current_body[auto_idx + len(_AUTOMATED_HEADING):]
    next_heading = re.search(r"\n(### |## |# )", tail)
    if next_heading is None:
        # Automated is the last section; replace until EOF.
        end_idx = len(current_body)
    else:
        end_idx = auto_idx + len(_AUTOMATED_HEADING) + next_heading.start() + 1

    proposed = (
        current_body[:auto_idx]
        + new_automated.rstrip() + "\n\n"
        + current_body[end_idx:]
    )
    return proposed, (proposed != current_body)


def sync_pr_checklist(gh_exe: str, project_root: str,
                       test_results: dict) -> tuple[bool, str]:
    """End-to-end: detect PR → patch body → write back.

    Returns ``(ok, message)``. Messages are user-readable; on success
    include the PR number. On failure include the specific stage that
    failed.
    """
    pr = get_open_pr(gh_exe, project_root)
    if pr is None:
        return False, (
            "No open PR detected for this branch. "
            "Push the branch and open a PR first."
        )
    number = pr.get("number")
    body   = pr.get("body") or ""
    if not isinstance(number, int):
        return False, "Could not read PR number from `gh pr view`."

    if _MARKER not in body:
        return False, (
            f"PR #{number} doesn't have a manager-managed testing "
            "checklist section. Use the manager's Draft PR feature to "
            "regenerate the PR body, or add a `## Testing checklist` "
            "section with the manager marker manually."
        )

    new_body, changed = sync_checklist_section(body, test_results)
    if not changed:
        return True, f"PR #{number} checklist already up to date."

    ok, msg = update_pr_body(gh_exe, project_root, number, new_body)
    if not ok:
        return False, f"PR #{number} update failed: {msg}"
    return True, f"PR #{number} testing checklist synced."


# Suppress unused-import warning for the os module — kept in case future
# checklist features need filesystem access (e.g. reading a local
# `.pr_context.tmp.md`).
_ = os
