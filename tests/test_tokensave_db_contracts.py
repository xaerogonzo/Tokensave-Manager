"""tests/test_tokensave_db_contracts.py — the three direct readers of tokensave.db.

Three helpers bypass the tokensave CLI and run their own SQL against
``.tokensave/tokensave.db``:

    helpers/doc_grounding.py   _count_tokensave_files   (codegraph divergence)
    helpers/shadow_links.py    indexed_extensions       (unindexed-ext scanner)
    helpers/refactor_scout.py  run_scout                (complexity/dead code)

That makes them exposed to tokensave's schema, which moves: v16 added the
``files.kind`` column, v17 added ``ambiguous_calls``. All three are also
fail-open, which is the dangerous part — a schema change does not raise, it
quietly produces 0 / None / nothing, and the surfaces above go silent while
still looking healthy. That exact failure shipped once: the grounding count
parsed ``tokensave status`` output, silently returned 0 for months once the
output became an ANSI table, and no test noticed (fixed in fce4842).

So these assert each reader's OWN CONTRACT, not merely "did not raise" and not
"returned non-zero" — non-zero is a second bad oracle, since zero shadowable
extensions is a legitimate answer for a healthy project.

The contract tests run against the REAL index, because the point is
compatibility with the schema tokensave actually writes; a synthetic fixture DB
would only test the fixture. They skip where no index exists (CI checks out a
repo with .tokensave/ gitignored). The edge-case tests below them use synthetic
databases and always run.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from helpers.doc_grounding import _count_tokensave_files
from helpers.refactor_scout import run_scout
from helpers.shadow_links import indexed_extensions

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB = os.path.join(_REPO, ".tokensave", "tokensave.db")

_needs_index = pytest.mark.skipif(
    not os.path.isfile(_DB),
    reason="no real tokensave index here (expected on CI — .tokensave/ is "
           "gitignored); these verify live-schema compatibility")


def _count_source_files_on_disk() -> int:
    """Filesystem upper bound: every file that could plausibly be indexed."""
    skip = {".git", ".tokensave", ".tokensave-manager", "__pycache__",
            ".codegraph", "dist", "logs", ".pytest_cache", ".venv", "venv"}
    n = 0
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        n += len(files)
    return n


# ── contracts, against the live index ─────────────────────────────────────────

@_needs_index
def test_grounding_count_is_bounded_by_the_filesystem():
    """An int in [0, files-on-disk].

    The upper bound is the half that catches schema drift: a wrong column or
    a changed table can still return a plausible small integer, but a count
    exceeding what exists on disk means the query is counting the wrong rows.
    """
    count = _count_tokensave_files(_REPO)
    assert isinstance(count, int)
    assert count >= 0
    assert count <= _count_source_files_on_disk(), (
        f"indexed code-file count {count} exceeds the number of files that "
        f"exist on disk — the query is counting the wrong thing")


@_needs_index
def test_grounding_count_is_actually_live():
    """Guards the specific regression in fce4842: silently returning 0.

    This repo unambiguously has indexed code, so 0 here means the reader has
    stopped working, not that the project is empty.
    """
    assert _count_tokensave_files(_REPO) > 0, (
        "the code-file count went to zero on a repo full of indexed Python — "
        "the same silent failure mode as the old `tokensave status` parse")


@_needs_index
def test_grounding_count_excludes_artifacts():
    """`kind = 'code'` must actually narrow the result.

    Since v16 tokensave also indexes .json/.yaml/.md by path. If the kind
    filter silently stopped applying, the count would jump to every row and
    drag the codegraph-divergence ratio toward a false "broken" verdict.
    """
    con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        if "kind" not in cols:
            pytest.skip("pre-v16 index without a kind column")
        total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        con.close()
    code = _count_tokensave_files(_REPO)
    assert code <= total
    assert total > code, (
        "no non-code artifacts counted at all — either the index has none "
        "(unlikely for this repo, which indexes .md and .toml) or the kind "
        "filter is not doing anything")


@_needs_index
def test_indexed_extensions_are_well_formed_and_real():
    """Every returned extension is lowercase, dot-prefixed, and on a real file.

    Emptiness is NOT failure — a project where nothing parsed is a valid
    answer, and the caller distinguishes it from None ("no index").
    """
    exts = indexed_extensions(_REPO)
    assert exts is not None, "a real index must not report as missing"
    assert isinstance(exts, set)
    for ext in exts:
        assert ext.startswith("."), f"{ext!r} is not dot-prefixed"
        assert ext == ext.lower(), f"{ext!r} is not lowercased"
        assert len(ext) > 1, f"{ext!r} is a bare dot"
    # This repo is overwhelmingly Python; if .py is absent the walk is broken.
    assert ".py" in exts


@_needs_index
def test_indexed_extensions_agree_with_the_files_table():
    """Cross-check the helper's set against the raw rows it derives from."""
    con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT path, node_count FROM files").fetchall()
    finally:
        con.close()
    expected = {
        os.path.splitext(str(p))[1].lower()
        for p, n in rows if n and n > 0 and os.path.splitext(str(p))[1]
    }
    assert indexed_extensions(_REPO) == expected


@_needs_index
def test_schema_is_v17_with_ambiguous_calls():
    """Pins the schema this suite was verified against.

    Not a correctness requirement so much as a tripwire: if this fails, the
    schema moved and the contracts above deserve a re-read rather than a
    blind version bump.
    """
    con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        has_ambiguous = bool(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='ambiguous_calls'").fetchone())
    finally:
        con.close()
    assert version >= 17, f"schema went backwards to v{version}"
    assert has_ambiguous, "v17 must carry the ambiguous_calls table"


@_needs_index
def test_scout_findings_point_at_real_files_and_lines():
    """Every finding must name a file that exists and a plausible line.

    run_scout joins nodes to files and reads source at the reported offset,
    so a schema shift shows up here as findings that point nowhere — the
    dialog would render an empty evidence pane rather than raising.
    """
    findings, _suppressed = run_scout(_REPO)
    assert isinstance(findings, dict)
    flat = [f for group in findings.values() for f in group]
    if not flat:
        pytest.skip("scout found nothing on this tree")
    for f in flat:
        abs_path = os.path.join(_REPO, f.file)
        assert os.path.isfile(abs_path), (
            f"{f.kind} finding names {f.file!r}, which is not a file")
        assert f.line >= 1, f"{f.file}:{f.line} is not a 1-indexed line"
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            n_lines = sum(1 for _ in fh)
        assert f.line <= n_lines, (
            f"{f.file}:{f.line} is past the end of the file ({n_lines} lines)")
        assert f.symbol, "every finding names a symbol"
        assert f.id, "every finding carries a suppression id"


@_needs_index
def test_scout_finding_ids_are_unique_and_stable():
    """Suppression keys off `id`; collisions would hide unrelated findings."""
    first, _ = run_scout(_REPO)
    flat = [f for group in first.values() for f in group]
    if not flat:
        pytest.skip("scout found nothing on this tree")
    ids = [f.id for f in flat]
    assert len(ids) == len(set(ids)), "duplicate finding ids"

    second, _ = run_scout(_REPO)
    assert {f.id for g in second.values() for f in g} == set(ids), (
        "finding ids changed between two runs over an unchanged tree")


@_needs_index
def test_scout_suppression_actually_filters():
    """Contract of the (findings, suppressed) tuple the dialog renders."""
    findings, _ = run_scout(_REPO)
    flat = [f for group in findings.values() for f in group]
    if not flat:
        pytest.skip("scout found nothing on this tree")
    victim = flat[0]
    kept, suppressed = run_scout(_REPO, ignored_ids={victim.id})
    assert suppressed >= 1
    assert victim.id not in {f.id for g in kept.values() for f in g}


def test_scout_raises_a_useful_error_without_an_index(tmp_path):
    """Deliberately NOT fail-open: the caller surfaces "run tokensave init"."""
    with pytest.raises(FileNotFoundError) as exc:
        run_scout(str(tmp_path))
    assert "tokensave init" in str(exc.value)


# ── fail-open edges, on synthetic databases (always run) ──────────────────────

def test_grounding_count_returns_zero_without_an_index(tmp_path):
    assert _count_tokensave_files(str(tmp_path)) == 0


def test_indexed_extensions_returns_none_without_an_index(tmp_path):
    """None, not an empty set — the caller distinguishes these.

    None means "no index to compare against, run a sync"; an empty set means
    "an index exists and nothing parsed". Collapsing them would make the
    shadow-link scanner recommend mappings off a nonexistent baseline.
    """
    assert indexed_extensions(str(tmp_path)) is None


def _make_db(tmp_path, ddl, rows=()):
    d = tmp_path / ".tokensave"
    d.mkdir()
    db = d / "tokensave.db"
    con = sqlite3.connect(str(db))
    con.execute(ddl)
    for r in rows:
        con.execute(
            f"INSERT INTO files VALUES ({','.join('?' * len(r))})", r)
    con.commit()
    con.close()
    return str(tmp_path)


def test_grounding_count_falls_back_when_kind_column_is_absent(tmp_path):
    """A pre-v16 index has no `kind`; count every row rather than failing."""
    root = _make_db(
        tmp_path,
        "CREATE TABLE files (path TEXT, node_count INT)",
        [("a.py", 3), ("b.py", 5)])
    assert _count_tokensave_files(root) == 2


def test_grounding_count_survives_a_corrupt_database(tmp_path):
    d = tmp_path / ".tokensave"
    d.mkdir()
    (d / "tokensave.db").write_text("this is not a database", encoding="utf-8")
    assert _count_tokensave_files(str(tmp_path)) == 0


def test_indexed_extensions_survives_a_corrupt_database(tmp_path):
    d = tmp_path / ".tokensave"
    d.mkdir()
    (d / "tokensave.db").write_text("this is not a database", encoding="utf-8")
    assert indexed_extensions(str(tmp_path)) is None


def test_indexed_extensions_skips_unparsed_and_extensionless_files(tmp_path):
    """node_count == 0 means tracked-but-not-parsed; MAPINFO has no extension."""
    root = _make_db(
        tmp_path,
        "CREATE TABLE files (path TEXT, node_count INT)",
        [("src/a.py", 4), ("notes.md", 0), ("MAPINFO", 7), ("B.PY", 2)])
    assert indexed_extensions(root) == {".py"}
