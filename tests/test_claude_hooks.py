"""tests/test_claude_hooks.py -- invariants EVERY owned hook must keep.

Why this exists: read nudge v3 changed its matcher from `Read` to
`Read|Bash|PowerShell`. Ownership included the matcher, so the old entry stopped
being ours, `install` appended a second entry, and `installed_state` reported
CURRENT with two copies of the hook registered. The per-hook tests only ever
exercised one spec, so nothing could notice. These run over every HookSpec.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

import pytest

from helpers import claude_hooks as ch
from helpers import read_nudge, session_note

SPECS = [read_nudge.SPEC, session_note.SPEC]
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _ids(spec):
    return spec.marker


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "settings.json"), str(tmp_path / "hooks" / "h.py")


def _install(spec, paths):
    return ch.install(spec, sys.executable, paths[0], paths[1])


def _entries(spec, paths):
    with open(paths[0], encoding="utf-8") as handle:
        return json.load(handle)["hooks"][spec.event]


def test_every_hook_spec_in_the_source_is_covered_by_these_tests():
    """A new HookSpec must join SPECS, or this file stops guarding it."""
    declared = set()
    for path in (_SRC / "helpers").glob("*.py"):
        if path.name == "claude_hooks.py":
            continue
        if "claude_hooks.HookSpec(" in path.read_text(encoding="utf-8"):
            declared.add(path.stem)
    assert declared == {"read_nudge", "session_note"}, (
        "a HookSpec was added or removed: update SPECS in this file")


@pytest.mark.parametrize("spec", SPECS, ids=_ids)
def test_changing_the_matcher_migrates_in_place_to_one_entry(spec, paths):
    old = dataclasses.replace(spec, matcher="Old", version=spec.version)
    assert _install(old, paths)[0] is True

    # The old entry is OUR script under another matcher: drift, not CURRENT.
    state, detail = ch.installed_state(spec, *paths)
    assert state == ch.STALE and "this Manager generates" in detail

    ok, error, _actions = _install(spec, paths)
    assert (ok, error) == (True, "")
    entries = _entries(spec, paths)
    assert len(entries) == 1, "a matcher change appended instead of migrating"
    assert (entries[0].get("matcher") or "") == spec.matcher
    assert ch.installed_state(spec, *paths) == (ch.CURRENT, "")


@pytest.mark.parametrize("spec", SPECS, ids=_ids)
def test_the_shipped_double_entry_is_reported_and_repaired_to_one(spec, paths):
    """The exact state the read nudge left on a real machine."""
    old = dataclasses.replace(spec, matcher="Old")
    _install(old, paths)
    with open(paths[0], encoding="utf-8") as handle:
        settings = json.load(handle)
    current = ch.hook_entry(spec, sys.executable, paths[1])
    settings["hooks"][spec.event].append(current)
    with open(paths[0], "w", encoding="utf-8") as handle:
        json.dump(settings, handle)

    assert ch.installed_state(spec, *paths)[0] == ch.DUPLICATE
    assert _install(spec, paths)[0] is True
    assert len(_entries(spec, paths)) == 1
    assert ch.installed_state(spec, *paths) == (ch.CURRENT, "")


@pytest.mark.parametrize("spec", SPECS, ids=_ids)
def test_installing_repeatedly_never_accumulates_entries(spec, paths):
    for _ in range(3):
        assert _install(spec, paths)[0] is True
    assert len(_entries(spec, paths)) == 1


@pytest.mark.parametrize("spec", SPECS, ids=_ids)
def test_a_foreign_entry_on_the_same_event_is_never_claimed(spec, paths):
    foreign = {"matcher": spec.matcher or "Read",
               "hooks": [{"type": "command", "command": "theirs",
                          "args": ["C:/theirs/other.py"]}]}
    with open(paths[0], "w", encoding="utf-8") as handle:
        json.dump({"hooks": {spec.event: [foreign]}}, handle)
    assert ch.installed_state(spec, *paths)[0] == ch.ABSENT
    _install(spec, paths)
    assert foreign in _entries(spec, paths)
    assert len(_entries(spec, paths)) == 2
