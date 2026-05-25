"""Scaffold a Claude Code Stop hook into a project's `.claude/` folder.

Writes two files:
  - `.claude/auto-commit-helper.py` (the smart auto-commit script)
  - `.claude/settings.json` (with a Stop hook that invokes the helper)

Both writes are idempotent. The helper script is rewritten every time
so projects pick up newer logic on the next Retrofit pass. The settings
merge is non-destructive — existing hooks for other matchers are
preserved.

Pure function: takes a project path, returns a list of action strings.
No globals; no dependencies on the rest of the manager.
"""

from __future__ import annotations

import json
import os


def _shell_quote_win(path: str) -> str:
    """Wrap `path` in double-quotes if it contains spaces, escaping embedded quotes.

    For use ONLY in cmd-compatible hook command strings embedded in
    .claude/settings.json hook JSON. NOT for PowerShell, bare subprocess
    lists, or contexts with variable expansion.
    """
    if " " not in path:
        return path
    return '"' + path.replace('"', '""') + '"'


def _make_stop_hook_cmd(helper_path: str) -> str:
    """Return the Stop hook command string for the given absolute helper script path."""
    return f"python {_shell_quote_win(helper_path)}"


# Legacy command we previously wrote — kept here so the idempotency check still
# recognises older projects that have the dumb single-line version and so the
# Retrofit pass can upgrade them in place.
_LEGACY_STOP_HOOK_CMD = (
    'git add -A && git diff --cached --quiet || '
    'git commit -m "auto: Claude session"'
)

# Helper script written to <project>/.claude/auto-commit-helper.py. Idempotent
# stop-hook brain: stages changes, bails if clean, then either amends the
# previous auto-commit or creates a new one. Designed to be path-portable so
# it works on Windows (cmd / PowerShell) and Unix shells alike — the Stop hook
# command is just `python ".claude/auto-commit-helper.py"`.
_AUTO_COMMIT_HELPER = '''"""Smart auto-commit Stop hook for Claude Code.

Managed by TokenSave Manager — do not hand-edit; it gets overwritten on
the next Retrofit pass. To disable: remove the Stop hook from
`.claude/settings.json`.

Behavior:
- Stages all changes (`git add -A`)
- Exits 0 if the working tree was already clean
- Builds a useful message: `auto: N file(s) (HH:MM) - path1, path2, +K more`
- If HEAD is itself an `auto:`-prefixed commit, AMENDS it rather than
  creating a new commit. So a long Claude Code session collapses into a
  SINGLE auto-commit that gets updated each session-end, instead of a wall
  of identical "auto: Claude session" entries.
"""
import subprocess
import sys
from datetime import datetime


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# 1. Stage everything Claude touched this session.
_run(["git", "add", "-A"])

# 2. Anything to commit?  `--quiet` exits 0 when index == HEAD.
if _run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
    sys.exit(0)

# 3. Build a useful commit message.
names = _run(["git", "diff", "--cached", "--name-only"]).stdout.splitlines()
names = [n for n in names if n.strip()]
count = len(names)
sample = ", ".join(names[:3])
if count > 3:
    sample += f", +{count - 3} more"
ts = datetime.now().strftime("%H:%M")
plural = "" if count == 1 else "s"
msg = f"auto: {count} file{plural} ({ts}) - {sample}" if sample else f"auto: Claude session ({ts})"

# 4. Amend if previous commit was also an auto-commit; otherwise fresh commit.
last_msg = _run(["git", "log", "-1", "--pretty=%s"]).stdout.strip()
if last_msg.startswith("auto:"):
    subprocess.run(["git", "commit", "--amend", "-m", msg])
else:
    subprocess.run(["git", "commit", "-m", msg])
'''


def _scaffold_git_hook(path: str) -> list:
    """Write/merge a Claude Code Stop hook into .claude/settings.json AND
    write the smart helper script to .claude/auto-commit-helper.py.

    Creates .claude/ if it doesn't exist. Merges settings.json
    non-destructively. The helper script is always rewritten so projects
    pick up newer logic on the next Retrofit pass. Returns a list of
    human-readable action strings (for retrofit summaries).
    """
    settings_dir   = os.path.join(path, ".claude")
    settings_path  = os.path.join(settings_dir, "settings.json")
    helper_path    = os.path.join(settings_dir, "auto-commit-helper.py")
    try:
        os.makedirs(settings_dir, exist_ok=True)
    except OSError:
        return ["Could not create .claude/ directory"]

    actions = []

    # ── 1. Always (re)write the helper script ──────────────────────────────
    try:
        with open(helper_path, "w", encoding="utf-8") as f:
            f.write(_AUTO_COMMIT_HELPER)
        actions.append("Wrote .claude/auto-commit-helper.py (smart auto-commit)")
    except OSError as exc:
        return [f"Could not write {helper_path}: {exc}"]

    # ── 2. Read existing settings ──────────────────────────────────────────
    existing = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, encoding="utf-8-sig") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # treat as empty rather than failing

    hooks = existing.setdefault("hooks", {})
    stop  = hooks.setdefault("Stop", [])

    # ── 3. Upgrade legacy hook OR insert fresh hook ────────────────────────
    # A "managed" hook is any Stop entry whose command references
    # "auto-commit-helper.py" (both old relative and new absolute forms) or
    # the old bare-git oneliner. We replace all older forms with the current
    # absolute-path command so re-runs on paths with spaces work correctly.
    stop_hook_cmd = _make_stop_hook_cmd(helper_path)
    upgraded = False
    inserted = False
    for entry in stop:
        for e in entry.get("hooks", []):
            if e.get("type") != "command":
                continue
            cmd = e.get("command", "")
            if cmd == stop_hook_cmd:
                # already current; nothing to do
                actions.append("Stop hook already on smart helper — skipped")
                break
            if ("auto-commit-helper.py" in cmd
                    or cmd == _LEGACY_STOP_HOOK_CMD
                    or cmd.startswith("git add -A")):
                e["command"] = stop_hook_cmd
                upgraded = True
                actions.append(
                    "Upgraded legacy Stop hook to smart helper "
                    "(amends consecutive auto-commits)")
                break
        else:
            continue
        break
    else:
        stop.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": stop_hook_cmd}],
        })
        inserted = True
        actions.append("Added smart Stop hook to .claude/settings.json")

    # ── 4. Persist settings only if we changed something ───────────────────
    if upgraded or inserted:
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except OSError as exc:
            return actions + [f"Could not write .claude/settings.json: {exc}"]

    return actions
