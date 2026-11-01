"""Is the Claude/Cursor integration still standing on what it was built on?

The integration leans on things this project does not own: two CLIs whose flags
change between releases, a Claude settings file several tools rewrite, and a
shell environment that a launcher can contaminate. Each of those has already
broken once (a Git-Bash `SHELL` made every Cursor shell call fail with a message
that read like a bad hook). This module is how that breakage becomes a line a
person can read before it costs an afternoon.

Four rules, the same ones the MCP and ledger audits keep:

* **Three-valued, with a fourth for "could not tell".** `PASS`, `WARN`, `FAIL`,
  `UNKNOWN`. A CLI that would not run is `UNKNOWN`, never `PASS`, and never
  `FAIL` for a reason that is really "the check could not look".
* **Verified is a dated fact, not a promise.** `VERIFIED` records the versions
  these integrations were last measured against and when. A different version
  is a `WARN` saying so, not a failure: it usually still works, and the point is
  that nobody is surprised if it does not.
* **Nothing is repaired here.** A check names the fix; touching a file that
  controls an agent's tools stays a reviewed diff with a yes (`cursor_hooks`
  already does that).
* **No model calls.** Everything here is free to run on every Doctor pass.
  Proving the flags still *work* is a separate, opt-in, paid probe.

No UI imports.
"""
import dataclasses
import json
import os
import subprocess

from constants import CREATE_NO_WINDOW

PASS = "pass"
WARN = "warn"
FAIL = "fail"
UNKNOWN = "unknown"

#: Worst first, for picking one state to stand for several.
_SEVERITY = (FAIL, UNKNOWN, WARN, PASS)

#: What the integrations were last measured against: `agent id -> (version,
#: date)`. Updated only by re-measuring (docs/ORCHESTRATION.md section 3), never
#: by editing the number to silence a warning.
VERIFIED = {
    "cursor": ("2026.10.22", "2026-10-31"),
    "claude": ("2.1.261", "2026-10-31"),
}

VERSION_TIMEOUT_SECONDS = 15

#: `(exe, mtime) -> (version text, error)`. A version does not change under an
#: unchanged binary, and Doctor runs once per project.
_VERSION_CACHE: dict = {}


@dataclasses.dataclass(frozen=True)
class Check:
    id: str
    label: str
    state: str
    detail: str = ""
    fix: str = ""


def worst(states) -> str:
    states = set(states)
    for s in _SEVERITY:
        if s in states:
            return s
    return PASS


# ── installed agent versions ────────────────────────────────────────────────────

def probe_version(exe: str) -> "tuple[str, str]":
    """`(first token of --version, "")` or `("", why)`. Never raises."""
    try:
        key = (exe, os.path.getmtime(exe))
    except OSError as exc:
        return "", f"cannot see {exe}: {exc}"
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=VERSION_TIMEOUT_SECONDS,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"could not run it: {exc}"
    words = (proc.stdout or "").split()
    result = ((words[0], "") if proc.returncode == 0 and words
              else ("", f"exited {proc.returncode} with no version"))
    if result[0]:
        _VERSION_CACHE[key] = result
    return result


def _same_version(found: str, verified: str) -> bool:
    """Cursor appends a build hash (`2026.10.22-27dcf5f`); the date is the version."""
    return found == verified or found.startswith(verified + "-")


def check_versions(exes: dict, probe=probe_version) -> list:
    """One check per agent that has an executable. `exes`: `{agent id: path}`."""
    out = []
    for agent, exe in sorted(exes.items()):
        if not exe or agent not in VERIFIED:
            continue
        want, when = VERIFIED[agent]
        found, why = probe(exe)
        if not found:
            out.append(Check(f"version:{agent}", f"{agent} version", UNKNOWN,
                             f"could not read the installed version: {why}"))
        elif _same_version(found, want):
            out.append(Check(f"version:{agent}", f"{agent} version", PASS,
                             f"{found}, the version verified on {when}"))
        else:
            out.append(Check(
                f"version:{agent}", f"{agent} version", WARN,
                f"{found} is installed; the integration was last verified "
                f"against {want} on {when}",
                "usually still fine; if a flag or hook misbehaves, re-run the "
                "live check and update VERIFIED"))
    return out


# ── routing: does the chosen agent exist? ───────────────────────────────────────

def check_routing(resolve) -> list:
    """`resolve(feature)` is `cfg.resolve_agent_cli`. Reports what cannot run."""
    from helpers.agent_cli import (
        FEATURES, RESOLUTION_OK, RESOLUTION_UNKNOWN_AGENT)
    problems, seen = [], set()
    for feature in ("", *[fid for fid, _ in FEATURES]):
        res = resolve(feature)
        if res.state == RESOLUTION_OK:
            continue
        msg = res.error_message()
        if msg in seen:
            continue
        seen.add(msg)
        problems.append(Check(
            "routing" + (f":{feature}" if feature else ""), "agent routing",
            FAIL if res.state == RESOLUTION_UNKNOWN_AGENT else WARN, msg))
    return problems or [Check("routing", "agent routing", PASS,
                              "every selected agent can run")]


# ── the tokensave hooks in ~/.claude/settings.json ──────────────────────────────

SHAPE_QUOTED = "quoted"          # one string, program path quoted
SHAPE_ARGS = "args"              # `command` + `args` (Claude Code runs it; Cursor reads only `command`)
SHAPE_BARE_SPACED = "bare"       # the command is an unquoted path with spaces and no args
SHAPE_OTHER = "other"


def hook_shape(entry: dict) -> str:
    command = str(entry.get("command") or "").strip()
    if command.startswith('"') or command.startswith("'"):
        return SHAPE_QUOTED
    if entry.get("args"):
        return SHAPE_ARGS if " " in command else SHAPE_OTHER
    if " " in command and os.path.exists(command):
        return SHAPE_BARE_SPACED
    return SHAPE_OTHER


def _hook_entries(settings: dict):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for entry in (group.get("hooks") if isinstance(group, dict) else None) or []:
                if isinstance(entry, dict) and entry.get("type") == "command":
                    yield event, entry


def check_claude_hooks(settings_path: str, cursor_present: bool) -> Check:
    """Do the tokensave hooks survive both agents that load this file?

    Cursor's agent loads Claude Code hooks from the same file and, as measured,
    builds its shell line from `command` alone. A spaced path left bare fails in
    both agents; the args form works in Claude Code and is dropped by Cursor.
    """
    label = "tokensave hooks in ~/.claude/settings.json"
    if not os.path.exists(settings_path):
        return Check("claude_hooks", label, PASS, "no settings file, no hooks")
    try:
        with open(settings_path, "r", encoding="utf-8-sig") as fh:
            settings = json.load(fh)
    except (OSError, ValueError) as exc:
        return Check("claude_hooks", label, UNKNOWN,
                     f"could not read {settings_path}: {exc}")
    if not isinstance(settings, dict):
        return Check("claude_hooks", label, UNKNOWN, "the settings file is not an object")
    found = [(event, hook_shape(e)) for event, e in _hook_entries(settings)
             if "tokensave" in str(e.get("command", "")).lower()]
    if not found:
        return Check("claude_hooks", label, PASS, "no tokensave hooks")
    bad = [event for event, shape in found if shape == SHAPE_BARE_SPACED]
    if bad:
        return Check("claude_hooks", label, FAIL,
                     "the command is a path with spaces and is not quoted: "
                     + ", ".join(sorted(set(bad))),
                     "quote the program path in the command (a reviewed edit)")
    args_only = sorted({event for event, shape in found if shape == SHAPE_ARGS})
    # Only PreToolUse was MEASURED to matter: it gates tool calls, and Cursor's
    # parse of the args form made every shell call fail (2026-10-31). The other
    # events were not seen to cause anything, so warning on them would be a
    # standing nag about an effect nobody has observed.
    if "PreToolUse" in args_only and cursor_present:
        return Check("claude_hooks", label, WARN,
                     "the PreToolUse hook uses the command+args form, which "
                     "Claude Code runs but Cursor's agent reads without the "
                     "args; that was measured to break Cursor's shell calls",
                     "put the arguments inside one quoted command string "
                     "(a reviewed edit)")
    note = (f"; {', '.join(a for a in args_only)} use the args form (no effect "
            f"observed)") if args_only and cursor_present else ""
    return Check("claude_hooks", label, PASS,
                 f"{len(found)} tokensave hook(s), runnable as written{note}")


# ── the Cursor session-end hook vs the handoff switch ───────────────────────────

def check_cursor_hook(handoff_on: bool, hook_state: str) -> "Check | None":
    """None when there is nothing to say (handoff off and no hook)."""
    from helpers import cursor_hooks as ch
    label = "Cursor session-end hook"
    if hook_state == ch.STATE_UNREADABLE:
        return Check("cursor_hook", label, UNKNOWN,
                     "~/.cursor/hooks.json could not be read")
    installed = hook_state in (ch.STATE_CURRENT, ch.STATE_STALE, ch.STATE_DUPLICATE)
    if hook_state == ch.STATE_STALE:
        return Check("cursor_hook", label, WARN,
                     "installed, but its entry is out of date", "reinstall it")
    if hook_state == ch.STATE_DUPLICATE:
        return Check("cursor_hook", label, WARN, "installed more than once",
                     "remove the extra entries")
    if handoff_on and not installed:
        return Check("cursor_hook", label, WARN,
                     "handoff is on, but Cursor sessions leave no end-of-session "
                     "marker (optional: publishing still works)",
                     "the Manager has no button for this yet; "
                     "helpers/cursor_hooks.install writes it")
    if installed and not handoff_on:
        return Check("cursor_hook", label, PASS,
                     "installed, but handoff is off so it records nothing")
    if installed:
        return Check("cursor_hook", label, PASS, "installed and handoff is on")
    return None


# ── the whole set ────────────────────────────────────────────────────────────────

def run_checks(cfg, *, home: str = "", probe=probe_version) -> list:
    """Every check, for a config object (`ManagerConfig`-shaped). Never raises."""
    from helpers import cursor_hooks as ch
    from helpers.agent_cli import AGENT_CLIS
    home = home or os.path.expanduser("~")
    exes = {agent: str(getattr(cfg, spec.config_key_exe, "") or "")
            for agent, spec in AGENT_CLIS.items()}
    checks = []
    for build in (
        lambda: check_routing(cfg.resolve_agent_cli),
        lambda: check_versions(exes, probe),
        lambda: [check_claude_hooks(os.path.join(home, ".claude", "settings.json"),
                                    cursor_present=bool(exes.get("cursor")))],
        lambda: [check_cursor_hook(cfg.raw.get("enable_agent_handoff") is True,
                                   ch.status(home))],
    ):
        try:
            checks += [c for c in build() if c is not None]
        except Exception as exc:                                  # noqa: BLE001
            checks.append(Check("internal", "integration check", UNKNOWN,
                                f"a check failed to run: {exc}"))
    return checks


def summarize(checks: list) -> str:
    counts = {s: sum(1 for c in checks if c.state == s) for s in _SEVERITY}
    return ", ".join(f"{counts[s]} {s}" for s in _SEVERITY if counts[s]) or "nothing checked"
