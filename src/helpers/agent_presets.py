"""Integration presets: one choice that sets several switches, and says what it did.

The Claude/Cursor integration is a handful of independent behaviours (which
agent runs by default, per-feature routing, shared handoff). Each is its own
setting and stays one. A preset is only a *named combination* of them, so a
person can get the whole arrangement in one step without the settings becoming
a mode they can no longer see through.

Three rules:

* **A preset is a diff, shown before it is applied.** `plan` returns exactly the
  keys that would change, old and new. A preset that changes nothing says so.
* **Detection is derived, never stored.** `detect` reads the current values and
  answers "this matches X" or "custom". No config key records which preset is
  active; one would go stale the moment someone changed a single switch, which is
  the whole point of per-behaviour control.
* **A preset never reaches outside the Manager's own config.** It does not edit
  `~/.claude/settings.json`, `~/.cursor/hooks.json` or any project: those are
  reviewed diffs with a yes, and the integration check names what is missing.

"Absent" is a real value. `enable_agent_handoff` not being in the file means off,
and `claude_only` leaves it absent rather than writing `false`, so choosing the
preset on an install that never had the key does not add one.

No UI imports.
"""
import dataclasses

#: A value meaning "this key is not in the config".
ABSENT = object()


@dataclasses.dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    #: `key -> required value` (ABSENT means the key must not be there). A key not
    #: listed is not the preset's business and is never touched.
    settings: tuple


@dataclasses.dataclass(frozen=True)
class Change:
    key: str
    old: object
    new: object

    def describe(self) -> str:
        show = lambda v: "(not set)" if v is ABSENT else repr(v)      # noqa: E731
        return f"{self.key}: {show(self.old)} -> {show(self.new)}"


PRESETS = (
    Preset(
        "claude_only", "Claude only",
        "How the Manager worked before Cursor: Claude Code runs every job, "
        "no per-feature overrides, no shared handoff.",
        (("agent_cli", "claude"), ("agent_overrides", ABSENT),
         ("enable_agent_handoff", ABSENT))),
    Preset(
        "claude_and_cursor", "Claude and Cursor",
        "Agents leave each other notes (decisions, next steps, open questions) "
        "and the Continue-in-the-other-agent actions are useful. The default "
        "agent and any per-feature choices are left as they are.",
        (("enable_agent_handoff", True),)),
)

BY_ID = {p.id: p for p in PRESETS}

CUSTOM = "custom"


def _current(raw: dict, key: str):
    return raw[key] if key in raw else ABSENT


def _equivalent(key: str, have, want) -> bool:
    """`have` already satisfies `want`, treating the documented defaults as absent."""
    if have == want:
        return True
    if want is ABSENT:
        # An empty override table, or a handoff switch that is explicitly off,
        # means the same as the key not being there.
        if key == "agent_overrides":
            return have is ABSENT or have in ({}, None)
        if key == "enable_agent_handoff":
            return have is ABSENT or have is not True
        return False
    if key == "agent_cli" and want == "claude":
        return have is ABSENT or have in ("", None)
    return False


def matches(raw: dict, preset: Preset) -> bool:
    return all(_equivalent(k, _current(raw, k), v) for k, v in preset.settings)


def detect(raw: dict) -> str:
    """The id of the first preset the config satisfies, else `CUSTOM`.

    The presets are written to be mutually exclusive on `enable_agent_handoff`,
    so at most one matches; an order-dependent tie would be a bug in the table.
    """
    for preset in PRESETS:
        if matches(raw, preset):
            return preset.id
    return CUSTOM


def plan(raw: dict, preset_id: str) -> list:
    """The changes applying a preset would make. Raises `KeyError` on a bad id."""
    preset = BY_ID[preset_id]
    changes = []
    for key, want in preset.settings:
        have = _current(raw, key)
        if not _equivalent(key, have, want):
            changes.append(Change(key, have, want))
    return changes


def apply(raw: dict, preset_id: str) -> list:
    """Apply a preset to `raw` in place. Returns the changes made."""
    changes = plan(raw, preset_id)
    for change in changes:
        if change.new is ABSENT:
            raw.pop(change.key, None)
        else:
            raw[change.key] = change.new
    return changes
