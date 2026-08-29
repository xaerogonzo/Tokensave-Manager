# Verification — which technique owns which failure

How this project checks a running application, and what that check is worth.
Written 2026-08-29 while applying a portable verification record from
OpenChem Studio. Most of the value here is the *measurements*, not the
mechanisms: the numbers are what tell you whether a guard is doing anything.

The rule the whole document hangs on:

> **Do not adopt a technique without first measuring that you have the
> failure class it addresses.** A technique that catches one class is not
> evidence for another.

---

## Which technique owns which failure

Choosing wrong produces a guard that cannot see its own subject.

| failure class | owned by | in this repo |
| --- | --- | --- |
| wrong value or logic | unit tests | most of `tests/` |
| wrong **structure** in source | static analysis over the AST | the six `tests/test_no_*.py` guards |
| wrong **geometry** in a laid-out UI | a driven visual oracle | `helpers/geometry.py` + `helpers/geometry_scan.py` |
| wrong **appearance over time** | golden-image diffing | not present, and not currently justified |
| the residue | a human | glyphs, colour, "does this look right" |

The worked example for why the mapping matters: a doc comment attached to the
wrong constant is a *structural* defect. No screenshot can ever find it,
because a doc comment never renders. A static guard finds it immediately.

---

## What was measured here

### Both failure classes are present

**Tier 0 — a guard that passes while testing nothing.** Confirmed
expensively by `f8e0a09`. The cross-thread Tk guard decided what counted as
worker code **by name** (`_worker`, `worker`), and 31 of the 111
`threading.Thread` targets in `src/` are named something else. That alone hid
17 sites and three entire files. It also could not see helpers *called from*
worker code — `app.py::_run_capture` says "must be called from a background
thread" in its own docstring and the guard missed it — and it never looked at
`concurrent.futures` at all, so `ChecksDialog` was invisible to every earlier
version. 61 known sites became 0 once the scope was resolved from actual
`Thread(target=…)`, `submit(…)` and `add_done_callback(…)` arguments.

**Geometry.** Four defects shipped to users with a fully green suite:

| surface | defect |
| --- | --- |
| doc-drafter | tab strip needed ~760px against the dialog's own 720px minsize; Tk does not scroll tab strips, so the last tab was unreachable |
| git-tab | Copy / Create PR / Open in Browser squeezed off-screen on tall windows |
| gitignore | Save/Cancel bar pushed off-screen when content exceeded screen height |
| test-gaps | long lists clipped, no scrollbar |

### The guards were mutation-tested, and they hold

Every guard was broken on purpose to confirm it goes red. The control and
mutated columns are `passed`/`failed`, so an arm that errored out and ran
*zero* tests — which greps as "no failures" — is visible rather than
silent.

| guard | control | mutated | verdict |
| --- | --- | --- | --- |
| `no_console_flash` | 7p/0f | 6p/1f | caught |
| `no_cross_thread_tk` | 17p/0f | 16p/1f | caught |
| `no_import_time_path_resolution` | 1p/0f | 0p/1f | caught |
| `no_thirdparty_module_imports` | 1p/0f | 0p/1f | caught |
| `no_tuple_pad_in_widget_ctor` | 1p/0f | 0p/1f | caught |
| `no_windows_only_subprocess_flags` | 3p/0f | 2p/1f | caught |

Six for six. Worth stating plainly because the source document expects the
opposite — it reports finding a vacuous guard on essentially every occasion
this was run. Plausibly this project is the exception *because* `f8e0a09`
already taught it the lesson at cost.

**Re-run it when you add a guard.** A guard is not verified by being green.

### Two findings survived, both about population rather than logic

The import-time path guard was correct in what it checked and wrong about
*how much* it checked.

**1. A blanket exclusion removed 27% of `src/`.** It skipped the entire
`dialogs.*` package — 43 of 160 modules — on the stated grounds that they
"import tkinter at module level which crashes on Linux CI without DISPLAY".
Measured, that reason was wrong twice: importing tkinter needs no display
(only constructing a widget does), and none of the 43 constructed one at
import time — the only two candidates sat inside `if __name__ == "__main__":`.
CI installs `python3-tk`, and the guard is not tk-marked, so it runs in the
non-xvfb step with tkinter present.

The sharpest part is *which* modules it removed. The failure this guard
catches "invisibly breaks the `fake_home` fixture for all dialog tests" — and
dialogs were exactly what it could not see. Running its own scan across the
43 found zero offenders, so nothing was hidden that day; a quarter of `src/`
was simply out of reach for anything added tomorrow.

Replaced by a per-module `_KNOWN_UNIMPORTABLE` list, currently empty, each
entry carrying a reason and kept honest by a staleness test — the shape
`test_no_console_flash` already uses for its `_EXEMPT` list. Population went
from 114 to 157.

**2. The scan never reported its population.** It printed `N module(s)
skipped as unimportable` and the test asserted only the exit code, so stdout
was never read on the passing path, and the diagnostic that would have shown
it is `@pytest.mark.skip` by default. A walk that yielded nothing would have
produced no offenders and passed. The subprocess now emits a
`SCANNED_MODULES=` line on every path including failure, the test asserts it
against a floor, and a module that cannot be imported exits `2` — its own
code, so an inconclusive run can never be read as a clean one.

Both fixes were then mutation-proved:

| proof | result |
| --- | --- |
| violation planted in `dialogs/gitignore.py` (previously excluded) | caught |
| population below the floor | caught |
| a module that cannot be imported | caught, not skipped |
| a stale `_KNOWN_UNIMPORTABLE` entry | caught |

---

## The geometric oracle

`helpers/geometry.py` (predicates) and `helpers/geometry_scan.py`
(extraction), reachable live as `{"do": "report", "what": "geometry"}` in the
drive harness.

### The rule that scopes it

> **The oracle owns geometric invariants that are mechanically measurable.
> The screenshot owns human judgment about appearance.**

Without that line the layer drifts into "does this UI look good?", which is
not a thing a predicate can answer.

### The two levels, and why they stay apart

    predicates    pure functions over rectangles and strings, unit-tested
                  on CONSTRUCTED geometry. They measure no fonts.
    extraction    reads ACTUAL laid-out geometry in root coordinates and
                  does all the measuring, against the running application.

A predicate that resolves its own text widths is a claim about the machine
running the test — a headless runner's default font can be twice the width of
the one a user sees.

### Population, not absence

`scan_window` returns `measured` alongside `findings`, and `format_result`
cannot print a verdict without stating the population. "No findings across
214 widgets" and "no findings across 0 widgets" are the same empty list and
entirely different claims.

### Proving it can still say NO

`test_the_scan_can_still_say_no` passes an impossible tolerance and requires
that genuinely clean geometry produces findings. Every other test in that file
would keep passing against a scan hard-wired to report nothing.

### What building it caught immediately

**Tk clamps a zero-width widget to 1px.** So `is_collapsed`'s natural
`w < 1` is correct in the abstract and can never fire on a real widget. The
predicate was dead on arrival and looked fine; only the integration test
against a real window surfaced it. The threshold now lives in the toolkit
layer at 2px, with separators and sizegrips exempted — a check that flags
every separator in the app is one that gets switched off, taking the real
signal with it.

That is the general lesson in miniature: **a fixture is not big or small, it
is degenerate or not with respect to a specific defect.**

### Deliberate scope limits

Stated so that "no findings" is not read as coverage this does not have:

- **Overlap is not wired into the walk.** Doing it properly needs the
  caption/value association from the layout manager; raw rectangle collision
  reports every parent/child pair, because Tk composites children constantly.
  The predicate exists and is tested; the scan does not call it.
- **Scrollable content is exempt from containment.** A canvas's content
  exceeds its viewport by design, so that comparison reports forever.
- **No golden-image diffing.** A golden tells you something changed; it
  cannot tell you a value is painted over its caption. The measurable layer
  comes first, and it works on a screen that has no baseline yet.

---

## Checklist for a new guard

1. Decide the failure class first, then pick the technique from the table.
2. Write the test that it **stays silent** on a clean surface, not only the
   one that it reports. A predicate returning `True` unconditionally passes
   the positive test perfectly.
3. Break the thing it names and confirm it goes red. Compare the test *count*
   against the control — an arm that errors runs zero tests and greps as "no
   failures".
4. Assert the population, not merely the absence of findings.
5. If you exclude anything, exclude it per-item with a reason, and add a
   staleness test. A blanket rule shrinks the universe silently and reads as
   a coverage win.
6. An inconclusive probe raises. "I could not find out" is not "it is
   absent".
