"""Test gap reporting — cross-references a branch diff with coverage gaps.

Given a base branch ref and a project root, returns the list of changed
``src/*.py`` files that have no corresponding test file yet.  Used by the
PR draft dialog to surface one-click scaffold actions alongside the PR body.

No Tkinter imports.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Literal

from constants import CREATE_NO_WINDOW

TemplateHint = Literal["pure_helper", "subprocess_helper", "dialog_tk", "blank"]


# Templates whose files genuinely benefit from automated tests. dialog_tk
# (Tk-coupled UI) and blank (unclassified) are low-ROI — they belong in the
# local test-gap panel for manual judgement, NOT in the drafted PR checklist.
_AUTOMATABLE_TEMPLATES: frozenset = frozenset({"pure_helper", "subprocess_helper"})


@dataclass
class SuggestedTest:
    """One test gap entry surfaced for a branch diff."""

    source_path: str        # absolute path to the source file
    rel_path: str           # path relative to project_root, for display
    template: TemplateHint  # scaffold template best suited for this file
    test_exists: bool       # always False here — only untested files are returned

    @property
    def requires_automation(self) -> bool:
        """True when this file warrants an automated test in the PR checklist.

        The classification lives HERE (not in the PR-draft renderer) so the
        text layer stays dumb: callers do ``if s.requires_automation`` and any
        future high-ROI template (e.g. ``api_client``) just joins
        ``_AUTOMATABLE_TEMPLATES``. Tk-dialog and unclassified files return
        False — they stay in the local panel, off the PR checklist.
        """
        return self.template in _AUTOMATABLE_TEMPLATES


def suggest_tests_for_diff(
    project_root: str,
    git_exe: str,
    base: str,
) -> list[SuggestedTest]:
    """Return one :class:`SuggestedTest` per changed ``src/*.py`` file with no test.

    Steps:
    1. ``git diff --name-only base...HEAD`` — changed files on this branch.
    2. ``scan_coverage_gaps`` — all src/ files that lack a test file.
    3. Intersect: changed AND untested.
    4. Detect the scaffold template hint from the file path + content.

    Returns an empty list if ``base`` is empty, git fails, or all changed
    files already have tests.
    """
    if not base:
        return []

    changed_rel = _changed_src_files(project_root, base, git_exe)
    if not changed_rel:
        return []

    gap_map = _build_gap_map(project_root)

    results: list[SuggestedTest] = []
    for rel in sorted(changed_rel):
        row = gap_map.get(rel)
        if row is None or row.has_tests:
            continue
        template = _detect_template(row.source_path, rel)
        results.append(SuggestedTest(
            source_path=row.source_path,
            rel_path=rel,
            template=template,
            test_exists=False,
        ))
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _changed_src_files(project_root: str, base: str, git_exe: str) -> list[str]:
    """Return project-relative paths of changed src/.py files on this branch.

    Uses ``git diff --name-only base...HEAD`` (triple-dot → merge-base diff).
    Only includes Python files under ``src/``.
    """
    _git = git_exe or "git"
    try:
        out = subprocess.check_output(
            [_git, "diff", "--name-only", f"{base}...HEAD"],
            cwd=project_root,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []

    results = []
    for line in out.splitlines():
        line = line.strip()
        # Normalise separators so the map lookup works on Windows
        norm = line.replace("\\", "/")
        if norm.startswith("src/") and norm.endswith(".py"):
            results.append(norm)
    return results


def _build_gap_map(project_root: str) -> "dict[str, object]":
    """Return ``{rel_path: CoverageRow}`` for all src/ files that lack tests."""
    try:
        from helpers.test_discovery import scan_coverage_gaps
        rows = scan_coverage_gaps(project_root)
    except Exception:
        return {}

    gap_map: dict[str, object] = {}
    for row in rows:
        # Normalise to forward-slash for consistent key lookups
        norm = row.rel_path.replace("\\", "/")
        gap_map[norm] = row
    return gap_map


def _detect_template(source_path: str, rel_path: str) -> TemplateHint:
    """Heuristically pick the best scaffold template for *source_path*.

    Rules (in priority order):
    - ``dialogs/``   → ``dialog_tk``
    - file contains ``subprocess.run`` or ``subprocess.Popen`` → ``subprocess_helper``
    - otherwise      → ``pure_helper``
    """
    norm = rel_path.replace("\\", "/")

    if "/dialogs/" in norm:
        return "dialog_tk"

    try:
        with open(source_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read(16_000)   # 16 KB is enough for a subprocess scan
        if "subprocess.run" in content or "subprocess.Popen" in content:
            return "subprocess_helper"
    except OSError:
        pass

    return "pure_helper"
