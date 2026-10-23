# Parallel sessions all edit the same changelog line

Harvested from OpenChem Studio (PR #432) and the TokenSave Manager port (PRs #93, #94).
Every PR inserted its entry at the same place, the top of `## [Unreleased]`, so
whichever merged second conflicted, was rebased, and waited for CI again. Measured in
OpenChem: 182 commits touched the file in three weeks. Running several agent sessions
on one project makes this constant, not occasional.

> **A PR adds a file. It does not edit a shared one.**

---

## 1. The pattern

A PR adds `changelog.d/<YYYYMMDDHHMM>-<slug>-<4 hex>.md` and never touches
`CHANGELOG.md`. Two PRs add two differently named files, so they cannot conflict.

- **Order is the filename stamp**, newest first. It needs no git history, so it is the
  same in a shallow CI clone and for an uncommitted file.
- **Create with an exclusive open** and retry on a name clash; a "create" that can
  overwrite loses someone's work. The hex suffix is what keeps two same-slug fragments
  from two branches apart.
- **`check` is read-only and strict:** reject the untouched template, a missing or
  repeated heading, a bullet-less section, text outside a bullet, an unclosed code fence,
  and a bullet that duplicates another fragment's. Fenced blocks hide their lines from
  every structural rule.
- **Match the shape of the changelog you have.** OpenChem's takes one `### Title` per
  fragment. A changelog grouped by `### Added` / `### Fixed` takes category sections and
  merges each into the matching group. Do not port the tool unchanged and then
  discover the shapes differ.

## 2. Decide where a fragment becomes release notes, before wiring anything

Writing a tool that folds fragments into `[Unreleased]` is the easy half. The half that
goes wrong is the release: if your release flow generates its own notes and then inserts
a versioned section, the folded fragments are duplicated, stranded under `[Unreleased]`,
or missing. Pick one path and state it:

- **Fold then release:** `build` merges fragments into `[Unreleased]`, a person reviews
  and commits, and the release step starts from a clean tree.
- **Consume at release:** the release flow drafts its notes *from* the fragments, writes
  the versioned section, and removes the fragments **in the same commit**.

## 3. Consuming a fragment must be committed with the section

If the changelog is written and the fragment deletions are not committed, the remote
still has the fragments and the next release processes them again. The commit must hold
the changelog **and every deletion**, with the path set taken from the operation, not
from a filename pattern. Use `git rm` for the deletions: it stages and removes together
and fails as a whole, so a failure leaves every fragment on disk and a re-run drafts the
same notes.

## 4. Write-then-delete needs a recovery rule

A tool that writes the changelog and then deletes fragments can fail partway. A re-run
then inserts the already-folded content a second time. Recognise a fragment whose
bullets are **all** already present as folded and only remove it; treat a partly present
one as an error (it is a hand edit, not a crash). Test the failure path with a delete
that fails for one file.

## 5. Details that bit

- **Line endings:** a CRLF changelog must stay CRLF. A patcher that reads with universal
  newlines and writes `\n` turns the first release into a diff of every line.
- **The legacy section:** entries already under `[Unreleased]` are left as they are. If
  the release step inserts its heading directly under `[Unreleased]`, those entries are
  promoted into the release; re-running the step after a failure drops them, so recover
  by restoring the files first.
- **Nothing enforces that a PR adds a fragment.** `check` validates the ones that exist;
  a reviewer notices a missing one. State that, so nobody believes the gate is stronger
  than it is.
- **Where the rule lives:** the project's own instructions carry the commands. The shared
  baseline carries only the one-line principle; it loads in every project, including
  ones that never adopt fragments.

Reference implementation: TokenSave Manager's `src/helpers/changelog_fragments.py`
(stdlib only, one file, launcher `scripts/changelog.py`), adapted from OpenChem's
`tools/changelog.py`. Copy and adapt it; do not import across projects.
