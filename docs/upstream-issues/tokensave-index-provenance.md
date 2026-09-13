<!--
STATUS: FILED 2026-09-12 as issue #554 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/554

DUPLICATE SEARCH (gh search issues, repo-scoped, 2026-09-12):
  "last_indexed_version reindex minor" (0), "reindex resolver change minor"
  (0), "full index version provenance" (0), "last_indexed_version" (#320 —
  empty version stamp forcing reindex, CLOSED, unrelated), "reindex" (#535,
  #454, #358 — unrelated).
  Adjacent: #342 (OPEN design discussion, auto index management). This is a
  concrete data point for it, not a duplicate.

LOCAL EVIDENCE (not for the issue body):
  Two repositories read last_indexed_version = 7.12.1 minutes after the
  upgrade, while metadata.last_full_sync_at was 2026-09-08 and 2026-08-19.
  The Manager's Doctor rule compared last_indexed_version against the binary
  and went silent. `sync --force` on the 516-file repository then moved:
    production -> tests/ edges: uses 827 -> 31, calls 24 -> 23
    audit-edges unreachable sole-candidate edges: 1,776 -> 797
  The Manager now records its own provenance for full indexes it runs
  (helpers/index_provenance.py) and reports everything else as unknown.
-->

# No record of which version built a project's graph, and a minor release can change resolver output without triggering a reindex

**tokensave 7.11.1 → 7.12.1** · Windows 11 · two Python projects

## Summary

Consumers cannot tell whether a project's graph was built by the running binary.

- **`last_indexed_version` doesn't say.** `run_version_reindex` advances it on patch and minor transitions without re-indexing, as `TOKENSAVE-VERSIONING.md` documents. So it means "last version that evaluated this project", not "version that built this graph".
- **`last_full_sync_at` doesn't say either.** It records when, not which binary.

That matters in 7.12.0. The release changed resolver output (#522, #503, the bare-name reachability gate), shipped as a minor, and therefore reached no existing graph until someone happened to run `sync --force`.

## Measured

- **Right after upgrading:** both projects read `last_indexed_version = 7.12.1`, while their last full syncs were 4 and 24 days old.
- **After `sync --force` on the larger project (516 files):**

| measure | before | after |
|---|---:|---:|
| production → `tests/` edges, `uses` | 827 | 31 |
| production → `tests/` edges, `calls` | 24 | 23 |
| `audit-edges` unreachable sole-candidate edges | 1,776 | 797 |

So for the time between upgrade and full re-index, every graph answer on that project described 7.11's resolver while its config said 7.12.1.

## The ask

Either would close this. The first seems cheapest and useful regardless.

1. **Record provenance.** Store the version that performed the last full index, e.g. `metadata.last_full_index_version` next to `last_full_sync_at`, and expose it in `status --json`. Consumers could then say "graph built by X" as a fact rather than an inference.
2. **Classify resolver or extractor output changes as reindex-requiring.** `TOKENSAVE-VERSIONING.md` already says a change that "alters the meaning of existing data" is a major. A release whose edges differ for an unchanged tree seems to fit. An explicit `EXTRACTOR_VERSION` (like the schema's `LATEST_VERSION`) would make the reindex automatic without spending a major version on it.

## What this is not

- **Not a complaint about the patch/minor marker advance itself.** It is documented, and it stops re-evaluation on every start (#320 shows the cost of getting that wrong).
- **Not about incremental sync correctness.** An incremental sync is not expected to re-resolve untouched call sites.
