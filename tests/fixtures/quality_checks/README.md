# quality_checks parser fixtures

Real captured output from `python -m pyflakes` and `python -m compileall -q`,
held as fixtures rather than reconstructed in test code. Every one of these was
captured by running the tool; none was written from memory.

**Provenance:** CPython 3.13.12, pyflakes 3.4.0, Windows, 2026-08-27. The
invocation matches what `helpers/quality_checks.py` builds — the `src/`
directory as an absolute path, `cwd` = the project root.

## Placeholders

| Token | Substituted with |
|---|---|
| `<PROJECT>` | the project root, native separators, as the tool printed it |
| `<PROJECT_ESC>` | the same root with every backslash doubled |

Both appear **in the same compileall block**: the `*** Error compiling '...'`
header prints a `repr()` of the path, so its separators are escaped, while the
`File "...", line N` line below it prints them raw. That is not a typo in the
fixture — it is the format, and a parser that assumes one form breaks on
whichever block it did not expect.

## Why each file exists

These are the cases that a parser written from a remembered format gets wrong:

- **`compileall_no_file_line.txt`** — a block with **no `File "..."` line at
  all**. The null-bytes error is raised before the parser can report a
  position, so the header path is the only path available and there is no line
  number. A parser that requires the `File` line drops the finding entirely.
- **`compileall_multichar_caret.txt`** — the caret can span several characters
  (`^^^^^^^^`), which yields an **end** column, not just a start.
- **`compileall_many.txt`** — three blocks, blank-line separated. Associating
  each header with the block below it is where a state machine goes wrong.
- **`pyflakes_stderr_no_column.txt`** — pyflakes writes *this* form to
  **stderr**, and it carries **no column**: `path:line: message`, not
  `path:line:col: message`. `run_pyflakes_check` concatenates stdout and
  stderr, so the parser sees both shapes in one string.
- **`pyflakes_posix.txt`** — forward slashes, which is what CI
  (`ubuntu-latest`) actually produces. The Windows fixtures alone would let a
  separator assumption through.
- **`compileall_path_with_space.txt`** — this project lives under
  `D:\Claude Co worker\...`, and a space in a path has already cost it one
  upstream bug report.

Note that the pyflakes line format contains a colon in the path itself on
Windows (`C:\...`), so splitting on `:` does not work; match on the first
`:<digits>:<digits>: ` instead.

## Regenerating

Re-capture rather than hand-edit if a tool version changes. Run each tool
against a scratch tree, then replace the machine-specific root prefix with the
placeholders above.
