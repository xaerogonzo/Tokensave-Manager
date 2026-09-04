"""tests/test_graph_trust.py — the states, and the direction.

Two properties carry this module and neither is obvious from reading it.

**Population is a state, not a footnote.** An index that could not be read
and an index with nothing in it both produce zero findings, and neither is a
clean bill of health. `memory/verification_oracle.md` records that the last
verification audit here found its real defect in the population a scan
reached, not in the predicate it applied — so `unknown` and `insufficient`
are asserted as distinct from `trustworthy`, not merely as "not tainted".

**The predicate is one-way.** A test calling the code under test is what a
test is; only production calling *into* the test tree is impossible. An
inverted predicate would flag every test file in the repository and still
pass a test that only checked "tainted when there are cross-tree edges" —
so the `tests/` to `src/` direction gets an explicit negative control.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from helpers.graph_trust import (
    MIN_MEANINGFUL_EDGES,
    STATE_INSUFFICIENT,
    STATE_TAINTED,
    STATE_TRUSTWORTHY,
    STATE_UNKNOWN,
    Collision,
    GraphTrust,
    db_path_for,
    inspect_graph,
)


# ── Fixture builder ──────────────────────────────────────────────────────

def _make_index(root, edges, *, node_rows=None, schema="full"):
    """Write a minimal tokensave-shaped index under ``root/.tokensave``.

    ``edges`` is a list of ``(src_file, dst_file, dst_name)``; each entry
    creates two nodes and one edge between them. Node ids are unique per
    edge so ``source <> target`` holds unless a test asks otherwise.
    """
    ts = os.path.join(str(root), ".tokensave")
    os.makedirs(ts, exist_ok=True)
    db = os.path.join(ts, "tokensave.db")
    conn = sqlite3.connect(db)
    if schema == "no_edges_table":
        conn.execute("CREATE TABLE nodes (id TEXT, file_path TEXT, name TEXT)")
        conn.commit()
        conn.close()
        return db
    if schema == "edges_missing_kind":
        conn.execute("CREATE TABLE nodes (id TEXT, file_path TEXT, name TEXT)")
        conn.execute("CREATE TABLE edges (source TEXT, target TEXT)")
        conn.commit()
        conn.close()
        return db

    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, "
                 "name TEXT, file_path TEXT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, "
                 "target TEXT, kind TEXT, line INTEGER)")
    seen = {}

    def node(file_path, name, forced_id=None):
        key = forced_id or f"{file_path}::{name}::{len(seen)}"
        if key not in seen:
            conn.execute("INSERT INTO nodes VALUES (?,?,?,?)",
                         (key, "function", name, file_path))
            seen[key] = True
        return key

    for i, (src_file, dst_file, dst_name) in enumerate(edges):
        sid = node(src_file, f"caller_{i}")
        did = node(dst_file, dst_name)
        conn.execute("INSERT INTO edges (source, target, kind, line) "
                     "VALUES (?,?,?,?)", (sid, did, "calls", i + 1))
    for row in (node_rows or []):
        node(*row)
    conn.commit()
    conn.close()
    return db


def _padding(n, *, src="src/pad.py", dst="src/other.py"):
    """``n`` unremarkable production-to-production edges."""
    return [(src, dst, f"ok_{i}") for i in range(n)]


# ── unknown: we could not look ───────────────────────────────────────────

def test_no_index_is_unknown_not_trustworthy(tmp_path):
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_UNKNOWN
    assert not r.is_conclusive
    assert "no tokensave index" in r.detail


def test_missing_table_is_unknown(tmp_path):
    _make_index(tmp_path, [], schema="no_edges_table")
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_UNKNOWN
    assert "edges" in r.detail


def test_missing_column_is_unknown(tmp_path):
    """A schema that drifts must not read as zero findings."""
    _make_index(tmp_path, [], schema="edges_missing_kind")
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_UNKNOWN
    assert "kind" in r.detail


# ── insufficient: we looked, there was nothing to see ────────────────────

def test_empty_index_is_insufficient_not_trustworthy(tmp_path):
    """The property this module exists for: zero examined is not clean."""
    _make_index(tmp_path, [])
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_INSUFFICIENT
    assert r.state != STATE_TRUSTWORTHY
    assert not r.is_conclusive
    assert r.edges_examined == 0


def test_zero_population_is_insufficient_even_with_no_floor(tmp_path):
    """The floor must not be the only thing between empty and "clean".

    Found by mutation: deleting the zero-population guard left every other
    test in this file green, because `examined < min_edges` was quietly
    covering for it. With the floor disabled that cover is gone, and zero
    examined edges must STILL not read as trustworthy.
    """
    _make_index(tmp_path, [])
    r = inspect_graph(str(tmp_path), min_edges=0)
    assert r.state == STATE_INSUFFICIENT
    assert r.state != STATE_TRUSTWORTHY


def test_below_floor_is_insufficient(tmp_path):
    _make_index(tmp_path, _padding(MIN_MEANINGFUL_EDGES - 1))
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_INSUFFICIENT
    assert r.edges_examined == MIN_MEANINGFUL_EDGES - 1


def test_floor_is_a_parameter(tmp_path):
    """Small projects are not condemned when the caller says so."""
    _make_index(tmp_path, _padding(3))
    assert inspect_graph(str(tmp_path), min_edges=1).state == STATE_TRUSTWORTHY


# ── trustworthy, and what it must report ─────────────────────────────────

def test_clean_graph_reports_the_population_it_examined(tmp_path):
    _make_index(tmp_path, _padding(MIN_MEANINGFUL_EDGES))
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_TRUSTWORTHY
    assert r.impossible_edges == 0
    # The number is the whole point: an empty finding list means nothing
    # without it.
    assert r.edges_examined == MIN_MEANINGFUL_EDGES
    assert str(MIN_MEANINGFUL_EDGES) in r.summary()


# ── tainted, and the direction ───────────────────────────────────────────

def test_production_into_test_tree_is_flagged(tmp_path):
    edges = _padding(MIN_MEANINGFUL_EDGES) + [
        ("src/agent.py", "tests/test_llm.py", "info"),
        ("src/app.py", "tests/test_llm.py", "info"),
        ("src/theme.py", "tests/test_debug_drive.py", "after"),
    ]
    _make_index(tmp_path, edges)
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_TAINTED
    assert r.impossible_edges == 3
    assert r.source_files_affected == 3
    assert r.is_tainted and r.is_conclusive


def test_test_into_production_is_NOT_flagged(tmp_path):
    """Negative control. This is what a test suite legitimately looks like.

    An inverted predicate passes every other assertion in this file and
    fails only here.
    """
    edges = _padding(MIN_MEANINGFUL_EDGES) + [
        ("tests/test_llm.py", "src/helpers/llm.py", "dispatch_llm"),
        ("tests/test_mcp.py", "src/helpers/mcp.py", "effective_scope"),
    ]
    _make_index(tmp_path, edges)
    r = inspect_graph(str(tmp_path))
    assert r.state == STATE_TRUSTWORTHY
    assert r.impossible_edges == 0


def test_test_to_test_is_NOT_flagged(tmp_path):
    edges = _padding(MIN_MEANINGFUL_EDGES) + [
        ("tests/conftest.py", "tests/test_llm.py", "info"),
    ]
    _make_index(tmp_path, edges)
    assert inspect_graph(str(tmp_path)).state == STATE_TRUSTWORTHY


def test_self_edges_are_excluded(tmp_path):
    """Phantom recursive self-edges are a different defect, not this one."""
    _make_index(tmp_path, _padding(MIN_MEANINGFUL_EDGES))
    db = os.path.join(str(tmp_path), ".tokensave", "tokensave.db")
    conn = sqlite3.connect(db)
    nid = conn.execute("SELECT id FROM nodes WHERE file_path LIKE 'tests/%' "
                       "OR file_path LIKE 'src/%' LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO edges (source, target, kind, line) "
                 "VALUES (?,?,?,?)", (nid, nid, "calls", 1))
    conn.commit()
    conn.close()
    r = inspect_graph(str(tmp_path))
    # The self-edge is neither counted as impossible nor as examined.
    assert r.impossible_edges == 0
    assert r.edges_examined == MIN_MEANINGFUL_EDGES


# ── the collision sample ─────────────────────────────────────────────────

def test_collisions_ranked_by_count_and_capped(tmp_path):
    edges = _padding(MIN_MEANINGFUL_EDGES)
    edges += [("src/a.py", "tests/t.py", "after")] * 5
    edges += [("src/b.py", "tests/t.py", "info")] * 2
    for i in range(30):
        edges.append((f"src/f{i}.py", "tests/t.py", f"name_{i}"))
    _make_index(tmp_path, edges)
    r = inspect_graph(str(tmp_path), max_collisions=3)
    assert len(r.collisions) == 3
    assert r.collisions[0] == Collision("after", "tests/t.py", 5)
    assert r.collisions[1] == Collision("info", "tests/t.py", 2)
    # capping the sample must not cap the count
    assert r.impossible_edges == 5 + 2 + 30


def test_summary_never_omits_the_population():
    """Every conclusive verdict states what it looked at."""
    for state in (STATE_TRUSTWORTHY, STATE_TAINTED):
        r = GraphTrust(state, edges_examined=1234, impossible_edges=7,
                       source_files_affected=3)
        assert "1234" in r.summary()


# ── locating the index ───────────────────────────────────────────────────

def test_db_path_prefers_branch_meta(tmp_path):
    ts = tmp_path / ".tokensave"
    ts.mkdir()
    (ts / "tokensave.db").write_text("")
    (ts / "branch-x.db").write_text("")
    (ts / "branch-meta.json").write_text('{"db_file": "branch-x.db"}')
    assert db_path_for(str(tmp_path)).endswith("branch-x.db")


def test_db_path_ignores_branch_meta_pointing_at_nothing(tmp_path):
    """A stale pointer is not evidence about this branch."""
    ts = tmp_path / ".tokensave"
    ts.mkdir()
    (ts / "tokensave.db").write_text("")
    (ts / "branch-meta.json").write_text('{"db_file": "gone.db"}')
    assert db_path_for(str(tmp_path)).endswith("tokensave.db")


def test_db_path_survives_corrupt_branch_meta(tmp_path):
    ts = tmp_path / ".tokensave"
    ts.mkdir()
    (ts / "tokensave.db").write_text("")
    (ts / "branch-meta.json").write_text("{not json")
    assert db_path_for(str(tmp_path)).endswith("tokensave.db")


def test_db_path_empty_when_nothing_there(tmp_path):
    assert db_path_for(str(tmp_path)) == ""


# ── against this repository ──────────────────────────────────────────────

def test_this_repo_is_measured_not_assumed():
    """Assert the shape of the answer, never the day's number.

    557/91 was the figure when this was written and 559/93 an hour later,
    because the index re-syncs. Pinning it would make an honest re-sync look
    like a regression.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = inspect_graph(root)
    if r.state == STATE_UNKNOWN:
        pytest.skip("no tokensave index in this checkout")
    assert r.edges_examined > 0
    if r.is_tainted:
        assert r.impossible_edges > 0
        assert r.source_files_affected > 0
        assert r.collisions
        assert all(c.target_file.startswith("tests/") for c in r.collisions)


# ── the Doctor surface ───────────────────────────────────────────────────

def test_doctor_is_silent_for_a_non_tokensave_project(tmp_path):
    """Every project would otherwise gain a permanent line about tokensave."""
    from helpers.doctor_rules import audit_graph_trust
    assert audit_graph_trust(str(tmp_path)) == []


def test_doctor_is_silent_when_the_graph_is_sound(tmp_path):
    from helpers.doctor_rules import audit_graph_trust
    _make_index(tmp_path, _padding(MIN_MEANINGFUL_EDGES))
    assert audit_graph_trust(str(tmp_path)) == []


def test_doctor_reports_population_alongside_findings(tmp_path):
    from helpers.doctor_rules import audit_graph_trust
    edges = _padding(MIN_MEANINGFUL_EDGES) + [
        ("src/a.py", "tests/t.py", "after"),
    ]
    _make_index(tmp_path, edges)
    notes = audit_graph_trust(str(tmp_path))
    assert notes
    joined = " ".join(notes)
    assert "1 call edge" in joined
    # the population is not optional garnish: 50 padding edges + the 1 bad one
    assert f"Examined {MIN_MEANINGFUL_EDGES + 1} edge(s)" in joined
    assert "quality_signal" in joined


def test_doctor_speaks_up_when_it_could_not_look(tmp_path):
    """`unknown` must not share the silent path with `sound`.

    Silence is reserved for "there is no index here". An index that exists
    but cannot be read is a fact worth a line, because the sub-scores
    derived from it are being reported to someone regardless.
    """
    from helpers.doctor_rules import audit_graph_trust
    _make_index(tmp_path, [], schema="edges_missing_kind")
    notes = audit_graph_trust(str(tmp_path))
    assert notes and "could not be established" in notes[0]


def test_doctor_reports_an_inconclusive_scan(tmp_path):
    from helpers.doctor_rules import audit_graph_trust
    _make_index(tmp_path, _padding(2))
    notes = audit_graph_trust(str(tmp_path))
    assert notes and "inconclusive" in notes[0]


def test_graph_trust_is_not_on_any_gating_path():
    """Warn-only, enforced structurally rather than by hoping.

    The pre-push hook and the generated CI step both call
    `_audit_project_tree`; neither may reach the graph-trust audit. This is
    an index defect, not a source defect — blocking a push on it would
    punish a developer for a bug in their indexer.
    """
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("src/prepush_runner.py", "src/helpers/ci_workflow.py",
                "src/precommit_review.py"):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        text = io.open(path, encoding="utf-8").read()
        assert "audit_graph_trust" not in text, (
            f"{rel} reaches the graph-trust audit; it must stay warn-only")
        assert "graph_trust" not in text, (
            f"{rel} imports graph_trust; it must stay warn-only")


# ── the CLI surface ──────────────────────────────────────────────────────

def _run_cli(capsys, argv):
    from cli import main
    import json as _json
    code = main(argv)
    out, err = capsys.readouterr()
    return code, (_json.loads(out) if out.strip() else None), err


def test_cli_does_not_gate_on_a_tainted_graph(capsys, tmp_path):
    """A build must not fail over a defect in someone else's indexer.

    `EXIT_OK` with the state in the payload, so a caller that *wants* to act
    on it can, and one that does not is not broken by it.
    """
    from cli import EXIT_OK
    edges = _padding(MIN_MEANINGFUL_EDGES) + [("src/a.py", "tests/t.py", "after")]
    _make_index(tmp_path, edges)
    code, env, _ = _run_cli(capsys, ["graph-trust", "--project", str(tmp_path)])
    assert code == EXIT_OK
    assert env["ok"] is True
    assert env["data"]["state"] == STATE_TAINTED
    assert env["data"]["impossible_edges"] == 1


def test_cli_names_what_it_quarantines(capsys, tmp_path):
    """The aggregate is listed too, not just its component.

    quality_signal is the geometric mean over all six dimensions, so a
    consumer told only that `acyclicity` is suspect would keep trending the
    aggregate that contains it.
    """
    edges = _padding(MIN_MEANINGFUL_EDGES) + [("src/a.py", "tests/t.py", "after")]
    _make_index(tmp_path, edges)
    _, env, _ = _run_cli(capsys, ["graph-trust", "--project", str(tmp_path)])
    assert env["data"]["quarantined_metrics"] == ["acyclicity", "quality_signal"]


def test_cli_quarantines_nothing_when_the_graph_is_sound(capsys, tmp_path):
    _make_index(tmp_path, _padding(MIN_MEANINGFUL_EDGES))
    _, env, _ = _run_cli(capsys, ["graph-trust", "--project", str(tmp_path)])
    assert env["data"]["state"] == STATE_TRUSTWORTHY
    assert env["data"]["quarantined_metrics"] == []


def test_cli_reports_verify_failed_when_it_could_not_look(capsys, tmp_path):
    """"We could not find out" must not be readable as "it is fine"."""
    from cli import EXIT_VERIFY_FAILED
    code, env, _ = _run_cli(capsys, ["graph-trust", "--project", str(tmp_path)])
    assert code == EXIT_VERIFY_FAILED
    assert env["ok"] is False
    assert env["data"]["state"] == STATE_UNKNOWN


def test_cli_emits_no_findings_because_nothing_is_wrong_at_that_line(capsys, tmp_path):
    """A phantom edge is not a defect in the file it points at.

    The only line one could be anchored to is the test double's definition,
    which is correct code. Emitting it as a diagnostic would put a warning
    on a file with nothing wrong with it — the same reason `test-run` stays
    out of the extension's DIAGNOSTIC_COMMANDS. The data travels in the
    payload instead.
    """
    edges = _padding(MIN_MEANINGFUL_EDGES)
    for i in range(40):
        edges.append((f"src/f{i}.py", "tests/t.py", f"name_{i}"))
    _make_index(tmp_path, edges)
    _, env, _ = _run_cli(capsys, ["graph-trust", "--project", str(tmp_path)])
    assert env["findings"] == []
    # ...but the sample is still capped, and the count is not
    assert len(env["data"]["collisions"]) == 20      # MAX_COLLISIONS
    assert env["data"]["impossible_edges"] == 40
