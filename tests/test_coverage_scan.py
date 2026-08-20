"""tests/test_coverage_scan.py — real coverage numbers, and their provenance.

Tab 2's old filename heuristic reported ✓ for any file with a matching
test_*.py, including one whose test asserts nothing. Replacing an unearned
green tick with a number only helps if the number is honest about two things:

  * an unmeasured file is NOT 0% — "not measured" and "measured, nothing
    covered" are different facts;
  * a percentage without a date reads as current however stale it is, so
    provenance travels with the numbers.
"""
from __future__ import annotations

import json
import time

import pytest

from helpers.coverage_scan import (
    WARN_BELOW_PCT,
    CoverageMeta,
    CoverageResult,
    format_cell,
    load_coverage,
    needs_attention,
    parse_coverage_json,
    save_coverage,
)


def _report(files):
    return json.dumps({
        "files": {p: {"summary": {"percent_covered": v}}
                  for p, v in files.items()}})


# ── parsing coverage json ─────────────────────────────────────────────────────

def test_parses_per_file_percentages():
    out = parse_coverage_json(_report({"src/helpers/git.py": 87.5}))
    assert out == {"src/helpers/git.py": 87.5}


def test_normalises_windows_separators():
    """The report can carry backslashes; Tab 2 keys on forward slashes."""
    winpath = "src" + chr(92) + "helpers" + chr(92) + "git.py"
    out = parse_coverage_json(_report({winpath: 50}))
    assert "src/helpers/git.py" in out


@pytest.mark.parametrize("text", [
    "", "not json", "[]", '{"no_files_key": 1}',
    '{"files": "not a dict"}',
])
def test_malformed_reports_degrade_to_empty_rather_than_raising(text):
    """A bad report should drop Tab 2 back to the heuristic, not break it."""
    assert parse_coverage_json(text) == {}


def test_entries_without_a_percentage_are_skipped():
    raw = json.dumps({"files": {
        "a.py": {"summary": {"percent_covered": 10}},
        "b.py": {"summary": {}},
        "c.py": "not a dict",
    }})
    assert parse_coverage_json(raw) == {"a.py": 10.0}


# ── unmeasured is not zero ────────────────────────────────────────────────────

def test_unmeasured_file_returns_none_not_zero():
    res = CoverageResult(percents={"src/a.py": 10.0})
    assert res.pct_for("src/a.py") == 10.0
    assert res.pct_for("src/never_measured.py") is None


def test_zero_percent_is_distinguishable_from_unmeasured():
    res = CoverageResult(percents={"src/a.py": 0.0})
    assert res.pct_for("src/a.py") == 0.0
    assert res.pct_for("src/a.py") is not None


def test_cell_for_unmeasured_does_not_claim_a_number():
    assert "no data" in format_cell(None, has_tests=False)
    assert "?" in format_cell(None, has_tests=True)


def test_cell_marks_low_coverage_and_leaves_high_alone():
    assert format_cell(12.0, True).startswith("⚠")
    assert format_cell(93.0, True).startswith("✓")


def test_needs_attention_only_for_measured_low_values():
    assert needs_attention(10.0) is True
    assert needs_attention(99.0) is False
    assert needs_attention(None) is False, "unmeasured is not a warning"


def test_warning_threshold_is_not_the_ci_gate():
    """CI enforces --cov-fail-under=14; this is a UX signal at 50.

    Pinned so the two cannot quietly converge — someone "fixing" one to
    match the other would either flood Tab 2 or gut the gate.
    """
    assert WARN_BELOW_PCT == 50.0
    assert WARN_BELOW_PCT != 14


# ── provenance ────────────────────────────────────────────────────────────────

def test_round_trip_preserves_numbers_and_metadata(tmp_path):
    meta = CoverageMeta(generated_at=time.time(), branch="Roadmap-9",
                        commit_sha="abc1234", project_root=str(tmp_path),
                        command="pytest --cov=src")
    save_coverage(str(tmp_path), {"src/a.py": 42.0}, meta)
    loaded = load_coverage(str(tmp_path))
    assert loaded.pct_for("src/a.py") == 42.0
    assert loaded.meta.branch == "Roadmap-9"
    assert loaded.meta.commit_sha == "abc1234"
    assert loaded.meta.command == "pytest --cov=src"


def test_missing_cache_is_empty_and_falsy(tmp_path):
    res = load_coverage(str(tmp_path))
    assert not res
    assert res.percents == {}


def test_corrupt_cache_degrades_quietly(tmp_path):
    d = tmp_path / ".tokensave-manager"
    d.mkdir()
    (d / "last_coverage.json").write_text("{{{ not json", encoding="utf-8")
    assert not load_coverage(str(tmp_path))


def test_cache_without_metadata_still_loads_but_reports_unknown_age(tmp_path):
    """A pre-metadata cache must not be presented as freshly generated."""
    d = tmp_path / ".tokensave-manager"
    d.mkdir()
    (d / "last_coverage.json").write_text(
        json.dumps({"percents": {"src/a.py": 5}}), encoding="utf-8")
    res = load_coverage(str(tmp_path))
    assert res.pct_for("src/a.py") == 5.0
    assert res.meta.age_label() == "unknown age"


@pytest.mark.parametrize("age,expected", [
    (10, "just now"),
    (600, "10 min ago"),
    (7200, "2h ago"),
    (172800, "2d ago"),
])
def test_age_label_is_human(age, expected):
    meta = CoverageMeta(generated_at=time.time() - age)
    assert meta.age_label() == expected


def test_numbers_from_another_branch_are_flagged_as_mismatched():
    """The reason branch travels with the numbers: coverage measured on a
    different branch is not this branch's coverage."""
    meta = CoverageMeta(branch="master", commit_sha="aaa")
    assert meta.matches("master", "aaa") is True
    assert meta.matches("Roadmap-9", "aaa") is False
    assert meta.matches("master", "bbb") is False


def test_unknown_provenance_does_not_claim_a_mismatch():
    """An old cache with no branch recorded should not be rejected outright."""
    assert CoverageMeta().matches("Roadmap-9", "abc") is True
