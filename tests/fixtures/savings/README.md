# `tokensave` savings/spend fixtures

Captured verbatim so that an upstream schema change breaks a test rather than
passing silently through hand-trimmed JSON. Do not tidy, reformat, or "fix"
these files — their exact bytes are the contract.

**Two tokensave versions are represented, and both are load-bearing.** The
7.10.0 captures are not stale leftovers: that shape is still a supported input,
and it is the only way to test the "this binary did not report it" path, which
must never render as a zero. Deleting them would delete half the contract.

### tokensave 7.10.0

| File | Produced by |
|---|---|
| `gain_project_30d.json` | `tokensave gain --json --range 30d` |
| `gain_all_30d.json` | `tokensave gain --all --json --range 30d` |
| `gain_history_7d.json` | `tokensave gain --history --json --range 7d` |
| `cost_7d.json` | `tokensave cost 7d --export json` |
| `cost_all.json` | `tokensave cost all --export json` |
| `cost_all.stderr` | the **stderr** of that same invocation |
| `discover_7d.json` | `tokensave discover --json --since 7d` |

### tokensave 7.11.0

Added for the #472 / #473 fixes. `cost --export json` gained
`total_cache_read_tokens`, `total_cache_creation_tokens` and `total_tokens`,
and its `tokens_saved` became range-scoped.

| File | Produced by |
|---|---|
| `cost_711_today.json` … `cost_711_all.json` | `tokensave cost <RANGE> --export json` |
| `gain_711_all_today.json` … `gain_711_all_all.json` | `tokensave gain --all --json --range <RANGE>` |

All four ranges are kept for each, and that is deliberate — two of the
properties under test are only visible across ranges:

* `tokens_saved` **differs per range** (under 7.10 one lifetime figure was
  returned for every range, which is what #473 fixed).
* `cost`'s `tokens_saved` equals `gain --all` **exactly, at every range**, and
  does *not* equal project-scoped `gain`. The paired `gain_711_all_*` captures
  exist so that agreement is asserted against real bytes rather than asserted
  in prose. Measured: today 97,704 · 7d 550,983 · 30d 876,775 · all 31,322,215.

A single range would let a regression that re-broke the scoping pass.

## The one edit

`gain_project_30d.json`'s `project` value was replaced with
`C:\projects\demo`. Only that value changed; the schema and formatting are
untouched, so its purpose — catching field renames and type changes — is intact.

## Why `cost_all.stderr` exists

`cost` and `discover` sometimes emit a line like

    Ingested or refreshed 1 local accounting rows.

before they answer. It was originally recorded in the plan as a **stdout**
preamble that the parser had to skip. That was wrong, and wrong in a way this
repo has been caught by before: it was observed through `2>&1`, which merges the
streams. Measured with the streams separated, the line goes to **stderr** and
stdout is always pure JSON.

So the load-bearing rule is *capture stdout and stderr separately and parse only
stdout* — never `2>&1`. `cost_all.stderr` pins the line to the stream it
actually uses, so a future change that moves it to stdout fails a test instead of
corrupting a parse.

The preamble is also **conditional**: it appears only when there was something to
ingest. A capture run twice in a row will usually produce it once. That is why
the fixture is kept rather than re-derived.

## The two synthetic files

Clearly marked, and the only hand-written ones here:

* `synthetic_stdout_preamble.txt` — `cost_7d.json` with a preamble line prepended
  to stdout. Nothing observed produces this; it exists to prove `_first_json`
  stays defensive if upstream ever moves the line.
* `synthetic_no_payload.txt` — output with no JSON at all, which must become
  `unavailable("no JSON payload")` rather than an exception or an empty dict.

## Refreshing

Re-capture with stderr redirected separately, never merged:

    tokensave gain --json --range 30d           > gain_project_30d.json  2>/dev/null
    tokensave cost all --export json            > cost_all.json          2>cost_all.stderr

    for R in today 7d 30d all; do
      tokensave cost $R --export json           > cost_711_$R.json       2>/dev/null
      tokensave gain --all --range $R --json    > gain_711_all_$R.json   2>/dev/null
    done

Then re-apply the one `project` edit above.

**Re-capture the `cost_711_*` and `gain_711_all_*` files together, in one
run.** Their agreement is the property under test, and capturing them minutes
apart lets ordinary traffic land in between and breaks it for a reason that has
nothing to do with tokensave.

**Do not re-capture the 7.10 files against a newer binary.** They would silently
become 7.11 captures under a 7.10 name, and the old-shape tests would then pass
without ever exercising the old shape. If they are ever lost, the tests that
depend on them should fail rather than be pointed at a modern capture.

No test hardcodes a figure from these files — the assertions read the value out
of the fixture — so a re-capture does not require editing test code.
