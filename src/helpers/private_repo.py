"""private_repo.py — Pure sync helper for private local git repos.

Completely decoupled from App/controllers.  Accepts only primitive args
(paths, lists, callables) so it can be unit-tested in isolation.

Public surface:
    sync_private_repo(git_exe, src_path, dest, tracked_files, on_log, commit_msg)
        -> bool  (True = success or nothing-to-do, False = error)
"""

from __future__ import annotations

import os
import shutil
import subprocess

from constants import C, CREATE_NO_WINDOW

# Git identity injected into every commit made by the manager so the
# operation never fails on machines without global user.name/email config.
_BACKUP_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME":    "Manager Backup Agent",
    "GIT_AUTHOR_EMAIL":   "backup@local",
    "GIT_COMMITTER_NAME": "Manager Backup Agent",
    "GIT_COMMITTER_EMAIL": "backup@local",
}


def sync_private_repo(
    git_exe: str,
    src_path: str,
    dest: str,
    tracked_files: list,
    on_log,
    commit_msg: str = "",
) -> bool:
    """Copy tracked files from *src_path* to *dest*, then commit any changes.

    Mirror semantics:
      - File exists in src  → copy to dest (create parent dirs as needed)
      - File missing in src → remove from dest; prune empty parent dirs
        (never removes the repo root itself)

    Returns True on success (including "nothing to commit"), False on any
    git error.  *on_log(msg, colour)* is called for each notable event and
    must be thread-safe (the caller is responsible for scheduling if needed).
    """
    # ── 1. Destination sanity check ──────────────────────────────────────────
    if not os.path.isdir(dest):
        on_log(
            f"  ✗ Private repo missing at {dest!r} — "
            "run 🔒 Create Private Local Repo… to set it up again.",
            C["red"],
        )
        return False

    abs_dest = os.path.abspath(dest)

    # ── 2. Mirror files ───────────────────────────────────────────────────────
    for fname in tracked_files:
        src_file = os.path.join(src_path, fname)
        dst_file = os.path.join(dest, fname)
        abs_dst_file = os.path.abspath(dst_file)

        if os.path.isfile(src_file):
            dst_dir = os.path.dirname(abs_dst_file)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src_file, dst_file)
        elif os.path.isfile(dst_file):
            os.remove(dst_file)
            parent_dir = os.path.abspath(os.path.dirname(dst_file))
            # Never prune the repo root itself — only intermediate dirs
            if parent_dir != abs_dest:
                try:
                    os.removedirs(parent_dir)
                except OSError:
                    pass  # Directory not empty; other files live there

    # ── 3. Check git status ───────────────────────────────────────────────────
    try:
        status_proc = subprocess.run(
            [git_exe, "-C", dest, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        on_log(f"  ✗ git status failed: {exc}", C["red"])
        return False

    if not status_proc.stdout.strip():
        on_log("  Private repo already up to date.", C["overlay0"])
        return True

    # ── 4. Stage ─────────────────────────────────────────────────────────────
    try:
        add_proc = subprocess.run(
            [git_exe, "-C", dest, "add", "-A"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            env=_BACKUP_GIT_ENV,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        on_log(f"  ✗ git add failed: {exc}", C["red"])
        return False

    if add_proc.returncode != 0:
        on_log(f"  ✗ git add: {add_proc.stderr.strip()}", C["red"])
        return False

    # ── 5. Commit ─────────────────────────────────────────────────────────────
    proj_name = os.path.basename(src_path)
    if commit_msg:
        msg = f"sync: {proj_name} — {commit_msg[:60]}"
    else:
        msg = f"sync: {proj_name} (manual)"

    try:
        commit_proc = subprocess.run(
            [git_exe, "-C", dest, "commit", "-m", msg],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            env=_BACKUP_GIT_ENV,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        on_log(f"  ✗ git commit failed: {exc}", C["red"])
        return False

    if commit_proc.returncode == 0:
        for line in commit_proc.stdout.strip().splitlines()[-3:]:
            on_log(f"  {line}", C["green"])
        on_log("  ✓ Private repo synced.", C["green"])
        return True
    else:
        on_log(f"  ✗ git commit: {commit_proc.stderr.strip()}", C["red"])
        return False
