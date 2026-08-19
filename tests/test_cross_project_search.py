"""tests/test_cross_project_search.py — merging results from several projects.

The correctness risk here is not crashing, it is producing a ranking that
looks authoritative and is not. BM25 scores are per-database: measured on two
real projects, equally good matches scored 19.3 in one and 11.2 in the other.
Sorting the union by score would put one project's hits on top and read as
"that project is more relevant" — a claim the numbers cannot support.

So the tests below mostly pin the merge order, and the distinction between
"that project had no matches" and "that project could not be searched".
"""
from __future__ import annotations

import json

import pytest

from helpers.cross_project_search import (
    Hit,
    format_hit_line,
    interleave,
    parse_search_output,
    search_projects,
)

A = "/proj/alpha"
B = "/proj/beta"


def _payload(*names, score=1.0):
    return json.dumps([
        {"id": f"function:{n}", "name": n, "kind": "function",
         "file": f"src/{n}.py", "line": i + 1,
         "signature": f"def {n}()", "score": score}
        for i, n in enumerate(names)
    ])


def _runner(mapping):
    """Injectable runner: {path: payload} or {path: Exception-ish error str}."""
    def run(exe, path, query, limit):
        val = mapping.get(path, "[]")
        if isinstance(val, tuple):
            return val
        return val, ""
    return run


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parses_hits_and_tags_them_with_their_project():
    hits = parse_search_output(_payload("foo", "bar"), "/proj/alpha")
    assert [h.name for h in hits] == ["foo", "bar"]
    assert all(h.project == "alpha" for h in hits)
    assert all(h.project_path == "/proj/alpha" for h in hits)


def test_rank_is_position_within_its_own_project():
    hits = parse_search_output(_payload("a", "b", "c"), A)
    assert [h.rank for h in hits] == [0, 1, 2]


def test_backslash_paths_are_normalised():
    raw = json.dumps([{"name": "x", "kind": "function",
                       "file": "src\\helpers\\x.py", "line": 3}])
    assert parse_search_output(raw, A)[0].file == "src/helpers/x.py"


@pytest.mark.parametrize("text", ["", "not json", "{}", '"a string"', "null"])
def test_unparseable_output_yields_no_hits_rather_than_raising(text):
    """One project with a broken index must not sink a five-project search."""
    assert parse_search_output(text, A) == []


def test_rows_without_a_name_are_skipped():
    raw = json.dumps([{"kind": "function", "file": "a.py", "line": 1},
                      {"name": "real", "kind": "function",
                       "file": "b.py", "line": 2}])
    assert [h.name for h in parse_search_output(raw, A)] == ["real"]


def test_a_non_numeric_line_degrades_to_zero():
    raw = json.dumps([{"name": "x", "file": "a.py", "line": "not a number"}])
    assert parse_search_output(raw, A)[0].line == 0


# ── the merge rule ────────────────────────────────────────────────────────────

def test_results_interleave_by_rank_round_robin():
    a = parse_search_output(_payload("a1", "a2", "a3"), A)
    b = parse_search_output(_payload("b1", "b2", "b3"), B)
    merged = interleave({A: a, B: b})
    assert [h.name for h in merged] == ["a1", "b1", "a2", "b2", "a3", "b3"]


def test_merge_ignores_score_entirely():
    """The whole point: a project scoring higher does not get to dominate.

    Alpha's hits score 2.0, Beta's 90.0. A score sort would put all of Beta
    first. Rank interleaving keeps each project's best hit at the top.
    """
    a = parse_search_output(_payload("a1", "a2", score=2.0), A)
    b = parse_search_output(_payload("b1", "b2", score=90.0), B)
    merged = interleave({A: a, B: b})
    assert [h.name for h in merged] == ["a1", "b1", "a2", "b2"]
    assert merged[0].project == "alpha", \
        "a higher-scoring project must not displace another project's best hit"


def test_uneven_result_counts_do_not_lose_the_tail():
    a = parse_search_output(_payload("a1"), A)
    b = parse_search_output(_payload("b1", "b2", "b3"), B)
    merged = interleave({A: a, B: b})
    assert [h.name for h in merged] == ["a1", "b1", "b2", "b3"]


def test_per_project_cap_stops_one_project_crowding_out_the_rest():
    a = parse_search_output(_payload(*[f"a{i}" for i in range(50)]), A)
    b = parse_search_output(_payload("b1"), B)
    merged = interleave({A: a, B: b}, cap=3)
    assert len([h for h in merged if h.project == "alpha"]) == 3
    assert "b1" in [h.name for h in merged]


def test_empty_projects_are_dropped_from_the_rotation():
    a = parse_search_output(_payload("a1", "a2"), A)
    merged = interleave({A: a, B: []})
    assert [h.name for h in merged] == ["a1", "a2"]


def test_interleaving_nothing_is_empty():
    assert interleave({}) == []
    assert interleave({A: [], B: []}) == []


# ── end to end ────────────────────────────────────────────────────────────────

def test_searches_every_project_and_merges():
    hits, failures = search_projects(
        "ts.exe", [A, B], "thing",
        runner=_runner({A: _payload("a1", "a2"), B: _payload("b1")}))
    assert [h.name for h in hits] == ["a1", "b1", "a2"]
    assert failures == []


def test_a_failing_project_is_reported_not_silently_empty():
    """"No matches there" and "could not search there" are different answers.

    Collapsing them lets a broken index masquerade as a clean miss.
    """
    hits, failures = search_projects(
        "ts.exe", [A, B], "thing",
        runner=_runner({A: _payload("a1"),
                        B: ("", "no tokensave index at /proj/beta")}))
    assert [h.name for h in hits] == ["a1"]
    assert failures == [("beta", "no tokensave index at /proj/beta")]


def test_a_project_with_no_matches_is_not_a_failure():
    hits, failures = search_projects(
        "ts.exe", [A, B], "thing",
        runner=_runner({A: _payload("a1"), B: "[]"}))
    assert [h.name for h in hits] == ["a1"]
    assert failures == []


def test_blank_query_searches_nothing():
    calls = []

    def spy(exe, path, query, limit):
        calls.append(path)
        return "[]", ""

    hits, failures = search_projects("ts.exe", [A, B], "   ", runner=spy)
    assert hits == [] and failures == []
    assert calls == [], "a blank query must not spawn a single process"


def test_the_query_and_project_reach_the_runner():
    seen = []

    def spy(exe, path, query, limit):
        seen.append((path, query, limit))
        return "[]", ""

    search_projects("ts.exe", [A, B], "needle", limit=7, runner=spy)
    assert seen == [(A, "needle", 7), (B, "needle", 7)]


# ── rendering ─────────────────────────────────────────────────────────────────

def test_rendered_row_leads_with_the_project():
    """The project is the new information; the symbol is not."""
    hit = parse_search_output(_payload("foo"), A)[0]
    line = format_hit_line(hit)
    assert line.startswith("alpha")
    assert "foo" in line and "src/foo.py:1" in line


def test_location_omits_a_missing_line_number():
    hit = Hit(project="p", project_path=A, name="n", kind="", file="a.py",
              line=0, signature="", rank=0)
    assert hit.location == "a.py"
