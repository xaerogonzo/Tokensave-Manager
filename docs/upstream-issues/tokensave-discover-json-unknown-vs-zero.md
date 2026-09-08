<!--
STATUS: FILED 2026-09-08 as issue #523 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/523

  Re-read from GitHub after filing: the published title and body are
  byte-identical to what was sent, and carry no absolute path, project name
  or username. The local-only header block below was stripped before
  publishing.

DUPLICATE SEARCH (gh search issues, repo-scoped; omit --state, it is rejected):
  Searched: "discover json unknown zero" (0), "addressable_input_tokens"
  (#474 only), "discover json flag" (0).

  #474 `discover --json`: addressable_input_tokens is implausibly small —
       CLOSED, fixed in 7.11.1, filed from this repo. This report is the
       DIRECT CONSEQUENCE of that fix rather than a regression in it: the
       fix is correct, and it introduced a state ("not measured for these
       turns") that the human output names and the JSON does not.

  Same class as #472 and #473, both filed from this repo and both fixed:
  a JSON export whose fields cannot be told apart from a wrong reading of
  the same fields. That is the argument for filing rather than absorbing it.

SANITISED: no absolute paths, project name, usernames or absolute usage
totals beyond the turn counts needed to show the arithmetic.
-->

# `discover --json`: an unmeasured token total is exported as a bare `0`, while the human output says "unknown rather than zero"

## Summary

7.11.1 fixed #474 by measuring `tool_result` sizes, and correctly documented
the limit that comes with it: turns ingested **before** the upgrade carry 0,
because the sizes come from transcript lines the database does not keep.

The human-readable output states that limit in as many words. `--json` does
not carry it in any form. So the two surfaces disagree about what `0` means,
and the one that cannot read English is the only one that needs the flag.

## Reproduction

7.11.1, against a store whose turns were all ingested before the upgrade.

Human output — correct, and explicit:

```
  912 navigation turns; addressable input tokens ≈ 0.
  Conservative recoverable ≈ 0 (lower bound: 50% of addressable; ...).
  These turns were recorded before tool-result sizes were measured, so their
  addressable total is unknown rather than zero. Turns ingested from now on
  carry it.
```

`--json` for the same run — the sentence is gone and nothing replaces it:

```json
{
  "since": "30d",
  "total_turns": 47209,
  "replaceable_turns": 912,
  "total_addressable_input_tokens": 0,
  "total_recoverable_input_tokens": 0,
  "buckets": [
    { "bucket": "grep", "tool": "Grep", "turns": 57,
      "addressable_input_tokens": 0, "recoverable_input_tokens": 0 },
    { "bucket": "read", "tool": "Read", "turns": 855,
      "addressable_input_tokens": 0, "recoverable_input_tokens": 0 }
  ]
}
```

There is no field distinguishing this payload from one where the tokens were
measured and genuinely came to zero.

## Why this is not cosmetic

A consumer reading this payload has three possible readings of `0` and no
way to choose between them:

1. measured, and there is genuinely no addressable input — a real result;
2. not measured for these turns — the actual case here;
3. the field is absent or the binary is too old — distinguishable, since the
   key would be missing.

Cases 1 and 2 are the same bytes. They lead to opposite conclusions: the
first says "no opportunity here, stop suggesting the graph", the second says
"we cannot tell yet, ask again after more turns are ingested".

This mattered concretely. A downstream consumer had quarantined `discover`'s
token figures since 7.10, on **measured** evidence rather than a version
check — it tested the degenerate identities the old estimate produced
(`total_recoverable_input_tokens == replaceable_turns`, and the same inside
every bucket). An honest `0` satisfies neither identity, so the upgrade
silently moved those figures from *quarantined* to *trusted* while they were
in fact unmeasured. The fix improved the data and, for that consumer, turned
a caught error into an uncaught one — purely because the JSON dropped the
qualifier the human text kept.

## Suggested remedy

Any of these closes it; the first is the smallest.

- A top-level boolean, e.g. `"token_sizes_measured": false`, set when no
  turn in the range carries a recorded `tool_result_tokens`.
- Per-bucket the same flag, since a range spanning the upgrade is partially
  measured and a single top-level boolean would have to round that off.
- Or export `null` rather than `0` for an unmeasured total, which is
  self-describing and needs no new key — at the cost of a type change for
  existing readers.

A `"turns_with_measured_sizes": <int>` alongside `replaceable_turns` would
also work, and has the advantage of quantifying a partially-measured range
instead of collapsing it to a boolean.

## Note on the range-spanning case

The release notes state that a range reaching back before the upgrade
under-reports, and that the command says so. That is true of the human
output. Once ingestion has been running for a while, `--json` will report a
*partial* total — neither zero nor complete — with nothing marking it as
partial, which is the same problem with a harder-to-notice symptom.
