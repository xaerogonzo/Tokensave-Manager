"""HousekeepingController — async orchestration for the Housekeeping dialog.

Sits between the dialog (presentation only) and the two things that actually
know how to do the work:

  * `DoctorController` — the single authority on running `tokensave doctor`,
    including the purge and the verification scan. This controller never
    shells out to tokensave itself; doing so would recreate the duplicate
    subprocess invocation that `doctor_ctrl.doctor_env` exists to prevent.
  * `helpers.housekeeping` — pure classification of whatever those runs return.

Every public method here is fire-and-forget from the caller's point of view: it
starts a worker thread and marshals the result back onto the Tk thread via
``after(0, …)``. The dialog therefore never blocks and never touches a thread.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from constants import C
from helpers import housekeeping
from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


@dataclass
class Findings:
    """Everything the dialog needs for one render.

    ``ok=False`` means the scan itself failed, which the dialog must show as an
    error rather than as an empty result — "nothing to clean" and "we couldn't
    find out" are different answers and must never look alike.
    """
    ok: bool
    entries: list = field(default_factory=list)      # list[StaleEntry]
    backups: "housekeeping.BackupScan" = field(
        default_factory=housekeeping.BackupScan)
    error: str = ""


@dataclass
class DeletionOutcome:
    path: str
    ok: bool
    reason: str = ""


class HousekeepingController:
    """Opens the Housekeeping dialog and runs its operations off the UI thread."""

    def __init__(self, root, cfg: "ManagerConfig", doctor, on_log: Callable) -> None:
        self._root = root
        self._cfg = cfg
        self._doctor = doctor
        self._on_log = on_log

    # ── Entry point ───────────────────────────────────────────────────────────

    def cmd_housekeeping(self, path: str) -> None:
        from dialogs.housekeeping import HousekeepingDialog
        HousekeepingDialog(self._root, path, self)

    # ── Async operations ──────────────────────────────────────────────────────

    def scan_async(self, path: str, cb: Callable) -> None:
        """Scan for stale entries + redundant backups."""
        def worker():
            try:
                result = self.scan(path)
            except Exception as e:                          # pragma: no cover
                log.exception("EXCEPTION in housekeeping scan")
                result = Findings(ok=False, error=str(e))
            self._root.after(0, cb, result)

        threading.Thread(target=worker, daemon=True,
                         name="housekeeping-scan").start()

    def scan(self, path: str) -> Findings:
        """Blocking scan. Separated from `scan_async` so it is directly testable."""
        scan = self._doctor.scan_stale(path)
        backups = housekeeping.find_redundant_backups(
            housekeeping.default_backup_roots())
        if not scan.ok:
            # Backups are still valid — a doctor failure says nothing about
            # them — but the stale side is unknown, so the whole scan reports
            # not-ok and the dialog renders that panel as an error.
            return Findings(ok=False, backups=backups, error=scan.error)

        entries = housekeeping.parse_stale_entries(scan.transcript)
        entries = housekeeping.classify_stale_entries(entries)
        entries = housekeeping.resolve_entry_source(entries, self._global_db())
        return Findings(ok=True, entries=entries, backups=backups)

    def purge_async(self, path: str, baseline: list, cb: Callable) -> None:
        """Purge, reusing ``baseline`` so doctor is not run again first.

        On a handoff this actually opens the terminal, rather than telling the
        user one was opened and leaving them to find it — the dialog's message
        has to be true.
        """
        def worker():
            from controllers.doctor_ctrl import PurgeResult
            try:
                result = self._doctor.purge_stale(path, baseline=baseline)
                if result.status == PurgeResult.HANDED_OFF:
                    try:
                        self._doctor.open_purge_terminal(path)
                    except OSError as e:
                        self._on_log(
                            f"  ✗ Could not open a terminal: {e}", C["red"])
                        result = PurgeResult(PurgeResult.PROCESS_ERROR,
                                             stale_before=baseline, error=str(e))
            except Exception as e:                          # pragma: no cover
                log.exception("EXCEPTION in housekeeping purge")
                result = PurgeResult(PurgeResult.PROCESS_ERROR, error=str(e))
            self._root.after(0, cb, result)

        threading.Thread(target=worker, daemon=True,
                         name="housekeeping-purge").start()

    def verify_async(self, path: str, baseline: list, cb: Callable) -> None:
        """Re-scan and compare against ``baseline`` — the authoritative step."""
        def worker():
            try:
                result = self._doctor.verify_purge(path, baseline)
            except Exception as e:                          # pragma: no cover
                log.exception("EXCEPTION in housekeeping verify")
                from controllers.doctor_ctrl import PurgeResult, VERIFY_UNVERIFIED
                result = PurgeResult(PurgeResult.VERIFICATION_FAILED,
                                     verification_status=VERIFY_UNVERIFIED,
                                     error=str(e))
            self._root.after(0, cb, result)

        threading.Thread(target=worker, daemon=True,
                         name="housekeeping-verify").start()

    def delete_backups_async(self, candidates: list, cb: Callable) -> None:
        """Delete verified duplicates one at a time, reporting each separately."""
        def worker():
            outcomes = [self._delete_one(c) for c in candidates]
            self._root.after(0, cb, outcomes)

        threading.Thread(target=worker, daemon=True,
                         name="housekeeping-delete").start()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _delete_one(self, cand) -> DeletionOutcome:
        """Revalidate, then delete. Never deletes a file that moved underneath us.

        The scan may be minutes old by the time the user clicks, so size, mtime
        and digest are all re-checked against what was recorded. A mismatch
        means the world changed and the safe answer is to skip, not to trust
        the stale finding.
        """
        name = os.path.basename(cand.path)
        try:
            if not housekeeping.revalidate_backup(cand):
                return DeletionOutcome(
                    name, False,
                    "changed since the scan — skipped for safety")
            os.remove(cand.path)
            self._on_log(f"  ✓ deleted {cand.path}", C["green"])
            return DeletionOutcome(name, True)
        except OSError as e:
            self._on_log(f"  ✗ could not delete {cand.path}: {e}", C["red"])
            return DeletionOutcome(name, False, str(e))

    @staticmethod
    def _global_db() -> str:
        return os.path.join(os.path.expanduser("~"), ".tokensave", "global.db")
