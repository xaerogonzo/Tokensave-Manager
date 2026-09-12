<!-- help:pyscope -->
# PyScope integration

PyScope is an **optional peer** of tokensave and CodeGraph. This file states the
rules the integration is built on, once, so a future change can be checked
against them rather than against a guess at what was intended.

## Why a third tool

tokensave and CodeGraph answer *where things are and what touches what*.
PyScope answers *how much of that is actually established* — its three
orthogonal axes (`Confidence` / `Dispatch` / `Completeness`) plus a closed
`UnresolvedReason` vocabulary saying where its resolver stopped.

That is not a nicer version of the same answer. Measured on PyScope's own
source tree, **4,191 of 9,906 relationships are `unknown`** — so roughly 43% of
what any code graph displays was never proved by anything. An assistant reading
a graph cannot tell a proved call edge from a name that happened to match, and
PyScope is the only one of the three that counts the difference.

## What "peer" means here

Peer at the **Manager's surface layer**: Settings section, Projects cascade,
Tool Manager row, Ask-tab tools, doc grounding, Doctor, Help.

Explicitly **not** a claim of identical lifecycle, installation model, MCP
scope, or project semantics. PyScope differs in all four, deliberately, and each
difference is stated in the UI rather than implied:

| | tokensave / CodeGraph | PyScope |
|---|---|---|
| Install | the Manager can install it | it cannot; the row says so |
| MCP scope | project-scoped | **user-scoped, correctly** |
| Wiring | one action | registration **and** an MCP entry |
| Per-project state | an index on disk | a registry entry + a cache location |

## The invariants

1. **Each application is complete without the other.** Removing the peer's
   binary removes an optional surface, never a working flow.
2. **`register()` is the only Manager→PyScope mutation, and no automatic flow
   calls it.** Explicit user action only.
3. **No partial data is authoritative — but a diagnostic surface still says
   which failure it was.** The read APIs collapse every unusable outcome to
   `None`/`[]`/`False`; `status()` keeps the reason. "Not installed" and
   "installed and crashing" send a user to two different fixes.
4. **MCP wiring and project registration are separate states.** Neither implies
   the other, and binding them is not a transaction.
5. **Integration crosses public interfaces only.** PyScope never reads
   `manager-config.json`; the Manager never reads PyScope's registry file or
   cache database; and **neither imports the other's internal Python modules**.
   The Manager talks to PyScope through its CLI. If an answer is not exposed
   there, the fix is a PyScope CLI addition — not a shortcut into `.pyscope/`.
6. **Tokensave facts inside PyScope stay tokensave's.** (Part 2, not yet built.)
   They must never become PyScope's own resolution.
7. **Analysis tools and coding-agent CLIs are two registries and stay two.**
   tokensave / CodeGraph / PyScope are *not* rows in `helpers/agent_cli.py`;
   that table describes CLIs by how they carry a prompt, and PyScope carries
   none.

## Where things live

| File | Role |
|---|---|
| `helpers/pyscope.py` | The integration client. ONE `_run` subprocess boundary |
| `helpers/mcp_pyscope.py` | The user-scoped MCP entry + the two-state reconciler |
| `dialogs/settings_pyscope.py` | `PyScopeSection` — three status rows, no install |
| `controllers/pyscope_ctrl.py` | Analyze / Open / Register / Bind / Status |
| `helpers/detection.py` | `_detect_pyscope`, `_is_pyscope_project` |
| `helpers/doc_grounding.py` | `build_pyscope_block` — the "how much is proved" caveat |
| `agent_tools.py` | `pyscope_confidence`, `pyscope_graph` (gated on the exe) |
| `helpers/doctor_rules.py` | `audit_pyscope_cache` — a recommendation, not a warning |

## Three things that are easy to get wrong

**Configured is not executable is not healthy.** `if cfg.pyscope_exe:` proves a
path is *configured*. It does not prove the file launches, and launching it does
not prove PyScope answers. Every surface reports these separately; do not
collapse them into "installed".

**A user-scoped MCP entry is correct here.** Everywhere else in this manager it
is the shadowing hazard from the desktop-scope-collision and trust-gate
findings. `EffectiveScope.is_shadowed` returns `True` for *any* user scope and
its own docstring admits it cannot tell "shadowed" from "never bound" — which is
right for a project-scoped server and backwards for this one. The exception
lives in `mcp_scope.USER_SCOPED_SERVERS`, keyed on the **server**; a predicate on
scope alone would silently reclassify tokensave. A four-row matrix test locks it.

**`.pyscope/` in `.gitignore` is functional, not cosmetic.** PyScope writes
`<project>/.pyscope/` only when git ignores it, and otherwise keeps the analysis
in per-user app data. Removing the baseline pattern does not merely leak a
directory into `git status` — it moves the cache.

## Verifying it degrades

Invariant 1 is tested, not assumed. Every surface has a unit test for its
PyScope-absent path: `status()` reports `absent`, `build_tools()` omits both
tools, the controller offers Settings instead of running, `audit_pyscope_cache`
returns nothing, and the Tool Manager row renders "not installed" with `Locate…`
still enabled. For a live check, defeat *detection* rather than editing config:

```bash
env -u PATH USERPROFILE=/tmp/nohome PATH=/usr/bin python src/app.py
```
