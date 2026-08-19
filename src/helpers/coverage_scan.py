"""Real line coverage for Test Manager Tab 2, and the cache that dates it.

Tab 2 has always used a filename heuristic: `src/helpers/foo.py` is "tested"
iff `tests/test_foo.py` exists. That reports ✓ for a file whose test asserts
nothing, which is worse than reporting nothing — it is a green tick that has
not been earned.

This reads what pytest-cov actually measured.

**The cache carries metadata or it lies.** Coverage numbers age badly: they are
tied to a commit and a branch, and a percentage rendered without a date reads
as current no matter how stale it is. Every entry records when it was
generated, on which branch and commit, and by what command, so the UI can say
"61.4% · 2 min ago" and refuse to show numbers gathered on a different branch.

**The 50% warning threshold here is a UX signal, NOT the CI gate.** CI enforces
`--cov-fail-under=14` in .github/workflows/ci.yml. The two numbers answer
different questions — "is this file worth attention?" versus "may this merge?"
— and conflating them would invite someone to "fix" one to match the other.

Pure module — stdlib only, no Tkinter.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

# Below this, a file is worth a second look. Not a gate; see the module
# docstring.
WARN_BELOW_PCT = 50.0

_CACHE_FILENAME = "last_coverage.json"
_CACHE_DIRNAME = ".tokensave-manager"


@dataclass(frozen=True)
class CoverageMeta:
    """Provenance for one coverage run. Without this the numbers are undated."""

    generated_at: float = 0.0
    branch:       str = ""
    commit_sha:   str = ""
    project_root: str = ""
    command:      str = ""

    @property
    def age_seconds(self) -> float:
        if not self.generated_at:
            return float("inf")
        return max(0.0, time.time() - self.generated_at)

    def age_label(self) -> str:
        """Human age, so a stale number cannot masquerade as a fresh one."""
        age = self.age_seconds
        if age == float("inf"):
            return "unknown age"
        if age < 90:
            return "just now"
        if age < 3600:
            return f"{int(age // 60)} min ago"
        if age < 86400:
            return f"{int(age // 3600)}h ago"
        return f"{int(age // 86400)}d ago"

    def matches(self, branch: str, commit_sha: str) -> bool:
        """Whether these numbers describe the tree the user is looking at.

        A mismatch is not an error — it is a reason to label the numbers as
        belonging to somewhere else rather than presenting them as current.
        """
        if self.branch and branch and self.branch != branch:
            return False
        if self.commit_sha and commit_sha and self.commit_sha != commit_sha:
            return False
        return True


@dataclass(frozen=True)
class CoverageResult:
    """Per-file percentages plus the provenance of the run that produced them."""

    percents: dict = field(default_factory=dict)   # rel_path -> float
    meta:     CoverageMeta = field(default_factory=CoverageMeta)

    def pct_for(self, rel_path: str) -> "float | None":
        """Coverage for one file, or None when the run did not measure it.

        None is distinct from 0.0: "not measured" and "measured, nothing
        covered" are different facts, and showing 0% for an unmeasured file
        would be inventing a result.
        """
        return self.percents.get(_norm(rel_path))

    def __bool__(self) -> bool:
        return bool(self.percents)


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def parse_coverage_json(text: str) -> dict:
    """Extract ``{rel_path: percent}`` from a ``coverage json`` report.

    Returns {} on anything unexpected rather than raising — a malformed
    report should degrade Tab 2 to the old heuristic, not break the dialog.
    """
    try:
        data = json.loads(text or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}            # a bare list/string is not a coverage report
    files = data.get("files")
    if not isinstance(files, dict):
        return {}
    out: dict = {}
    for path, entry in files.items():
        if not isinstance(entry, dict):
            continue
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        pct = summary.get("percent_covered")
        if isinstance(pct, (int, float)):
            out[_norm(path)] = round(float(pct), 1)
    return out


def cache_path(project_root: str) -> str:
    return os.path.join(project_root, _CACHE_DIRNAME, _CACHE_FILENAME)


def save_coverage(project_root: str, percents: dict,
                  meta: CoverageMeta) -> str:
    """Persist percentages together with their provenance."""
    path = cache_path(project_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "meta": {
            "generated_at": meta.generated_at or time.time(),
            "branch":       meta.branch,
            "commit_sha":   meta.commit_sha,
            "project_root": meta.project_root or project_root,
            "command":      meta.command,
        },
        "percents": percents,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def load_coverage(project_root: str) -> CoverageResult:
    """Read the cached run, or an empty result.

    A cache written before metadata existed still loads — its meta simply
    reports an unknown age, which the UI renders as such rather than
    pretending the numbers are fresh.
    """
    try:
        with open(cache_path(project_root), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return CoverageResult()
    if not isinstance(raw, dict):
        return CoverageResult()

    percents_raw = raw.get("percents")
    percents = {}
    if isinstance(percents_raw, dict):
        for k, v in percents_raw.items():
            if isinstance(v, (int, float)):
                percents[_norm(k)] = round(float(v), 1)

    m = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    meta = CoverageMeta(
        generated_at=float(m.get("generated_at") or 0.0),
        branch=str(m.get("branch") or ""),
        commit_sha=str(m.get("commit_sha") or ""),
        project_root=str(m.get("project_root") or ""),
        command=str(m.get("command") or ""),
    )
    return CoverageResult(percents=percents, meta=meta)


def format_cell(pct: "float | None", has_tests: bool) -> str:
    """Tab 2's coverage cell.

    Falls back to the filename heuristic's answer when there is no measured
    number, and marks it as a guess — an unqualified "✓" from a heuristic is
    the overclaim this module exists to retire.
    """
    if pct is None:
        return "— (no data)" if not has_tests else "? (file exists)"
    if pct < WARN_BELOW_PCT:
        return f"⚠ {pct:.0f}%"
    return f"✓ {pct:.0f}%"


def needs_attention(pct: "float | None") -> bool:
    """True for a measured percentage below the warning threshold."""
    return pct is not None and pct < WARN_BELOW_PCT
