<!--
STATUS: FILED 2026-09-03 as issue #503 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/503

  Re-read from GitHub after filing: the published body is byte-identical
  to what was sent, and carries no path, project name or username. The
  local-only header block below was stripped before publishing.

Found against tokensave 7.11.0 (stable), Python extractor, Windows 11.

DUPLICATE SEARCH (gh search over all issues, open and closed):
  #378 TS extractor: phantom call edges when unindexed receiver methods fall
       back to bare-name resolution — CLOSED 2026-08-17. This is the SAME
       defect and the same mechanism, reported for TypeScript. The present
       report is that the Python extractor still does it in 7.11.0.
  #412 find_best_match breaks score ties on scan order — CLOSED. Establishes
       that a scoring/tie-break stage exists; this report concerns what that
       scorer accepts when there is only ONE candidate to score.
  #346 tokensave_circular fabricates cross-language cycles; dead_code FP rate
       (Go + TypeScript) — CLOSED. Same consequence, different extractors.
  #149 / #153 Go same-name package collisions — CLOSED. Same class.
  No OPEN issue covers the Python extractor. Filing as a new report rather
  than a comment on #378, which is closed and TS-scoped.

SANITISED: the reproduction is a synthetic three-file project written for
this report. No path, module name, project name or username from the
author's own repository appears below. The one aggregate quoted from a real
repository ("557 edges across 91 files") carries no identifying detail.
Re-read the filed issue from GitHub after filing and confirm this held.
-->

# Python extractor: unqualified calls on untracked receivers still fall back to bare-name resolution (the #378 defect, in Python)

## Summary

When a Python call's receiver is a type the index does not track — a stdlib
object, a third-party instance, a `logging.Logger` — the resolver falls back
to matching the bare method name against every indexed symbol. If exactly one
symbol in the whole project carries that name, the call is bound to it,
**regardless of which module or directory it lives in**.

This is the defect reported for the TypeScript extractor in #378 and closed in
August. The Python extractor exhibits it in 7.11.0.

The failure is quiet and it is directional: production code binds to helpers
in the test tree, because test doubles are deliberately named after the API
they stand in for. A faithful fake of a UI toolkit defines `after`, `delete`,
`grid` and `winfo_width`; a fake logger defines `info`. Those are exactly the
names production code calls on untracked receivers.

## Reproduction

A whole project, three files. No configuration.

`src/a.py` — production code. Calls a logger. Defines no `info` of its own:

```python
import logging

log = logging.getLogger(__name__)


def do_work(payload):
    log.info("starting %s", payload)
    return payload
```

`tests/helper.py` — a test double. Never imported by `src/`:

```python
def info(msg, *args):
    return (msg, args)
```

Then:

```bash
git init . && git add -A && git commit -m init
tokensave init
```

### Observed — one candidate

The graph contains exactly one `calls` edge, and it is wrong:

```
src/a.py:7   do_work --calls--> info   (tests/helper.py)
ambiguous_calls: 0 rows
```

`do_work` does not call anything in `tests/`. It cannot: nothing in `src/`
imports that module. The edge is the only edge in the graph.

### Observed — two candidates

Add a second file that changes nothing about `src/`:

`tests/other.py`

```python
def info(msg, *args):
    return None
```

Re-index from scratch. The resolver now declines, correctly:

```
calls edges:     (none)
ambiguous_calls: log.info at src/a.py:7  -> 2 candidates
```

**This contrast is the report.** The resolver already has a "not confident
enough" path and already routes this exact call site into it. It just does not
take that path when the ambiguity set happens to have one member. One
candidate is not evidence of a correct match when nothing checked that the
candidate is reachable from the call site.

## Secondary finding: `sync` leaves the superseded edge behind

Found while building the contrast above, and reproducible with the same files.

Running `tokensave sync` after adding `tests/other.py` — rather than indexing
the three files from scratch — produces a graph that asserts both things at
once for the same call site:

| Path | `calls` edge at `src/a.py:7` | `ambiguous_calls` row |
|---|---|---|
| Clean index of all three files | absent (correct) | present |
| `init` with two files, then `sync` after adding the third | **present** | present |

The sync reports `1 added, 0 modified, 0 removed` and does not re-resolve the
call sites whose resolution the new file invalidates. The stale edge outlives
the fact that justified it, and the two tables then disagree about whether
that call was resolvable.

This is visible in real repositories, not just the synthetic one: in a Python
project of ~15k edges, 85 call sites carry both a `calls` edge and an
`ambiguous_calls` row naming the same symbol.

## Consequence for consumers

Everything computed from `calls` edges inherits the phantom edges:

* `tokensave_circular` — production and test files are reported inside one
  strongly-connected component. In the repository this was found in, that is a
  single 140-file cycle, which is not a cycle.
* `tokensave_health` — `acyclicity` is computed from those edges, and
  `quality_signal` is the geometric mean over all six dimensions, so the
  headline number is affected too. A consumer tracking that number across
  releases is partly tracking name collisions in their test tree.
* `tokensave_file_dependents` / `impact` / `callers` — report production
  modules as depending on test files.
* `tokensave_dead_code` — a symbol whose only caller is a phantom edge looks
  live. This is the consequence #378 called out, and #346 measured.

Scale in one real Python repository, tokensave 7.11.0: **557 edges whose
source is production code and whose target is a test file, across 91 source
files.** Every one is impossible by construction — the test tree imports
production code, never the reverse.

## Suggested fix

#378 proposed the shape and it applies unchanged here: when a call's receiver
type cannot be resolved, suppress the bare-name fallback rather than binding
to whatever single symbol shares the name. Recording the call site in
`ambiguous_calls` with its one candidate — instead of dropping it silently —
would preserve the diagnostic without asserting the edge.

If the fallback is worth keeping for same-module calls, scoping it to the
defining module (or requiring an import path from caller to candidate) would
remove the whole cross-directory class while leaving intra-module resolution
untouched.

For the secondary finding: adding a file needs to re-resolve call sites whose
candidate set it changes, not only the sites inside the added file. A cheaper
mitigation is to treat the presence of an `ambiguous_calls` row as
authoritative and drop any `calls` edge for the same site, so the two tables
cannot contradict each other.

## Environment

* tokensave 7.11.0 (stable channel), Python extractor
* Windows 11
* Reproduction is a synthetic three-file project created for this report;
  behaviour was identical on `init` and on `sync` except where stated.
