<!--
STATUS: FILED 2026-09-08 as issue #522 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/522

  Re-read from GitHub after filing: the published title and body are
  byte-identical to what was sent, and carry no absolute path, project name
  or username. The local-only header block below was stripped before
  publishing.

DUPLICATE SEARCH (gh search issues, repo-scoped; omit --state, it is rejected):
  Searched: "uses edge bare name" (0), "reference resolver bare name" (0),
  "unqualified reference untracked" (0), "bare-name fallback" (#503, #378,
  #414, #412, #153 — all CALL-path), "uses edges" (#459, #148, #224, #334,
  #167, #378), "resolver evidence" (0), "phantom edge test" (0).

  #503 Python extractor: unqualified CALLS on untracked receivers — OPEN,
       filed from this repo. The primary report was fixed in #508 and
       shipped in 7.11.1; the maintainer left the issue open for a
       secondary finding (an incremental sync not re-resolving invalidated
       call sites). This report is NOT that secondary finding, and not the
       primary either: #508 fixed the `calls` path and this is the `uses`
       path, measured untouched by it.
  #378 TS extractor: the same defect, calls path, TypeScript — CLOSED.
       This is the argument that the pattern recurs per-language and
       per-edge-kind, and gets fixed one instance at a time.
  #224 Python extractor MISSES several reference kinds — CLOSED. Adjacent
       but opposite in direction: that is references not emitted, this is
       references emitted to the wrong target.

  So the class is well known and this specific edge kind is not filed.

SANITISED: no absolute paths, project name, usernames or usage totals. Every
figure below is from the tool's own output or a read-only query of the index
it produced. File paths are repo-relative and generic.
-->

# Python extractor: the bare-name fallback still binds `uses` edges without evidence (the #503 defect, one edge kind over)

## Summary

7.11.1 (#508) taught the **call** resolver that being the only candidate is
not evidence. The **reference** resolver was not taught the same thing.

A production function that declares a local variable named `exe` acquires a
`uses` edge pointing at a **pytest fixture** named `exe` in the test tree —
because that fixture is the only symbol of that name in the project. The
remedy #508 applied to calls (require the candidate to be in the caller's
file, its directory, or something the caller imports) is exactly the remedy
this needs, and none of it is applied here.

The result is the same class of impossible edge #503 described, surviving in
a graph that #508 otherwise cleaned up by 94%.

## Measured, before and after 7.11.1

One Python project, 489 files, 14,422 nodes. Full `sync --force` under
7.11.0, then the identical tree fully re-indexed under 7.11.1. Counting
edges from production files into the test tree — impossible by construction,
since tests import production code and never the reverse:

| edge kind | 7.11.0 | 7.11.1 | examined (7.11.1) |
|---|---|---|---|
| `calls`   | 455 | **29** | 10,656 |
| `uses`    | 351 | **351** | 7,277 |
| `annotates` | 0 | 0 | 531 |
| `extends` | 0 | 0 | 45 |

`calls` fell 94%. `uses` did not move by a single edge.

The per-name counts show the same split, and are their own proof that the
two populations are disjoint. Names that are **method** names moved:
`after` 168 → 20, `askyesno` 77 → 0, `delete` 55 → 0, `grid` 24 → 6,
`winfo_width` 34 → 7. Names that are **variable / fixture** names did not
move at all: `exe` 109 → 109, `node` 55 → 55, `lines` 46 → 46,
`cfg_path` 40 → 40, `proj` 11 → 11, `python_exe` 11 → 11, `npm_exe` 9 → 9.

## Reproduction

The target — a pytest fixture, and the only symbol named `exe` anywhere in
the project:

```python
# tests/test_<something>.py:42
@pytest.fixture
def exe(tmp_path):
    p = tmp_path / "pyscope.exe"
    p.write_text("", encoding="utf-8")
    return str(p)
```

A source — an ordinary local variable in production code, in a different
directory, in a file that imports nothing from the test tree:

```python
# src/cli.py:146
    exe = (cfg.get("tokensave_exe") or "").strip()
    if not exe:
        raise _Prerequisite(...)
    return exe
```

Query the resulting index:

```sql
SELECT s.file_path, s.name, e.line
FROM edges e
JOIN nodes s ON s.id = e.source
JOIN nodes t ON t.id = e.target
WHERE e.kind = 'uses'
  AND t.name = 'exe'
  AND t.file_path LIKE 'tests/%';
```

109 rows, from production files across the tree — `src/cli.py:150`,
`src/cli.py:1188`, `scripts/*.py:154`, and so on. There is no production
symbol named `exe` at all; the fixture is the sole candidate, so it wins by
default.

## Why the same argument as #503 applies

The maintainer's own framing on #503 was that the confidence gate already
exists and already routes this site into it — it simply is not taken when
the candidate set has one member. That is true here verbatim, one edge kind
over.

And the directionality is the same, for the same reason: test code is
deliberately named after the API it stands in for, and test *fixtures* are
deliberately named after the values they supply. `exe`, `cfg_path`,
`python_exe`, `npm_exe`, `proj`, `lines` are exactly the names a fixture
gets, and exactly the names a production local gets. So the collisions are
not incidental — the naming convention that makes tests readable is what
generates them.

## Consequences

Everything computed over `uses` inherits the edges: `file_dependents` and
`impact` report production modules as depending on test files,
`unused_imports` and `dead_code` see phantom referents, and any acyclicity
or modularity measure over the reference graph is contaminated in the one
direction that cannot be real.

It also blocks a consumer from reporting honestly on #508's success. Summed
across kinds this project reads 806 → 380, which looks like a fix that half
worked. Split by kind it is one defect essentially fixed and a second one
untouched — a materially different thing to tell a user.

## Suggested remedy

Apply #508's evidence test to the reference-resolution path: bind a lone
bare-name candidate only when it is in the referring file, in the same
directory, or named by one of the referrer's imports. The import lookup
needs the same last-`::`-segment handling #508 already documents for Python
dotted paths.

If that is too broad for references specifically, a narrower rule would
still remove most of this population: never resolve a bare name from
production code to a symbol in a **test** file. The asymmetry is safe
because the reverse direction is legitimate and common.
