"""Low-level git helpers — pure functions, no module globals.

Every function that shells out to git takes `git_exe` (path to git.exe)
as an explicit parameter — per the Round 4 plan, helpers don't read the
GIT_EXE module global. Callers in the monolith / dialogs / controllers
pass `self._cfg.git_exe` (post-ManagerConfig) or the module global
`GIT_EXE` (during the Phase A transition window).

The two pure-parsing helpers (`_parse_git_status_v2`,
`_format_git_status_cell`) don't need git_exe — they consume the output
of a previous git call rather than running git themselves.

`_is_local_git_repo` is also pure — it inspects the filesystem and
never runs git.
"""

from __future__ import annotations

import os
import subprocess

from constants import CREATE_NO_WINDOW


def _is_git_repo(path: str, git_exe: str) -> bool:
    """Return True if *path* is inside an initialised git repository.

    NOTE: this walks UPWARD via `git rev-parse --git-dir` — so a project
    folder inside a parent git repo will also return True. For the strict
    'this folder IS a repo root' check, use _is_local_git_repo instead.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def _find_gitignored_on_disk(path: str, git_exe: str) -> list:
    """Return rel paths of files that are gitignored but exist on disk.

    Uses `git ls-files --others --ignored --exclude-standard` which lists
    untracked files whose paths match the project's .gitignore rules.
    Directories (trailing /) are excluded — only individual files are returned.
    Returns [] on any failure (not a repo, git missing, etc.).
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", path,
             "ls-files", "--others", "--ignored", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines()
            if ln.strip() and not ln.endswith("/")]


def _staged_deletions(path: str, git_exe: str) -> list:
    """Return repo-relative paths currently staged for deletion.

    Reads ``git diff --cached --name-only --diff-filter=D`` — files that have
    been ``git rm`` / ``git rm --cached``'d but not yet committed. Returns []
    on any failure (not a repo, git missing, timeout).

    Used in two places:
      * ``_find_tracked_but_ignored`` — to exclude in-progress untracks from
        the stale-ignore warning so it doesn't loop.
      * the commit flow — to reason about staged deletions that a pathspec
        commit would otherwise leave dangling.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", path,
             "diff", "--cached", "--name-only", "--diff-filter=D"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _find_tracked_but_ignored(path: str, git_exe: str) -> list:
    """Return a list of paths that are TRACKED by git in `path` but ALSO
    match a pattern in `.gitignore`.

    Uses `git ls-files -ci --exclude-standard`:
      -c  show cached (tracked) files
      -i  filter to those that are ignored
      --exclude-standard  use the project's actual .gitignore rules

    Files that are already staged for deletion (``git rm --cached`` was
    already run but not yet committed) are excluded from the result — the
    user already did the right thing; re-prompting them on every commit
    until they commit the deletion would be a confusing loop.

    Returns paths relative to the repo root, one per line, empty string
    filtered out. Returns [] if the call fails (not a repo, git missing,
    etc.) — caller can treat empty as "nothing to do".

    This is the canonical way to find the "stale tracking" problem: a
    file that was committed before being added to .gitignore. Git will
    keep tracking it until `git rm --cached <file>` is run, even though
    .gitignore implies the user no longer wants it in the repo.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", path,
             "ls-files", "-ci", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    stale = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not stale:
        return []

    # Filter out files already staged for deletion — `git rm --cached` was
    # run but the commit hasn't landed yet. Re-prompting would create a loop
    # where every commit attempt re-warns about a fix already in progress.
    already_staged = {
        d.replace("\\", "/") for d in _staged_deletions(path, git_exe)
    }
    if already_staged:
        stale = [f for f in stale if f.replace("\\", "/") not in already_staged]

    return stale


def _is_local_git_repo(path: str) -> bool:
    """Return True only if *path* itself is a git repo root.

    Strict local check — does NOT walk upward. Use this whenever the
    intent is 'should we treat this folder as its own version-controlled
    project?' (e.g. commit-prompt flows, .gitignore writes).

    Uses os.path.exists rather than os.path.isdir because git worktrees
    store `.git` as a flat text file pointing to the main repo's .git/.
    os.path.isdir would miss those; os.path.exists handles both.
    """
    return os.path.exists(os.path.join(path, ".git"))


def _parse_git_status_v2(text: str) -> dict:
    """Parse `git status --porcelain=v2 --branch` output.

    Returns a dict with keys:
      dirty      — True if any working-tree or index changes exist
      ahead      — int, commits ahead of upstream (0 if no upstream)
      behind     — int, commits behind upstream
      has_remote — True if `# branch.upstream <name>` line is present

    Pure function — never raises; bad input returns the empty default.
    """
    result = {"dirty": False, "ahead": 0, "behind": 0, "has_remote": False}
    for line in text.splitlines():
        if line.startswith("# branch.upstream "):
            result["has_remote"] = True
        elif line.startswith("# branch.ab "):
            # Format: "# branch.ab +N -M"
            try:
                parts = line.split()
                # parts: ['#', 'branch.ab', '+N', '-M']
                result["ahead"]  = int(parts[2].lstrip("+"))
                result["behind"] = int(parts[3].lstrip("-"))
            except (ValueError, IndexError):
                pass
        elif line and line[0] in ("1", "2", "u", "?"):
            # Tracked-modified (1), renamed/copied (2), unmerged (u), untracked (?)
            result["dirty"] = True
    return result


def _format_git_status_cell(status: dict | None, has_git: bool) -> tuple:
    """Return (display_text, tag_name) for the Git column on the Projects tab.

    status: dict from _parse_git_status_v2, or None if not yet computed
    has_git: True if the project has a .git/ directory at all

    Tags map to colours in _build_projects_tab via tree.tag_configure.
    """
    if not has_git:
        return ("—", "git_none")
    if status is None:
        return ("…", "git_pending")
    if not status["has_remote"]:
        # Repo exists but no remote — can't be ahead/behind
        if status["dirty"]:
            return ("●", "git_dirty")
        return ("✓", "git_clean")
    dirty  = status["dirty"]
    ahead  = status["ahead"]
    behind = status["behind"]
    if not dirty and ahead == 0 and behind == 0:
        return ("✓", "git_clean")
    parts = []
    if dirty:
        parts.append("●")
    if ahead:
        parts.append(f"↑{ahead}")
    if behind:
        parts.append(f"↓{behind}")
    text = "".join(parts)
    # Tag priority: mixed (dirty + remote drift) > behind > ahead > dirty
    if dirty and (ahead or behind):
        tag = "git_mixed"
    elif behind:
        tag = "git_behind"
    elif ahead:
        tag = "git_ahead"
    else:
        tag = "git_dirty"
    return (text, tag)


# ── Release-wizard tag helpers ───────────────────────────────────────────────

def _fetch_tags(path: str, git_exe: str) -> None:
    """Pull tags from origin so ``_last_release_tag`` reflects releases that
    were created remotely without a local ``git tag`` step.

    Why this exists: pre-v1.0.4 releases of this manager (and any release
    created by ``gh release create`` directly) only tag remotely. The local
    tree has no record of those tags until ``git fetch --tags`` runs. If
    the wizard relies purely on ``git describe --tags --abbrev=0``, it'll
    pick up an old prerelease like ``v1.0.0-alpha.1`` and suggest bumps
    from THAT, not the real current version.

    Silent on failure — wizard still works with whatever local tags exist.
    Short timeout (5 s) so a flaky network doesn't block dialog open for
    long.
    """
    try:
        subprocess.run(
            [git_exe, "-C", path, "fetch", "--tags", "--quiet", "origin"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _git_tag(path: str, tag: str, message: str, git_exe: str) -> tuple:
    """Create an annotated local tag. Returns (stdout+stderr, rc)."""
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "tag", "-a", tag, "-m", message],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (f"Error invoking git: {exc}", 1)
    out = (proc.stdout or "") + (proc.stderr or "")
    return (out, proc.returncode)


def _git_push_with_tags(path: str, git_exe: str) -> tuple:
    """Push HEAD plus the new annotated tag in one network round-trip."""
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "push", "origin", "HEAD", "--follow-tags"],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (f"Error invoking git: {exc}", 1)
    out = (proc.stdout or "") + (proc.stderr or "")
    return (out, proc.returncode)


def _current_branch(path: str, git_exe: str) -> "str | None":
    """Return the checked-out branch name, or None.

    None covers every case where there is no branch to name: not a repo,
    git missing, or a detached HEAD (where `--abbrev-ref HEAD` prints the
    literal "HEAD"). Callers that want to display something must supply
    their own fallback rather than treating None as a branch called HEAD.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    name = (proc.stdout or "").strip()
    if not name or name == "HEAD":
        return None
    return name


def git_status_argv(path: str, git_exe: str) -> list:
    """The argv for the porcelain-v2 status both readers parse.

    Extracted so the command itself cannot drift between the Projects-tab
    scanner — which must route through the app's injected shell runner to stay
    off the Tk thread — and the headless CLI, which runs it directly. The
    parser (`_parse_git_status_v2`) was already shared; this makes the input
    shared too.
    """
    return [git_exe, "-C", path, "status", "--porcelain=v2", "--branch"]


def read_git_status(path: str, git_exe: str) -> "dict | None":
    """Branch + dirty state for *path*, or None when it cannot be determined.

    None means "we could not find out", which is deliberately not the same as
    a clean tree — reporting an unreadable repository as clean is the failure
    this codebase keeps designing against.

    Cheap by construction: one `git status` and one `rev-parse`, no tree walk.
    """
    try:
        proc = subprocess.run(
            git_status_argv(path, git_exe),
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    status = _parse_git_status_v2(proc.stdout or "")
    status["branch"] = _current_branch(path, git_exe)
    return status


def ref_exists(path: str, git_exe: str, ref: str) -> bool:
    """Does *ref* resolve in the repo at *path*?

    Exists because a diff against a ref that is not there produces an empty
    changed-file list, which reads as "nothing changed" — the most reassuring
    possible rendering of "the question was never asked". A caller that wants
    to say "that base does not exist" has to check first.
    """
    if not ref:
        return False
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "rev-parse", "--verify", "--quiet",
             f"{ref}^{{commit}}"],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0
