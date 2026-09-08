"""Portable agent rules files — `AGENTS.md` and `.cursor/rules/*.mdc`.

Why this module exists at all
-----------------------------
Retrofit's existing strategy is to prepend one line to `CLAUDE.md`::

    @D:\\...\\templates\\project-baseline.md

That is a *pointer*, and it works because Claude Code resolves `@path`
includes. **Cursor does not.** Neither does Codex, Gemini CLI, or opencode.
Handing them that line gives them a literal at-sign and a path, and the rules
never load. So for every non-Claude agent the baseline content has to be
INLINED, and inlining raises the question this module answers: how do you write
generated content into a file a human also edits, repeatedly, without eating
their work?

Marker blocks, not heuristics
------------------------------
Both writers manage a delimited region and leave everything outside it byte
for byte alone::

    <!-- tokensave-manager:begin -->
    ...generated from templates/project-baseline.md...
    <!-- tokensave-manager:end -->

Re-running replaces only what is between the markers. Text above, below, and
around it survives. This is the same lesson as
`helpers/changelog_patch.insert_changelog_release`: a generated block needs an
explicit boundary, because "find the bit that looks generated" is a guess and a
guess eventually deletes someone's paragraph.

One source, two outputs
-----------------------
`templates/project-baseline.md` is the single source. `AGENTS.md` is the
portable form (plain markdown, also read by Codex/Gemini/opencode) and
`.cursor/rules/tokensave.mdc` is Cursor's native form. They are rendered from
the same text rather than maintained as two files, so there is nothing to keep
in sync.

The `.mdc` frontmatter is the one Manager-owned exception: Cursor requires YAML
frontmatter at the very top of the file, so it cannot sit inside a marker block.
Everything below it follows the same preserve-around-the-block rule. Note also
that Cursor **ignores plain `.md` files** in `.cursor/rules/` — the extension is
load-bearing, not cosmetic.
"""

from __future__ import annotations

import os

BEGIN_MARKER = "<!-- tokensave-manager:begin -->"
END_MARKER = "<!-- tokensave-manager:end -->"

#: Frontmatter for the Cursor rule. `alwaysApply: true` because these are
#: project-wide conventions rather than globbed, file-type-specific rules.
_MDC_FRONTMATTER = (
    "---\n"
    "description: TokenSave Manager project baseline\n"
    "alwaysApply: true\n"
    "---\n"
)

_AGENTS_PREAMBLE = (
    "# Agent Instructions\n"
    "\n"
    "Instructions for AI coding agents working in this repository.\n"
    "Read by Cursor, Codex, Gemini CLI, opencode and others.\n"
)


def _wrap(body: str) -> str:
    """Put *body* inside the managed markers."""
    return f"{BEGIN_MARKER}\n{body.strip()}\n{END_MARKER}\n"


def _replace_block(existing: str, body: str) -> "tuple[str, bool]":
    """Swap the managed block inside *existing*, or append one.

    Returns ``(text, changed)``. ``changed`` is False when the file already
    says exactly this, which is what makes a re-run a no-op rather than a
    pointless rewrite (and keeps the git status clean after a repeat Retrofit).

    An unterminated or inverted marker pair is treated as "no block": the
    content is appended rather than the file being mangled by a slice against
    a boundary that isn't there.
    """
    block = _wrap(body)
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        base = existing.rstrip()
        new = (base + "\n\n" + block) if base else block
        return new, new != existing
    tail_at = end + len(END_MARKER)
    tail = existing[tail_at:]
    if tail.startswith("\n"):
        tail = tail[1:]
    new = existing[:start] + block + tail
    return new, new != existing


def _compute_agents_md(existing: str, baseline_text: str,
                       project_name: str = "") -> "tuple[str, bool]":
    """Render `AGENTS.md`. Pure — no filesystem access.

    A brand-new file gets a short preamble above the managed block so it reads
    as a document rather than as a machine artefact. An existing file gets only
    its block touched; the preamble is NOT re-inserted, because the user may
    have deliberately rewritten the top of their own file.
    """
    body = baseline_text.strip()
    if not existing.strip():
        header = _AGENTS_PREAMBLE
        if project_name:
            header = header.replace("# Agent Instructions",
                                    f"# Agent Instructions — {project_name}")
        return header + "\n" + _wrap(body), True
    return _replace_block(existing, body)


def _compute_cursor_rule(existing: str,
                         baseline_text: str) -> "tuple[str, bool]":
    """Render `.cursor/rules/tokensave.mdc`. Pure — no filesystem access.

    The frontmatter is rewritten on every run (Cursor requires it at the top,
    so it cannot live inside the managed block); everything the user added
    below it, outside the markers, is preserved.
    """
    body = baseline_text.strip()
    if not existing.strip():
        return _MDC_FRONTMATTER + "\n" + _wrap(body), True

    rest = existing
    if existing.startswith("---"):
        # Drop the existing frontmatter so ours replaces it, rather than
        # ending up with two frontmatter blocks and a file Cursor rejects.
        closing = existing.find("\n---", 3)
        if closing != -1:
            rest = existing[closing + len("\n---"):].lstrip("\n")
    updated, _changed = _replace_block(rest, body)
    new = _MDC_FRONTMATTER + "\n" + updated
    return new, new != existing


# ── IO wrappers ──────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    """File contents, or "" when absent. utf-8-sig tolerates a Windows BOM."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _write_atomic(path: str, text: str) -> "tuple[bool, str]":
    """Temp file + os.replace, so an interrupted write cannot truncate."""
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"
    return True, ""


def agents_md_path(project_root: str) -> str:
    return os.path.join(project_root, "AGENTS.md")


def cursor_rule_path(project_root: str) -> str:
    """Cursor ignores plain `.md` here — the `.mdc` extension is required."""
    return os.path.join(project_root, ".cursor", "rules", "tokensave.mdc")


def write_agents_md(project_root: str, baseline_text: str,
                    project_name: str = "") -> "tuple[bool, str, bool]":
    """Create or update `AGENTS.md`. Returns ``(ok, error, changed)``."""
    path = agents_md_path(project_root)
    new, changed = _compute_agents_md(_read(path), baseline_text, project_name)
    if not changed:
        return True, "", False
    ok, err = _write_atomic(path, new)
    return ok, err, ok


def write_cursor_rule(project_root: str,
                      baseline_text: str) -> "tuple[bool, str, bool]":
    """Create or update `.cursor/rules/tokensave.mdc`. ``(ok, error, changed)``."""
    path = cursor_rule_path(project_root)
    new, changed = _compute_cursor_rule(_read(path), baseline_text)
    if not changed:
        return True, "", False
    ok, err = _write_atomic(path, new)
    return ok, err, ok


def read_baseline(template_dir: str) -> str:
    """The shared baseline text both outputs are rendered from.

    Returns "" when the template is missing, so callers can skip the step with
    a message instead of writing an empty rules file that looks configured.
    """
    return _read(os.path.join(template_dir, "project-baseline.md"))
