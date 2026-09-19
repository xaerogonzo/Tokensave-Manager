"""helpers/index_scope.py -- tracked ``vendor/`` code the graph cannot see.

WHY THIS EXISTS. ``tokensave init`` seeds ``.tokensave/config.json`` with an
``exclude`` list that includes ``**/vendor/**``, on the convention that a
``vendor`` folder holds third-party code. When the project OWNS that folder --
OpenChem Studio keeps its naming engine in ``src/openchem/vendor/`` -- the
graph is silently blind to it: ``tokensave_context`` and ``tokensave_search``
find nothing there, and an agent falls back to ``Grep``/``Read`` for every task
touching it. Nothing errors, so the index just looks smaller than the project.

WHAT THIS CAN AND CANNOT KNOW. Whether a folder is third-party or owned is a
fact about the team, not the tree, so this never says "wrong". It reports the
population: tracked files that an exclude pattern hides, so the person can
decide. Silent when nothing tracked sits under a ``vendor`` directory.

PATTERN SEMANTICS ARE MEASURED, NOT REMEMBERED (tokensave 7.12.1, a three-file
probe -- ``vendor/rv.py``, ``src/pkg/vendor/nv.py``, ``src/pkg/core/c.py`` --
run through ``sync --force`` once per pattern):

* ``vendor/**``          excludes ONLY the root ``vendor/``. This is the older
                         default some configs still carry; it would NOT have
                         hidden ``src/openchem/vendor/``.
* ``**/vendor/**``       excludes ``vendor/`` at any depth (today's default).
* ``src/pkg/vendor/**``  excludes only that path.
* A non-empty ``include`` does NOT override an exclude.

So a pattern is anchored to the project root unless it starts with ``**/``.
Only the forms above are interpreted. A pattern that names ``vendor`` in any
other shape (a bare ``vendor``, ``vendor/``, ``vend*/**``) is reported as
NOT EVALUATED rather than guessed at -- an unknown pattern is never "does not
hide".
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

# Pathspec so git lists only what could matter, not the whole tree.
_VENDOR_PATHSPEC = ":(glob)**/vendor/**"
_VENDOR = "vendor"
_GIT_TIMEOUT_S = 30

# Report states. UNKNOWN is a state, never a synonym for CLEAR.
NOT_APPLICABLE = "not_applicable"   # not a tokensave project / not a git repo
CLEAR = "clear"                     # nothing tracked under vendor is hidden
HIDES = "hides"                     # tracked files are hidden by a pattern
UNKNOWN = "unknown"                 # could not establish it


@dataclass
class ScopeReport:
    state: str
    reason: str = ""
    #: pattern -> the tracked files it hides (only interpreted patterns)
    hidden: "dict[str, list[str]]" = field(default_factory=dict)
    #: patterns that mention vendor in a shape this module does not interpret
    not_evaluated: "list[str]" = field(default_factory=list)
    tracked_vendor_files: int = 0


def parse_pattern(pattern: str) -> "list[str] | None":
    """Segments of *pattern*, or None when it is outside the measured forms.

    Interpreted: contains a ``/``, ends in ``**``, other segments literal,
    with ``**`` allowed only as the first and last segment.
    """
    text = str(pattern).replace("\\", "/").strip()
    if "/" not in text or text.endswith("/") or text.startswith("/"):
        return None
    segs = text.split("/")
    if segs[-1] != "**" or "" in segs:
        return None
    for i, seg in enumerate(segs):
        if seg == "**":
            if i not in (0, len(segs) - 1):
                return None
        elif any(ch in seg for ch in "*?[{"):
            return None
    return segs


def _match(pat: "list[str]", path: "list[str]") -> bool:
    """Segment match where ``**`` spans zero or more directories."""
    if not pat:
        return not path
    if pat[0] == "**":
        return any(_match(pat[1:], path[i:]) for i in range(len(path) + 1))
    return bool(path) and path[0] == pat[0] and _match(pat[1:], path[1:])


def hides(pattern: str, rel_path: str) -> "bool | None":
    """Whether *pattern* excludes *rel_path*; None = pattern not interpreted."""
    segs = parse_pattern(pattern)
    if segs is None:
        return None
    return _match(segs, rel_path.replace("\\", "/").split("/"))


def vendor_dir_of(rel_path: str) -> str:
    """The path up to and including the first ``vendor`` segment."""
    parts = rel_path.replace("\\", "/").split("/")
    for i, seg in enumerate(parts[:-1]):
        if seg == _VENDOR:
            return "/".join(parts[:i + 1])
    return ""


def _names_vendor(pattern: str) -> bool:
    return _VENDOR in str(pattern)


def _read_excludes(project_path: str) -> "tuple[list[str] | None, str]":
    """``(patterns, problem)``. ``(None, "")`` = not a tokensave project."""
    import json

    cfg = os.path.join(project_path, ".tokensave", "config.json")
    if not os.path.isfile(cfg):
        return None, ""
    try:
        with open(cfg, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "cannot read .tokensave/config.json (%s)" % exc
    if not isinstance(data, dict):
        return None, ".tokensave/config.json is not a JSON object"
    raw = data.get("exclude")
    if not isinstance(raw, list):
        return [], ""
    return [str(p) for p in raw if isinstance(p, str)], ""


def _tracked_vendor_files(project_path: str,
                          git_exe: str) -> "tuple[list[str], str]":
    """``(files, problem)`` from ``git ls-files`` -- git decides, not us."""
    from constants import CREATE_NO_WINDOW

    if not git_exe:
        return [], "no git executable is configured"
    try:
        out = subprocess.run(
            [git_exe, "-C", project_path, "ls-files", "-z", "--",
             _VENDOR_PATHSPEC],
            capture_output=True, timeout=_GIT_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], "could not run git (%s)" % exc
    if out.returncode != 0:
        msg = out.stderr.decode("utf-8", "replace").strip()[:120]
        return [], "git ls-files failed (%s)" % (msg or out.returncode)
    names = out.stdout.decode("utf-8", "replace").split("\0")
    return [n for n in names if n], ""


def scan(project_path: str, git_exe: str) -> ScopeReport:
    """Which tracked ``vendor`` files does the graph's exclude list hide?"""
    patterns, problem = _read_excludes(project_path)
    if problem:
        return ScopeReport(UNKNOWN, problem)
    if patterns is None:
        return ScopeReport(NOT_APPLICABLE)
    if not os.path.exists(os.path.join(project_path, ".git")):
        return ScopeReport(NOT_APPLICABLE)     # "tracked" needs a repository

    files, problem = _tracked_vendor_files(project_path, git_exe)
    if problem:
        return ScopeReport(UNKNOWN, problem)
    if not files:
        return ScopeReport(CLEAR)

    relevant = [p for p in patterns if _names_vendor(p)]
    interpreted = [p for p in relevant if parse_pattern(p) is not None]
    report = ScopeReport(
        CLEAR, tracked_vendor_files=len(files),
        not_evaluated=[p for p in relevant if parse_pattern(p) is None])
    for rel in files:
        for pat in interpreted:
            if hides(pat, rel):
                report.hidden.setdefault(pat, []).append(rel)
                break
    if report.hidden:
        report.state = HIDES
    elif report.not_evaluated:
        report.state = UNKNOWN
        report.reason = ("%d tracked file(s) under a vendor directory, and an "
                         "exclude pattern that names vendor in a form this "
                         "check does not interpret" % len(files))
    return report
