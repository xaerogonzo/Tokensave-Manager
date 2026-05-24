# Upstream issue draft — `tokensave_redundancy` aggregate tool

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** `Feature: tokensave_redundancy — AST-level functional-duplication detector`
**Type:** Feature request / new tool
**Status:** Draft — review and strip any proprietary code before filing.

---

## Summary

Tokensave currently offers two redundancy-adjacent tools:

- **`tokensave_similar <symbol>`** — name-based fuzzy match. Returns symbols whose names look similar.
- **`tokensave_signature_search <signature>`** — finds symbols whose signature shape matches a pattern (e.g. `(str) -> bool`).

Neither catches **functional duplication** — two functions that do the same thing under different names with different parameter lists. The current workflow for finding actual duplicates is a 5-step LLM-driven chain:

1. `tokensave_similar` on top-complexity symbols (weak first-pass signal)
2. `tokensave_signature_search` for the most common signature shapes
3. `tokensave_node` on every candidate to fetch the body
4. LLM compares bodies side-by-side
5. LLM buckets findings as 🔴 definite / 🟡 likely / 🔵 naming-only

This works but it's expensive (one MCP call per candidate body), unreliable (LLM body-comparison varies by model), and the structural signal (AST overlap) is computed by the LLM from text rather than by tokensave from its own index.

Please consider a server-side `tokensave_redundancy` aggregate tool that does the AST-level comparison at index time.

## Proposed shape

```json
// Call
{ "name": "tokensave_redundancy",
  "arguments": {
    "min_lines": 10,
    "max_pairs": 20,
    "similarity_threshold": 0.7
  } }

// Response
{
  "pair_count": 14,
  "pairs": [
    {
      "similarity": 0.94,
      "severity": "definite",         // definite | likely | naming_only
      "a": { "file": "src/foo.py", "line": 42, "name": "_parse_args" },
      "b": { "file": "src/bar.py", "line": 128, "name": "parse_cli" },
      "overlap_kind": "ast_isomorphic",   // ast_isomorphic | control_flow | algorithmic | naming
      "diff_summary": "identical control flow; renamed loop var x→item; renamed param `raw`→`source`"
    },
    ...
  ],
  "ranked_by": "similarity desc",
  "scope": "src/"
}
```

## Detection heuristics (suggestions, in order of cost)

1. **AST isomorphism** — same node-kind tree shape, normalised over identifier names. Cheap and catches the highest-value cases.
2. **Control-flow graph match** — same branch/loop structure regardless of statement order. Catches reorder-refactor duplicates.
3. **Algorithmic match** — same sequence of stdlib/api calls in the same order, regardless of intermediate naming. Catches "I rewrote this from scratch and didn't notice the helper existed" cases.
4. **Token-shingle match** (last resort) — Jaccard similarity over n-gram shingles of normalised tokens. Catches the long tail.

A composite `similarity: float` blended over these signals is more useful than any one in isolation.

## Why this matters

1. **Code-health audits become accurate.** Right now the "Redundancy" sub-score isn't exposed by `tokensave_health` because there's no aggregate primitive — only per-symbol name/signature matches. A real `tokensave_redundancy` would let `tokensave_health` surface a meaningful redundancy score (see also the sibling issue draft on `tokensave_health details=true`).

2. **Server-side is cheaper than LLM-side.** AST comparison runs in milliseconds against the existing graph; LLM body-comparison costs context tokens AND latency for every candidate pair. Doing it at index time once amortises the cost across every audit.

3. **More accurate than naming overlap.** The `tokensave_similar` + LLM workflow misses functional duplicates with non-overlapping names ("parse_args" vs "extract_options") and false-positives on coincidental naming overlap ("parse_json" vs "parse_csv" are obviously different despite name proximity).

4. **Refactor planning.** With ranked duplicate pairs available, an LLM can suggest a single canonical helper + a deletion path for each pair. Without this primitive, the same workflow requires the LLM to discover the pairs first.

## Workaround in use today

Driven by an LLM prompt — see the `🪦 Redundancy hunt` snippet in `src/prompts.py` in TokenSave Manager. It explicitly buckets findings as `🔴 Definite duplicate (>80% body overlap)` / `🟡 Likely refactor target` / `🔵 Naming overlap only (false positive)` to force the LLM into actual body comparison rather than name matching. Works, but expensive and model-dependent.

## Out-of-scope variants worth flagging

- **Cross-language duplicates** (e.g. a Python helper and its TypeScript twin in a polyglot repo) — out of scope for v1. AST shapes don't align across languages.
- **Semantic duplicates** ("these two functions both implement Levenshtein distance with completely different code") — out of scope; requires LLM-level reasoning. AST/CFG matching is the realistic ceiling.
- **Generated code** (e.g. `protoc`-output Python) — should probably be excluded by default via a `.tokensaveignore`-style mechanism, or at least flagged with `"likely_generated": true` in the response so audits can filter.

## Author note

Filed by a TokenSave Manager user. Project: https://github.com/(your-fork-or-source)
