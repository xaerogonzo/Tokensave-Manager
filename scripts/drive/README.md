# Drive scripts

Committed inputs for the in-process drive harness (`src/debug_drive.py`).

They live here because before this directory existed, **not one drive script
in this project had ever been committed**. Every live check was written once,
run once, read by a human and thrown away — which made the most productive
verification technique available here also the least repeatable one.

## Running

```bash
TOKENSAVE_MANAGER_DRIVE=scripts/drive/geometry-sweep.json python src/app.py
```

Steps run on Tk's own timer inside the real process: no cursor, no focus
stealing, and `shot` renders with `PrintWindow`, so **the window can sit
behind whatever you are working in**. Never drive this app with mouse or
foreground automation — see the module docstring in `src/debug_drive.py` for
what that cost.

## The scripts

| script | what it does |
| --- | --- |
| `geometry-sweep.json` | Walks every main-window tab and reports laid-out geometry defects on each. The routine check. |
| `dialog-sweep.json` | Opens the dialogs with a track record of geometry defects and reports each. **This is where the findings are** — the main window has no history of them. |
| `geometry-selftest.json` | Reports the Git tab twice — once normally, once with an impossible tolerance. **Run this whenever you doubt a clean result.** |

## Why the self-test exists

A clean geometry run is worth nothing on its own: "0 findings" and "the scan
is broken" produce identical output. `geometry-selftest.json` reports the same
window twice, and the second pass forces every child into a finding:

```
drive: geometry: 0 finding(s) across 60 mapped widget(s) (109 unmapped, ...)
drive: geometry: 60 finding(s) across 60 mapped widget(s) (109 unmapped, ...)
```

Same geometry, same population, opposite verdicts. That is what makes the
first line evidence rather than an absence.

Note that every report states the population it measured. A run that says
"0 findings across 0 widgets" is not a pass — it means the walk found
nothing to look at.

## Which dialogs a script can open

`{"do": "dialog", "name": ...}` knows: `mcp`, `settings`, `savings`,
`gitignore`, `docdrafter`, `testmanager`, `testgaps`, `prdraft`.

The last two are built inline by controllers rather than by a dialog class,
so the harness captures them by diffing the root's children instead of relying
on a return value — no controller has to grow an API that exists only for this.

`testgaps` is deliberately **not** in `dialog-sweep.json`. Opening it with an
empty suggestion set (so the step does not kick off a scan) renders a panel of
three widgets, and "0 findings across 3 widgets" is not evidence of anything.
It stays available for ad-hoc use against a branch that actually has gaps.

They default to the Manager's **own checkout**, not whatever project happens to
be selected, so a committed script does not depend on today's project list.
Override with `"project": "<path>"`, and `"base": "<branch>"` for the two that
diff against a branch.

## Adding one

`after_ms` is the pause **after** a step, which is how a background task is
waited on. Steps: `tab`, `dialog`, `click`, `scroll`, `shot`, `report`,
`wait`, `quit`. Give `shot` an absolute path outside the repository so a
diagnostic run does not leave untracked images in a checkout.

Keep live state out of a committed script — no current selection, no clock,
no network, no whatever-happens-to-be-open. A script that depends on today's
project list stops working tomorrow for reasons unrelated to the code.
