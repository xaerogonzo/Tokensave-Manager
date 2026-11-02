"""Find, and with a person's yes repair, tokensave hooks Cursor cannot run.

`tokensave install --agent cursor` writes each hook into `~/.cursor/hooks.json`
as `<path to tokensave.exe> hook-...`. When that path has a space in it (this
machine's does: `D:/Claude Co worker/Token Save/tokensave.exe`) the command is
cut at the first space, and Windows answers `'D:/Claude' is not recognized`. A
failing `preToolUse` hook on `Grep|Shell` is the one that can stop a tool call, so
it is not cosmetic. The same installer re-writes the unquoted form on every
upgrade, which is why this is a check that can be repeated and not a one-off fix.

**The form it writes is `& "<exe>" <args>`.** Cursor evaluates a hook through
PowerShell on Windows (measured 2026-10-31: a `SHELL=/bin/bash.exe` made it
evaluate its PowerShell wrapper in bash and fail, which read like a bad hook), and
in PowerShell a quoted program has to be called with `&`. That is a belief about
Cursor, not a fact this module can prove, so a repair is not left standing on it:
every rewritten command is RUN, through PowerShell, with an empty payload, and a
repair whose commands do not all exit 0 is rolled back to the backup.

**Agent-control config, so the discipline is `cursor_hooks`'s**: the change is a
pure function of the existing text shown as a diff before anything is written, a
file that does not parse is refused, a timestamped backup is verified byte for byte
before the write, and every hook that is not a tokensave command is preserved
untouched (the person's own `sessionEnd` hook, this Manager's session marker).

No UI imports.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess

from constants import CREATE_NO_WINDOW
from helpers import cursor_hooks as ch
from helpers.agent_rules import _read, _write_atomic

SHAPE_QUOTED = "quoted"        # starts with a quote or `&`: the path survives a shell
SHAPE_BARE = "bare"            # `D:/path with spaces/tokensave.exe hook-x`: breaks
SHAPE_OTHER = "other"          # a path without spaces, or a command we do not recognise

#: Seconds a repaired command may take when it is tried. `hook-stop` reads a
#: stats file and nothing else; the slowest measured was well under one.
TEST_TIMEOUT_SECONDS = 30

_BARE = re.compile(r'^(?P<exe>[A-Za-z]:[\\/][^"]*?\.exe)(?P<rest>(?:\s+\S.*)?)$')


@dataclasses.dataclass(frozen=True)
class Hook:
    event: str
    command: str
    shape: str


@dataclasses.dataclass(frozen=True)
class Fix:
    event: str
    old: str
    new: str


@dataclasses.dataclass(frozen=True)
class RepairPlan:
    ok: bool
    new_text: str = ""
    fixes: tuple = ()
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.fixes)


@dataclasses.dataclass(frozen=True)
class RepairResult:
    ok: bool
    changed: bool = False
    backup: str = ""
    fixes: tuple = ()
    #: `(event, exit code, first line of output)` for each rewritten command.
    tests: tuple = ()
    rolled_back: bool = False
    error: str = ""


def shape_of(command: str) -> str:
    text = (command or "").strip()
    if text.startswith(("&", '"', "'")):
        return SHAPE_QUOTED
    found = _BARE.match(text)
    if found and " " in found.group("exe"):
        return SHAPE_BARE
    return SHAPE_OTHER


def _is_tokensave(command: str) -> bool:
    return "tokensave" in (command or "").lower()


def _entries(data: dict):
    """`(event, index, entry)` for every dict entry under `hooks`."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if isinstance(entry, dict):
                yield event, i, entry


def scan(existing: "str | None") -> "tuple[list, str]":
    """Every tokensave hook with its shape: `(hooks, error)`. The Manager's own
    session marker is not one (its script merely has the word in its name). A file
    that does not parse is an error, never an empty list that reads as "nothing wrong"."""
    data, error = ch._parse(existing)
    if data is None:
        return [], error
    return [Hook(event, str(entry.get("command") or ""), shape_of(str(entry.get("command") or "")))
            for event, _i, entry in _entries(data)
            if _is_tokensave(str(entry.get("command") or "")) and not ch._is_ours(entry)], ""


def requote(command: str) -> str:
    """`D:/a b/tokensave.exe hook-x` -> `& "D:/a b/tokensave.exe" hook-x`."""
    found = _BARE.match((command or "").strip())
    if not found or " " not in found.group("exe"):
        return command
    return f'& "{found.group("exe")}"{found.group("rest")}'


def plan_repair(existing: "str | None") -> RepairPlan:
    """The text `hooks.json` should have after the repair. Writes nothing."""
    data, error = ch._parse(existing)
    if data is None:
        return RepairPlan(False, error=error)
    fixes = []
    for event, i, entry in _entries(data):
        command = str(entry.get("command") or "")
        if _is_tokensave(command) and shape_of(command) == SHAPE_BARE:
            new = requote(command)
            data["hooks"][event][i] = {**entry, "command": new}
            fixes.append(Fix(event, command, new))
    if not fixes:
        return RepairPlan(True, existing or "")
    return RepairPlan(True, json.dumps(data, indent=2, ensure_ascii=False) + "\n", tuple(fixes))


def run_command(command: str) -> "tuple[int, str]":
    """Try one hook command the way Cursor's PowerShell wrapper would: `{}` on stdin."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            input="{}", capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TEST_TIMEOUT_SECONDS,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"could not run it: {exc}"
    text = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, (text.splitlines() or [""])[0][:120]


def repair(home: str = "", now_ms: "int | None" = None, runner=run_command) -> RepairResult:
    """Quote the broken commands, prove each one runs, or put the file back."""
    path = ch.hooks_json_path(home)
    if not os.path.exists(path):
        return RepairResult(True, error="")
    existing = _read(path)
    plan = plan_repair(existing)
    if not plan.ok:
        return RepairResult(False, error=plan.error)
    if not plan.changed:
        return RepairResult(True)
    backup, err = ch._backup(path, now_ms)
    if err:
        return RepairResult(False, error=err)
    ok, err = _write_atomic(path, plan.new_text)
    if not ok:
        return RepairResult(False, backup=backup, error=err)
    tests = []
    for fix in plan.fixes:
        code, line = runner(fix.new)
        tests.append((fix.event, code, line))
    if any(code != 0 for _e, code, _l in tests):
        # The belief this module rests on did not hold here. Put the file back.
        restored, rerr = _write_atomic(path, existing)
        failed = ", ".join(e for e, c, _l in tests if c != 0)
        return RepairResult(
            False, True, backup, plan.fixes, tuple(tests), rolled_back=restored,
            error=(f"the quoted command did not run for: {failed}; "
                   + ("hooks.json was put back as it was" if restored
                      else f"AND hooks.json could not be restored ({rerr}); "
                           f"the backup is {backup}")))
    return RepairResult(True, True, backup, plan.fixes, tuple(tests))
