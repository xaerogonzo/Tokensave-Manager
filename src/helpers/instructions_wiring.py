"""instructions_wiring — the one writer that repairs an instruction chain.

## Scope, and why it is narrower than the classifier

`instructions_posture` resolves a bounded, general include graph. **This module
writes exactly one topology**::

    CLAUDE.md  ->  @BASIC_INSTRUCTIONS.md  ->  @<template_dir>/project-baseline.md

That asymmetry is deliberate and is documented in both modules because it is
easy to erode: reading the classifier alone suggests Retrofit ought to repair
anything the classifier can resolve. It must not. A project that already
resolves through some other valid chain needs nothing, and the only repair this
module knows how to make would add a SECOND path to a baseline that already
arrives.

## What it will not do

- **It never deletes.** An obsolete direct baseline include left behind by a
  moved `template_dir` is reported (`ADVISORY_DOUBLE_LOAD`), not removed.
  Deleting a line a human may have written is not a repair, and this phase's
  rule is surgical addition and in-place repair only.
- **It never rewrites authored prose.** A project whose `CLAUDE.md` tells the
  agent to reach for Grep first keeps saying so after wiring. That contradiction
  is surfaced for a human, because resolving it is an editorial decision.
- **It refuses rather than guesses.** Two `@BASIC_INSTRUCTIONS.md` lines in one
  file is not a thing to normalise silently: repairing the first and leaving the
  second would have the writer preserving the duplication the advisories exist
  to surface.

## Bytes outside the managed line are preserved

Reading a file with Python's default universal-newline translation turns every
CRLF into LF, so a "surgical" one-line insert would silently rewrite every line
ending in the file. Everything here reads with `newline=""` and inserts using
the file's own dominant terminator.

Pure computation split from IO, the shape `agent_rules` and `changelog_patch`
already use: every `_compute_*` is testable without touching a disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from helpers.instructions_posture import (
    ADVISORY_DUPLICATE_DIRECTIVE,
    BASIC_MD,
    CLAUDE_MD,
    REACH_ABSENT,
    REACH_ORPHANED,
    REACH_RESOLVED,
    REACH_STALE,
    REACH_UNKNOWN,
    canonical,
    is_baseline,
    scan_text,
)
from helpers.io_utils import _atomic_write

# ── plan vocabulary ──────────────────────────────────────────────────────

#: Add `@BASIC_INSTRUCTIONS.md` to an existing `CLAUDE.md`.
ACTION_LINK_BASIC = "link_basic"
#: Create `CLAUDE.md` carrying only that link.
ACTION_CREATE_CLAUDE = "create_claude"
#: Create `BASIC_INSTRUCTIONS.md` from the template.
ACTION_CREATE_BASIC = "create_basic"
#: Add the baseline include to an existing `BASIC_INSTRUCTIONS.md`.
ACTION_ADD_BASELINE = "add_baseline"
#: Point an existing baseline directive at the configured baseline.
ACTION_REPAIR_STALE = "repair_stale"

_LABELS = {
    ACTION_LINK_BASIC: "link BASIC_INSTRUCTIONS.md from CLAUDE.md",
    ACTION_CREATE_CLAUDE: "create CLAUDE.md",
    ACTION_CREATE_BASIC: "create BASIC_INSTRUCTIONS.md",
    ACTION_ADD_BASELINE: "add the baseline include",
    ACTION_REPAIR_STALE: "point the baseline include at the current template",
}


@dataclass(frozen=True)
class WiringStep:
    action: str
    path: str
    detail: str = ""

    @property
    def label(self) -> str:
        return _LABELS.get(self.action, self.action)


@dataclass(frozen=True)
class WiringPlan:
    """What would be written, computed from a posture. Pure."""

    steps: tuple = ()
    blocked: str = ""

    @property
    def files(self) -> tuple:
        """Distinct files this plan touches, in the order first named."""
        return tuple(dict.fromkeys(step.path for step in self.steps))

    @property
    def is_noop(self) -> bool:
        return not self.steps and not self.blocked


# ── planning (pure) ──────────────────────────────────────────────────────

def plan_wiring(posture, has_template: bool = True) -> WiringPlan:
    """Decide the writes for one project from its posture. Pure.

    Takes an already-classified `ProjectInstructions` rather than re-reading
    the files, so there is exactly one classifier in the system. A second
    opinion here is how the two halves drift apart.
    """
    if getattr(posture, "excluded", False):
        # The guard belongs HERE and not only on the button. `repairable`
        # gates the UI, but any caller reaching the planner directly would
        # have walked straight past it — and what this particular exclusion
        # protects is writing Manager-authored files into somebody else's
        # repository, which is not a mistake to leave one call site away.
        return WiringPlan(blocked=posture.exclude_reason or
                          "excluded from instruction wiring")
    if posture.reach == REACH_RESOLVED:
        return WiringPlan()
    if posture.reach == REACH_UNKNOWN:
        return WiringPlan(blocked=posture.detail or
                          "state could not be determined")
    if ADVISORY_DUPLICATE_DIRECTIVE in posture.advisories:
        return WiringPlan(blocked="two %s includes in %s — repairing one and "
                                  "leaving the other would preserve the "
                                  "duplication" % (BASIC_MD, CLAUDE_MD))

    steps: list = []

    if posture.reach == REACH_STALE:
        where = posture.stale_at.split(":")[0] if posture.stale_at else CLAUDE_MD
        steps.append(WiringStep(ACTION_REPAIR_STALE, where,
                                "reaches %s" % posture.reached_baseline))
        # A stale directive that lives in BASIC_INSTRUCTIONS.md is only useful
        # once something links that file, so the chain still has to be closed.
        if os.path.basename(where).upper() == BASIC_MD.upper():
            steps.append(WiringStep(ACTION_LINK_BASIC, CLAUDE_MD))
        return WiringPlan(steps=tuple(steps))

    if posture.reach == REACH_ORPHANED:
        steps.append(WiringStep(ACTION_LINK_BASIC, CLAUDE_MD,
                                "%s already carries the baseline include"
                                % BASIC_MD))
        return WiringPlan(steps=tuple(steps))

    if posture.reach == REACH_ABSENT:
        if posture.has_basic:
            steps.append(WiringStep(ACTION_ADD_BASELINE, BASIC_MD))
        elif has_template:
            steps.append(WiringStep(ACTION_CREATE_BASIC, BASIC_MD))
        else:
            # Nothing to create BASIC_INSTRUCTIONS.md from. Wire the one-hop
            # shape rather than refusing: `CLAUDE.md` -> baseline is a
            # documented topology too, and it needs no file the user did not
            # ask for. Refusing here would leave a project unwired purely
            # because a template path was blank.
            steps.append(WiringStep(ACTION_ADD_BASELINE, CLAUDE_MD))
            return WiringPlan(steps=tuple(steps))
        steps.append(WiringStep(ACTION_LINK_BASIC, CLAUDE_MD))
        return WiringPlan(steps=tuple(steps))

    return WiringPlan()


# ── text computation (pure) ──────────────────────────────────────────────

def dominant_newline(text: str) -> str:
    """The terminator this file already uses, so an insert does not convert it."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _has_directive(text: str, target_basename: str) -> bool:
    directives, _indented, _fence = scan_text(text)
    return any(os.path.basename(raw).lower() == target_basename.lower()
               for _lineno, raw in directives)


def compute_created_claude(project_name: str) -> str:
    """A brand-new `CLAUDE.md`. Deterministic, and tested byte-for-byte.

    Minimal on purpose: a wiring action earns a wiring line, not a starter
    document. Anything more would be this module deciding what a project's
    instructions should say.
    """
    return "# %s — Claude Instructions\n\n@%s\n" % (project_name, BASIC_MD)


def compute_link_basic(existing: str) -> "tuple[str, bool]":
    """Ensure `CLAUDE.md` includes `BASIC_INSTRUCTIONS.md`. Pure.

    Returns ``(text, changed)``; `changed` is False when the file already says
    this, which is what makes a re-run a genuine no-op with a clean git status
    rather than a rewrite that only looks idempotent.
    """
    if _has_directive(existing, BASIC_MD):
        return existing, False
    nl = dominant_newline(existing)
    line = "@%s%s" % (BASIC_MD, nl)
    if not existing.strip():
        return line, line != existing
    return line + nl + existing, True


def compute_add_baseline(existing: str, baseline_line: str) -> "tuple[str, bool]":
    """Ensure `BASIC_INSTRUCTIONS.md` carries the configured baseline include."""
    directives, _indented, _fence = scan_text(existing)
    if any(is_baseline(raw) for _lineno, raw in directives):
        return existing, False
    nl = dominant_newline(existing)
    line = baseline_line.strip() + nl
    if not existing.strip():
        return line, True
    return line + nl + existing, True


def compute_repair_stale(existing: str, baseline_line: str,
                         containing_dir: str,
                         current_target: str) -> "tuple[str, bool]":
    """Repoint baseline directives that name a different baseline. Pure.

    Only lines the parser recognises as directives naming a baseline are
    touched, and only when they resolve somewhere other than the configured
    baseline. Every other byte — including a prose mention of the filename,
    which is what defeated the substring test this replaces — is untouched.
    """
    directives, _indented, _fence = scan_text(existing)
    stale_lines = set()
    for lineno, raw in directives:
        if not is_baseline(raw):
            continue
        target = canonical(raw if os.path.isabs(raw)
                           else os.path.join(containing_dir, raw))
        if target != current_target:
            stale_lines.add(lineno)
    if not stale_lines:
        return existing, False

    replacement = baseline_line.strip()
    out = []
    for lineno, line in enumerate(existing.splitlines(keepends=True), 1):
        if lineno not in stale_lines:
            out.append(line)
            continue
        tail = ""
        for ending in ("\r\n", "\n", "\r"):
            if line.endswith(ending):
                tail = ending
                break
        out.append(replacement + tail)
    return "".join(out), True


# ── IO ───────────────────────────────────────────────────────────────────

def _read_preserving(path: str) -> "str | None":
    """Text with line endings intact, or None when unreadable.

    `newline=""` is the whole point: the default would translate CRLF to LF on
    read, and writing that back would rewrite every line in the file while
    claiming to have inserted one.
    """
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class WiringResult:
    ok: bool
    changed_files: tuple = ()
    error: str = ""
    skipped: str = ""


def _render_step(step: WiringStep, path: str, existing: str,
                 baseline_line: str, project_name: str, template_text: str,
                 current_target: str) -> "tuple[str, bool]":
    """The text one step would write, and whether it differs. Pure-ish.

    Only touches the filesystem to ask whether *path* exists, which decides
    create-versus-edit. Split from `apply_wiring` so that function is a loop
    over writes rather than a switch nested inside one.
    """
    exists = os.path.isfile(path)

    if step.action == ACTION_LINK_BASIC:
        if not exists:
            return compute_created_claude(project_name), True
        return compute_link_basic(existing)

    if step.action == ACTION_CREATE_CLAUDE:
        return compute_created_claude(project_name), True

    if step.action == ACTION_CREATE_BASIC:
        if exists:
            # Never overwrite a file the user may have filled in. The posture
            # said this was absent; if it is here now the state changed under
            # us, and stopping is the correct answer.
            return existing, False
        return template_text, bool(template_text)

    if step.action == ACTION_ADD_BASELINE:
        return compute_add_baseline(existing, baseline_line)

    if step.action == ACTION_REPAIR_STALE:
        target = current_target or canonical(baseline_line.lstrip("@").strip())
        return compute_repair_stale(existing, baseline_line,
                                    os.path.dirname(path), target)

    return existing, False


def apply_wiring(project_root: str, plan: WiringPlan, baseline_line: str,
                 project_name: str, template_text: str = "",
                 current_target: str = "") -> WiringResult:
    """Execute *plan*. Returns which files actually changed.

    Reports the files it CHANGED rather than the files it considered, so a
    caller can show what was touched instead of what was intended.
    """
    if plan.blocked:
        return WiringResult(ok=False, skipped=plan.blocked)
    if plan.is_noop:
        return WiringResult(ok=True)

    changed: list = []
    for step in plan.steps:
        path = os.path.join(project_root, step.path)
        existing = ""
        if os.path.isfile(path):
            raw = _read_preserving(path)
            if raw is None:
                return WiringResult(ok=False, changed_files=tuple(changed),
                                    error="could not read %s" % step.path)
            existing = raw

        text, changed_now = _render_step(
            step, path, existing, baseline_line, project_name, template_text,
            current_target)
        if not changed_now:
            continue
        ok, msg = _atomic_write(path, text, step.path)
        if not ok:
            return WiringResult(ok=False, changed_files=tuple(changed),
                                error=msg)
        changed.append(step.path)

    return WiringResult(ok=True, changed_files=tuple(changed))
