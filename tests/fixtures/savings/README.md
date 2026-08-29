# `tokensave` savings/spend fixtures

Captured verbatim from **tokensave 7.10.0** so that an upstream schema change
breaks a test rather than passing silently through hand-trimmed JSON. Do not
tidy, reformat, or "fix" these files — their exact bytes are the contract.

| File | Produced by |
|---|---|
| `gain_project_30d.json` | `tokensave gain --json --range 30d` |
| `gain_all_30d.json` | `tokensave gain --all --json --range 30d` |
| `gain_history_7d.json` | `tokensave gain --history --json --range 7d` |
| `cost_7d.json` | `tokensave cost 7d --export json` |
| `cost_all.json` | `tokensave cost all --export json` |
| `cost_all.stderr` | the **stderr** of that same invocation |
| `discover_7d.json` | `tokensave discover --json --since 7d` |

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

Then re-apply the one `project` edit above.
