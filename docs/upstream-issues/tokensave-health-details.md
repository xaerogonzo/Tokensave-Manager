# Upstream issue draft — `tokensave_health details=true` sub-score breakdown

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** `tokensave_health: expose sub-score breakdown (depth / acyclicity / coverage / etc.) via a details=true flag`
**Type:** Feature request / API enhancement
**Status:** Draft — review and strip any proprietary code before filing.

---

## Summary

`tokensave_health` currently returns only two top-level numbers:

```json
{
  "files_analyzed": 69,
  "quality_signal": 7003
}
```

The `quality_signal` is a useful single composite score, but it conceals the underlying signals that produce it. Today, getting the breakdown requires composing six separate tool calls (`tokensave_gini`, `tokensave_circular`, `tokensave_recursion`, `tokensave_unsafe_patterns`, `tokensave_dependency_depth`, `tokensave_doc_coverage`) and assembling the result by hand.

Please consider a `details=true` parameter (or a separate `tokensave_health_details` tool) that returns the structured sub-scores in one call.

## Proposed shape

```json
{
  "files_analyzed": 69,
  "quality_signal": 7003,
  "details": {
    "equality":            { "score": 0.57, "interpretation": "high inequality", "source": "gini(lines, file)" },
    "acyclicity":          { "score": 1.00, "cycle_count": 0 },
    "recursion_safety":    { "score": 1.00, "recursive_sites": 0 },
    "unsafe_patterns":     { "score": 1.00, "match_count": 0 },
    "depth":               { "score": 1.00, "max_chain": 3, "ideal_max": 7 },
    "inheritance_depth":   { "score": 1.00, "max_depth": 0 },
    "coverage_discipline": { "score": 0.99, "undocumented_count": 5, "scope": "src/" },
    "modularity":          { "score": "high", "max_fan_in": 5 }
  }
}
```

Exact field names and weight schema are up to the project — the goal is just "expose the components, in one call, without the caller having to re-derive them."

## Why this matters

1. **Reproducibility.** Two runs of `tokensave_health` returning the same `quality_signal` could hide very different underlying changes (e.g. coverage worsened, modularity improved, the two cancelled out). Sub-scores expose this.

2. **Caching.** A single composite call is roughly six times cheaper than six separate tool calls — both in MCP round-trips and in LLM context window consumed by tool-result framing.

3. **Reporting.** The "🏥 Health audit" workflow in TokenSave Manager (and presumably similar workflows elsewhere) currently asks the LLM to call six tools and assemble a breakdown. With this change, one call produces a complete report.

4. **Comparability across runs.** A delta report between two snapshots (`before` vs. `after` a refactor) is trivially derivable from sub-score deltas but ambiguous from a composite delta alone.

## Counterpart suggestion (smaller scope, equivalent benefit)

If `details=true` is too invasive, an alternative is a `tokensave_health_report` tool that internally composes the six existing calls and returns the structured result. Same caller-facing benefit, zero risk to the existing `tokensave_health` contract.

## Calibration note

When implementing, please consider language-specific calibration for `coverage_discipline`:

- **Python:** `tokensave_doc_coverage` currently treats `## Heading` lines in `.md` files as undocumented symbols. For Python projects this inflates the count by 50–100× and the score becomes meaningless without a path filter (`path="src/"`). Either default-exclude `.md` files from doc-coverage scoring or expose a `language_filter` parameter.

- **Tkinter (and similar callback-heavy frameworks):** `tokensave_dead_code` and `tokensave_unused_imports` produce massive false-positive counts because callbacks registered via `command=self._method` and `bind("<<Event>>", self._handler)` aren't traced through the call graph. If `dead_code_count` ends up factoring into `quality_signal`, please consider a callback-pattern aware mode.

## Workaround in use today

Six chained tool calls. See `src/prompts.py` in the TokenSave Manager project (the `📊 Full health audit` snippet) for the exact prompt that drives an LLM through the composition.

## Author note

Filed by a TokenSave Manager user. Project: https://github.com/(your-fork-or-source)
