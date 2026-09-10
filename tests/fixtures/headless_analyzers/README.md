# headless_analyzers parser fixtures

Real captured output from `ruff`, `pyright` and `markdownlint-cli2`, held as
fixtures rather than reconstructed in test code. Every one was captured by
running the tool; **none was written from memory**, and three of the facts below
would have been wrong if it had been.

**Provenance:** ruff 0.16.6, pyright 1.1.414, markdownlint-cli2 0.23.2
(markdownlint 0.41.1). Windows, 2026-09-10. Each invocation matches what
`helpers/headless_analyzers.py` builds.

## Placeholders

| Token | Substituted with |
|---|---|
| `<PROJECT>` | the project root, as the tool printed it |

The substitution must be **JSON-escaped** for the `.json` fixtures: they are
JSON documents, so a raw Windows root drops `C:\Users\...` into a string full of
invalid escapes and the payload stops parsing. `tests/test_headless_analyzers.py`
does this with `json.dumps(root)[1:-1]`. Same class of trap as `quality_checks`'s
`<PROJECT_ESC>`.

## The three facts a remembered format gets wrong

### 1. ruff's `severity` field exists and says `error` about everything

`ruff_many_codes.json` is one row per distinct code from a real run over
`src/helpers` (843 rows reduced to 28 codes). **Every row says
`"severity": "error"`** — all 1,812 of them on `src/` — including `S110`
(try-except-pass) and `BLE001` (blind except), which this codebase does
deliberately and documents. Forwarding that field paints every unused import in
the colour reserved for code that does not compile, so the Manager assigns
severity from the **code** instead.

Contrast `pyright_types.json`, where `severity` **is** forwarded: pyright's
varies with `typeCheckingMode` and per-rule configuration, so it carries a real
decision. That contrast is why severity policy belongs to the table row rather
than to the module.

### 2. pyright's coordinates are 0-BASED, and its payload is not an array

`pyright_types.json` reports `"line": 6` for what is line **7** of the file, and
wraps its rows in `generalDiagnostics` rather than being a top-level array like
ruff's. Two independent ways to be silently wrong: iterate the document and find
nothing (clean project), or forward the range and put every squiggle one line
above the code it describes.

### 3. markdownlint puts its findings on STDERR

`markdownlint_issues.txt` is stderr. `markdownlint_banner_stdout.txt` is what
stdout carried at the same moment:

```
markdownlint-cli2 v0.23.2 (markdownlint v0.41.1)
Finding: *.md
Linting: 2 files
Summary: 5 issues in 1 file
```

A runner that reads stdout parses **zero findings out of a banner** and reports
the project clean while the tool is saying `5 issues in 1 file`. That is what
`AnalyzerSpec.stream` exists for. (This repo has met the same inversion before —
`tokensave doctor` also writes to stderr.)

Note also the two line shapes in the stderr fixture, `file:line` and
`file:line:col`, exactly as pyflakes has two.

## Why the other files exist

- **`ruff_unused_and_syntax.json`** — the two rows that decide ruff's severity
  policy. `"code": "invalid-syntax"`: a parse failure does **not** arrive as an
  `E9xx` code. The filename `<PROJECT>/src\unused.py` also carries **mixed
  separators in one path** — forward from the argument, back for the leaf. That
  is the format, not a typo; `findings.relative_to` normalises it and the test
  pins that it must.
- **`ruff_clean.json` / `pyright_clean.json` / `markdownlint_clean.txt`** — the
  empty results. "No findings" and "the tool did not run" must never reduce to
  the same thing; these are the half that legitimately means zero.

## The volume, recorded because it is the surprising part

Measured with **no `[tool.ruff]` in `pyproject.toml` and no config file anywhere
above the repository**:

```
413 rules enabled
1,812 rows on src/
```

That is not ruff's documented default (`E4,E7,E9,F`). Whatever resolves it, the
Manager does not own it — which is why `helpers/headless_analyzers.py` reports
the row count and lets the tool's own configuration stand, rather than selecting
a rule subset of its own. Picking a subset here would be the Manager quietly
owning somebody else's rules.

## Regenerating

Re-capture rather than hand-edit if a tool version changes. Run the tool against
a scratch tree, then replace the machine-specific root prefix with `<PROJECT>`.
The `time` field is stripped from pyright's output: a timestamp is not part of
the format.

## Adding another analyzer

One row in `ANALYZERS`, and **one fixture captured from a real invocation** on a
machine where the tool is installed. A parser written from a remembered format
is the thing this directory exists to prevent — and the three facts above are
what that costs.
