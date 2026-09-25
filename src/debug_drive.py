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
      {"do": "select", "project": "D:/path/to/project"},  // Projects tab row
      {"do": "doctor", "project": "D:/path/to/project",   // the REAL Doctor,
                       "timeout_ms": 120000},             //  waited on
      {"do": "report", "what": "log", "from": "Extra checkouts"},  // OUTPUT pane
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

**A run is evidence, not only a demonstration.** The ledger
(`helpers/drive_ledger.py`) starts before the window is built and hears
`logging`, Tk callback exceptions, Python warnings, and uncaught exceptions on
any thread. Three steps turn that into a verdict:

    {"do": "expect", "id": "on_git", "check": "tab", "text": "Git",
     "within_ms": 4000}                      assert state, waiting for it
    {"do": "expect_clean", "settle_ms": 1500}  the app said nothing wrong, and
                                               kept quiet for a moment after
    {"do": "log_report"}                       print what the ledger holds

`expect` knows the checks `tab`, `dialog_open`, `log_contains`, `widget_text`
and `diagnostic_contains` (the last reads the LIVE ledger, never a report
file). A failed expectation is permanent: a later success never erases it.
On the way out -- `quit`, or interpreter exit, exactly once -- the run writes
`<script>.report.json` beside the script (gitignored) and exits non-zero if
anything it was meant to prove failed. Two simultaneous drives of one script
are unsupported: they would share that file (the report's `run_id`/`pid` say
whose it is).

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

import atexit
import json
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any

from helpers import drive_ledger

#: Read at import so a script named after the app has already started is
#: ignored — a half-driven window is worse than an undriven one.
_DRIVE_SCRIPT = os.environ.get("TOKENSAVE_MANAGER_DRIVE")

#: The ledger for this process, started by `begin()` before the window is
#: built so launch diagnostics are on the record. None unless a drive was asked
#: for, which is what keeps a normal launch untouched.
_LEDGER: "drive_ledger.Ledger | None" = None

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


def begin(app=None) -> None:
    """Start listening, when `TOKENSAVE_MANAGER_DRIVE` names a script.

    Called by `App.__init__` as early as a Tk root exists, so what the
    application says while it is being built -- a callback that raises during
    layout, a warning from an import -- is on the record. It is tagged
    `startup`: it can never fail a script's `expect_clean`, and never vanishes.
    """
    global _LEDGER
    if not _DRIVE_SCRIPT or _LEDGER is not None:
        return
    _LEDGER = drive_ledger.Ledger()
    _LEDGER.install()
    if app is not None:
        _LEDGER.install_tk(app)


def end() -> None:
    """Put every hook back. Idempotent."""
    global _LEDGER
    if _LEDGER is not None:
        _LEDGER.uninstall()
        _LEDGER = None


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
    driver = _Driver(app, steps, ledger=_LEDGER, script=_DRIVE_SCRIPT)
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


def _tree_iids(tree, parent: str = "") -> "list[str]":
    """Every row id in a Treeview, depth first, including collapsed rows."""
    out = []
    for iid in tree.get_children(parent):
        out.append(iid)
        out.extend(_tree_iids(tree, iid))
    return out


def _widget_text(widget) -> str:
    """A widget's visible text, or "" — `cget` raises for those without it."""
    try:
        return str(widget.cget("text"))
    except (tk.TclError, AttributeError):
        return ""


class _Driver:
    """Runs the steps on Tk's own timer, one at a time."""

    def __init__(self, app, steps: "list[dict[str, Any]]", *,
                 ledger: "drive_ledger.Ledger | None" = None,
                 script: "str | None" = None) -> None:
        self._app = app
        self._steps = steps
        self._index = 0
        #: Never None, so `expect_clean` always has something to ask. A driver
        #: built by hand gets a ledger that listens to nothing and so is clean.
        self._ledger = ledger if ledger is not None else drive_ledger.Ledger()
        self._script = script
        self._run_id = drive_ledger.new_run_id()
        self._started = time.time()
        self._shots: "list[str]" = []
        #: How often `_run_next` re-checks a held chain. `expect` shortens it.
        self._hold_ms = 500
        #: The dialog most recently opened by a `dialog` step. `shot` and
        #: `click` default to it, because a script that just opened a dialog
        #: is almost always talking about that dialog.
        self._dialog = None
        #: A callable returning True while a background job a step started is
        #: still running. `_run_next` re-checks it instead of advancing, which
        #: is how `doctor` is waited on without a guessed `after_ms`.
        self._hold = None

    def start(self) -> None:
        _say("drive: %d step(s) from %s" % (len(self._steps), _DRIVE_SCRIPT))
        self._ledger.start_drive()
        # The fallback for a run that ends WITHOUT a `quit` step (the window
        # closed, the interpreter exiting under an error). `os._exit` bypasses
        # atexit, so `_do_quit` finalizes itself; `finalize` is one-shot, so
        # both being armed is safe.
        atexit.register(self._finalize, "exit")
        self._app.after(600, self._run_next)

    def _run_next(self) -> None:
        """Perform the next step **and arm the one after it**.

        The timer is the half `step()` deliberately does not have: anything
        driving this by hand should call that, or every call leaves a shot
        armed on a window nobody is going to let fire.
        """
        if self._hold is not None:
            if self._hold():
                self._app.after(self._hold_ms, self._run_next)
                return
            self._hold, self._hold_ms = None, 500
        if not self.step():
            return
        last = self._steps[self._index - 1]
        delay = int(last.get("after_ms", _DEFAULT_AFTER_MS)) \
            if isinstance(last, dict) else _DEFAULT_AFTER_MS
        self._app.after(max(0, delay), self._run_next)

    def step(self) -> bool:
        """Perform the next step and arm nothing. True if one ran."""
        if self._index >= len(self._steps):
            _say("drive: done")
            return False
        step = self._steps[self._index]
        self._index += 1
        name = str(step.get("do", "")) if isinstance(step, dict) else ""
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            _say("drive: unknown step %r" % (step,))
            self._ledger.step_failed(self._index, name, "unknown step")
        else:
            try:
                handler(step)
            except Exception as exc:                         # noqa: BLE001
                # One bad step must not strand the rest: the remaining steps
                # usually include the `quit` that ends the run. It is also a
                # failure OF the run, not a remark -- `_say` alone lost it.
                _say("drive: step %r failed: %s" % (name, exc))
                self._ledger.step_failed(self._index, name, str(exc), exc)
        return True

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

    def _do_select(self, step: "dict[str, Any]") -> bool:
        """Select a Projects-tab row by path. True when a row was selected.

        The same selection a user's click makes, so anything that reads the
        selected project (`_cmd_bar.cmd_doctor`) sees it. Matching is on the
        normalised path, because the tree holds the path as discovered and a
        script should not have to spell it identically.
        """
        tree = getattr(getattr(self._app, "_projects", None), "_tree", None)
        if tree is None:
            _say("drive: select: the app has no project tree")
            return False
        want = os.path.normcase(os.path.normpath(self._project(step)))
        for iid in _tree_iids(tree):
            if iid.startswith("proj:") and os.path.normcase(
                    os.path.normpath(iid[5:])) == want:
                tree.selection_set(iid)
                tree.see(iid)              # opens collapsed parent categories
                _say("drive: select -> %s" % iid[5:])
                return True
        _say("drive: select: no project row matching %r" % want)
        return False

    def _do_doctor(self, step: "dict[str, Any]") -> None:
        """Run the REAL Doctor on a project, and hold the chain until it ends.

        Goes through `CommandBarCtrl.cmd_doctor`, the callback the right-click
        menu binds, so the tokensave subprocess, the analysis and every audit
        block run exactly as they do for a user. It does not click the
        follow-up dialogs Doctor may offer (purge, worktree repair); those are
        offers, and a drive run never accepts one.

        Waits on the `doctor-worker` thread rather than a fixed delay, up to
        `timeout_ms`. Selecting first is what keeps a missing row from
        reaching `_selected_path`, which would raise a modal warning.
        """
        import threading
        import time

        ctrl = getattr(self._app, "_projects", None)
        if ctrl is None or not self._do_select(step):
            return
        ctrl._cmd_bar.cmd_doctor()
        deadline = time.monotonic() + int(step.get("timeout_ms", 120000)) / 1000

        def running() -> bool:
            alive = any(t.name == "doctor-worker" and t.is_alive()
                        for t in threading.enumerate())
            if alive and time.monotonic() >= deadline:
                _say("drive: doctor: still running at the timeout; moving on")
                return False
            return alive

        self._hold = running

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
        if ok:
            self._shots.append(path)
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
        if what == "output":
            self._report_output()
            return
        if what == "log":
            self._report_log(step)
            return
        lines = [t for t in (_widget_text(w).strip() for w in _walk(target))
                 if t]
        _say("drive: report (%d labels)" % len(lines))
        for line in lines:
            _say("    " + line)

    def _report_output(self) -> None:
        """The OUTPUT pane as measured: where it is, how tall, and read-only how.

        Measured rather than inferred: `docked` is membership in the paned
        window's live pane list, and the proxy is checked by asking Tcl for
        the alias, so a pane that silently lost either reports it.
        """
        out = getattr(self._app, "_output", None)
        paned = getattr(self._app, "_paned", None)
        if out is None or paned is None:
            _say("drive: output: the app has no output pane")
            return
        docked = str(out.frame) in [str(p) for p in paned.panes()]
        _say("output: docked=%s popped_out=%s"
             % (docked, bool(getattr(out, "is_popped_out", False))))
        _say("  pane height : %d px (preference %r)"
             % (out.frame.winfo_height(),
                self._app._cfg.raw.get("output_pane_height")))
        _say("  text state  : %s" % out.text.cget("state"))
        _say("  proxy alias : %s" % bool(
            out.text.tk.call("interp", "alias", "", out.text._w)))
        _say("  content end : %s" % out.text.index("end-1c"))
        win = getattr(out, "_popout", None)
        if win is not None and win.winfo_exists():
            _say("  pop-out     : %s state=%s peer_end=%s"
                 % (win.geometry(), win.state(), out._peer.index("end-1c")))
        _say("  paned height: %d, panes=%d"
             % (paned.winfo_height(), len(paned.panes())))

    def _report_log(self, step: "dict[str, Any]") -> None:
        """The OUTPUT pane's text, optionally from the last line naming `from`.

        The pane is where Doctor writes, so this is what a user would read.
        `from` reports whether the marker was found at all: a block that is
        absent and a block that was never looked for must not read alike.
        """
        out = getattr(self._app, "_output", None)
        if out is None:
            _say("drive: log: the app has no output pane")
            return
        lines = out.text.get("1.0", "end-1c").splitlines()
        marker = str(step.get("from", ""))
        if marker:
            hits = [i for i, line in enumerate(lines) if marker in line]
            _say("drive: log: marker %r found=%s" % (marker, bool(hits)))
            lines = lines[hits[-1]:] if hits else []
        _say("drive: log: %d line(s)" % len(lines))
        for line in lines[:int(step.get("max", 200))]:
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
            _say("    %-28s carriage: %-8s reach: %-9s delivery: %-17s "
                 "copy: %-10s healthy: %-5s %s B ~%s tok  %s"
                 % (project.name[:28], project.carriage, project.reach,
                    project.delivery, project.copy_state or "-",
                    project.healthy,
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

    # -- evidence ---------------------------------------------------------

    def _check_value(self, step: "dict[str, Any]") -> "tuple[bool, Any]":
        """One `expect` check as (satisfied, what was actually seen)."""
        check = str(step.get("check", ""))
        text = str(step.get("text", ""))
        want = text.strip().lower()
        if check == "tab":
            notebook = getattr(self._app, "nb", None)
            label = str(notebook.tab(notebook.select(), "text")) if notebook else ""
            return want in label.strip().lower(), label
        if check == "dialog_open":
            dialog = self._dialog
            alive = dialog is not None and bool(dialog.winfo_exists())
            return alive, alive
        if check == "log_contains":
            out = getattr(self._app, "_output", None)
            body = out.text.get("1.0", "end") if out is not None else ""
            return want in body.lower(), "%d chars" % len(body)
        if check == "widget_text":
            hit = next((_widget_text(w).strip() for w in _walk(self._target(step))
                        if want and want in _widget_text(w).lower()), "")
            return bool(hit), hit
        if check == "diagnostic_contains":
            # The LIVE ledger, not a report file: nothing is written until the
            # run ends, so a check against the file could only ever see the
            # previous run.
            return self._ledger.contains(text), self._ledger.summary()
        raise ValueError("unknown check %r" % check)

    def _do_expect(self, step: "dict[str, Any]") -> None:
        """Assert state, waiting up to `within_ms` for it to become true.

        Tk state settles asynchronously, so a single look is a race. Failure is
        permanent: it is written to the ledger, and a later success on the same
        id does not erase it.
        """
        ident = str(step.get("id") or step.get("check") or "expect")
        within = int(step.get("within_ms", 0))
        began = time.monotonic()
        expected = {k: step[k] for k in ("check", "text") if k in step}

        def poll() -> bool:
            """True while still waiting."""
            elapsed = int((time.monotonic() - began) * 1000)
            try:
                ok, actual = self._check_value(step)
            except Exception as exc:                         # noqa: BLE001
                self._ledger.expect(ident, False, expected=expected,
                                    actual=None, elapsed_ms=elapsed,
                                    reason="check raised: %s" % exc)
                _say("drive: expect %s: FAILED (%s)" % (ident, exc))
                return False
            if ok:
                self._ledger.expect(ident, True, expected=expected,
                                    actual=actual, elapsed_ms=elapsed)
                _say("drive: expect %s: ok (%d ms)" % (ident, elapsed))
                return False
            if elapsed >= within:
                self._ledger.expect(ident, False, expected=expected,
                                    actual=actual, elapsed_ms=elapsed,
                                    reason="not satisfied within %d ms" % within)
                _say("drive: expect %s: FAILED, saw %r" % (ident, actual))
                return False
            return True

        if poll():
            self._hold, self._hold_ms = poll, 100

    def _do_expect_clean(self, step: "dict[str, Any]") -> None:
        """The application said nothing wrong -- and kept quiet for a moment.

        `settle_ms` is the observation window. Without it a callback that posts
        work and throws 200 ms later would sail past a check made at 100 ms:
        the expectation would pass and the run still not be clean.
        """
        settle = int(step.get("settle_ms", 1000))
        began = time.monotonic()
        deadline = began + settle / 1000

        def poll() -> bool:
            elapsed = int((time.monotonic() - began) * 1000)
            bad = self._ledger.drive_diagnostics()
            if bad:
                self._ledger.expect("expect_clean", False,
                                    expected="no diagnostics",
                                    actual=self._ledger.summary(),
                                    elapsed_ms=elapsed,
                                    reason=self._ledger.summary())
                _say("drive: expect_clean: FAILED -- %s" % self._ledger.summary())
                return False
            if time.monotonic() >= deadline:
                self._ledger.expect("expect_clean", True,
                                    expected="no diagnostics", actual="none",
                                    elapsed_ms=elapsed)
                _say("drive: expect_clean: ok (%d ms window)" % settle)
                return False
            return True

        if poll():
            self._hold, self._hold_ms = poll, 100

    def _do_log_report(self, step: "dict[str, Any]") -> None:
        """Print what the ledger holds, so the transcript carries it too."""
        _say("drive: ledger: startup -- %s"
             % self._ledger.summary(drive_ledger.STARTUP))
        _say("drive: ledger: drive   -- %s"
             % self._ledger.summary(drive_ledger.DRIVE))
        passed = sum(1 for e in self._ledger.expectations if e["passed"])
        _say("drive: ledger: %d/%d expectation(s) passed"
             % (passed, len(self._ledger.expectations)))

    def _report_path(self) -> "str | None":
        if not self._script:
            return None
        return str(Path(self._script).with_suffix(".report.json"))

    def _finalize(self, reason: str) -> "dict[str, Any]":
        """Snapshot, write the report, once. Never raises.

        Order matters: hooks are uninstalled and the ledger frozen BEFORE the
        report is built, and the report exists on disk BEFORE anything exits.
        A second caller (`atexit` after `quit`) gets the first result and
        writes nothing again.
        """
        git_exe = str(getattr(getattr(self._app, "_cfg", None), "git_exe", "") or "")
        first = not self._ledger.finalized
        report = self._ledger.finalize(lambda: drive_ledger.build_report(
            self._ledger, script=self._script, run_id=self._run_id,
            started=self._started, reason=reason, shots=self._shots,
            git_exe=git_exe, cwd=str(Path(__file__).resolve().parent)))
        path = self._report_path()
        if first and path:
            try:
                drive_ledger.write_report_atomically(path, report)
                _say("drive: report -> %s" % path)
            except (OSError, ValueError, TypeError) as exc:
                _say("drive: could not write %s: %s" % (path, exc))
        return report

    def _do_quit(self, step: "dict[str, Any]") -> None:
        report = self._finalize("quit")
        code = 0 if report["passed"] else 1
        _say("drive: quit (%s, exit %d)"
             % ("passed" if code == 0 else "FAILED", code))
        # `destroy` alone leaves the tray thread holding the process open,
        # and a diagnostic run that never exits cannot be scripted.
        # `os._exit` skips interpreter shutdown, which includes flushing --
        # so `_exit_now` flushes first, and the exit is on a timer so the
        # destroy below has run. A driver built by hand (no script) never
        # exits the process: that would take the test runner with it.
        if self._script:
            timer = threading.Timer(0.2, self._exit_now, args=(code,))
            timer.daemon = True
            timer.start()
        try:
            self._app.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def _exit_now(code: int = 0) -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        os._exit(code)
