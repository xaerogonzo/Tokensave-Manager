# Changelog fragments

A pull request does **not** edit `CHANGELOG.md`. It adds one file here. Two PRs add two
differently named files, so they cannot conflict, which is the whole point: every PR used to
insert its entry at the top of the same `### Added` / `### Fixed` group, and the second to
merge had to be rebased.

```bash
python scripts/changelog.py new short-topic-name   # prints the new path
# edit it: one or more "### Added|Changed|Fixed|Removed|Deprecated|Security|Docs|Other" sections of "- " bullets
python scripts/changelog.py check                  # read-only
python scripts/changelog.py preview                # the CHANGELOG.md `build` would write
```

- **Filename** `YYYYMMDDHHMM-<slug>-<4 hex>.md` (UTC). The stamp is the order, newest first; the
  hex keeps two same-slug fragments from independent branches apart.
- **Shape** is exactly what a CHANGELOG entry looks like: `- (area) **Bold lead.** Why it matters.`
  A category may appear once per fragment. Continuation lines and fenced blocks belong to the bullet above.
- **`check` rejects** an unedited template, an unknown or repeated category, a category with no bullet,
  text outside a bullet, an unclosed fence, and a bullet that duplicates another fragment's.
- **`build`** (release preparation) merges each category into its `[Unreleased]` group, newest on top,
  then deletes the fragments. It makes no commit. If a deletion fails, run it again: a fragment whose
  bullets are all already in `[Unreleased]` is recognised as folded and only removed.
- Existing `[Unreleased]` entries are left as they are.
