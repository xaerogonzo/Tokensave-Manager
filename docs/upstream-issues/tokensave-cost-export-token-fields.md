<!--
STATUS: FILED 2026-08-29 as issue #472 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/472
  (the exported token fields cannot account for the exported cost)

Filed against tokensave 7.10.0. Searched for duplicates first (gh search
over all issues): none. #457 is the same CLASS of bug — a count whose name
promises more than it delivers — but unrelated code.

SANITISED BEFORE FILING: absolute usage totals were replaced with rates and
ratios, which carry the whole finding and disclose nothing about this
machine's volume or spend. The filed body was re-read from GitHub and
confirmed to contain no paths, project names, usernames or usage totals.
This file is the local source of truth; the issue is what was published.
-->

# `cost --export json`: the exported token fields cannot account for the exported cost, and no cache-token field exists

## Summary

`tokensave cost --export json` publishes `total_input_tokens`,
`total_output_tokens` and `total_cost_usd`, but the cost cannot be derived from
the tokens — it implies a price per million tokens several times any published
Anthropic rate. The tool's own `--by-agent` view reports a token count for the
same window roughly **600× larger** than the JSON export's.

The most likely explanation is that the cost is computed from full usage
records (including cache-read and cache-creation input tokens) while the export
publishes only uncached input. If so, the export is missing the fields that
would make it self-consistent, and there is currently no way for a consumer to
reconstruct them.

This matters because a consumer displaying these fields side by side shows a
user four numbers that cannot be reconciled by arithmetic, and has no way to
explain why.

## Reproduction

Any project with recorded agent sessions:

```bash
tokensave cost 7d --by-agent
tokensave cost 7d --export json
```

Compare the `Raw tokens` column from the first against
`total_input_tokens + total_output_tokens` from the second.

## What is observed

**1. The cost totals are internally consistent.** In every range,
`total_cost_usd`, `sum(by_model[].cost)` and `sum(by_category[].cost)` agree to
the last decimal place. Whatever is wrong is not a summation error.

**2. `sum(by_model[].tokens)` equals `total_input_tokens + total_output_tokens`
exactly — the difference is 0 in every range tested** (`today`, `7d`, `30d`,
`all`). So `by_model[].tokens` carries no token category that the two totals do
not, and there is nowhere in the payload for cache tokens to be hiding.

**3. The implied price per million tokens is far above any published rate.**
Dividing `total_cost_usd` by the token total gives roughly **$270–$360 per
million tokens**, varying by range. Per model it is the same story — a
`claude-opus-5` row implies about **$359/Mtok**. Anthropic's published Opus
output price is an order of magnitude below that, and input lower still.

**4. `--by-agent` and `--export json` disagree about token volume by roughly
600×** for the same range, while agreeing on cost to within rounding. One view
reports raw tokens three orders of magnitude above the other's total.

**5. The output:input ratio is backwards for agent traffic.** The export shows
roughly **455 output tokens per input token**. Agent workloads are strongly
input-heavy — a large context is resent every turn and the completion is
comparatively small. A ratio in that direction is only possible if most input
is not being counted.

Taken together, (2), (4) and (5) point the same way: the input side of the
export is missing the cached tokens that dominate real agent usage, and the
cost — correctly — is not.

### Observed (tokensave 7.10.0, Windows, Claude Code sessions)

Absolute totals are omitted deliberately — they describe one machine's usage
and none of the findings depend on them. Every figure below is a rate or a
ratio, and reproduces on any dataset:

| range | implied $/Mtok | `sum(by_model.tokens) − (input + output)` | totals reconcile |
|---|---|---|---|
| today | ~$301 | 0 | yes |
| 7d | ~$359 | 0 | yes |
| 30d | ~$331 | 0 | yes |
| all | ~$271 | 0 | yes |

For one 7d window, `--by-agent`'s raw-token count was **~599×** the JSON
export's `input + output`, while the two cost figures agreed to within a
fraction of a percent (the residual being live ingestion between the calls).
Per model, a `claude-opus-5` row implied **~$359/Mtok**; output:input in the
export was **~455:1**.

## Why it is hard to work around

A consumer cannot compute cache tokens itself. The obvious derivation —
`sum(by_model.tokens) − (total_input_tokens + total_output_tokens)` — is
**provably zero on every payload**, per observation (2). So a UI that tries to
show a cache-read figure will confidently render `0`, which is worse than
showing nothing: it asserts there was no caching when the cache-hit column in
`tokensave cost`'s own human table reads 100%.

## Suggested fix

Either of these would make the export self-describing; the first is preferable
because it is additive:

1. **Add the missing token categories to the export**, e.g.
   `total_cache_read_input_tokens` and `total_cache_creation_input_tokens` at
   the top level and per `by_model[]` entry, so that
   `input + cache_read + cache_creation + output` accounts for the tokens the
   cost was computed from.
2. **Or document that `total_input_tokens` excludes cached input**, and expose
   whatever total the cost *is* computed from (the `--by-agent` "Raw tokens"
   figure appears to be it), so a consumer can at least explain the
   discrepancy rather than appear to be miscomputing.

Either way, a note in `--help` that the token fields and the cost field are not
on the same basis would prevent the next consumer from assuming they are.

## Environment

* tokensave 7.10.0 (stable channel)
* Windows 11, Claude Code sessions
* Reproduced across four ranges in a single session; figures drift slightly
  between consecutive calls because `cost` ingests new rows as it runs.
