"""tests/test_tokensave_config.py — strict_tree state reading.

`strict_tree` (tokensave v7.10.0, upstream #372 §2) decides whether a
wrong-tree query errors or returns a plausible-looking answer about a checkout
you are not in. The manager reports on it, so the thing under test is not just
"does it parse" but the honesty of each verdict — specifically that an
unreadable config never comes back looking like a disabled one.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers.tokensave_config import (
    DISABLED,
    ENABLED,
    MALFORMED,
    MISSING,
    UNREADABLE,
    config_path,
    read_strict_tree,
    should_recommend_enabling,
)


def _write_config(root, payload, *, raw_text=None):
    d = os.path.join(str(root), ".tokensave")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "config.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(raw_text if raw_text is not None else json.dumps(payload))
    return p


# ── the five states ───────────────────────────────────────────────────────────

def test_enabled(tmp_path):
    _write_config(tmp_path, {"strict_tree": True})
    state = read_strict_tree(str(tmp_path))
    assert state.verdict == ENABLED
    assert state.is_enabled and state.is_known and not state.is_defect


def test_disabled(tmp_path):
    _write_config(tmp_path, {"strict_tree": False})
    state = read_strict_tree(str(tmp_path))
    assert state.verdict == DISABLED
    assert not state.is_enabled
    assert state.is_known          # we know it — it is off
    assert not state.is_defect     # off is a legitimate upstream default


def test_key_absent_is_missing_not_disabled(tmp_path):
    """An old config predates the key; say so rather than paraphrasing it."""
    _write_config(tmp_path, {"version": 1})
    state = read_strict_tree(str(tmp_path))
    assert state.verdict == MISSING
    assert not state.is_enabled
    assert "older than v7.10.0" in state.detail


def test_invalid_json_is_malformed(tmp_path):
    _write_config(tmp_path, None, raw_text="{not json,,,")
    state = read_strict_tree(str(tmp_path))
    assert state.verdict == MALFORMED
    assert state.is_defect
    assert not state.is_known


def test_non_bool_value_is_malformed(tmp_path):
    """`"strict_tree": "true"` is a real mistake — a string is not the flag."""
    _write_config(tmp_path, {"strict_tree": "true"})
    state = read_strict_tree(str(tmp_path))
    assert state.verdict == MALFORMED
    assert not state.is_enabled


def test_json_that_is_not_an_object_is_malformed(tmp_path):
    _write_config(tmp_path, None, raw_text="[1, 2, 3]")
    assert read_strict_tree(str(tmp_path)).verdict == MALFORMED


def test_absent_config_file_is_unreadable(tmp_path):
    state = read_strict_tree(str(tmp_path))
    assert state.verdict == UNREADABLE
    assert not state.is_known


# ── the invariant the whole module exists for ─────────────────────────────────

@pytest.mark.parametrize("setup,raw", [
    ("missing_file", None),
    ("bad_json", "{{{"),
])
def test_unknown_never_reads_as_disabled(tmp_path, setup, raw):
    """An unreadable or unparseable config must not masquerade as "off".

    Reporting "strict_tree is disabled" when we simply could not read the file
    is exactly the class of confidently-wrong statement strict_tree itself
    exists to prevent.
    """
    if setup == "bad_json":
        _write_config(tmp_path, None, raw_text=raw)
    state = read_strict_tree(str(tmp_path))
    assert state.verdict != DISABLED
    assert not state.is_known


def test_never_raises_on_a_directory_in_place_of_the_config(tmp_path):
    """Doctor runs this mid-pass; one weird path must not kill the whole run."""
    os.makedirs(os.path.join(str(tmp_path), ".tokensave", "config.json"))
    state = read_strict_tree(str(tmp_path))
    assert state.verdict in (UNREADABLE, MALFORMED)


def test_config_path_points_at_the_project_not_the_home_dir(tmp_path):
    """The setting is per-project JSON, NOT the global ~/.tokensave TOML."""
    p = config_path(str(tmp_path))
    assert p.endswith(os.path.join(".tokensave", "config.json"))
    assert str(tmp_path) in p


# ── recommendation gating ─────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict_payload,expected", [
    ({"strict_tree": False}, True),    # off + risk -> recommend
    ({"version": 1},         True),    # missing + risk -> recommend
    ({"strict_tree": True},  False),   # already on -> nothing to say
])
def test_recommends_only_when_off_and_risk_present(tmp_path, verdict_payload,
                                                   expected):
    _write_config(tmp_path, verdict_payload)
    state = read_strict_tree(str(tmp_path))
    assert should_recommend_enabling(state, risk_present=True) is expected


def test_no_recommendation_without_demonstrated_risk(tmp_path):
    """The anti-nag rule: off alone is not a finding.

    Upstream ships strict_tree off deliberately, so a project with no
    wrong-tree exposure should produce no line at all — otherwise Doctor
    prints the same advice on every run of every project until the user
    stops reading it.
    """
    _write_config(tmp_path, {"strict_tree": False})
    state = read_strict_tree(str(tmp_path))
    assert should_recommend_enabling(state, risk_present=False) is False


def test_unknown_state_never_recommends(tmp_path):
    """Cannot read the setting -> cannot advise changing it."""
    state = read_strict_tree(str(tmp_path))       # no config file at all
    assert should_recommend_enabling(state, risk_present=True) is False
