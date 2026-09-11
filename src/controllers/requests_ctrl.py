"""requests_ctrl.py - the Manager's IPC inbox.

Extracted from `App` (2026-09-11), which had 53 direct methods against a cap
of 40. These twelve are the whole of one concern - poll a per-project inbox,
validate what is in it, dispatch it to an allowlisted handler - and moving
them takes the class to 41 on its own. The startup-check cluster follows for
the last one.

Low blast radius is why this cluster and not another: `App` reaches it
through a single `self._requests.start()`, and nothing outside calls in.
The commit cluster looked similar in size but is injected into controllers
and dialogs as `on_commit=` in several places, so moving it would have
required delegating wrappers - which put the method count straight back.

`_REQUEST_HANDLERS` is an allowlist, not a lookup convenience: anything on
the machine can write into the inbox, so a request naming an unknown action
is REJECTED rather than ignored. That property travels with this file.

Callback injection, as with the other sub-controllers. `get_projects` is a
callable rather than the controller itself because this is constructed in
`App.__init__`, before `_build()` has made the Projects tab - passing the
attribute directly would capture `None` forever.
"""
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


class RequestsController:
    """Drains the file-based request inbox the CLI and extension write to."""

    def __init__(
        self,
        root,
        cfg: "ManagerConfig",
        get_projects: Callable,
        open_commit_dialog: Callable,
        get_current_proc: Callable,
        get_project_list: Callable,
    ) -> None:
        self._root               = root
        self._cfg                = cfg
        self._get_projects       = get_projects
        self._open_commit_dialog = open_commit_dialog
        self._get_current_proc   = get_current_proc
        # App's `projects` LIST (not `_projects`, the tab controller). Reached
        # through a callable because the original read it as
        # `getattr(self, "projects", [])` -- string-based, so a move to any
        # other object silently returns the default forever instead of
        # raising. Found by reading the moved body; no test failed on it.
        self._get_project_list  = get_project_list

    def start(self, delay_ms: int = 1_500) -> None:
        """Begin polling. Deferred so the first tick lands after the UI."""
        self._root.after(delay_ms, self._request_tick)

    #: While something is queued. A handoff nobody sees land stops being used.
    _REQUEST_TICK_MS = 2_000
    #: While the inbox is empty. Still cheap — one `listdir` per project — but
    #: there is no reason to spin at handoff speed when nothing is waiting.
    _REQUEST_IDLE_MS = 6_000

    def _request_tick(self) -> None:
        """Drain the inbox, then reschedule at a cadence matching the queue."""
        pending = 0
        try:
            # The same idle guard the refresh uses: a request opens a dialog,
            # and doing that on top of a running operation would interrupt work
            # the user started themselves. Not idle means "try again shortly",
            # which is what a non-zero count asks for.
            ctrl_idle = (not hasattr(self, "_projects")
                         or self._get_projects().current_proc is None)
            if self._get_current_proc() is None and ctrl_idle:
                pending = self._drain_requests()
            else:
                pending = 1
        except Exception:                           # noqa: BLE001
            # A drain that raises must not strand the timer — that would take
            # the whole channel down silently until the next restart.
            log.exception("request drain failed")
        finally:
            self._root.after(self._REQUEST_TICK_MS if pending
                       else self._REQUEST_IDLE_MS, self._request_tick)

    # ── Request inbox ─────────────────────────────────────────────────────
    #
    # An external tool — normally the VS Code extension — asks the running
    # Manager to open a dialog by writing a file. Propose-only holds: every
    # action here opens a window in front of a person, and none of them
    # commits, applies or approves anything.
    #
    # The payload is untrusted: the file sits in the project directory and
    # anything on the machine can write it. `manager_ipc.validate` is therefore
    # run again HERE, not only where the request was written, and the project
    # is checked against this Manager's own configured search roots. A
    # `--project` argument is not an authorization.

    def _known_roots(self) -> list:
        from helpers.detection import _root_path
        return [_root_path(r) for r in (self._cfg.search_roots or [])]

    def _request_projects(self) -> list:
        """Projects whose inboxes are worth reading: the ones on screen."""
        return [p["path"] for p in self._get_project_list()
                if p.get("path")]

    def _drain_requests(self) -> int:
        """Dispatch queued requests. Returns how many are still waiting."""
        from helpers import manager_ipc

        roots = self._known_roots()
        outstanding = 0
        for path in self._request_projects():
            try:
                requests, skipped = manager_ipc.load_requests(path, roots)
            except Exception:                       # noqa: BLE001
                continue
            for bad in skipped:
                # Never silent. A request that simply disappears reads to the
                # user as "nothing happened", which is worse than an error.
                log.warning("ignored malformed request %s: %s",
                            bad["path"], bad["reason"])
            for request in requests:
                if not self._dispatch_request(manager_ipc, path, request):
                    outstanding += 1
            if requests:
                manager_ipc.prune_acknowledgements(path)
        return outstanding

    def _dispatch_request(self, manager_ipc, path: str, request: dict) -> bool:
        """Act on one request. True when it left the queue.

        Four outcomes, and the distinction between the last two is the point:
        a *transient* failure keeps the request so the next tick retries it,
        because deleting on the first stumble silently loses what the user
        asked for. Only the attempt limit gives up, and it says `quarantined`
        rather than `drained`.
        """
        ident = request.get("id", "")

        # Recovery: a crash between the acknowledgement write and the pending
        # delete leaves a request that is queued AND already done. Finish the
        # interrupted delete instead of acting twice.
        already = manager_ipc.completed_outcome(path, ident)
        if already:
            log.info("request %s was already %s; clearing", ident, already)
            manager_ipc.acknowledge(path, request, already,
                                    "recovered an interrupted acknowledgement")
            return True

        handler = self._REQUEST_HANDLERS.get(request["action"])
        if handler is None:
            manager_ipc.acknowledge(path, request, manager_ipc.REJECTED,
                                    f"no handler for {request['action']!r}")
            return True

        try:
            done = handler(self, path, request)
        except Exception as exc:                    # noqa: BLE001
            log.exception("request %s raised", ident)
            manager_ipc.acknowledge(path, request, manager_ipc.REJECTED,
                                    f"{type(exc).__name__}: {exc}")
            return True

        if done:
            log.info("drained %s (%s)", ident, request["action"])
            manager_ipc.acknowledge(path, request, manager_ipc.DRAINED,
                                    f"opened {request['action']}")
            return True

        attempts = manager_ipc.record_attempt(request)
        if attempts >= manager_ipc.MAX_ATTEMPTS:
            manager_ipc.acknowledge(
                path, request, manager_ipc.QUARANTINED,
                f"gave up after {attempts} attempt(s)")
            return True
        return False

    def _focus_project(self, path: str) -> bool:
        """Select `path` in the Projects tab. False when the row is not there.

        Every handler below reaches code that reads the *selected* project, so
        this runs first. A project the Manager has not discovered yet is a
        transient condition, not a bad request.
        """
        if not hasattr(self, "_projects"):
            return False
        return self._get_projects().select_project(path)

    def _req_open_project(self, path: str, request: dict) -> bool:
        return self._focus_project(path)

    def _req_commit(self, path: str, request: dict) -> bool:
        """Seed the EXISTING commit-request handoff and open the dialog.

        Routed through `commit_request.json` rather than a second seeding
        mechanism: `GitCommitDialog` already reads that file, pre-checks only
        those paths, and consumes it on commit. Adding a parallel path would
        be a second way to do the same thing, with its own bugs.
        """
        from helpers.commit_request import write_commit_request
        if not self._focus_project(path):
            return False
        payload = request.get("payload", {})
        write_commit_request(path, payload.get("files", []),
                             payload.get("scope", ""), payload.get("note", ""))
        self._open_commit_dialog(path)
        return True

    def _req_doctor(self, path: str, request: dict) -> bool:
        if not self._focus_project(path):
            return False
        # `cmd_doctor` lives on the command bar rather than the tab
        # controller — the menu binds straight to it, and this follows the
        # same route rather than adding a pass-through nobody else uses.
        self._get_projects()._cmd_bar.cmd_doctor()
        return True

    def _req_savings(self, path: str, request: dict) -> bool:
        from dialogs.cost_viewer import SavingsDialog
        self._focus_project(path)
        SavingsDialog(self._root, self._cfg, path)
        return True

    def _req_test_manager(self, path: str, request: dict) -> bool:
        from dialogs.test_manager import TestManagerDialog
        self._focus_project(path)
        TestManagerDialog(self._root, path, self._cfg)
        return True

    def _req_doc_updates(self, path: str, request: dict) -> bool:
        if not self._focus_project(path):
            return False
        self._get_projects().cmd_doc_updates()
        return True

    #: action -> handler. An allowlist, matching `manager_ipc.ACTIONS`; a
    #: request naming anything else is rejected rather than ignored.
    _REQUEST_HANDLERS = {
        "open-project": _req_open_project,
        "commit": _req_commit,
        "doctor": _req_doctor,
        "savings": _req_savings,
        "test-manager": _req_test_manager,
        "doc-updates": _req_doc_updates,
    }

