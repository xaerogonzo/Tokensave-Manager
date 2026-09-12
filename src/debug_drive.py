"""Script the running Manager, for checks that need the REAL window.

**WHY THIS IS IN THE APP RATHER THAN A HARNESS.** A harness that builds its own
`App` and drives that is a *parallel construction* of the thing under test. It
gets a different theme, different fonts and a different DPI, and every
measurement taken from it is of a window no user has. A check has to happen
inside the real window.

**WHAT IT REPLACES.** Driving the machine's mouse and keyboard. That approach
made this very session unusable: `SetCursorPos` + `mouse_event` jumped the
cursor, the window had to hold focus for every step, and a misdirected click
landed in a browser and started its dictation recorder. It is also fragile in a
way that reads as an application bug — anything stealing focus mid-sequence
sends the next click somewhere else, and the run looks like *the Manager
ignored it*.

Here the steps happen INSIDE the process: no cursor, no focus, and the capture
is a `PrintWindow` render rather than a screen grab. **The window can sit behind
whatever you are working in.**

    TOKENSAVE_MANAGER_DRIVE=<script.json>  python src/app.py

The script is a JSON list of steps, run in order:

    [
      {"do": "tab",      "name": "Projects"},
      {"do": "settings", "page": "integrations"},  // "" keeps the current page
      {"do": "dialog",   "name": "mcp"},        // also: savings, gitignore,
                                                //  docdrafter, testmanager,
                                                //  toolmanager, policy,
                                                //  extensionmanager,
                                                //  testgaps, prdraft
                                                // ("settings" still works and
                                                //  routes to the tab above)
      {"do": "click",  "text": "show"},
      {"do": "report", "what": "mcp", "after_ms": 3000},
      {"do": "report", "what": "posture"},      // MCP state, not rendered text
      {"do": "report", "what": "instructions"}, // carriage vs reach, per project
      {"do": "report", "what": "identity"},     // install identity + fleet owner
      {"do": "report", "what": "geometry"},     // laid-out geometry defects
      {"do": "shot",   "path": "C:/tmp/mcp.png", "target": "dialog"},
      {"do": "quit"}
    ]

`after_ms` is how long to wait BEFORE the next step, which is how a background
check is waited on. Every step defaults to 400 ms.

Give `shot` an **absolute** path outside the repository: a relative one
resolves against the working directory, and a diagnostic run should not leave
untracked images in a checkout.

`report` is usually the better step. A screenshot has to be looked at; a report
of what each row actually says can be diffed, pasted and asserted on — and for
"is this row claiming the wrong thing" it answers directly.

It reaches into private attributes on purpose: this drives the window a user
actually gets, rather than a public API invented for it that would become a
second way to do everything.
"""

from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path
from typing import Any

#: Read at import so a script named after the app has already started is
#: ignored — a half-driven window is worse than an undriven one.
_DRIVE_SCRIPT = os.environ.get("TOKENSAVE_MANAGER_DRIVE")

#: Pause between steps when one does not say otherwise. Long enough for a
#: layout pass and a queued event to be delivered, short enough that a
#: twenty-step script is not a coffee break.
_DEFAULT_AFTER_MS = 400


def _say(message: str) -> None:
    """Print without the console's encoding being able to stop the run.

    **Not a nicety.** Every status badge in this app is `✓`, `⚠` or `✗`, and a
    Windows console is cp1252, which cannot encode them. The `UnicodeEncodeError`
    would be raised inside a step handler, escape the timer chain, and the run
    would look exactly like the application hanging — a diagnostic dying on the
    glyphs of the thing it is diagnosing.
    """
    stream = sys.stdout
    try:
        print(message, file=stream, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding),
              file=stream, flush=True)


def start_if_requested(app) -> "_Driver | None":
    """Begin driving `app` when `TOKENSAVE_MANAGER_DRIVE` names a script.

    Returns the driver so the caller can keep a reference to it. Nothing here
    owns the `after` chain otherwise, and a driver that is garbage collected
    mid-script strands the run in a way indistinguishable from a hang.
    """
    if not _DRIVE_SCRIPT:
        return None
    try:
        steps = json.loads(Path(_DRIVE_SCRIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _say("TOKENSAVE_MANAGER_DRIVE: cannot read %s: %s"
             % (_DRIVE_SCRIPT, exc))
        return None
    if not isinstance(steps, list):
        _say("TOKENSAVE_MANAGER_DRIVE: %s is not a JSON list of steps"
             % _DRIVE_SCRIPT)
        return None
    driver = _Driver(app, steps)
    driver.start()
    return driver


def _walk(widget):
    """Every widget under `widget`, including itself."""
    yield widget
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    for child in children:
        yield from _walk(child)


def _widget_text(widget) -> str:
    """A widget's visible text, or "" — `cget` raises for those without it."""
    try:
        return str(widget.cget("text"))
    except (tk.TclError, AttributeError):
        return ""


class _Driver:
    """Runs the steps on Tk's own timer, one at a time."""

    def __init__(self, app, steps: "list[dict[str, Any]]") -> None:
        self._app = app
        self._steps = steps
        self._index = 0
        #: The dialog most recently opened by a `dialog` step. `shot` and
        #: `click` default to it, because a script that just opened a dialog
        #: is almost always talking about that dialog.
        self._dialog = None

    def start(self) -> None:
        _say("drive: %d step(s) from %s" % (len(self._steps), _DRIVE_SCRIPT))
        self._app.after(600, self._run_next)

    def _run_next(self) -> None:
        if self._index >= len(self._steps):
            _say("drive: done")
            return
        step = self._steps[self._index]
        self._index += 1
        name = str(step.get("do", "")) if isinstance(step, dict) else ""
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            _say("drive: unknown step %r" % (step,))
        else:
            try:
                handler(step)
            except Exception as exc:                         # noqa: BLE001
                # One bad step must not strand the rest: the remaining steps
                # usually include the `quit` that ends the run.
                _say("drive: step %r failed: %s" % (name, exc))
        delay = int(step.get("after_ms", _DEFAULT_AFTER_MS)) \
            if isinstance(step, dict) else _DEFAULT_AFTER_MS
        self._app.after(max(0, delay), self._run_next)

    # ── targets ────────────────────────────────────────────────────────

    def _target(self, step: "dict[str, Any]"):
        """Which window a step acts on: the named dialog, or the main window."""
        which = str(step.get("target", "dialog" if self._dialog else "main"))
        if which == "dialog":
            if self._dialog is None:
                _say("drive: no dialog is open; using the main window")
                return self._app
            return self._dialog
        return self._app

    # ── steps ──────────────────────────────────────────────────────────

    def _do_tab(self, step: "dict[str, Any]") -> None:
        """Select a notebook tab by the text on it."""
        want = str(step.get("name", "")).strip().lower()
        notebook = getattr(self._app, "nb", None)
        if notebook is None:
            _say("drive: tab: the app has no notebook")
            return
        for tab_id in notebook.tabs():
            label = str(notebook.tab(tab_id, "text")).strip().lower()
            # Tabs carry glyphs ("🗂 Tasks"), so match on containment rather
            # than equality — a script should not have to spell the icon.
            if want and want in label:
                notebook.select(tab_id)
                _say("drive: tab -> %s" % label)
                return
        _say("drive: tab: no tab matching %r" % want)

    def _project(self, step: "dict[str, Any]") -> str:
        """Which project a dialog step operates on.

        Defaults to the Manager's own checkout rather than whatever happens to
        be selected. A committed drive script must not depend on today's
        project list, or it stops working tomorrow for reasons unrelated to
        the code under test.
        """
        explicit = str(step.get("project", "")).strip()
        if explicit:
            return explicit
        return str(Path(__file__).resolve().parent.parent)

    def _capture_new_window(self, action) -> "tk.Toplevel | None":
        """Run *action* and return the Toplevel it created, if any.

        The controller-built windows (Test Gaps, PR draft) construct a
        `tk.Toplevel` inline and do not return it, so there is nothing to
        assign. Diffing the root's children is independent of that: it works
        whatever the callee chooses to return, and does not require the
        controller to grow an API that exists only for this harness.
        """
        def tops():
            return [w for w in self._app.winfo_children()
                    if isinstance(w, tk.Toplevel)]
        before = set(map(id, tops()))
        action()
        fresh = [w for w in tops() if id(w) not in before]
        return fresh[-1] if fresh else None

    def _do_settings(self, step: "dict[str, Any]") -> None:
        """Select the Settings tab, optionally on a named page.

        `page` is a key from `controllers.settings_tab.PAGES` -- "paths",
        "projects", "git", "ai", "integrations". Two sections fill their
        path entry from auto-detection on a build timer, so a `report` or
        `shot` against this wants an `after_ms` past
        `settings_tab.DETECTION_SETTLE_MS` if it cares what they show.
        """
        page = str(step.get("page", "")).strip().lower()
        opener = getattr(self._app, "open_settings", None)
        if opener is None:
            _say("drive: settings: the app has no open_settings")
            return
        opener(page)

    def _do_dialog(self, step: "dict[str, Any]") -> None:
        """Open a dialog and remember it as the default target."""
        name = str(step.get("name", "")).strip().lower()
        if name in ("mcp", "mcpconfig", "mcpconfigdialog"):
            from dialogs.mcp_config import MCPConfigDialog
            self._dialog = MCPConfigDialog(
                self._app, self._app._cfg,
                focus_project=str(step.get("project", "")))
        elif name in ("settings", "settingsdialog"):
            # COMPATIBILITY ALIAS, not a second surface. Settings stopped
            # being a dialog; existing drive scripts that ask for it by that
            # name get the tab, and `{"do": "settings"}` is the spelling to
            # use from here. There is no `self._dialog` to remember, so a
            # following step targets the app -- which is right, because the
            # pages are inside the main window now.
            self._do_settings(step)
            self._dialog = None
        elif name in ("instructions", "instructionsoverview",
                      "instructionsdialog"):
            # Scans every project in a worker thread, so a `report` step
            # against this dialog needs an `after_ms` long enough for the
            # scan to land — the rows are empty until it does.
            from dialogs.instructions_overview import InstructionsDialog
            self._dialog = InstructionsDialog(self._app, self._app._cfg)
        elif name in ("split", "instructionssplit", "splitproposal"):
            # Reads and computes the whole file in a worker, so a `shot` or
            # `report` needs an `after_ms` long enough for the sections to
            # land -- the list is empty until they do.
            from dialogs.instructions_split import SplitProposalDialog
            path = self._project(step)
            self._dialog = SplitProposalDialog(
                self._app, path, os.path.basename(path.rstrip("/\\")))
        elif name in ("savings", "cost", "savingsdialog"):
            # Reads `tokensave gain`/`cost`/`discover` in worker threads, so a
            # `report` step needs an `after_ms` long enough for them to land.
            from dialogs.cost_viewer import SavingsDialog
            self._dialog = SavingsDialog(
                self._app, self._app._cfg, str(step.get("project", "")))
        elif name in ("docdrafter", "doc_drafter", "docs"):
            from dialogs.doc_drafter import DocDrafterDialog
            self._dialog = DocDrafterDialog(
                self._app, self._project(step), self._app._cfg)
        elif name in ("gitignore", "ignore"):
            from dialogs.gitignore import GitignoreDialog
            self._dialog = GitignoreDialog(
                self._app, self._project(step), self._app._cfg)
        elif name in ("policy", "composer", "agentpolicy", "agent_policy"):
            # Reads fleet ownership in a worker before offering anything, so a
            # `report` or `shot` needs an `after_ms` long enough for that scan
            # to land -- until it does the preview says so and Apply is
            # disabled, which is the correct state rather than an empty one.
            from dialogs.instruction_composer import InstructionComposerDialog
            self._dialog = InstructionComposerDialog(self._app, self._app._cfg)
        elif name in ("toolmanager", "tools", "tool_manager"):
            from dialogs.tool_manager import ToolManagerDialog
            self._dialog = ToolManagerDialog(self._app, self._app._cfg)
        elif name in ("extensionmanager", "extension", "extension_manager"):
            from dialogs.extension_manager import ExtensionManagerDialog
            self._dialog = ExtensionManagerDialog(
                self._app, self._app._cfg, app=self._app)
        elif name in ("testmanager", "tests", "test_manager"):
            from dialogs.test_manager import TestManagerDialog
            self._dialog = TestManagerDialog(
                self._app, self._project(step), self._app._cfg)
        elif name in ("testgaps", "gaps", "test_gaps"):
            ctrl = getattr(getattr(self._app, "_git", None), "_test_gap", None)
            if ctrl is None:
                _say("drive: dialog: the Git tab has no test-gap controller")
                return
            base = str(step.get("base", "master"))
            # suggestions=[] so opening the window does not kick off a scan;
            # this step is about how the window LAYS OUT, not what it finds.
            self._dialog = self._capture_new_window(
                lambda: ctrl._open_test_gaps_window(
                    self._project(step), base, suggestions=[]))
        elif name in ("prdraft", "pr", "pr_draft"):
            ctrl = getattr(getattr(self._app, "_git", None), "_pr_draft", None)
            if ctrl is None:
                _say("drive: dialog: the Git tab has no PR-draft controller")
                return
            base = str(step.get("base", "master"))
            self._dialog = self._capture_new_window(
                lambda: ctrl._open_pr_draft_dialog(self._project(step), base))
        else:
            _say("drive: dialog: unknown name %r" % name)
            return
        # A modal grab would leave the driven window unable to receive the
        # events the remaining steps generate, and the run would hang with
        # the dialog up.
        try:
            self._dialog.grab_release()
        except tk.TclError:
            pass
        _say("drive: dialog -> %s" % type(self._dialog).__name__)

    def _do_click(self, step: "dict[str, Any]") -> None:
        """Invoke a button by its text.

        `invoke()` runs the command the button is wired to. That is the same
        callback a real click reaches, without a cursor, a focused window, or
        any way for another application to intercept it.
        """
        want = str(step.get("text", "")).strip().lower()
        nth = int(step.get("nth", 0))
        seen = 0
        for widget in _walk(self._target(step)):
            if not hasattr(widget, "invoke"):
                continue
            label = _widget_text(widget).strip().lower()
            if not want or want not in label:
                continue
            if seen < nth:
                seen += 1
                continue
            # Read the label BEFORE invoking. A button whose command re-renders
            # the pane destroys the widget, and reading it afterwards reports
            # an empty string — which reads as "matched the wrong thing"
            # rather than as "worked, then went away".
            found = _widget_text(widget).strip()
            widget.invoke()
            _say("drive: click -> %r" % found)
            return
        _say("drive: click: no button matching %r" % want)

    def _do_scroll(self, step: "dict[str, Any]") -> None:
        """Move a dialog's canvas to the top or the bottom."""
        where = str(step.get("to", "bottom")).lower()
        for widget in _walk(self._target(step)):
            if isinstance(widget, tk.Canvas):
                widget.update_idletasks()
                widget.yview_moveto(0.0 if where == "top" else 1.0)
                _say("drive: scroll -> %s" % where)
                return
        _say("drive: scroll: no canvas found")

    def _do_shot(self, step: "dict[str, Any]") -> None:
        """Render the target window to a PNG, wherever it is on screen."""
        from helpers.window_capture import capture_window, hwnd_for

        path = str(step.get("path", "")).strip()
        if not path:
            _say("drive: shot: no path")
            return
        target = self._target(step)
        target.update_idletasks()
        ok, detail = capture_window(hwnd_for(target), path)
        _say("drive: shot -> %s %s" % (path if ok else "FAILED", detail))

    def _report_settings(self) -> None:
        """What the Settings tab claims about itself.

        Reported rather than screenshotted because the interesting facts here
        are all assertions the surface makes: which page is showing, whether
        it believes it has unsaved changes, and whether the Save bar agrees
        with that belief. A page that opens already claiming unsaved changes
        is the failure this exists to catch -- two sections fill their path
        entry from auto-detection on a build timer.
        """
        ctl = getattr(self._app, "_settings_ctrl", None)
        if ctl is None:
            _say("drive: settings: the app has no settings tab")
            return
        selected = ""
        for key, frame in ctl._pages.items():
            if str(frame) == str(ctl._inner.select()):
                selected = key
                break
        _say("drive: settings: pages=%s" % ",".join(ctl._pages))
        _say("    showing   : %s" % (selected or "(none)"))
        _say("    dirty     : %s" % ctl.is_dirty())
        _say("    save bar  : %s" % ("shown" if ctl._bar.winfo_ismapped()
                                     else "hidden"))
        for index, section in enumerate(ctl._sections):
            _say("    section %d : %s" % (index, type(section).__name__))

    def _report_help(self, step: "dict[str, Any]") -> None:
        """What the Help tab is showing, and what the corpus scan found.

        The corpus numbers are the assertable part: a document that failed to
        load shows up as a problem here rather than as a list that is merely
        shorter than you remember. `query` drives the search box first, so a
        script can check that searching narrows the list rather than emptying
        it.
        """
        from helpers import help_docs

        ctl = getattr(self._app, "_help_ctrl", None)
        if ctl is None:
            _say("drive: help: the app has no help tab")
            return
        query = str(step.get("query", ""))
        if query:
            ctl._query.set(query)
        topics, problems = help_docs.scan()
        _say("drive: help: %d topics from %d documents, %d problem(s)"
             % (len(topics), len(help_docs.HELP_DOCUMENTS), len(problems)))
        for problem in problems:
            _say("    PROBLEM %s: %s" % (problem.document, problem.detail))
        _say("    query     : %r" % query)
        _say("    listed    : %d" % ctl._help_lb.size())
        _say("    showing   : %s" % (ctl._current_key or "(none)"))
        body = ctl._help_txt.get("1.0", "end").strip()
        _say("    rendered  : %d chars" % len(body))
        for line in body.splitlines()[:int(step.get("lines", 6))]:
            _say("    | " + line)

    def _do_report(self, step: "dict[str, Any]") -> None:
        """Dump what a window actually says, as text.

        Cheaper than a screenshot and directly assertable: for "is this row
        claiming something it cannot know", the label text IS the finding.
        """
        what = str(step.get("what", "text")).lower()
        target = self._target(step)
        if what == "mcp":
            self._report_mcp(target)
            return
        if what == "posture":
            self._report_posture()
            return
        if what == "observations":
            self._report_observations(step)
            return
        if what == "instructions":
            self._report_instructions()
        if what == "identity":
            self._report_identity()
            return
        if what == "geometry":
            self._report_geometry(target, step)
            return
        if what == "settings":
            self._report_settings()
            return
        if what == "help":
            self._report_help(step)
            return
        lines = [t for t in (_widget_text(w).strip() for w in _walk(target))
                 if t]
        _say("drive: report (%d labels)" % len(lines))
        for line in lines:
            _say("    " + line)

    def _report_geometry(self, target, step: "dict[str, Any]") -> None:
        """Assert geometric invariants on what is actually on screen.

        The step that turns a drive run from something a human reads into
        something that can fail. `report what=text` says what a window
        claims; this says whether the window is laid out such that a user
        could reach it — the class of defect that has shipped here repeatedly
        with a green suite (buttons off-screen, a tab strip wider than its
        own minimum width, a clipped list).

        Always prints the population it measured, so "0 findings" can never
        be confused with "nothing was looked at".

        `tolerance` is the live equivalent of the suite's
        test_the_scan_can_still_say_no: pass a negative value and every child
        becomes a finding against the same geometry. A clean run is only
        worth something once you have seen the same window report, because
        otherwise "0 findings" and "the scan is broken" look identical.
        """
        try:
            from helpers.geometry_scan import format_result, scan_window
        except Exception as exc:                       # pragma: no cover
            _say("drive: report: geometry scan unavailable (%s)" % exc)
            return
        if "tolerance" in step:
            result = scan_window(target, tolerance=int(step["tolerance"]))
        else:
            result = scan_window(target)
        for line in format_result(result).splitlines():
            _say("drive: " + line)

    def _report_identity(self) -> None:
        """Where am I, and who owns the fleet — as text, not a screenshot.

        Two questions printed separately on purpose. A run that silently
        collapsed them, or that reported one owner for a split fleet, is
        visible in a diff of this output.
        """
        from constants import _BASE_DIR
        from helpers.install_identity import (
            read_identity, read_ownership, relocation_plan,
        )
        from helpers.instructions_posture import read_posture

        cfg = self._app._cfg
        raw = dict(cfg.raw)
        identity = read_identity(raw, _BASE_DIR)
        fleet = read_posture(list(raw.get("search_roots") or []), cfg)
        ownership = read_ownership(fleet.projects, cfg.template_dir)
        plan = relocation_plan(identity, ownership, raw, cfg.template_dir)

        _say("identity : %s" % identity.state)
        _say("  recorded %s" % (identity.recorded_display or "(none)"))
        _say("  current  %s" % identity.current_display)
        _say("ownership: %s" % ownership.state)
        for owner in ownership.owners:
            _say("  %-3d %s" % (owner.count, owner.display_dir))
        _say("  unresolved %d%s"
                  % (len(ownership.unresolved),
                     (": " + ", ".join(ownership.unresolved[:5]))
                     if ownership.unresolved else ""))
        _say("config updates: %d" % len(plan.config_updates))
        for key, old, new in plan.config_updates:
            _say("  %s: %s -> %s" % (key, old, new))
        _say("projects to repoint: %d" % len(plan.projects))
        _say("blocked : %s" % (plan.blocked or "(no)"))
        _say("downgrade risk: %s" % plan.downgrades)

    def _report_instructions(self) -> None:
        """Instruction-chain state, with carriage and reach kept apart.

        Printing one `status:` per project would defeat the purpose. The whole
        model rests on the gap between what a project's files DECLARE and what
        the chain from CLAUDE.md actually resolves to, and eleven projects sat
        in exactly that gap — `carriage: current` beside `reach: orphaned` —
        while every check the Manager had reported them fine. Two columns is
        what makes that visible, and assertable.

        The distribution is printed whole for the same reason the dialog shows
        it whole: a resolved count that rose while a new `unknown` appeared is
        not the same result, and one number cannot say so.

        Everything goes through `_say` — the badges here are the same `✓`/`⚠`
        glyphs a cp1252 console cannot encode.
        """
        try:
            from helpers.instructions_posture import read_posture
            fleet = read_posture(
                list(self._app._cfg.raw.get("search_roots") or []),
                self._app._cfg)
        except Exception as exc:                       # pragma: no cover
            _say("drive: report: instructions unavailable (%s)" % exc)
            return

        _say("drive: instructions  baseline=%s ok=%s"
             % (fleet.baseline or "(unusable)", fleet.baseline_ok))
        counts = fleet.counts()
        _say("    distribution: " + "  ".join(
            "%s=%d" % (state, counts[state]) for state in sorted(counts)))
        for project in sorted(fleet.projects, key=lambda p: p.name.lower()):
            _say("    %-28s carriage: %-8s reach: %-9s %s B ~%s tok  %s"
                 % (project.name[:28], project.carriage, project.reach,
                    f"{project.weight_bytes:,}",
                    f"{project.estimated_tokens:,}",
                    ",".join(project.advisories) or "-"))
            if project.detail:
                _say("        %s" % project.detail)

    def _report_observations(self, step: "dict[str, Any]") -> None:
        """Both diagnostic sources, side by side, each with its own population.

        Printed as **two rows that never combine**. The whole design rests on
        the two being incomparable: the headless half can state a real
        population (the project tree, or the `--paths` scope), while the editor
        half structurally cannot — `getDiagnostics()` enumerates resources that
        HAVE diagnostics, not what was analysed. A single line with one count
        would erase exactly that difference, which is the `verification_oracle`
        population failure in a new place.

        So `analyzed files: unknown` is printed rather than omitted: an absent
        field reads as zero to whoever scans the output later.

        Everything goes through `_say` — a cp1252 console cannot encode the
        glyphs, and the exception escapes the timer chain and looks like a hang.
        """
        path = self._project(step)
        try:
            from helpers.headless_analyzers import run_all
            from helpers.observations import (
                findings_to_report, merge_rows, read_snapshot)
        except Exception as exc:                        # pragma: no cover
            _say("drive: report: observations unavailable (%s)" % exc)
            return

        editor = read_snapshot(path)
        results = run_all(path, self._app._cfg.raw)
        findings = [f for r in results for f in r.findings]
        headless = findings_to_report(findings, scope="whole project")

        _say("drive: observations  project=%s" % os.path.basename(path))
        for report in (headless, editor):
            cov = report.coverage
            _say("    %-8s status=%-8s entries=%-5d files=%-4d %s  as-of=%s"
                 % (report.key, report.read_status, cov.diagnostic_entries,
                    cov.files_with_diagnostics, cov.analyzed_files_text,
                    report.as_of or "-"))
            if report.detail:
                _say("        %s" % report.detail)
        for result in results:
            _say("    analyzer %-13s %-12s rows=%d"
                 % (result.key, result.availability, result.rows))
        merged = merge_rows([headless, editor])
        # `shared` is "more than one SOURCE saw it", never "more than one row".
        # A single analyzer legitimately reports one position several times —
        # two deprecations on an import line, one per union member at an
        # attribute access — and an earlier version of this line counted those
        # as corroboration, reporting 321 rows "seen by both" on a project with
        # no editor snapshot at all.
        both = [m for m in merged if m.shared]
        _say("    merged rows=%d  seen by >1 source=%d  (NOT a total of the two)"
             % (len(merged), len(both)))
        for shared in both[:5]:
            _say("        %s:%d [%s] %s"
                 % (shared.file, shared.line, shared.rule,
                    " vs ".join("%s=%s" % (k, group[0].severity)
                                for k, group in shared.sources)))

    def _report_posture(self) -> None:
        """The MCP posture as STATE, not as the string a row happens to show.

        `report what=mcp` prints what each row claims, which is the right
        tool for "is this label saying something it cannot know". This is the
        other half: a rendered badge can be produced by the wrong underlying
        state — that is exactly how ten rows read "bound to this project"
        while every session was answered by the user-scoped entry — so a live
        check that asserts only on the visible string can still pass against a
        wrong classification.

        So this prints the inputs the badge is derived FROM: both lifecycles,
        both read statuses, the fallback, and per project its tier alongside
        its service. Tier and service are printed together on purpose; they
        are the pair whose separation the whole model rests on, and seeing
        `explicit_inert / automatic` on one line is what makes it obvious that
        the second column is not a restatement of the first.

        Everything goes through `_say`: every badge here is a `✓`/`⚠`/`✗`, and
        a cp1252 console raises `UnicodeEncodeError` inside the step handler,
        which stops the timer chain and looks exactly like the app hanging.
        """
        try:
            from helpers.mcp_posture import read_posture
            posture = read_posture(self._app._cfg)
        except Exception as exc:                       # pragma: no cover
            _say("drive: report: posture unavailable (%s)" % exc)
            return

        _say("drive: posture")
        _say("    desktop      %s  (read: %s)"
             % (posture.desktop_state, posture.desktop_read))
        _say("    userscope    %s  (read: %s)"
             % (posture.userscope_state, posture.userscope_read))
        _say("    fallback     %s" % posture.automatic_fallback)
        _say("    independent  %s" % posture.independent)
        _say("    covered      %s" % posture.covered)
        _say("    headline_ok  %s   reads_ok %s"
             % (posture.headline_ok, posture.reads_ok))
        # The population, always. A table that silently emptied would make
        # every per-project assertion below vacuous, and "0 problems" would
        # be indistinguishable from "0 projects looked at".
        _say("    projects     %d" % len(posture.projects))
        for project in posture.projects:
            _say("    %-30s %-16s %-10s %s"
                 % (project.name[:30], project.tier,
                    posture.service(project), project.display_root))
        _say("    unserved     %s"
             % (", ".join(p.name for p in posture.unserved) or "(none)"))
        _say("    misbound     %s"
             % (", ".join(p.name for p in posture.misbound) or "(none)"))

    def _report_mcp(self, dialog) -> None:
        """The MCP dialog's per-row verdicts, as state + badge + path."""
        states = getattr(dialog, "_config_state", None)
        if not isinstance(states, dict):
            _say("drive: report: that window has no MCP row state")
            return
        _say("drive: report: %d MCP row(s)" % len(states))
        for path, info in states.items():
            _say("    %-22s %s" % (info.get("state", "?"), path))
            label = str(info.get("label", "")).strip()
            if label:
                _say("        badge: %s" % label)
            issue = " ".join(str(info.get("issue", "")).split())
            if issue:
                _say("        issue: %s" % issue[:300])

    def _do_wait(self, step: "dict[str, Any]") -> None:
        """Do nothing. `after_ms` is the point of the step."""

    def _do_quit(self, step: "dict[str, Any]") -> None:
        _say("drive: quit")
        try:
            self._app.destroy()
        except tk.TclError:
            pass
        # `destroy` alone leaves the tray thread holding the process open,
        # and a diagnostic run that never exits cannot be scripted.
        # `os._exit` skips interpreter shutdown, which includes flushing --
        # so a piped transcript can lose its tail, and the run then looks like
        # it died mid-report.
        self._app.after(200, self._exit_now)

    @staticmethod
    def _exit_now() -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        os._exit(0)
