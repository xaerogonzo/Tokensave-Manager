# Upstream issue — worktree index resolution silently answers from the wrong checkout

**Repo:** https://github.com/aovestdipaperino/tokensave
**Title:** Project-path resolution: worktree mismatch answered silently, plus `.` and case-variant drive letters registered as distinct projects
**Type:** Bug / footgun — correctness
STATUS: PARTIALLY FIXED in tokensave v7.9.0 (sections 3 and 4 only)
**Filed:** ✅ 2026-08-05 — https://github.com/aovestdipaperino/tokensave/issues/372 (still OPEN)

## Resolution status per section (updated 2026-08-12, tokensave v7.9.0)

| Section | Ask | Status in v7.9.0 |
|---|---|---|
| 1 | `init`'s already-initialized exit signal is ambiguous | ❌ Unchanged |
| 2 | Opt-in strict mode for the worktree-mismatch case | ❌ Unchanged |
| 3 | A literal `.` is registered as a project name | ✅ Fixed |
| 4 | Case-variant Windows drive letters duplicate rows | ✅ Fixed |

Sections 3 and 4 were fixed by canonicalising project paths at the global-DB
boundary. Every key written to or read from `~/.tokensave/global.db` —
`projects.path` and `savings_ledger.project_path` alike — now goes through
`global_db::normalize_project_key`, which resolves the path on disk (collapsing
`.`, `./`, `../name`, symlinks and trailing separators), strips Windows `\\?\`
verbatim prefixes, and upper-cases the drive letter so `d:\...` and `D:\...`
land on one row. A one-time migration merges existing duplicate spellings by
summing `tokens_saved`, re-points ledger rows at the canonical key, and drops
rows keyed by a relative path. A companion fix makes `tokensave init .` resolve
the relative argument instead of persisting `.` as the project key.

Upstream states plainly: *"This addresses sections 3 and 4 of #372; the
worktree-mismatch strict mode (section 2) and `init`'s already-initialized exit
signal (section 1) are unchanged."* Issue #372 therefore remains OPEN, and the
original correctness problem this document is named for — a session inside a
worktree getting confident answers from a different checkout — still stands.

Related but not a fix for section 2: v7.9.0 also made the PreToolUse hook
discover its project by walking ancestors rather than requiring
`.tokensave/tokensave.db` in the hook process's own working directory. That
makes the guardrail *activate* in more places; it does not make a mismatched
tool call refuse to answer.

> The filed version is this document with paths anonymised
> (`D:\Projects\MyProject`, `D:\Work\ProjectA`, `D:\Projects\ProjectB`) —
> real project names and directory layout were stripped before submission.
> Sizes and token counts went out verbatim, since they're the evidence for
> asks #3 and #4. This local copy keeps the real paths for our own reference.

> Four related asks, all rooted in how a project path is resolved and then
> recorded in the global DB. Sections 1–2 are the original correctness
> problem; 3–4 are global-DB hygiene bugs found while working on it. They
> could be filed separately, but they share a root cause (paths are used
> as-provided rather than canonicalised) so they are grouped here.

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

## Four independent asks

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

### 3. A literal `.` is registered as a project name in the global DB

Running any indexing command with `.` as the path argument from inside a
project registers the **literal string `.`** as a project in the global DB,
rather than resolving it to an absolute path first:

```
$ cd <some project> && tokensave status .
...
$ tokensave list -a
Found 14 tokensave project(s):
  ...
  .                                            43.3 MB           — tokens
```

That row is permanently ambiguous — it records a size but no recoverable
identity, since `.` means something different from every directory. It also
can't be cleaned up: `doctor`'s stale-purge only removes rows whose
`.tokensave/` is *gone*, and this one resolves to a real indexed directory
from wherever `doctor` happens to run, so it's never considered stale.

**Suggested fix:** canonicalise the path argument (`realpath`/`canonicalize`)
before it's written to the global DB. `.` and `./` and `../<name>` should all
land on the same absolute row as an explicit absolute path would.

### 4. Case-variant Windows drive letters create duplicate global-DB rows

On Windows, `d:\foo` and `D:\foo` are the same directory, but tokensave
records them as two separate projects with independently-accumulated token
counts:

```
  d:\Claude Co worker\Token Save Manager Source     43.3 MB    14.4M tokens
  D:\Claude Co worker\Token Save Manager Source     43.3 MB    12.2M tokens
  D:\Random Projects\KicomAI_Project                11.0 MB   107.4k tokens
  d:\Random Projects\KicomAI_Project                11.0 MB   107.4k tokens
```

Confirmed identical directories — `os.path.samefile()` returns `True` for the
pair, and `os.path.normcase()` makes them equal. Note the *differing* token
counts on the first pair (14.4M vs 12.2M): reported savings are being split
across the duplicates rather than accumulated on one project, so the
`tokensave gain` / worldwide-counter totals under-report per project and the
`list -a` disk-usage total double-counts.

Same root cause and same fix as #3: normalise the path (on Windows,
case-fold the drive letter at minimum — `normcase`-equivalent) before using
it as the global-DB key. A migration that merges existing case-variant rows
would be a nice-to-have; even just preventing new ones would stop the drift.

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
- Asks #3 and #4 are Windows-observed; #3 (`.` as a project name) looks
  platform-independent, #4 (drive-letter case) is Windows-specific but the
  same class of bug could appear anywhere paths are keyed without
  canonicalisation (e.g. symlinked or trailing-slash variants on POSIX —
  not verified).

## Author note

Filed from TokenSave Manager. Paths were anonymised before submission (see
the status note at the top); everything else went out as written here.
