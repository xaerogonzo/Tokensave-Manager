"""Cross-reference RoadmapEntry objects against git history.

Returns AuditFinding records that surface:
  - "promotable" : status is 🟡 AND recent git evidence found (commits
                   mentioning the title or touching mentioned files).
  - "stale"      : status is 🟡 AND no evidence in the last 30 days.
  - "ok"         : all other cases (completed ✅, future 🔮/💭/💤, or
                   in-progress with fresh evidence).

Pure-ish module — performs read-only subprocess calls (git log).
No Tkinter.  Safe to call from a background thread.
"""

from __future__ import annotations

import datetime
import subprocess
from dataclasses import dataclass, field

from helpers.roadmap_parser import RoadmapEntry

try:
    from constants import CREATE_NO_WINDOW
except ImportError:
    CREATE_NO_WINDOW = 0


_STALE_DAYS = 30
_IN_PROGRESS = {"🟡"}
_FUTURE      = {"🔮", "💭"}
_DONE        = {"✅", "💤"}


@dataclass
class AuditFinding:
    entry:                    RoadmapEntry
    flag:                     str          # "stale" | "promotable" | "ok"
    evidence:                 list = field(default_factory=list)
    days_since_last_evidence: int | None = None


def audit_entries(
    entries: list[RoadmapEntry],
    repo_path: str,
    git_exe: str,
    tokensave_exe: str = "",
) -> list[AuditFinding]:
    """Return AuditFinding for each entry, ordered to match ``entries``."""
    now = datetime.datetime.now()
    findings: list[AuditFinding] = []

    for entry in entries:
        if entry.status in _DONE:
            findings.append(AuditFinding(entry=entry, flag="ok"))
            continue

        evidence: list[str] = []
        last_date: datetime.datetime | None = None

        # --- git log --grep on the first 3 words of the title ---------------
        keywords = entry.title.split()[:3]
        if keywords:
            grep_term = " ".join(keywords)
            _git_grep(repo_path, git_exe, grep_term,
                      evidence, _update_date(last_date), _max_hits=8)
            # Re-derive last_date from the returned evidence (we can't mutate
            # a closure variable from a helper; compute inline instead).
            last_date = _git_grep_date(repo_path, git_exe, grep_term, now)

        # --- git log on mentioned files --------------------------------------
        for fpath in entry.mentioned_files[:3]:
            fd, fl = _git_file_log(repo_path, git_exe, fpath)
            evidence.extend(s for s in fd if s not in evidence)
            if fl and (last_date is None or fl > last_date):
                last_date = fl

        evidence = evidence[:8]

        days: int | None = None
        if last_date:
            days = max(0, (now - last_date).days)

        # --- Determine flag --------------------------------------------------
        if entry.status in _FUTURE:
            flag = "ok"
        elif entry.status in _IN_PROGRESS:
            if evidence:
                flag = "promotable"
            elif days is None or days >= _STALE_DAYS:
                flag = "stale"
            else:
                flag = "ok"
        else:
            flag = "ok"

        findings.append(AuditFinding(
            entry=entry,
            flag=flag,
            evidence=evidence,
            days_since_last_evidence=days,
        ))

    return findings


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_git(args: list[str], repo_path: str, git_exe: str) -> str:
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, *args],
            capture_output=True, text=True, timeout=12,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _update_date(current: datetime.datetime | None):
    # Returns a no-op updater (we derive dates inline in audit_entries).
    return current


def _git_grep(repo_path, git_exe, grep_term,
               evidence: list, _ignored, _max_hits=8) -> None:
    raw = _run_git(
        ["log", "--oneline", f"--grep={grep_term}", "-i",
         "--format=%s", f"-{_max_hits}"],
        repo_path, git_exe,
    )
    if raw:
        for line in raw.splitlines():
            if line and line not in evidence:
                evidence.append(line)


def _git_grep_date(
    repo_path: str, git_exe: str, grep_term: str,
    fallback_now: datetime.datetime,
) -> datetime.datetime | None:
    raw = _run_git(
        ["log", "--oneline", f"--grep={grep_term}", "-i",
         "--format=%ai", "-1"],
        repo_path, git_exe,
    )
    if not raw:
        return None
    date_str = raw.split()[0]
    try:
        return datetime.datetime.fromisoformat(date_str)
    except ValueError:
        return None


def _git_file_log(
    repo_path: str, git_exe: str, file_path: str
) -> tuple[list[str], datetime.datetime | None]:
    raw = _run_git(
        ["log", "--format=%ai\t%s", "-5", "--", file_path],
        repo_path, git_exe,
    )
    subjects: list[str] = []
    latest: datetime.datetime | None = None
    for line in raw.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                date = datetime.datetime.fromisoformat(parts[0].split()[0])
                if latest is None or date > latest:
                    latest = date
            except ValueError:
                pass
            subj = parts[1].strip()
            if subj and subj not in subjects:
                subjects.append(subj)
    return subjects, latest
