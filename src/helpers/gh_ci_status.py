"""GitHub Actions status for the branch you are actually on.

Answers one question — "is CI green for this work?" — without a browser trip.

Two design points that are easy to get wrong and both produce confidently
misleading UI:

* **Branch.** The obvious implementation polls `master`. That is the one
  branch whose CI you do not care about while working on a feature branch: you
  would see master's reassuring green while your own run is red. This resolves
  the CURRENT branch and says so in the label.
* **"No result" is not a failure.** A brand-new branch has no runs at all, and
  a run whose jobs were all skipped proved nothing. If either renders red,
  branches start life looking broken and the badge teaches you to ignore it.
  `NO_RESULT` covers both.

Pure module — stdlib + the `gh` CLI, no Tkinter. Safe to call from a worker
thread; every failure is a state, never an exception.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from constants import CREATE_NO_WINDOW

# States. The first four mirror what the GitHub API reports; the last two are
# local conditions that must stay distinguishable from a real red run.
SUCCESS     = "success"
RUNNING     = "running"
FAILED      = "failed"
NO_RESULT   = "no_result"   # never ran, or ran and proved nothing
UNAVAILABLE = "unavailable"   # gh missing / not authenticated / not a repo

_GLYPH = {
    SUCCESS:     "🟢",
    RUNNING:     "🟡",
    FAILED:      "🔴",
    NO_RESULT:   "⚪",
    UNAVAILABLE: "⚫",
}

# GitHub's `status` field, when the run has not finished.
_IN_FLIGHT = {"queued", "in_progress", "waiting", "requested", "pending"}

# GitHub's `conclusion` field once it has.
_BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure",
                    "stale", "action_required"}

# Ran, but proved nothing. This project's CI has four jobs gated by `if:`
# predicates, so a wholly-skipped run is routine — painting it red would be
# the same false alarm as treating a queued run as a failure.
_INCONCLUSIVE = {"skipped", "neutral"}


@dataclass(frozen=True)
class CIStatus:
    """One branch's latest CI run, reduced to something renderable."""

    state: str
    branch: str = ""
    url: str = ""
    detail: str = ""

    @property
    def glyph(self) -> str:
        return _GLYPH.get(self.state, _GLYPH[UNAVAILABLE])

    @property
    def is_clickable(self) -> bool:
        """Only offer to open a run when there is actually a run to open."""
        return bool(self.url)

    def label(self) -> str:
        """One-line badge text. Always names the branch it is talking about."""
        where = self.branch or "?"
        if self.state == SUCCESS:
            return f"{self.glyph} CI passing on {where}"
        if self.state == RUNNING:
            return f"{self.glyph} CI running on {where}"
        if self.state == FAILED:
            extra = f" ({self.detail})" if self.detail else ""
            return f"{self.glyph} CI failed on {where}{extra}"
        if self.state == NO_RESULT:
            extra = f" ({self.detail})" if self.detail else ""
            return f"{self.glyph} no CI result for {where}{extra}"
        return f"{self.glyph} CI status unavailable"


def classify_run(run: dict) -> str:
    """Map one `gh run list` record onto a state.

    `status` wins over `conclusion`: an in-flight run carries a null
    conclusion, and treating null as "not success" is how a running build ends
    up rendered as a failure.
    """
    status = (run.get("status") or "").lower()
    if status in _IN_FLIGHT:
        return RUNNING
    conclusion = (run.get("conclusion") or "").lower()
    if conclusion == "success":
        return SUCCESS
    if conclusion in _BAD_CONCLUSIONS:
        return FAILED
    if conclusion in _INCONCLUSIVE:
        return NO_RESULT
    if not conclusion:
        # Finished with no conclusion recorded — unknown rather than failed.
        return RUNNING
    return FAILED


def get_latest_run_status(gh_exe: str, project_root: str,
                          branch: str, timeout: int = 15) -> CIStatus:
    """Latest Actions run for *branch*, as a CIStatus.

    Never raises. A missing `gh`, an unauthenticated one, a repo with no
    remote, or malformed output all resolve to UNAVAILABLE — which the badge
    renders differently from a failing run, because "we could not ask" and
    "the build is broken" must not look the same.
    """
    if not gh_exe or not branch:
        return CIStatus(UNAVAILABLE, branch=branch,
                        detail="no gh executable or branch configured")
    cmd = [gh_exe, "run", "list", "--branch", branch, "--limit", "1",
           "--json", "conclusion,status,url,displayTitle"]
    try:
        proc = subprocess.run(
            cmd, cwd=project_root, capture_output=True, text=True,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as exc:
        return CIStatus(UNAVAILABLE, branch=branch, detail=str(exc))
    except subprocess.TimeoutExpired:
        return CIStatus(UNAVAILABLE, branch=branch, detail="gh timed out")

    if proc.returncode != 0:
        return CIStatus(UNAVAILABLE, branch=branch,
                        detail=(proc.stderr or "").strip()[:120])
    try:
        runs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return CIStatus(UNAVAILABLE, branch=branch,
                        detail="could not parse gh output")
    if not isinstance(runs, list) or not runs:
        return CIStatus(NO_RESULT, branch=branch, detail="no runs yet")

    run = runs[0]
    if not isinstance(run, dict):
        return CIStatus(UNAVAILABLE, branch=branch,
                        detail="unexpected gh record shape")
    state = classify_run(run)
    return CIStatus(
        state,
        branch=branch,
        url=str(run.get("url") or ""),
        detail=str(run.get("displayTitle") or "")[:60],
    )
