"""git_scrub — Pure helpers for the "Scrub from History" privacy feature (v4.5).

Companion to ``UntrackIgnoredDialog`` / ``GitignoreDialog._on_save`` which
handle the lightweight "stop tracking going forward" path. This module
backs the advanced ``ScrubHistoryDialog`` which fully erases a file from
all commit history via ``git filter-repo``.

Why filter-repo (not filter-branch, not BFG):
  • Git 2.42+ deprecates ``filter-branch`` and prints a banner steering
    users to filter-repo.  Filter-branch is error-prone and 10–720× slower.
  • BFG Repo-Cleaner is a viable alternative (Java JAR, simpler interface)
    but requires Java.  The manager defaults to filter-repo because once
    installed via ``pip install --user git-filter-repo`` it's a single
    Python script with zero further dependencies.
  • If a user prefers BFG, they can run it manually — this module's
    helpers (backup branch, affected-commit display) still apply.

Why ``--force`` is required:
  filter-repo refuses by default to run on non-fresh-clones (a safety
  check against accidental overwrites).  The manager always runs against
  the user's working clone — there is no opportunity to fresh-clone
  invisibly.  We substitute the safety check with our own layered nets:
    1. auto-created backup branch (recoverable via ``git reset --hard``)
    2. confirmation-phrase typing requirement in the dialog
    3. visible commit-affected list before any action
    4. universal-language destructive-action banner
  These reproduce the intent of filter-repo's "fresh clone" guard:
  prevent accidental loss.

All shell-out callsites use ``subprocess.run`` with explicit timeouts and
``CREATE_NO_WINDOW`` on Windows to avoid console-flicker. UTF-8 with
``errors='replace'`` so a misencoded filename never crashes the helper.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Tuple, List

try:
    from constants import CREATE_NO_WINDOW
except ImportError:
    CREATE_NO_WINDOW = 0


# ── filter-repo availability ──────────────────────────────────────────────────

def _user_scripts_dir() -> str:
    """Return the pip --user Scripts directory for the running interpreter.

    On Windows this is ``%LOCALAPPDATA%\\Python\\PythonXX\\Scripts\\`` (or the
    equivalent ``site.getuserbase() / Scripts``).  On POSIX it's
    ``~/.local/bin``.  Returns an empty string on any error.
    """
    try:
        import site as _site
        base = _site.getuserbase()
        if sys.platform == "win32":
            return os.path.join(base, "Scripts")
        return os.path.join(base, "bin")
    except Exception:
        return ""


def _find_filter_repo_script() -> str:
    """Locate the git-filter-repo standalone script, or return ``""``.

    Tries in order:
      1. ``shutil.which("git-filter-repo")`` — covers the case where the user
         scripts dir is already on PATH.
      2. The pip ``--user`` scripts directory derived from
         ``site.getuserbase()`` — the newly-installed script may not appear in
         the current process's PATH snapshot even though it's on disk.
    """
    found = shutil.which("git-filter-repo")
    if found:
        return found
    scripts_dir = _user_scripts_dir()
    if scripts_dir:
        for name in ("git-filter-repo", "git-filter-repo.exe",
                     "git-filter-repo.cmd"):
            candidate = os.path.join(scripts_dir, name)
            if os.path.isfile(candidate):
                return candidate
    return ""


def has_filter_repo(git_exe: str) -> bool:
    """Return True if ``git filter-repo`` (or the standalone script) is found.

    Detection order:
      1. ``git filter-repo --version`` via the git subcommand mechanism —
         the canonical check when the Scripts directory is already on PATH.
      2. ``_find_filter_repo_script()`` — fallback for Windows pip ``--user``
         installs where the Scripts dir is NOT yet in the process's PATH
         snapshot.  If the script file exists on disk we treat it as installed;
         the script itself is invokable directly or via an augmented PATH in
         ``run_scrub``.
    """
    # Primary: git subcommand probe (augmented PATH so newly-installed scripts
    # are visible even if the current process inherited a stale PATH).
    scripts_dir = _user_scripts_dir()
    env = os.environ.copy()
    if scripts_dir:
        env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")

    if git_exe and os.path.isfile(git_exe):
        try:
            proc = subprocess.run(
                [git_exe, "filter-repo", "--version"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                env=env,
            )
            if proc.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    # Fallback: check for the script file directly (covers fresh pip --user
    # installs where git hasn't picked up the new PATH entry yet).
    return bool(_find_filter_repo_script())


def install_filter_repo(on_log=None) -> Tuple[bool, str]:
    """Attempt ``pip install --user git-filter-repo`` and return (success, log).

    Streams stdout/stderr line-by-line into ``on_log(line)`` if provided,
    so the dialog can display install progress in real time. Returns a
    ``(ok, combined_output)`` tuple — ``ok`` is True iff pip exited 0.

    Pip availability: tries ``python -m pip`` (more portable than calling
    ``pip`` directly, which may not be on PATH on Windows).
    """
    py = sys.executable
    if not py:
        return False, "could not locate python interpreter"
    try:
        proc = subprocess.Popen(
            [py, "-m", "pip", "install", "--user", "git-filter-repo"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as exc:
        return False, f"pip launch failed: {exc}"

    log_lines: List[str] = []
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                log_lines.append(line.rstrip())
                if on_log is not None:
                    try:
                        on_log(line.rstrip())
                    except Exception:
                        pass
        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "\n".join(log_lines) + "\n[timed out after 5 min]"
    return proc.returncode == 0, "\n".join(log_lines)


# ── Pre-flight inspection ─────────────────────────────────────────────────────

def is_tracked_in_head(repo_path: str, git_exe: str, rel_file: str) -> bool:
    """Return True if ``rel_file`` is currently tracked by git in HEAD.

    Uses ``git ls-files --error-unmatch -- <file>``: returns 0 only when
    the file is in the index. This is the signal for "the file is still
    tracked and needs to be untracked first before filter-repo can run".
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "ls-files", "--error-unmatch",
             "--", rel_file],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def working_tree_clean(repo_path: str, git_exe: str) -> bool:
    """Return True if there are no uncommitted changes (clean for scrub).

    filter-repo refuses to run with an unclean working tree. The dialog
    uses this to gate the Scrub Now button.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0 and not proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def list_affected_commits(repo_path: str, git_exe: str, rel_file: str,
                          max_n: int = 200) -> List[Tuple[str, str]]:
    """Return ``[(short_sha, subject), …]`` for every commit that touched the file.

    Powers the "show consequences before scrubbing" display in the dialog.
    Caps at ``max_n`` to avoid runaway output for files with very long
    histories — the UI shows "… and N more" beyond the cap.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "log",
             f"--max-count={max_n}",
             "--pretty=format:%h\t%s",
             "--all",
             "--", rel_file],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    out: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if "\t" in line:
            sha, subject = line.split("\t", 1)
            out.append((sha.strip(), subject.strip()))
    return out


# ── Backup branch ─────────────────────────────────────────────────────────────

# First character must NOT be `-` so a name like "--force" can't be
# interpreted as a git flag (subprocess.run uses argv, but git's own
# parser would still see it as a flag). Subsequent characters may
# include hyphens for normal branch names like ``backup/before-scrub-1700000000``.
_BACKUP_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/+][A-Za-z0-9._/+-]*$")


def build_backup_branch_name(prefix: str = "backup/before-scrub") -> str:
    """Return a unique backup branch name like ``backup/before-scrub-1716835200``."""
    ts = int(time.time())
    return f"{prefix}-{ts}"


def create_backup_branch(repo_path: str, git_exe: str,
                         branch_name: str) -> Tuple[bool, str]:
    """Create ``branch_name`` pointing at HEAD; return ``(ok, log)``.

    Validates ``branch_name`` matches a conservative whitelist regex before
    invoking git, so a malformed name can't smuggle CLI flags.
    """
    if not _BACKUP_BRANCH_RE.match(branch_name):
        return False, f"refusing branch name: {branch_name!r}"
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "branch", branch_name, "HEAD"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return (proc.returncode == 0,
                (proc.stdout or "") + (proc.stderr or ""))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


# ── Untrack-and-commit helper (workflow-ordering preamble) ───────────────────

def untrack_and_commit(repo_path: str, git_exe: str, rel_file: str,
                       on_log=None) -> Tuple[bool, str]:
    """Untrack ``rel_file`` and commit the change, in one atomic step.

    Sequence:
      1. ``git rm --cached -- <rel_file>``
      2. ``git commit -m "Stop tracking <rel_file>"``

    Adding the path to ``.gitignore`` is the caller's responsibility
    (the gitignore dialog already does this via its own Save flow).

    Returns ``(ok, combined_log)``. On failure, no commit is created;
    the caller can re-stage / re-commit manually.
    """
    log_chunks: List[str] = []
    def _log(msg: str) -> None:
        log_chunks.append(msg)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    try:
        # Step 1: untrack
        rm_proc = subprocess.run(
            [git_exe, "-C", repo_path, "rm", "--cached", "--", rel_file],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        _log(f"$ git rm --cached -- {rel_file}")
        if rm_proc.stdout:
            _log(rm_proc.stdout.rstrip())
        if rm_proc.stderr:
            _log(rm_proc.stderr.rstrip())
        if rm_proc.returncode != 0:
            return False, "\n".join(log_chunks)

        # Step 2: commit
        msg = f"Stop tracking {rel_file}"
        ci_proc = subprocess.run(
            [git_exe, "-C", repo_path, "commit", "-m", msg],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        _log(f"$ git commit -m '{msg}'")
        if ci_proc.stdout:
            _log(ci_proc.stdout.rstrip())
        if ci_proc.stderr:
            _log(ci_proc.stderr.rstrip())
        return ci_proc.returncode == 0, "\n".join(log_chunks)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log(f"untrack_and_commit error: {exc}")
        return False, "\n".join(log_chunks)


def git_rm_cached(repo_path: str, git_exe: str, rel_file: str,
                  on_log=None) -> Tuple[bool, str]:
    """Run ``git rm --cached -- <rel_file>`` WITHOUT committing.

    Use this instead of :func:`untrack_and_commit` when you want to open the
    manager's ``GitCommitDialog`` afterwards so the user can compose (and
    AI-suggest) the commit message themselves.

    Returns ``(ok, log)``.
    """
    log_chunks: List[str] = []

    def _log(msg: str) -> None:
        log_chunks.append(msg)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "rm", "--cached", "--", rel_file],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        _log(f"$ git rm --cached -- {rel_file}")
        if proc.stdout:
            _log(proc.stdout.rstrip())
        if proc.stderr:
            _log(proc.stderr.rstrip())
        return proc.returncode == 0, "\n".join(log_chunks)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log(f"git_rm_cached error: {exc}")
        return False, "\n".join(log_chunks)


def get_remote_url(repo_path: str, git_exe: str,
                   remote: str = "origin") -> str:
    """Return the fetch URL for ``remote``, or '' if the remote doesn't exist.

    Used to snapshot the URL before ``git filter-repo`` runs, because
    filter-repo unconditionally removes all remotes as part of its safety
    model (see 'Why is my origin removed?' in the filter-repo manual).
    Capturing the URL first lets ``restore_remote_if_missing`` put it back
    immediately before the force-push.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "remote", "get-url", remote],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def restore_remote_if_missing(repo_path: str, git_exe: str,
                               remote: str, url: str,
                               on_log=None) -> bool:
    """Re-add ``remote`` → ``url`` if it was removed by filter-repo.

    filter-repo always removes remotes during a history rewrite (its
    'Why is my origin removed?' note in the manual explains this is
    intentional — it prevents an accidental push of the rewritten
    history to the wrong remote).  Before force-pushing, the manager
    re-adds the remote so the push can proceed without manual
    intervention.

    Returns True if the remote already existed or was re-added
    successfully; False if the re-add command failed.
    """
    def _log(msg: str) -> None:
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    if not url:
        return False
    # Check whether remote already exists
    chk = subprocess.run(
        [git_exe, "-C", repo_path, "remote", "get-url", remote],
        capture_output=True, text=True, timeout=10,
        encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if chk.returncode == 0:
        return True  # still there — nothing to do
    _log(f"filter-repo removed '{remote}' — re-adding: {url}")
    add = subprocess.run(
        [git_exe, "-C", repo_path, "remote", "add", remote, url],
        capture_output=True, text=True, timeout=10,
        encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if add.returncode == 0:
        _log(f"✓ Remote '{remote}' restored.")
        return True
    _log(f"✗ Could not re-add remote: {(add.stdout + add.stderr).strip()}")
    return False


def force_push(repo_path: str, git_exe: str, branch: str,
               on_log=None) -> Tuple[bool, str]:
    """Run ``git push --force origin <branch>``.

    Called from ``ScrubHistoryDialog`` after a successful scrub so the
    rewritten history reaches the remote.  Returns ``(ok, log)``.

    Why ``--force`` and not ``--force-with-lease``:
    ``--force-with-lease`` compares the remote-tracking ref (what we last
    fetched from origin) against the remote's current tip.  After
    ``git filter-repo`` removes the remote and the user re-adds it via
    Set Remote, there are no remote-tracking refs — git has never fetched
    from this fresh remote entry — so ``--force-with-lease`` always
    reports "stale info" and refuses.

    ``--force`` is appropriate here because the user has already passed
    through:
      1. the destructive-action banner,
      2. the confirmation-phrase typing gate,
      3. the "Proceed?" askyesno dialog in ``_on_force_push``.
    Three explicit confirmations substitute for the lease check.
    """
    log_chunks: List[str] = []

    def _log(msg: str) -> None:
        log_chunks.append(msg)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    try:
        _log(f"$ git push --force origin {branch}")
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "push", "--force", "origin", branch],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        combined = (proc.stdout + proc.stderr).rstrip()
        if combined:
            _log(combined)
        return proc.returncode == 0, "\n".join(log_chunks)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log(f"force_push error: {exc}")
        return False, "\n".join(log_chunks)


# ── Scrub execution ───────────────────────────────────────────────────────────

def run_scrub(repo_path: str, git_exe: str, rel_file: str,
              on_log=None) -> Tuple[bool, str]:
    """Erase ``rel_file`` from all of git history.

    Invokes ``git filter-repo --invert-paths --path <rel_file> --force``
    in ``repo_path``.  Returns ``(ok, combined_log)``.

    **Why ``--force``**: filter-repo refuses non-fresh-clones by default
    to prevent accidental overwrites.  Our use case ALWAYS runs on the
    user's working clone — there is no fresh-clone opportunity.  We
    substitute filter-repo's safety check with our own layered nets
    (auto-backup branch + confirmation phrase + visible commit list +
    destructive-action banner). See module docstring.

    Caller MUST ensure (via ``working_tree_clean`` + ``is_tracked_in_head``):
      • working tree is clean (no uncommitted changes)
      • file is no longer tracked in HEAD (use ``untrack_and_commit`` first)
    Otherwise filter-repo will error early.
    """
    log_chunks: List[str] = []
    def _log(msg: str) -> None:
        log_chunks.append(msg)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    # Build the subprocess environment with the pip --user Scripts dir prepended
    # so git can find filter-repo even when the current process has a stale PATH
    # snapshot (common after a fresh pip --user install this session).
    env = os.environ.copy()
    scripts_dir = _user_scripts_dir()
    if scripts_dir:
        env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")

    # Primary: invoke as a git subcommand (git filter-repo --invert-paths …).
    # Fallback: invoke the standalone script directly if git still can't find it
    # (covers Windows pip --user paths that git's exec-path doesn't scan).
    args = [git_exe, "-C", repo_path, "filter-repo",
            "--invert-paths", "--path", rel_file, "--force"]
    _log(f"$ git filter-repo --invert-paths --path {rel_file} --force")

    def _run_args(cmd):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        if proc.stdout is not None:
            for line in proc.stdout:
                _log(line.rstrip())
        proc.wait(timeout=600)
        return proc.returncode

    try:
        rc = _run_args(args)
    except subprocess.TimeoutExpired:
        _log("[scrub timed out after 10 min]")
        return False, "\n".join(log_chunks)
    except (FileNotFoundError, OSError) as exc:
        _log(f"git filter-repo launch error: {exc}")
        return False, "\n".join(log_chunks)

    if rc != 0:
        # git subcommand failed — git for Windows has its own exec-path and
        # often can't find scripts installed via pip --user even when they're
        # on the Windows PATH.  Two fallback strategies in order:
        #
        # 1. importlib: find git_filter_repo.py in site-packages and invoke it
        #    via sys.executable — zero PATH dependency, always works when the
        #    package is installed.
        # 2. shutil.which / _find_filter_repo_script(): invoke the .exe/.cmd
        #    wrapper script directly if importlib can't locate the module.
        #
        # Both fallbacks run with cwd=repo_path so filter-repo auto-detects
        # the repository (same behaviour as `git -C repo_path filter-repo`).

        fallback_script: str = ""

        # Strategy 1: importlib (most reliable on Windows pip --user installs)
        try:
            import importlib.util as _ilu
            spec = _ilu.find_spec("git_filter_repo")
            if spec and spec.origin and os.path.isfile(spec.origin):
                fallback_script = spec.origin
        except Exception:
            pass

        # Strategy 2: PATH / user-scripts probe
        if not fallback_script:
            fallback_script = _find_filter_repo_script()

        if fallback_script:
            # Determine the interpreter: if it's a .py file, run via Python.
            # If it's a .exe/.cmd wrapper, execute it directly.
            if fallback_script.lower().endswith(".py"):
                direct_cmd = [sys.executable, fallback_script]
            else:
                direct_cmd = [fallback_script]
            direct_cmd += ["--invert-paths", "--path", rel_file, "--force"]
            _log(f"[git subcommand not found; retrying via: {fallback_script}]")

            def _run_direct(cmd):
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                    cwd=repo_path,   # filter-repo auto-detects the repo from cwd
                    env=env,
                )
                if proc.stdout is not None:
                    for line in proc.stdout:
                        _log(line.rstrip())
                proc.wait(timeout=600)
                return proc.returncode

            try:
                rc = _run_direct(direct_cmd)
            except subprocess.TimeoutExpired:
                _log("[scrub timed out after 10 min (standalone)]")
            except (FileNotFoundError, OSError) as exc:
                _log(f"standalone scrub error: {exc}")
        else:
            _log("[no standalone filter-repo script found; cannot retry]")

    return rc == 0, "\n".join(log_chunks)


# ── Convenience: full audit before showing the dialog ────────────────────────

def preflight(repo_path: str, git_exe: str) -> dict:
    """Single-call snapshot of everything the scrub dialog needs at open.

    Returns a dict::

        {
            "git_exe":           str,
            "git_exe_present":   bool,
            "filter_repo":       bool,        # is `git filter-repo` callable
            "is_git_repo":       bool,
            "head_branch":       str,
            "working_tree_clean": bool,
        }

    Lets the dialog disable/enable widgets atomically without scattering
    subprocess calls across the UI thread.
    """
    info = {
        "git_exe":            git_exe,
        "git_exe_present":    bool(git_exe and os.path.isfile(git_exe)),
        "filter_repo":        False,
        "is_git_repo":        False,
        "head_branch":        "",
        "working_tree_clean": False,
    }
    if not info["git_exe_present"]:
        return info
    info["filter_repo"] = has_filter_repo(git_exe)
    # is_git_repo via `git rev-parse --is-inside-work-tree`
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        info["is_git_repo"] = (proc.returncode == 0
                               and proc.stdout.strip() == "true")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return info
    # head branch
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            info["head_branch"] = proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    info["working_tree_clean"] = working_tree_clean(repo_path, git_exe)
    # Remote URL — captured at open so the force-push path can restore it
    # even when the dialog is re-opened after a previous scrub session.
    info["remote_url"] = get_remote_url(repo_path, git_exe)
    return info


# Suppress unused-import warnings for `shutil` — reserved for future
# add-on (sanity-cleanup of stale .git/refs/original/ post-scrub).
_ = shutil
