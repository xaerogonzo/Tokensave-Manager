<!--
STATUS: FILED 2026-08-29 as issue #474 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/474
  (addressable_input_tokens is implausibly small)

Filed against tokensave 7.10.0. Searched for duplicates first (gh search
over all issues): none. #457 is the same CLASS of bug — a count whose name
promises more than it delivers — but unrelated code.

SANITISED BEFORE FILING: absolute usage totals were replaced with rates and
ratios, which carry the whole finding and disclose nothing about this
machine's volume or spend. The filed body was re-read from GitHub and
confirmed to contain no paths, project names, usernames or usage totals.
This file is the local source of truth; the issue is what was published.
-->

# `discover --json`: `addressable_input_tokens` is implausibly small — single-digit tokens per turn

## Summary

`tokensave discover --json` reports `total_addressable_input_tokens` and
`total_recoverable_input_tokens`, described as the input a tokensave query
could have served more cheaply. The values are **one to five tokens per
replaceable turn** — far too small to be token counts for `Read` and `Grep`
turns, which inject hundreds to thousands of tokens each.

At short ranges the figure is *exactly* 2.000 tokens per turn in every bucket,
which looks like a placeholder rather than a measurement.

The turn counts themselves look right and are useful. It is only the token
columns that appear not to mean what they say.

## Reproduction

```bash
for r in today 7d 30d all; do tokensave discover --json --since $r; done
```

Then divide `total_addressable_input_tokens` by `replaceable_turns`, and do the
same per bucket.

## What is observed

Absolute counts are omitted — they describe one machine's usage, and the
finding is entirely in the per-turn ratio. Sample size is given as an order of
magnitude so the short ranges can be weighed appropriately:

| since | replaceable turns | addressable tokens **per turn** |
|---|---|---|
| today | single digits | **2.000** |
| 7d | hundreds | **2.000** |
| 30d | ~1e3 | **3.773** |
| all | ~3e3 | **4.726** |

Per bucket, for `--since all`:

| bucket | tool | turns | addressable tokens per turn |
|---|---|---|---|
| read | Read | ~3e3 | 4.43 |
| glob | Glob | ~50 | **47.47** |
| grep | Grep | ~400 | **1.45** |

Three things stand out:

1. **The magnitude is wrong by orders of magnitude.** A single `Read` turn puts
   a file into the context — commonly hundreds to thousands of tokens. 4.43
   tokens per Read turn is not a plausible measurement of anything the tool
   means by "addressable input".

2. **At `today` and `7d` the value is exactly 2.000 per turn in every bucket.**
   An exact constant across independent buckets suggests a stub or a default
   rather than a computation over real data.

3. **`recoverable = addressable × 0.5` exactly, everywhere.** This matches the
   reported `recoverable_fraction: 0.5`, so `total_recoverable_input_tokens`
   is a derived value, not an independent measurement. If the input to that
   derivation is wrong, both fields are.

## Consequence for consumers

A UI cannot tell whether these fields are a rough estimate worth showing or a
placeholder that should not be shown. Rendering them as-is would tell a user
that switching from `Read` to a tokensave query saves a handful of tokens,
which would argue *against* the tool.

The downstream consumer that found this now displays turn counts only, and
suppresses the token estimate behind a recorded consistency check — but that
check is necessarily a heuristic, and it fires inconsistently. At `7d` the
totals identity (`recoverable == replaceable_turns`) holds exactly and the
estimate is suppressed; at `30d` it does not hold, so the same estimate — still
implausible at 3.8 tokens per turn — passes. A correct fix upstream would let
consumers drop the heuristic entirely.

## Suggested fix

1. **If the fields are computing something other than tokens** (turn counts, a
   normalised score, a placeholder), rename them so their unit is not `tokens`,
   or omit them until they measure what the name claims.
2. **If they are meant to be token counts**, the estimate looks like it is
   measuring the tool call's own arguments rather than the payload the call
   returned — the thing a tokensave query would actually have replaced. The
   response size is what makes the comparison meaningful.
3. Either way, **document `recoverable_fraction`'s role**: it is currently
   applied as an exact multiplier, so a consumer that treats `recoverable` as
   independent evidence is double-counting an assumption.

The turn counts are genuinely useful as they stand — nothing here argues for
removing the command, only for the token columns being either fixed or
withdrawn until they can be trusted.

## Environment

* tokensave 7.10.0 (stable channel)
* Windows 11, Claude Code sessions
* Ratios above are stable across repeated calls in one session; only the
  absolute counts grow as sessions continue.
