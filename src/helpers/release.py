"""Release-wizard helpers — pure functions used by ReleaseWizardDialog.

Includes:
  * Conventional-commit classification (`_CONVENTIONAL_RE`, `_TYPE_TO_SECTION`,
    `_SECTION_ORDER`).
  * Git-tag walk (`_last_release_tag`, `_commits_since`).
  * Changelog grouping (`_classify_commits_for_changelog`).
  * Version bumping (`_bump_version`, `_suggest_bump_kind`).
  * Notes rendering (`_render_release_notes`).
  * CHANGELOG patching (`_patch_changelog`).
  * Artefact zipping (`_zip_dist`, `_release_basename`, `_fmt_size`).

The three functions that shell out to git take an explicit `git_exe`
parameter (per Rule 1 — helpers don't read the GIT_EXE module global):
`_last_release_tag`, `_commits_since`, `_release_basename`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from constants import CREATE_NO_WINDOW


# ── Conventional-commit constants ────────────────────────────────────────────

# Conventional-commit subject parser. Matches the prefix (type), optional
# (scope), optional ! breaking-marker, then ': ' and the rest of the subject.
# Subject-only — never applied to body text (the body can contain unrelated
# matching strings that would cause false positives).
_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|perf|style|test|build|ci)"
    r"(?:\(([^)]+)\))?(!)?:\s*(.+)$"
)

# Map conventional prefix → section header in the changelog.
_TYPE_TO_SECTION = {
    "feat":     "Added",
    "fix":      "Fixed",
    "chore":    "Changed",
    "docs":     "Docs",
    "refactor": "Changed",
    "perf":     "Changed",
    "style":    "Changed",
    "test":     "Changed",
    "build":    "Changed",
    "ci":       "Changed",
}

# Order sections appear in the rendered notes. Breaking always wins.
_SECTION_ORDER = ["Breaking", "Added", "Fixed", "Changed", "Docs", "Other"]


# ── Git tag / log helpers (take git_exe per Rule 1) ──────────────────────────

def _last_release_tag(path: str, git_exe: str) -> str | None:
    """Return the most recent annotated/lightweight tag, or None if no tags."""
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    tag = proc.stdout.strip()
    return tag or None


def _commits_since(path: str, ref: str | None, git_exe: str) -> list:
    """Return commits between ``ref`` (exclusive) and HEAD (inclusive).

    Uses a custom git-log format with three fields per commit separated by
    \\x09 (tab), and records separated by \\x1f (unit separator). This lets
    multi-line bodies coexist with the field separator without ambiguity.

    Returns list of dicts: ``{"hash": str, "subject": str, "body": str}``.
    If ``ref`` is None (no prior tag), returns ALL commits.
    """
    range_spec = f"{ref}..HEAD" if ref else "HEAD"
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "log", range_spec,
             "--pretty=format:%H%x09%s%x09%b%x1f"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    commits = []
    # Each record ends with \x1f. Split, drop empty trailing, parse fields.
    for record in proc.stdout.split("\x1f"):
        if not record.strip():
            continue
        # Strip leading newline that git inserts between records
        record = record.lstrip("\n")
        parts = record.split("\x09", 2)
        if len(parts) < 2:
            continue
        h = parts[0]
        s = parts[1] if len(parts) >= 2 else ""
        b = parts[2] if len(parts) >= 3 else ""
        commits.append({"hash": h, "subject": s, "body": b})
    return commits


def _classify_commits_for_changelog(commits: list) -> dict:
    """Group commits into changelog sections by conventional-commit prefix.

    Rules:
      * Subject parsed via ``_CONVENTIONAL_RE`` (subject-only — body is
        excluded from the regex match so unrelated body text never causes
        false positives).
      * Substring check on body for ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:``
        escalates to ``Breaking`` regardless of subject prefix.
      * The ``!`` marker in the subject also forces ``Breaking``.
      * ``auto:`` commits (from the smart auto-commit Stop hook) are skipped
        entirely — internal noise, not changelog material.
      * Unmatched subjects fall into ``Other`` so nothing silently disappears.
      * Scope (the parenthetical) is preserved in the rendered line:
        ``feat(ui): button`` → ``- (ui) button``.

    Returns dict of section → list[str], in the order of ``_SECTION_ORDER``.
    Empty sections are omitted from the returned dict.
    """
    buckets = {name: [] for name in _SECTION_ORDER}
    for c in commits:
        subject = (c.get("subject") or "").strip()
        body    = c.get("body") or ""

        if not subject:
            continue

        # Skip auto-commit noise from the Stop hook helper.
        if subject.startswith("auto:"):
            continue

        m = _CONVENTIONAL_RE.match(subject)
        breaking_body = ("BREAKING CHANGE:" in body
                         or "BREAKING-CHANGE:" in body)

        if m:
            ctype, scope, bang, desc = m.group(1), m.group(2), m.group(3), m.group(4)
            line = f"({scope}) {desc}" if scope else desc
            if bang or breaking_body:
                buckets["Breaking"].append(line)
            else:
                section = _TYPE_TO_SECTION.get(ctype, "Other")
                buckets[section].append(line)
        else:
            # Non-conventional subject. If the body still mentions a BREAKING
            # CHANGE, surface it; else dump in Other.
            if breaking_body:
                buckets["Breaking"].append(subject)
            else:
                buckets["Other"].append(subject)

    # Drop empty sections from the returned dict, preserving order.
    return {name: buckets[name] for name in _SECTION_ORDER if buckets[name]}


def _bump_version(tag: str, kind: str) -> str:
    """Return the next tag for ``kind`` ∈ {patch, minor, major, hotfix}.

    Accepts tags with or without a leading ``v``. Output preserves the ``v``
    prefix if present. Non-semver inputs fall back to a date-stamped tag.

    Prerelease tail (``-alpha.1``, ``-rc.2``) and build metadata (``+abc``)
    are stripped before parsing per semver — we only bump the MAJOR.MINOR.PATCH
    core. Without this, a tag like ``v1.0.0-alpha.1`` would fail the
    ``int()`` parse on ``"0-alpha"`` and fall back to a date-stamped tag,
    producing three identical radio values in the wizard.

    Hotfix bump produces a four-part version: ``v1.0.4`` → ``v1.0.4.1``,
    ``v1.0.4.1`` → ``v1.0.4.2``. This is intentionally not strict semver —
    it's the "small adjustment on top of an existing release without
    starting a new patch series" idiom common in enterprise / Windows
    versioning (assembly versions, NuGet 4-part). The patch / minor / major
    bumps always normalise back to three parts, so a hotfix branch
    eventually merges into a clean semver line on the next regular release.
    """
    raw  = tag.lstrip("v") if tag else ""
    core = raw.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    try:
        major  = int(parts[0])
        minor  = int(parts[1])
        patch  = int(parts[2])
        # Optional 4th segment (hotfix counter). Default 0 if absent.
        hotfix = int(parts[3]) if len(parts) >= 4 else 0
    except (ValueError, IndexError):
        # Fallback for non-semver — date-stamped tag.
        from datetime import datetime as _dt
        return _dt.now().strftime("v%Y.%m.%d")

    prefix = "v" if (tag or "").startswith("v") else ""

    if kind == "major":
        return f"{prefix}{major + 1}.0.0"
    if kind == "minor":
        return f"{prefix}{major}.{minor + 1}.0"
    if kind == "hotfix":
        # Bump or introduce the 4th segment, leaving MAJOR.MINOR.PATCH intact.
        return f"{prefix}{major}.{minor}.{patch}.{hotfix + 1}"
    # default: patch — drop any hotfix segment, bump patch
    return f"{prefix}{major}.{minor}.{patch + 1}"


def _suggest_bump_kind(commits: list) -> str:
    """Pick the appropriate bump kind based on commit content.

    Returns ``"major"`` if any commit is breaking, ``"minor"`` if any new
    feature, else ``"patch"``. Mirrors conventional-commits semantics.
    """
    any_feat = False
    for c in commits:
        subject = (c.get("subject") or "").strip()
        body    = c.get("body") or ""
        if not subject:
            continue
        m = _CONVENTIONAL_RE.match(subject)
        if m and m.group(3):    # ! marker
            return "major"
        if "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body:
            return "major"
        if m and m.group(1) == "feat":
            any_feat = True
    return "minor" if any_feat else "patch"


# ── Notes rendering + CHANGELOG patching ─────────────────────────────────────

def _render_release_notes(version: str, date: str, sections: dict,
                          summary: str = "") -> str:
    """Render the canonical release-notes markdown.

    Single source of truth used by BOTH the wizard textarea pre-fill AND
    the CHANGELOG.md section body. The leading ``## [version] — date``
    header is included so the same text can be inserted into the changelog
    verbatim.

    ``version`` should be passed WITHOUT the leading ``v`` (we add the
    bracket notation here). ``sections`` is the dict returned by
    ``_classify_commits_for_changelog``.
    """
    clean_version = version.lstrip("v")
    lines = [f"## [{clean_version}] — {date}", ""]
    if summary:
        lines.append(summary.strip())
        lines.append("")
    for section in _SECTION_ORDER:
        items = sections.get(section)
        if not items:
            continue
        lines.append(f"### {section}")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    # Trim trailing blank line
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


# `_patch_changelog` was removed in Roadmap-2 Phase 2 — its idempotent
# replace logic moved into `helpers.changelog_patch.insert_changelog_release`
# (the single canonical implementation). Update the import in
# `dialogs/release_wizard.py` if you're searching for the new entry point.


# ── Artefact zipping ─────────────────────────────────────────────────────────

def _zip_dist(dist_path: str, zip_path: str) -> str | None:
    """Zip the contents of ``dist_path`` into ``zip_path`` (flat).

    Uses ``shutil.make_archive`` with ``root_dir=dist_path, base_dir="."``
    so the archive contains files at the root, NOT nested under ``dist/``.
    Strips a trailing ``.zip`` from ``zip_path`` before passing it to
    ``make_archive`` (which re-appends the format extension automatically).

    Returns the absolute path of the created zip, or None on failure.
    """
    if not os.path.isdir(dist_path):
        return None
    try:
        any_files = any(True for _ in os.scandir(dist_path))
    except OSError:
        return None
    if not any_files:
        return None

    # Strip trailing .zip so make_archive doesn't double it.
    base = zip_path[:-4] if zip_path.lower().endswith(".zip") else zip_path
    try:
        produced = shutil.make_archive(
            base_name=base,
            format="zip",
            root_dir=dist_path,
            base_dir=".",
        )
    except (OSError, shutil.Error):
        return None
    return os.path.abspath(produced)


def _release_basename(path: str, git_exe: str) -> str:
    """Return a clean release-artefact prefix (no spaces) for ``path``.

    Tries the git remote first — ``https://github.com/user/Repo.git`` →
    ``Repo``, ``git@github.com:user/Repo.git`` → ``Repo``. This matches the
    GitHub repo name, which is what users expect to see in
    ``Repo-vX.Y.Z-windows.zip``. Falls back to the folder basename with
    spaces replaced by dashes when the remote can't be read (no repo yet,
    no remote set, git missing).

    Pure-ish — does a single fast git call, no network.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        proc = None

    if proc and proc.returncode == 0:
        url = proc.stdout.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        # Split on both '/' (https) and ':' (ssh), take the last segment.
        tail = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if tail:
            return tail

    # Fallback: sanitize the folder basename.
    base = os.path.basename(path.rstrip(os.sep))
    return base.replace(" ", "-") or "release"


def _fmt_size(byte_count: int) -> str:
    """Format ``byte_count`` as a human-friendly size string.

    - <1 KB → "<1 KB" (avoids "0.0 MB" / "0.0 KB" for tiny files)
    - <1 MB → "N KB" with no decimal
    - ≥1 MB → "N.N MB" with one decimal
    """
    if byte_count < 1024:
        return "<1 KB"
    if byte_count < 1024 * 1024:
        return f"{byte_count // 1024} KB"
    return f"{byte_count / 1024 / 1024:.1f} MB"
