"""The local rule that tells an agent the handoff command exists.

An agent only publishes to the ledger if something tells it how. The usual place
for instructions is `AGENTS.md`, and that file is committed and shared, while the
command has to name THIS machine's Manager (`python D:/.../src/cli.py ...`).
Committing a machine path is the mistake `project-baseline.md` is written to
avoid, so the instruction goes in a file that git never sees.

WHERE, AND WHY ONE FILE. `CLAUDE.local.md`. Measured 2026-10-31 with a canary
line in each of two files, both git-excluded: Claude Code loaded
`CLAUDE.local.md`, and Cursor loaded BOTH `CLAUDE.local.md` and its own
`.cursor/rules/*.mdc`. So one file reaches both agents, and a second one for
Cursor would put the same instruction in its context twice (the baseline did
exactly that until it was caught). That Cursor reads `CLAUDE.local.md` is
observed on one machine with one Cursor configuration, not promised by Cursor.

HOW IT STAYS OUT OF GIT. The name is added to `.git/info/exclude` (found with
`git rev-parse --git-path`, so a worktree resolves correctly), which is local to
the clone and changes nothing in the repository. It is added BEFORE the file is
written, so there is never a moment when a file holding a machine path is
visible to `git add -A`. A file that is already TRACKED is refused: writing a
machine path into a committed file would modify it for everyone.

The instruction lives in a marked block, so a person's own `CLAUDE.local.md`
text around it is never touched, and removing the block leaves the rest.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess

from constants import CREATE_NO_WINDOW
from helpers.agent_rules import _read, _replace_block, _write_atomic

RULE_FILE = "CLAUDE.local.md"
BEGIN_MARKER = "<!-- tokensave-manager:handoff:begin -->"
END_MARKER = "<!-- tokensave-manager:handoff:end -->"

STATE_ABSENT = "absent"
STATE_CURRENT = "current"
#: The block exists but differs from what would be written now: the Manager
#: moved, or its config did.
STATE_STALE = "stale"
STATE_TRACKED = "tracked"


@dataclasses.dataclass(frozen=True)
class RuleResult:
    ok: bool
    path: str = ""
    changed: bool = False
    state: str = ""
    error: str = ""
    warnings: tuple = ()


def rule_path(project: str) -> str:
    return os.path.join(project, RULE_FILE)


def _quote(value: str, always: bool = False) -> str:
    """Forward slashes, and quotes only where the text needs them: a bare
    `python` is not made stranger by being wrapped."""
    value = value.replace("\\", "/")
    return f'"{value}"' if always or any(c in value for c in ' \t"&()') else value


def render_rule(runner_argv: list, project: str, config_path: str) -> str:
    """The block's text. Pure.

    `runner_argv` is `vscode_tasks.default_runner(...).argv`: the one answer the
    Manager already has to "how do I run my own CLI here", so the rule cannot
    disagree with the generated VS Code tasks about it.
    """
    base = " ".join(_quote(part) for part in runner_argv)
    # The two paths are always quoted: they are the ones most likely to hold
    # spaces, and a quoted path that did not need it is harmless.
    cmd = (f"{base} handoff --project {_quote(project, True)} "
           f"--config {_quote(config_path, True)}")
    return "\n".join([
        "## Leaving a note for the next agent (TokenSave Manager)",
        "",
        "This file is local to this machine and git-ignored. Another agent (for",
        "example Claude Code or Cursor) may pick this project up after you. When you",
        "finish a meaningful piece of work, leave it a short note by running:",
        "",
        "```",
        f"{cmd} --action publish --agent <your-name> --goal \"...\" "
        "--decision \"...\" --next-step \"...\" --question \"...\"",
        "```",
        "",
        "- `<your-name>` is `claude` if you are Claude Code, `cursor` if you are",
        "  Cursor's agent.",
        "- Write only what you decided, what should happen next and what is still",
        "  open, in your own words. Repeat `--decision`, `--next-step`, `--question`",
        "  and `--file` as needed. `--file` is your own claim; the Manager records",
        "  the branch and the changed files itself.",
        "- Never put a secret, key or password in a note.",
        "- To read what others left: the same command with `--action list --limit 5`.",
        "- If the command says handoff is off, stop. The person has not opted in.",
        "- Skip trivial turns. A note is for a decision someone would otherwise have",
        "  to re-explain.",
    ])


# ── git, asked rather than assumed ───────────────────────────────────────────

def _run_git(git_exe: str, project: str, *args: str):
    try:
        return subprocess.run(
            [git_exe or "git", "-C", project, *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def is_tracked(project: str, git_exe: str = "") -> "bool | None":
    """Is `CLAUDE.local.md` committed? `None` when git cannot say."""
    proc = _run_git(git_exe, project, "ls-files", "--error-unmatch", "--", RULE_FILE)
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    return False if proc.returncode == 1 else None


def is_ignored(project: str, git_exe: str = "") -> "bool | None":
    proc = _run_git(git_exe, project, "check-ignore", "-q", "--", RULE_FILE)
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    return False if proc.returncode == 1 else None


def _is_repository(project: str, git_exe: str) -> bool:
    proc = _run_git(git_exe, project, "rev-parse", "--git-dir")
    return bool(proc and proc.returncode == 0)


def _exclude_path(project: str, git_exe: str) -> str:
    proc = _run_git(git_exe, project, "rev-parse", "--git-path", "info/exclude")
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return ""
    path = proc.stdout.strip()
    return path if os.path.isabs(path) else os.path.join(project, path)


def ensure_ignored(project: str, git_exe: str = "") -> "tuple[bool, str]":
    """Make git ignore the rule file, locally. Returns `(ok, why_not)`.

    Not a repository is `ok`: there is nothing to commit to. A repository where
    it still is not ignored afterwards is NOT ok, because the next step writes a
    machine path into it.
    """
    if not _is_repository(project, git_exe):
        return True, ""
    if is_ignored(project, git_exe):
        return True, ""
    path = _exclude_path(project, git_exe)
    if not path:
        return False, "git did not say where its local exclude file is"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        glue = "" if (not existing or existing.endswith("\n")) else "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{glue}{RULE_FILE}\n")
    except OSError as exc:
        return False, f"could not update {path}: {exc}"
    if is_ignored(project, git_exe) is True:
        return True, ""
    return False, (f"{RULE_FILE} is still not ignored after adding it to "
                   f"{path}; a rule in this repository must be overriding it")


# ── install / status / remove ────────────────────────────────────────────────

def status(project: str, runner_argv: list, config_path: str,
           git_exe: str = "") -> str:
    """`absent` / `current` / `stale` / `tracked`. Derived, never stored."""
    if is_tracked(project, git_exe) is True:
        return STATE_TRACKED
    existing = _read(rule_path(project))
    if BEGIN_MARKER not in existing or END_MARKER not in existing:
        return STATE_ABSENT
    want, _changed = _replace_block(
        existing, render_rule(runner_argv, project, config_path),
        BEGIN_MARKER, END_MARKER)
    return STATE_CURRENT if want == existing else STATE_STALE


def install(project: str, runner_argv: list, config_path: str,
            git_exe: str = "") -> RuleResult:
    """Write the block. Order matters: refuse tracked, ignore, THEN write."""
    path = rule_path(project)
    if not os.path.isdir(project):
        return RuleResult(False, path, error=f"no such folder: {project}")
    if is_tracked(project, git_exe) is True:
        return RuleResult(False, path, state=STATE_TRACKED, error=(
            f"{RULE_FILE} is committed in this repository, and writing this "
            f"machine's paths into a tracked file would change it for everyone. "
            f"Nothing was written."))
    ok, why = ensure_ignored(project, git_exe)
    if not ok:
        return RuleResult(False, path, error=f"not written, because {why}")
    new, changed = _replace_block(
        _read(path), render_rule(runner_argv, project, config_path),
        BEGIN_MARKER, END_MARKER)
    if not changed:
        return RuleResult(True, path, False, STATE_CURRENT)
    ok, err = _write_atomic(path, new)
    if not ok:
        return RuleResult(False, path, error=err)
    return RuleResult(True, path, True, STATE_CURRENT)


def remove(project: str, git_exe: str = "") -> RuleResult:
    """Take the block out, leave everything else, delete the file if nothing is
    left. The `.git/info/exclude` line is kept: it is harmless and the person
    may have put their own text in the file."""
    path = rule_path(project)
    existing = _read(path)
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        return RuleResult(True, path, False, STATE_ABSENT)
    tail = existing[end + len(END_MARKER):].lstrip("\n")
    head = existing[:start].rstrip("\n")
    rest = (head + ("\n\n" if head and tail else "") + tail).strip("\n")
    try:
        if rest:
            ok, err = _write_atomic(path, rest + "\n")
            if not ok:
                return RuleResult(False, path, error=err)
        else:
            os.remove(path)
    except OSError as exc:
        return RuleResult(False, path, error=f"could not update {path}: {exc}")
    return RuleResult(True, path, True, STATE_ABSENT)
