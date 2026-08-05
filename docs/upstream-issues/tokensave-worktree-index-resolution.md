# Upstream issue — worktree index resolution silently answers from the wrong checkout

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** `tokensave_*` MCP tools should refuse (or `init` should error non-zero) rather than silently detect+ignore a worktree mismatch
**Type:** Bug / footgun — correctness
**Status:** Draft — review and strip any proprietary code before filing.

---

## Summary

A Claude Code session started inside a git worktree gets confident,
well-formed `tokensave_*` answers about a **different checkout** — with no
error. tokensave already detects this precisely (every call carries a
`worktree_mismatch` block, and prints a warning naming both `worktree_root`
and `index_root`), but the tools answer anyway.

Reproduced with tokensave 7.8.1 on Windows 11, inside a project's own
`.claude/worktrees/<name>` worktree (Claude Code's default worktree
location — physically nested inside the main repo, `.claude/` gitignored):

```
$ tokensave list
Found 1 tokensave project(s):
  D:\Random Projects\OpenChem Studio     29.1 MB    210.2k tokens
```

That's the **main checkout's** project — `tokensave_search` for a class
written minutes earlier in the worktree returned `[]`; the graph returned the
main checkout's version of files that had been rewritten, at line numbers
that no longer existed in the worktree's tree.

## Why it happens

tokensave resolves its project by searching **upward** from cwd for
`.tokensave/`. A fresh worktree has none, so the search walks past the
worktree boundary and finds a sibling/ancestor project's index — silently,
successfully, with no error.

**Confirmed as the specific trigger**: passing an **explicit** path (absolute
or a literal `.`) does NOT search upward — `tokensave status <path-with-no-index>`
correctly reports `No TokenSave index found at '<path>'. Create one now?`
rather than falling back. The upward search only fires when no path is given
at all, i.e. when the invoking process's cwd is trusted implicitly (exactly
the shape of a bare `tokensave.exe serve` MCP registration with no `-p`).

## Two independent asks

### 1. `init`'s no-op behavior on an existing index is inconsistent and easy to trust wrongly

Observed **exit 1** running `tokensave init <path>` non-interactively
(explicit path, closed stdin) against an already-initialized project:

```
error: TokenSave is already initialized at '<path>'.
Use tokensave sync to update the index, or tokensave sync --force to rebuild it.
```

This is a *good* outcome — but it was reported to us as exit **0** with a
silent-looking message in an interactive/TTY session, which cost real
debugging time (a stale index sat unnoticed for several minutes because a
caller trusted `init`'s exit code as proof a rebuild had happened). Whether
this is genuinely TTY-dependent or was a version difference, the fix is the
same either way: **make "already initialized, nothing done" structurally
unambiguous** — non-zero exit unconditionally, in every invocation mode, or
have `init` auto-upgrade to the rebuild the message already suggests
(`sync --force`) rather than requiring a second command. Right now a caller
has to pre-check for `.tokensave/` themselves before deciding whether to call
`init` or `sync --force` — which works, but means `init`'s own signal can't
be trusted for this and has to be worked around.

### 2. An opt-in strict mode for the mismatch case

The `worktree_mismatch` detection is already excellent — `worktree_root` /
`index_root` are exactly the right two facts. The only gap is that it's
advisory. A `--strict` flag (or a `tokensave.toml` setting) that makes
`tokensave_*` MCP tools **refuse** with a clear error when a mismatch is
detected, instead of answering with the other tree's data, would let
worktree-heavy workflows opt into "wrong answer is worse than no answer" —
which it demonstrably is here, since every downstream tool built on top of
tokensave (agent rules that say "always check tokensave before reading
files," for instance) inherits the wrong-tree answer with no signal
anything is off.

## Why it matters

Any wrapper or agent-rule that treats tokensave as the trusted source of
truth for "does this symbol exist / what does this file say" (which is the
documented, intended usage) inherits the wrong-tree answer silently. It's
worse than having no index at all, because the tool never signals uncertainty
— it answers with the same confidence as a correct hit.

## Environment

- tokensave 7.8.1
- Windows 11
- Claude Code worktree at `<repo>/.claude/worktrees/<name>`, no `.tokensave/`
  of its own, MCP server registered globally with no `-p`/`--root`

## Author note

Filed by a TokenSave Manager user. Strip any proprietary code paths from
repros before submitting — the details above are file paths and public
symbol names from the reporter's own projects only.
