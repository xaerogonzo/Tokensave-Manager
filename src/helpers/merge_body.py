"""Turn the CHANGELOG's ``[Unreleased]`` block into a merge-commit body.

GitHub's default merge commit carries only the PR title, so the history loses
the detail that was already written down in the CHANGELOG. This converts that
block into something readable in ``git log``.

**This is opt-in for a reason, and the reason is a correctness one.**
``[Unreleased]`` accumulates everything since the last release, which is not
necessarily what THIS pull request contains — merging two PRs in a row would
otherwise attribute the first one's entries to the second as well. The caller
surfaces it unchecked by default; this module only does the conversion, and
`looks_broader_than_pr` gives the UI a signal to warn with.

Pure module — stdlib only, no Tkinter, no git.
"""

from __future__ import annotations

import re

from helpers.changelog_patch import read_unreleased_from_text

# "### Added" / "#### Detail" — any markdown heading inside the block.
_HEADING_RE = re.compile(r"^(#{3,6})\s+(.*?)\s*#*\s*$")

# Markdown bullets, including indented (nested) ones.
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")


def _convert_line(line: str) -> "str | None":
    """One CHANGELOG line as it should read in a commit body, or None to drop.

    Headings become plain labels: a literal ``### Added`` in a commit message
    is noise, and on some hosts a leading ``#`` is read as a comment and can be
    stripped entirely — taking the section label with it.
    """
    if not line.strip():
        return ""
    m = _HEADING_RE.match(line)
    if m:
        depth = len(m.group(1)) - 3          # ### -> 0, #### -> 1, ...
        return ("  " * depth) + m.group(2).strip() + ":"
    m = _BULLET_RE.match(line)
    if m:
        indent, text = m.group(1), m.group(2).strip()
        return f"{indent}- {text}"
    return line.rstrip()


def build_merge_body(changelog_text: str) -> str:
    """Render the ``[Unreleased]`` block as a merge-commit body.

    Returns "" when there is nothing to say — no CHANGELOG, no
    ``[Unreleased]`` anchor, or an empty block. The caller must treat empty as
    "fall back to whatever GitHub would have done" rather than writing a blank
    body over the default.

    Content is passed through verbatim apart from heading conversion: bullets
    routinely contain backticks, ``#``, ``&`` and ``=``, and rewrapping or
    escaping them would corrupt the very detail this exists to preserve.
    """
    block = read_unreleased_from_text(changelog_text or "")
    if not block.strip():
        return ""

    out: list[str] = []
    for raw in block.splitlines():
        converted = _convert_line(raw)
        if converted is None:
            continue
        out.append(converted)

    # Collapse runs of blank lines and trim the ends.
    cleaned: list[str] = []
    for line in out:
        if not line and (not cleaned or not cleaned[-1]):
            continue
        cleaned.append(line)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


def count_bullets(changelog_text: str) -> int:
    """How many bullets the block holds — the UI's warning signal."""
    block = read_unreleased_from_text(changelog_text or "")
    return sum(1 for ln in block.splitlines() if _BULLET_RE.match(ln))


def looks_broader_than_pr(changelog_text: str, pr_commit_count: int,
                          slack: int = 2) -> bool:
    """True when ``[Unreleased]`` plausibly covers more than this PR.

    A rough guard, not a proof: if the block holds substantially more bullets
    than the PR has commits, it is probably carrying entries from work merged
    earlier. Deliberately biased toward *not* warning — a warning on every
    merge would be ignored, and the checkbox is already off by default.
    """
    if pr_commit_count <= 0:
        return False
    return count_bullets(changelog_text) > pr_commit_count + slack
