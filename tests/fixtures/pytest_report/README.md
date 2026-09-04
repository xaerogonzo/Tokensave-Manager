# pytest_report fixtures — captured, never hand-written

These back `tests/test_pytest_report.py`. They are **real pytest output**,
captured from a throwaway project built to carry every awkward case at once,
and they exist because this repository has already learned what happens when a
tool's output format is written from memory: Roadmap-13 shipped three parsers
against three "obvious" formats and all three were wrong, each corrected only
by a captured sample.

## The one edit made to them

The machine's absolute project path was replaced with `/proj`, the interpreter
path with `/python`, and the XML's `hostname`/`timestamp` with fixed values.
Nothing else was touched. A fixture that leaked a local tree would make the
tests machine-dependent and would publish a directory layout with them.

## The files

| file | what it is |
|---|---|
| `verbose_run.txt` | the full `pytest -v` run, stdout+stderr |
| `report_xunit2.xml` | `--junitxml` from the same run, default family |
| `source_test_alpha.py.txt` | the source that produced most of it |
| `source_test_beta.py.txt` | the second file, for the cross-file name clash |

The sources are kept with a `.txt` suffix so pytest does not collect them as
part of this repository's own suite.

## What each case is here to prove

Every one of these was chosen because a plausible implementation gets it wrong.

- **`test_ordinary`** — the baseline, so a parser that matches nothing is
  distinguishable from one that matches everything.
- **`test_fails` / `test_errors` / `test_skipped`** — three different XML child
  tags (`failure`, `error`, `skipped`), three different `message` shapes. The
  error's message is multi-line and contains a Windows path.
- **`test_parametrised[...]`** — six ids including `with space`, `sl/ash`,
  `br[ack]et`, `3` and `None`. A nodeid regex anchored on whitespace, or one
  that assumes brackets do not nest, fails here.
- **`test_param_containing_separator[a::b]` and `[c[d]::e]`** — the important
  one. **`::` occurs inside parameter ids**, so `nodeid.split("::")` reads
  `test_param_containing_separator[a` as a class name. This is not a
  theoretical edge case; it is two lines of `@pytest.mark.parametrize`.
- **`TestOne::test_shared_method_name` and `TestTwo::test_shared_method_name`**
  — the same method name in two classes in one file. Any attribution that keys
  on the function name alone merges them.
- **`test_same_name_in_two_files`** in both `test_alpha` and `test_beta` — the
  same clash across files.
- **`TestOuter::TestInner::test_nested`** — pytest addresses nested classes by
  chaining, which is why `test_discovery` joins the whole class chain rather
  than keeping only the innermost.

## The two findings these fixtures produced

Both are recorded in `helpers/pytest_report.py`'s docstring, and both
contradicted the design that was written before the capture:

1. **The XML cannot identify a test.** `classname` is dotted
   (`tests.test_alpha.TestOne`), and a dotted path cannot say where the module
   ends and the class begins — package `tests/test_alpha/` containing
   `TestOne.py` produces the identical string. So identity comes from the `-v`
   lines and the XML supplies only duration and message.

2. **The default XML has no location at all.** Under pytest 9's `xunit2` a
   `<testcase>` carries no `file` and no `line`. The legacy `xunit1` family does
   emit them, with **native Windows separators** — the producer-dependent
   spelling `helpers/findings.relative_to` exists to keep out of an envelope.
   Location therefore comes from `helpers/test_discovery`, which already knows
   every definition's 1-based range.

## Re-capturing

Rebuild the throwaway project from the two `source_*.py.txt` files, run

    python -m pytest tests/ -o addopts= -v --junitxml=report.xml -p no:cacheprovider

and re-apply the path substitutions above. **Do not hand-edit these files.** An
edited fixture proves the parser matches what someone believed, which is the
belief the fixture was supposed to check.
