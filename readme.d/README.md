# README fragments

A pull request does **not** edit `README.md`. It adds one file here. Two PRs add two differently
named files, so they cannot conflict, which is the whole point: the README has one section per
feature, and two PRs that each described theirs in the same section conflicted on it.

```bash
python scripts/readme_fragments.py targets               # the sections you may name
python scripts/readme_fragments.py new short-topic tasks-tab   # prints the new path
# edit it: the first line names the section, the rest is Markdown for the END of that section
python scripts/readme_fragments.py check                 # read-only
python scripts/readme_fragments.py preview               # the README.md `build` would write
```

A fragment looks like this:

```markdown
<!-- readme: tasks-tab -->
A paragraph, a list or a table, written exactly as it should read in the README.
```

- **The first line** is `<!-- readme: KEY -->`, where `KEY` is the key of a `<!-- help:KEY -->` anchor
  in `README.md` (`targets` lists them). The text is appended to the end of that section, before its
  trailing `---` rule. Several fragments for one section are appended oldest first.
- **Filename** `YYYYMMDDHHMM-<slug>-<4 hex>.md` (UTC), the same as changelog fragments. A `.md` file here
  that does not match is an error, because it would never be folded.
- **`check` rejects** an unedited template, an unknown section, a level-1 or level-2 heading (it would
  start a section the anchors do not know about; use `###`), an anchor comment, a merge conflict marker,
  an unclosed code fence, an empty body, and two fragments that say the same thing. It runs in the test
  suite (`tests/test_readme_fragments.py`).
- **Nothing enforces that a PR adds a fragment.** `check` validates the ones that exist; a reviewer
  notices a missing one.
- **`build`** folds each fragment into `README.md` (keeping its line endings) and deletes it. It validates
  everything first, writes the README completely before deleting anything, and makes no commit. If a deletion
  fails, run it again: a fragment whose paragraphs are all already in the README is recognised as folded and
  only removed. A fragment only partly present is an error (a hand edit).
- **Until `build` runs, `README.md` does not show a pending fragment**, and neither does the in-app Help
  when run from a checkout (the Help reads `README.md`). That is the price of never conflicting. Run
  `build` when preparing a release, and commit the result before the Release Wizard (its pre-flight refuses
  a dirty tree).
- This folder is development material and is not shipped (`docs/RELEASE_CONTENTS.md`).
