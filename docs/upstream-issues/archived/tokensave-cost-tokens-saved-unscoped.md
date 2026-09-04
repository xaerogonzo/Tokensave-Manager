<!--
STATUS: CLOSED — verified via GitHub API 2026-08-30
  https://github.com/aovestdipaperino/tokensave/issues/473
  (tokens_saved ignores the requested range)

RESOLVED: fixed in tokensave 7.11.0 (issue #473).
  cost_summary now derives tokens_saved from the same `since` the rest of the
  summary uses, reading the savings_ledger that `gain` reads. The parameter
  is gone, so a caller can no longer pass a value scoped differently from the
  summary it lands in.

  Verified against 7.11.0: today 97,704 / 7d 550,983 / 30d 876,775 /
  all 31,322,215 — four different figures where 7.10.0 returned one lifetime
  number for every range.

  ONE THING THE FIX DOES NOT CHANGE, and it matters to any consumer: the
  figure is still MACHINE-GLOBAL. It equals `gain --all` exactly at every
  range measured, and does NOT equal project-scoped `gain` (7d: 550,983
  against 19,644 on this machine). `cost` has no project filter, so this is
  correct — but a UI that puts it beside the project-scoped Gain reproduces
  the scope ambiguity this issue was about, in a new form.

  Manager side: the field is published with a `tokens_saved_spans_range`
  flag, and a test asserts neither can appear in the envelope without the
  other. The agreement with `gain --all` and the disagreement with `gain` are
  both pinned against captured fixtures.

Filed against tokensave 7.10.0. Searched for duplicates first (gh search
over all issues): none. #457 is the same CLASS of bug — a count whose name
promises more than it delivers — but unrelated code.

SANITISED BEFORE FILING: absolute usage totals were replaced with rates and
ratios, which carry the whole finding and disclose nothing about this
machine's volume or spend. The filed body was re-read from GitHub and
confirmed to contain no paths, project names, usernames or usage totals.
This file is the local source of truth; the issue is what was published.
-->

# `cost --export json`: `tokens_saved` ignores the requested range, and disagrees with `gain` about the same quantity

## Summary

`tokensave cost <RANGE> --export json` accepts a range and scopes
`total_cost_usd`, `total_input_tokens` and `total_output_tokens` to it — but
`tokens_saved` is **byte-identical for every range**, including `today`. It
appears to be a lifetime, all-projects counter that has been placed inside a
range-scoped payload.

`efficiency_ratio` is derived from it and inherits the problem: because a fixed
numerator is divided by a moving denominator, the ratio changes with the range
while describing nothing that changed.

Separately, that counter and `tokensave gain` — which is documented as the
savings ledger — report **different totals for the same scope**.

## Reproduction

```bash
for r in today 7d 30d all; do tokensave cost $r --export json; done
for r in today 7d 30d all; do tokensave gain --all --json --range $r; done
```

## What is observed

### `cost`'s `tokens_saved` does not move; its `efficiency_ratio` does

Writing the constant as **S** (absolute totals are omitted — they describe one
machine's usage, and the finding does not depend on them):

| range | `tokens_saved` | `efficiency_ratio` |
|---|---|---|
| today | S | 0.9185 |
| 7d | S | 0.5513 |
| 30d | S | 0.2652 |
| all | S | 0.1970 |

Byte-identical in all four — the savings figure for *today* equals the savings
figure for *all time*. The
efficiency ratio then falls monotonically as the range widens, which is exactly
what dividing a constant by a growing denominator produces — so the ratio is an
artefact of the bug rather than a measurement.

### `gain` scopes correctly, and reports a different total

Normalised against its own all-time total, over the same four ranges:

| range | `gain --all` `saved_tokens`, as a share of its all-time value |
|---|---|
| today | 0.06% |
| 7d | 1.4% |
| 30d | 2.7% |
| all | 100% |

`gain` behaves as expected: it grows monotonically with the range. But its
all-time figure is roughly **2.15× larger** than `cost`'s supposedly-equivalent
`tokens_saved`, for what a reader would take to be the same quantity at the
same scope.

So the tool currently reports two different "tokens saved" numbers, one of
which also ignores the range it was asked about.

## Why it matters

`cost` is the natural place for a consumer to look for a savings figure — it is
the command whose output the human table labels `Savings X tokens (N%
efficiency)`. A UI that reads `tokens_saved` from a `cost 7d` payload will
report a lifetime, all-projects total under a "past 7 days" heading and have no
way to know it has done so, because nothing in the payload distinguishes the
two scopes.

(That is not hypothetical: it is precisely the bug a downstream consumer
shipped, and the reason this was investigated.)

## Suggested fix

Any one of these resolves the ambiguity; they are listed in order of
preference:

1. **Scope `tokens_saved` to the requested range**, so it means what its
   position in a range-scoped payload implies — and recompute
   `efficiency_ratio` from the scoped value.
2. **Or rename it** to something that states its scope, e.g.
   `lifetime_tokens_saved_all_projects`, and drop `efficiency_ratio` (a ratio
   between a lifetime numerator and a ranged denominator has no meaning to
   preserve).
3. **Or remove both from the `cost` export** and point consumers at `gain`,
   which already answers this question with correct scoping and an explicit
   valuation basis.

Whichever is chosen, it would help to state in `--help` which command owns the
savings figure — `cost` and `gain` currently both appear to, and disagree.

## Environment

* tokensave 7.10.0 (stable channel)
* Windows 11, Claude Code sessions
* Both tables captured minutes apart in one session; `gain`'s figures grow
  slightly between calls as sessions continue, but `cost`'s `tokens_saved`
  stays fixed across ranges within a single call sequence.
