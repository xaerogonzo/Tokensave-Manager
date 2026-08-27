"""tests/test_vscode_mcp_fixtures.py — Phase-A measurement records.

The VS Code MCP investigation records what each client actually does as
sanitised JSON fixtures rather than prose, for two reasons.

  * Duplicate-name arbitration and config precedence are exactly the sort of
    thing a client update changes silently. Held as fixtures, such a change
    fails a test; held as prose in a markdown file, it surprises a user
    eighteen months later.
  * The baseline shapes are the contract the schema adapter must round-trip,
    so the same file that records "this is what VS Code writes" is the file
    that pins "this is what we must not mangle".

The rules being pinned here:

  * a fixture never carries a machine path or a token — these are committed,
    and the whole point of `_canonical_project_entry` returning a template is
    that this repo's own paths do not leak into other people's checkouts;
  * the four states stay four. A fixture that collapses `configured`,
    `started`, `connected` and `serving_project` into one status has thrown
    away the distinction the subsystem exists to preserve;
  * a verdict field may not claim an answer the evidence does not support —
    `manager_action` stays `PENDING-BEHAVIOURAL` until a behavioural
    observation exists to justify it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vscode_mcp"

#: Absolute-path shapes that must never reach a committed fixture. The Phase-C
#: acceptance test applies the same patterns to generated config files.
_MACHINE_PATH_MARKERS = (
    "c:/users", "d:/", "e:/", "/home/", "/users/",
)

#: The four states, recorded separately. See docs/vscode-mcp-matrix.md.
_REQUIRED_LAYERS = ("configured", "started", "connected", "serving_project")

_ACTION_VOCABULARY = frozenset({
    "managed", "detect-only", "unsupported", "no action required",
    "PENDING-BEHAVIOURAL",
})

_EVIDENCE_TAGS = frozenset({"config", "process", "behavioural"})


def _fixtures() -> list:
    return sorted(p for p in _FIXTURE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_dir_is_populated():
    """A silently-empty glob would make every test below vacuously pass."""
    assert _fixtures(), f"no fixtures found in {_FIXTURE_DIR}"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_is_valid_json_and_versioned(path: Path):
    data = _load(path)
    assert data.get("fixture_version") == 1, "unversioned fixture"
    assert data.get("experiment"), "fixture does not name its experiment"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_carries_no_machine_paths(path: Path):
    """Committed fixtures use <HOME> / <APPDATA> / <PROJECT> placeholders.

    Checked over the raw text rather than parsed values so a path buried in a
    nested note or an evidence string is caught too.
    """
    raw = path.read_text(encoding="utf-8").lower()
    # Normalise separators so one slash-only marker catches both spellings;
    # JSON escapes a literal backslash, so the doubled form appears too.
    haystack = raw.replace(chr(92) + chr(92), "/").replace(chr(92), "/")
    found = [m for m in _MACHINE_PATH_MARKERS if m in haystack]
    assert not found, f"{path.name} leaks machine path(s): {found}"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_experiment_fixtures_keep_the_four_states_separate(path: Path):
    """`layers` is all four states or the fixture is not an experiment record.

    Baseline fixtures describe on-disk shapes rather than a client's behaviour,
    so they legitimately have no `layers` block; anything claiming an A0.x
    experiment must have one.
    """
    data = _load(path)
    if not str(data["experiment"]).startswith("A0"):
        return
    layers = data.get("layers")
    assert isinstance(layers, dict), "experiment fixture has no `layers` block"
    assert set(layers) == set(_REQUIRED_LAYERS), (
        f"expected exactly {_REQUIRED_LAYERS}, got {sorted(layers)}")
    for name, layer in layers.items():
        assert layer.get("status") in ("measured", "pending"), (
            f"layer {name!r} has no measured/pending status")


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_verdict_is_not_claimed_ahead_of_the_evidence(path: Path):
    """A concluded `manager_action` requires a behavioural observation.

    This is the guard against the failure this whole investigation exists to
    correct: a confident verdict derived purely from reading config files.
    """
    data = _load(path)
    action = data.get("manager_action")
    if action is None:
        return
    assert action in _ACTION_VOCABULARY, f"unknown action {action!r}"
    if action == "PENDING-BEHAVIOURAL":
        return
    tags = {e.get("tag") for e in data.get("evidence", [])}
    assert "behavioural" in tags, (
        f"{path.name} concludes {action!r} with only {sorted(tags)} evidence")


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_evidence_entries_are_tagged_and_sourced(path: Path):
    data = _load(path)
    for entry in data.get("evidence", []):
        assert entry.get("tag") in _EVIDENCE_TAGS, (
            f"evidence tag {entry.get('tag')!r} not in {sorted(_EVIDENCE_TAGS)}")
        assert entry.get("claim"), "evidence entry has no claim"
        assert entry.get("method"), "evidence entry does not say how it was measured"


def test_baseline_records_the_schema_split_the_adapter_must_handle():
    """Only the VS Code-native surface uses `servers`; the others use `mcpServers`.

    Pinned because it is the single fact that decides how much adapter code
    Phase C needs: two of the three surfaces are already in the shape the
    existing classifier reads.
    """
    data = _load(_FIXTURE_DIR / "observed_configs_baseline.json")
    by_key: dict = {}
    for surface in data["surfaces"]:
        by_key.setdefault(surface["schema_key"], []).append(surface["name"])
        # Whatever the key, the entry must round-trip intact.
        assert surface["schema_key"] in surface["shape"]

    assert by_key["servers"] == ["VS Code user MCP"]
    assert len(by_key["mcpServers"]) == 2


def test_baseline_pins_the_inputs_sibling_the_adapter_must_preserve():
    """`inputs` sits beside `servers` and is not ours to drop."""
    data = _load(_FIXTURE_DIR / "observed_configs_baseline.json")
    vscode = next(s for s in data["surfaces"] if s["schema_key"] == "servers")
    assert "inputs" in vscode["shape"], (
        "the VS Code shape lost its `inputs` sibling — a read/write "
        "round-trip that drops it would corrupt the user's file")
