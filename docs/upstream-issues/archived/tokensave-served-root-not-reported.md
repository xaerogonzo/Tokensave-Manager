# No surface reports which project the server is actually serving

STATUS: CLOSED — verified via GitHub API 2026-08-29

**Version observed:** tokensave 7.10.0, Windows 11
**Relationship to prior issues:** follow-up to #372 (a fifth ask it did not
cover), adjacent to #368 and #396, not a duplicate of any.

---

## Summary

A `serve` with no `-p` resolves its project by an upward search from the
invoking process's cwd — #372 established this ("the upward search only fires
when no path is given at all, i.e. when the invoking process's cwd is trusted
implicitly, exactly the shape of a bare `tokensave.exe serve` MCP registration
with no `-p`").

That is reasonable behaviour. The gap is that **nothing in any default-path
output names the project that was chosen.** An MCP client that spawns `serve`
with a cwd the operator did not expect gets a server bound to a different
indexed project, and every answer it returns is about the wrong codebase while
looking entirely normal.

`strict_tree` (#372 ask 2, shipped in 7.10.0) does not cover this case, because
there is no mismatch to detect: the server resolved a real, correctly-indexed
project and is serving it faithfully. Nothing is wrong from tokensave's point
of view. The wrongness is only visible to the operator, and the one fact that
would make it visible is not reported.

## The asymmetry

A **selected** call announces its root:

```
tokensave_context(task=..., graph_root="D:\Work\ProjectB")
→ tokensave_graph: root="D:\Work\ProjectB" branch="single-db" read_only=true
```

A **default** call announces nothing. And `tokensave_status` — the tool that
`strict_tree` deliberately exempts, on the grounds that a refused caller needs
it to understand the refusal — returns no path field at all:

```
node_count, edge_count, file_count, nodes_by_kind, edges_by_kind,
db_size_bytes, last_updated, total_source_bytes, files_by_language,
last_sync_at, last_full_sync_at, last_sync_duration_ms, version,
server{...}, active_branch, stale_files, sibling_projects
```

`active_branch` is there. `sibling_projects` lists *other* projects' absolute
paths. The served project's own root is the one path not present.

The CLI is the same. `tokensave status` prints Files / Nodes / Edges / DB Size
/ Branch / sync ages, and no project path or name:

```
│    TokenSave v7.10.0                                               │
│           Project ~12.2M  All projects ~14.2M                      │
│                        Last sync 10m ago (647ms)  Full sync 2d ago │
│                                                Branch: [single-db] │
├──────────────────────┬──────────────────────┬──────────────────────┤
│ Files            288 │ Nodes          8,971 │ Edges         10,146 │
```

So the only way to determine which project a session is bound to is to compare
`file_count` against what you expect, or to inspect the OS process table for
the server's cwd.

## How it showed up

An agent session was observed answering about a different indexed project than
the working directory it was launched against. Determining *which* project it
had bound to required listing OS processes and correlating parent PIDs — there
was no answer available from tokensave itself. Two plausible explanations were
proposed and both were falsified by inspection, and the actual cwd the client
used could not be recovered after the fact, precisely because nothing recorded
it.

We are not reporting the binding itself as a defect — per #372 it is the
documented consequence of trusting cwd, and the client's choice of cwd is not
tokensave's responsibility. The report is that the choice is unobservable.

## Suggested fix

1. **Add the served root to `tokensave_status`** — one field, e.g.
   `"project_root": "<absolute path>"`, alongside the existing `active_branch`.
   This alone closes it: it makes the exemption `strict_tree` grants
   `tokensave_status` actually deliver what the exemption is for, and gives
   agents a way to self-check before trusting an answer.
2. **Name the project in the CLI `status` panel**, next to `Branch:`.
3. Optionally, mirror the `tokensave_graph:` header on default calls, or emit
   it once per session on first use, so a wrongly-bound server is visible
   without an explicit check. (Lower priority — 1 is the load-bearing one, and
   a per-call header costs tokens on every response.)

## Why it matters

This is the same failure mode #372 closed with: *"the tool never signals
uncertainty — it answers with the same confidence as a correct hit."* #372
addressed the case where tokensave can detect the problem. This is the case
where it cannot detect anything, and the operator can — but only if told which
project is being served.

Paths in this report are generic stand-ins; the shapes are as observed.

---

## Confirmed again 2026-08-26, with a cheaper identification trick

This issue predicted the failure exactly, and it happened three times before
being believed. A session in `Token Save Manager Source` was answered from
`OpenChem Studio` for its entire length. What made it so hard to diagnose is
precisely what this issue describes: **every answer looked normal.**

| Source | Files | Nodes | `db_size_bytes` | Branch |
|---|---|---|---|---|
| `tokensave_status` over MCP | 741 | 24,530 | 93,863,936 | `joback-thermophysical` |
| `tokensave status` over CLI | 307 | 9,813 | 35,635,200 | `Roadmap-11` |

Two workarounds worth recording until a served-root line exists upstream:

- **`db_size_bytes` identifies the tree.** It matched
  `OpenChem Studio\.tokensave\tokensave.db` byte for byte, which turned a
  suspicion into a fact in one step. `active_branch` is the cheaper first
  check — a branch the repository does not have is conclusive on its own —
  but a shared branch name like `main` will not discriminate, and
  `db_size_bytes` will.
- **`uptime_secs` resetting while `last_sync_at` and the node count stay
  byte-identical** means the server restarted and reopened *the same* wrong
  database. That distinguishes "my re-sync did not reach it" from "my re-sync
  did nothing", which are otherwise indistinguishable from the client.

The local cause here was not an unexpected cwd but an MCP registration that
resolved a *pin* — Claude Desktop spawning one app-level wrapper shared by
every session (see `docs/MCP_INTEGRATION_GOTCHAS.md`). That is a client-side
problem and has been fixed client-side. It does not change the ask: **a
default-path server should name the project it resolved**, because no amount
of client correctness makes a silent wrong-tree answer detectable from the
inside.
