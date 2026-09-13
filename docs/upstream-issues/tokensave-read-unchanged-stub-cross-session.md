<!--
STATUS: FILED 2026-09-13 as issue #556 - awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/556

DUPLICATE SEARCH (gh search issues, repo-scoped, 2026-09-13):
  "tokensave_read unchanged" (0), "unchanged stub" (0), "cross-session cache
  read" (0), "tokensave_read" (#553, #540, #535, #204 - unrelated),
  "read cache" (#293, #169, #377, #474, #472 - unrelated).

LOCAL EVIDENCE (not for the issue body):
  OpenChem Studio session 6e1675e4, tokensave 7.12.1, 2026-09-13.
    04:01:17  tokensave_read {file: domain/molecule.py, mode: map}   -> body
    04:01:21  tokensave_read {file: domain/molecule.py}  (full)
              -> {"unchanged": true, digest db970f61..., token_count 555}, no body
    04:02:21  the agent re-read the same file with Claude Code's Read.
  The session had never received that body. The same session's first two
  calls passed `path` (-> "missing required parameter: file") and one passed
  start/end for mode=lines. After those three failures and the empty stub it
  used Read for the rest of the session. The Manager's baseline now spells
  out the argument shape (templates/project-baseline.md).
-->

# `tokensave_read` returns `unchanged: true` with no body to a session that never received the file

## What happens

`tokensave_read` is cross-session cached: a re-call on an unchanged file returns a stub with `"unchanged": true` and no content. The stub is returned even when the calling session has never received that file's body.

Observed on 7.12.1, Windows, Claude Code, in a fresh session:

1. `tokensave_read {"file": "<pkg>/domain/molecule.py", "mode": "map"}` returns the symbol map.
2. `tokensave_read {"file": "<pkg>/domain/molecule.py"}` (mode `full`) returns:

   ```json
   {"unchanged": true, "file": "<pkg>/domain/molecule.py", "mode": "full",
    "mtime_ns": 1785381273434885900, "digest": "db970f61...", "token_count": 555}
   ```

   The session had not read this file in `full` mode. An earlier session may have.
3. The agent has no body and no documented way to ask for one, so it reads the file with the client's own file-read tool instead.

## Why it matters

The stub only saves tokens if the content is already in the caller's context. A context is per session, and a new session, a compacted conversation or a subagent does not hold what an earlier one received. For those callers the stub is a failed read. It also teaches the agent that `tokensave_read` is unreliable: in the session above it did not call `tokensave_read` again for whole files.

## Ask

Either of these would fix it:

- Return `unchanged` only when **this MCP connection** already sent that file's body in that mode (and a `lines` range only when it sent that range).
- Or accept an argument that opts out, e.g. `force: true`, or `if_digest: "<digest>"` so the client states what it holds and gets the body when it does not match.

A smaller, separate improvement from the same session: the error for a call passing `path` could suggest `file` (`missing required parameter: file (got 'path')`). Clients that call a deferred tool without loading its schema guess `path` first.
