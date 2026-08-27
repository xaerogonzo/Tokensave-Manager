"""helpers/doctor_service.py — running `tokensave doctor` and reading it back.

The Doctor's *rules* moved to :mod:`helpers.doctor_rules` when CI needed to run
them on a headless runner. This module is the other half: invoking the doctor
binary, parsing its transcript, and deciding what a purge actually accomplished.

It exists for the same reason `doctor_rules` did. ``controllers/doctor_ctrl``
imports Tk at module scope, so nothing there is reachable from the headless
CLI — and a CLI that re-derived "how do we run doctor" would become a second,
silently diverging definition of it. That module's own comment already warned
about this ("two subtly different invocations of the same command is the drift
this exists to prevent"); the extraction makes the shared definition reachable
rather than merely intended.

Moved verbatim, docstrings included, because they carry measured facts that
are expensive to rediscover — notably that doctor writes its report to
**stderr** and emits ANSI regardless of ``NO_COLOR``.

The controller keeps thin delegating methods, so ``DoctorController``'s API is
unchanged for the Projects tab, ``housekeeping_ctrl`` and
``dialogs/housekeeping``.

No Tk, and no third-party imports at module scope.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

from constants import CREATE_NO_WINDOW, _ANSI
from helpers import housekeeping


# ONE definition of how `tokensave doctor` is invoked, shared by the streaming
# run, the housekeeping scan, and the ConPTY purge. Two subtly different
# invocations of the same command is the drift this exists to prevent: without
# it the streaming path could get clean text while the scan path got raw ANSI.

def doctor_env() -> dict:
    """Environment for every `tokensave doctor` invocation.

    ``NO_COLOR`` + ``TERM=dumb`` are set as a best effort, but measured against
    v7.9.0 doctor ignores both and emits ANSI regardless — so every caller must
    strip escapes itself rather than trusting the environment. They stay because
    they cost nothing and a future version may honour them.

    Note also that doctor writes its entire report to **stderr**, not stdout.
    Any caller capturing output must merge the two (``stderr=STDOUT``) or it
    will get an empty transcript and read it as "nothing to report".
    """
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


@dataclass
class DoctorScanResult:
    """Outcome of a non-interactive doctor run used purely to observe state.

    ``ok=False`` means "we don't know", which callers must render differently
    from "nothing to clean" — an empty entry list from a failed scan would
    otherwise read as a clean bill of health.
    """
    ok: bool
    transcript: str = ""
    exit_code: "int | None" = None
    error: str = ""


@dataclass
class PurgeResult:
    """Machine-readable outcome of a purge attempt.

    Replaces the old convention of inferring success by re-parsing log text.

    ``handed_off`` is a **first-class outcome, not an error**: it means the
    operation was correctly delegated to a terminal the user drives, and the
    result is not yet known. The Manager must never report success merely
    because cmd.exe launched or because the terminal process exited — only a
    post-operation scan settles that, which is what `verification_status`
    carries. ``cancelled`` stays distinct from ``process_error`` so backing out
    never reads as a transport failure.
    """
    status: str                       # see the constants below
    exit_code: "int | None" = None
    answers_sent: int = 0
    transcript: str = ""
    stale_before: list = field(default_factory=list)
    stale_after: list = field(default_factory=list)
    verification_status: str = ""     # one of the VERIFY_* values, once known
    error: str = ""

    SUCCESS = "success"
    HANDED_OFF = "handed_off"
    PROCESS_ERROR = "process_error"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"

    @property
    def succeeded(self) -> bool:
        return self.status == PurgeResult.SUCCESS


# Verification vocabulary. The post-operation scan — not the mechanism — is
# what determines the outcome, so these are the values that actually matter.
VERIFY_VERIFIED = "verified"        # nothing left
VERIFY_PARTIAL = "partial"          # fewer than before, but not zero
VERIFY_NO_CHANGE = "no_change"      # exactly as many as before
VERIFY_UNVERIFIED = "unverified"    # the scan itself failed — outcome unknown

VERIFY_LABELS = {
    VERIFY_VERIFIED:   "✓ Purge verified — no stale entries remain",
    VERIFY_PARTIAL:    "⚠ Partially purged — {n} remaining",
    VERIFY_NO_CHANGE:  "⚠ No change detected — {n} still reported",
    VERIFY_UNVERIFIED: "? Outcome could not be verified",
}


# One human sentence per non-success status. Keeps `_after_purge` free of a
# branching wall and keeps the wording in one place.
PURGE_EXPLANATIONS = {
    PurgeResult.PROCESS_ERROR:
        "tokensave doctor could not be run.",
    PurgeResult.VERIFICATION_FAILED:
        "The purge ran but stale entries are still reported afterwards.",
    PurgeResult.CANCELLED:
        "Purge cancelled.",
}


# doctor emits `run `tokensave install --agent <id>`` on any integration it
# considers unconfigured. Agent ids are lowercase alnum + hyphen (e.g. roo-code).
_INSTALL_NAG_RE = re.compile(r"tokensave install\s+--agent\s+([a-z0-9-]+)")


def extract_install_nags(lines: list) -> tuple:
    """Parse doctor output into (actionable_agents, other_count).

    An agent is actionable only when it is BOTH installed on this machine
    AND not already wired. Both halves matter:

    * installed — doctor checks all 20 integrations and nags about every one
      it can't find, which on a typical machine is ~18 agents the user has
      never heard of.
    * not already wired — several agents cover multiple surfaces under one
      ``--agent`` id (Copilot spans VS Code, Insiders, the CLI, JetBrains),
      and doctor emits a separate nag per missing surface. Checking only
      "installed" meant a fully-wired Copilot got re-offered on EVERY doctor
      run, because the nags were about Insiders/JetBrains — surfaces the
      user doesn't have and that re-running install cannot create. A prompt
      that reappears no matter how many times you accept it is worse than
      no prompt.

    Deliberately does NOT treat doctor's ✘-vs-! severity as the signal:
    upstream marks some optional integrations (OpenCode, Kiro) as ✘ even
    when the accompanying text says "if you use it", so severity alone
    would resurface exactly the noise this filters out.
    """
    from helpers.mcp import _tokensave_agent_installed, _tokensave_agent_wired
    seen: list = []
    for line in lines:
        m = _INSTALL_NAG_RE.search(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    actionable = [a for a in seen
                  if _tokensave_agent_installed(a) and not _tokensave_agent_wired(a)]
    return actionable, len(seen) - len(actionable)


def scan_stale(project_root: str, tokensave_exe: str,
               timeout: float = 120.0) -> DoctorScanResult:
    """Run doctor non-interactively and capture the transcript. Blocking.

    Used by the housekeeping surface to observe state without touching it.
    A timeout or launch failure returns ``ok=False`` rather than an empty
    transcript, so the caller can distinguish "nothing to clean" from
    "we couldn't find out".
    """
    try:
        r = subprocess.run(
            [tokensave_exe, "doctor"],
            cwd=project_root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=doctor_env(), creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except FileNotFoundError:
        return DoctorScanResult(False, error="tokensave executable not found")
    except subprocess.TimeoutExpired:
        return DoctorScanResult(
            False, error=f"tokensave doctor timed out after {timeout:.0f}s")
    except OSError as e:
        return DoctorScanResult(False, error=str(e))
    return DoctorScanResult(
        True, transcript=_ANSI.sub("", r.stdout or ""),
        exit_code=r.returncode)


def purge_stale(project_root: str, tokensave_exe: str,
                baseline: "list | None" = None) -> PurgeResult:
    """Begin a purge. Finishes in a terminal the user drives.

    Flow::

        baseline scan (reused, never re-run)  ->  handed_off  ->  verify

    tokensave only offers its purge prompt when ``isatty()`` is true, so the
    Manager cannot answer it in-process. Driving a pseudoconsole was
    implemented and abandoned — see docs/WINDOWS_CONPTY_FINDINGS.md — and it
    would only have removed a manual step, not made the outcome any more
    trustworthy. `verify_purge` is what settles that either way.

    ``baseline`` lets the caller pass a scan it already performed, so the
    purge does not run ``tokensave doctor`` a second time for no reason.
    Whatever list is used here is also the comparison point for
    `verify_purge` — one scan, two jobs.

    A ``handed_off`` return is NOT a failure. It means the operation is now
    in the user's terminal and its outcome is genuinely unknown until
    `verify_purge` says otherwise.
    """
    if baseline is None:
        before_scan = scan_stale(project_root, tokensave_exe)
        if not before_scan.ok:
            return PurgeResult(PurgeResult.PROCESS_ERROR,
                               error=before_scan.error)
        before = housekeeping.parse_stale_entries(before_scan.transcript)
    else:
        before = list(baseline)

    if not before:
        return PurgeResult(PurgeResult.SUCCESS, stale_before=[],
                           stale_after=[],
                           verification_status=VERIFY_VERIFIED)

    return PurgeResult(PurgeResult.HANDED_OFF, stale_before=before)


def verify_purge(project_root: str, tokensave_exe: str,
                 stale_before: list) -> PurgeResult:
    """Re-scan and report what actually happened. Blocking.

    This is the authoritative step. The Manager never concludes anything
    from the fact that a terminal opened or exited — only from comparing a
    fresh scan against the baseline. A scan that fails yields
    ``unverified``, which is deliberately NOT the same as "no change".
    """
    after_scan = scan_stale(project_root, tokensave_exe)
    if not after_scan.ok:
        return PurgeResult(
            PurgeResult.VERIFICATION_FAILED, stale_before=stale_before,
            verification_status=VERIFY_UNVERIFIED, error=after_scan.error)

    after = housekeeping.parse_stale_entries(after_scan.transcript)
    if not after:
        vstatus = VERIFY_VERIFIED
    elif len(after) < len(stale_before):
        vstatus = VERIFY_PARTIAL
    else:
        vstatus = VERIFY_NO_CHANGE

    return PurgeResult(
        PurgeResult.SUCCESS if vstatus == VERIFY_VERIFIED
        else PurgeResult.VERIFICATION_FAILED,
        stale_before=stale_before, stale_after=after,
        verification_status=vstatus)


def verification_label(result: PurgeResult) -> str:
    """Human sentence for a verification outcome."""
    template = VERIFY_LABELS.get(result.verification_status, "")
    return template.format(n=len(result.stale_after)) if template else ""
