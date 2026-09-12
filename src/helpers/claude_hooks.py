"""One owned Claude Code hook, described by a row rather than written twice.

`read_nudge` was the first; the session note is the second, and the second is
where this project's rule applies: **a new hook is a table row, never a new
literal** — the same principle `helpers/agent_cli.py` keeps for agent CLIs.
Everything that differs between hooks is a `HookSpec` field; everything that is
the same (resolving an interpreter, owning exactly one entry, backing up and
atomically rewriting `~/.claude/settings.json`, reporting state) lives here once.

WHY OWNERSHIP IS STRUCTURAL. A marker string is for discovery only; identity is
the marker PLUS a hash of the generated body PLUS the script path an entry
actually points at. A hand-written hook of the user's that matches the same tool,
or that happens to contain our marker, is not ours and is never repaired,
replaced or removed. D1b's "unknown is never false", applied to ownership.

WHY `uninstall` REMOVES THE SCRIPT TOO. A toggle that stops the behaviour must
leave no trace that a later audit misreads. `installed_state` distinguishes
*never installed* (no entry, no script — silence) from *installed and then
unregistered* (script present, entry gone — loud), so an uninstall that removed
only the entry would leave every future Doctor run reporting a drift the user
deliberately created.

THE SETTINGS FILE IS AN AGENT-CONTROL FILE, and this module treats it as one:
unreadable or malformed is `UNKNOWN` with its reason and **no repair**, a backup
is taken before any mutation, and everything not ours survives semantically.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sys
import time

from helpers.mcp_paths import _write_json_atomic

# ── Install states ────────────────────────────────────────────────────────
CURRENT = "current"
STALE = "stale"
DUPLICATE = "duplicate"
ABSENT = "absent"
UNKNOWN = "unknown"

#: Which half is wrong, so a diagnostic can say. An entry that is perfect
#: beside a script somebody replaced by hand is not "the hook is stale", and
#: telling the user it is sends them to the wrong repair.
HALF_ENTRY = "settings entry"
HALF_SCRIPT = "hook script"


@dataclasses.dataclass(frozen=True)
class HookSpec:
    """Everything that distinguishes one owned hook from another."""

    #: Stable identity used in the script's first-line marker.
    marker: str
    #: Bumped when the generated script's semantics change; `installed_state`
    #: reports STALE and `install` replaces exactly one owned entry.
    version: int
    #: Claude Code event name, e.g. "PostToolUse" / "Stop".
    event: str
    #: Tool matcher. Empty string for events that do not match a tool.
    matcher: str
    #: Filename under ~/.claude/hooks/.
    basename: str
    #: The generated script BODY. The marker header is prepended here, so a
    #: row cannot forget it and a version bump cannot miss it.
    body: str

    @property
    def script(self) -> str:
        return "# %s v%d\n%s" % (self.marker, self.version, self.body)


def script_fingerprint(text: str) -> str:
    """Content identity, newline-insensitive.

    A checkout or an editor may rewrite line endings without changing a single
    instruction, and calling that a stale hook would send the user to a repair
    that changes nothing.
    """
    normalised = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def hooks_dir() -> str:
    """`~/.claude/hooks/` — deliberately NOT the Manager's install directory.

    Rule D1f: an absolute pointer into the install dir is one more thing a
    relocation breaks, and `template_dir` is meant to be the only relocatable
    key. The Manager owns these files' content; the home directory owns where
    they live.
    """
    return os.path.join(os.path.expanduser("~"), ".claude", "hooks")


def default_script_path(spec: HookSpec) -> str:
    return os.path.join(hooks_dir(), spec.basename)


def default_settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def resolve_interpreter(python_exe: str = "") -> "tuple[str, str]":
    """`(interpreter, error)`. A console python, never `pythonw`, never bare.

    `pythonw.exe` has no stdout to write to, and a bare `python` is not reliably
    resolvable from a hook's environment. Returning an error rather than a guess
    is the point: a hook that cannot run is worse than no hook, because it looks
    installed.
    """
    candidates = []
    if python_exe:
        base = os.path.basename(python_exe)
        if base.lower().startswith("pythonw"):
            candidates.append(os.path.join(os.path.dirname(python_exe),
                                           base[:6] + base[7:]))
        else:
            candidates.append(python_exe)
    if (sys.executable
            and not os.path.basename(sys.executable).lower().startswith("pythonw")):
        candidates.append(sys.executable)
    for name in ("python.exe", "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate, ""
    return "", ("No console Python interpreter could be resolved, so the hook "
                "was not installed. A hook that cannot run looks installed and "
                "does nothing.")


def hook_entry(spec: HookSpec, interpreter: str, script_path: str) -> dict:
    """The one owned settings entry.

    `command` + `args` array, the shape tokensave's own entries use and the one
    the hook-quoting bug was fixed to — so no shell quoting is involved at all.
    """
    entry = {"hooks": [{"type": "command", "command": interpreter,
                        "args": [script_path]}]}
    if spec.matcher:
        entry = {"matcher": spec.matcher, **entry}
    return entry


def is_owned_entry(spec: HookSpec, entry: dict, script_path: str) -> bool:
    """Ours, structurally — never "an entry that looks a bit like ours"."""
    if not isinstance(entry, dict):
        return False
    if spec.matcher and entry.get("matcher") != spec.matcher:
        return False
    target = os.path.normcase(os.path.abspath(script_path))
    for handler in entry.get("hooks") or []:
        if not isinstance(handler, dict) or handler.get("type") != "command":
            continue
        for arg in handler.get("args") or []:
            try:
                if os.path.normcase(os.path.abspath(str(arg))) == target:
                    return True
            except (OSError, ValueError):
                continue
    return False


def read_settings(settings_path: str) -> "tuple[dict, str]":
    """`(settings, unknown_reason)`. Absent is a fact; unreadable is not.

    An absent settings file is an empty one for our purposes — there is simply
    no owned entry in it. Unreadable and malformed are different, and both are
    UNKNOWN with distinct reasons, because a repair written on top of a file we
    could not parse is exactly the agent-control accident to avoid.
    """
    if not os.path.exists(settings_path):
        return {}, ""
    try:
        with open(settings_path, encoding="utf-8-sig") as handle:
            raw = handle.read()
    except OSError as exc:
        return {}, "settings unreadable (%s)" % exc.__class__.__name__
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}, "settings JSON malformed"
    if not isinstance(data, dict):
        return {}, "settings JSON is not an object"
    return data, ""


def owned_entries(spec: HookSpec, settings: dict, script_path: str) -> list:
    """Indices of our entries within `hooks.<event>`, in order."""
    entries = ((settings.get("hooks") or {}).get(spec.event) or [])
    if not isinstance(entries, list):
        return []
    return [index for index, entry in enumerate(entries)
            if is_owned_entry(spec, entry, script_path)]


def installed_state(spec: HookSpec, settings_path: str = "",
                    script_path: str = "") -> "tuple[str, str]":
    """`(state, detail)` over BOTH identities — the entry and the script."""
    settings_path = settings_path or default_settings_path()
    script_path = script_path or default_script_path(spec)

    settings, unknown = read_settings(settings_path)
    if unknown:
        return UNKNOWN, unknown

    indices = owned_entries(spec, settings, script_path)
    if not indices:
        return ABSENT, "no owned %s entry in %s" % (spec.event, settings_path)
    if len(indices) > 1:
        return DUPLICATE, ("%d owned %s entries; exactly one is expected"
                           % (len(indices), spec.event))

    if not os.path.exists(script_path):
        return STALE, "%s present, but %s is missing" % (HALF_ENTRY, script_path)
    try:
        with open(script_path, encoding="utf-8") as handle:
            on_disk = handle.read()
    except OSError as exc:
        return UNKNOWN, "script unreadable (%s)" % exc.__class__.__name__

    if script_fingerprint(on_disk) != script_fingerprint(spec.script):
        return STALE, ("%s is current; the %s does not match the version this "
                       "Manager generates" % (HALF_ENTRY, HALF_SCRIPT))
    return CURRENT, ""


def _refuse(unknown: str) -> str:
    return ("Refusing to write: %s. This is an agent-control file, and a repair "
            "written over one that could not be parsed is worse than the "
            "warning it would silence." % unknown)


def _backup(settings_path: str, actions: list) -> str:
    """Timestamped copy before any mutation. `""`, or the error."""
    if not os.path.exists(settings_path):
        return ""
    backup = "%s.backup.%d" % (settings_path, int(time.time() * 1000))
    try:
        shutil.copy2(settings_path, backup)
    except OSError as exc:
        return "Could not back up %s: %s" % (settings_path, exc)
    actions.append("Backed up to %s" % backup)
    return ""


def install(spec: HookSpec, python_exe: str = "", settings_path: str = "",
            script_path: str = "") -> "tuple[bool, str, list]":
    """Write the script and register exactly one owned entry.

    Everything not ours survives: other event groups, other matchers, several
    handlers per entry, non-command hooks and hand-written additions.
    """
    settings_path = settings_path or default_settings_path()
    script_path = script_path or default_script_path(spec)

    interpreter, error = resolve_interpreter(python_exe)
    if error:
        return False, error, []

    settings, unknown = read_settings(settings_path)
    if unknown:
        return False, _refuse(unknown), []

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False, "Refusing to write: `hooks` is not an object.", []
    entries = hooks.setdefault(spec.event, [])
    if not isinstance(entries, list):
        return False, "Refusing to write: `hooks.%s` is not a list." % spec.event, []

    actions = []
    try:
        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(spec.script)
    except OSError as exc:
        return False, "Could not write %s: %s" % (script_path, exc), []
    actions.append("Wrote %s (v%d)" % (script_path, spec.version))

    indices = owned_entries(spec, settings, script_path)
    entry = hook_entry(spec, interpreter, script_path)
    if indices:
        entries[indices[0]] = entry
        for index in reversed(indices[1:]):
            del entries[index]
            actions.append("Removed a duplicate owned %s entry" % spec.event)
        actions.append("Updated the owned %s entry" % spec.event)
    else:
        entries.append(entry)
        actions.append("Added the owned %s entry%s"
                       % (spec.event,
                          " (matcher %r)" % spec.matcher if spec.matcher else ""))

    # Only now — after every refusal above, so a validation failure leaves no
    # litter behind it.
    failed = _backup(settings_path, actions)
    if failed:
        return False, failed, actions

    ok, write_error = _write_json_atomic(settings_path, settings)
    if not ok:
        return False, write_error, actions
    actions.append("Wrote %s" % settings_path)
    return True, "", actions


def uninstall(spec: HookSpec, settings_path: str = "",
              script_path: str = "") -> "tuple[bool, str, list]":
    """Remove our entry AND our script. `(ok, error, actions)`.

    Re-reads settings first and matches structurally, rather than mutating a
    stale in-memory copy: another installation, or a `tokensave install`, may
    have rewritten the hooks block since this dialog opened.

    The script goes too. `installed_state` distinguishes never-installed from
    installed-then-unregistered, so leaving the file behind would make every
    later audit report a drift the user deliberately created.

    Absent is success: turning off something already off is not an error.
    """
    settings_path = settings_path or default_settings_path()
    script_path = script_path or default_script_path(spec)

    settings, unknown = read_settings(settings_path)
    if unknown:
        return False, _refuse(unknown), []

    actions = []
    entries = ((settings.get("hooks") or {}).get(spec.event) or [])
    indices = owned_entries(spec, settings, script_path)
    if indices:
        failed = _backup(settings_path, actions)
        if failed:
            return False, failed, actions
        for index in reversed(indices):
            del entries[index]
        actions.append("Removed %d owned %s entr%s" % (
            len(indices), spec.event, "y" if len(indices) == 1 else "ies"))
        # An empty list is litter; the `hooks` object itself stays, since
        # removing a key the user may have other plans for is a bigger
        # surprise than leaving it.
        if not entries:
            settings["hooks"].pop(spec.event, None)
        ok, write_error = _write_json_atomic(settings_path, settings)
        if not ok:
            return False, write_error, actions
        actions.append("Wrote %s" % settings_path)

    if os.path.exists(script_path):
        try:
            os.remove(script_path)
            actions.append("Removed %s" % script_path)
        except OSError as exc:
            return False, "Could not remove %s: %s" % (script_path, exc), actions

    if not actions:
        actions.append("Nothing to remove — the hook was not installed")
    return True, "", actions
