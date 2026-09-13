"""instructions_wiring — the one writer that repairs an instruction chain.

## Scope, and why it is narrower than the classifier

`instructions_posture` resolves a bounded, general include graph. **This module
writes exactly one topology**::

    CLAUDE.md  ->  @BASIC_INSTRUCTIONS.md  ->  @project-baseline.md
                                               (a Manager copy of the template,
                                                committed in the project)

That asymmetry is deliberate and is documented in both modules because it is
easy to erode: reading the classifier alone suggests Retrofit ought to repair
anything the classifier can resolve. It must not. A project that already
resolves through some other valid chain needs nothing, and the only repair this
module knows how to make would add a SECOND path to a baseline that already
arrives.

## A copy, not a pointer

The topology used to end in an absolute include of `<template_dir>`. Claude
Code loaded none of those: an include outside the project needs a per-project
approval the desktop app never asks for, and a path with a backslash or an
unescaped space is not parsed at all (both measured, see
`helpers/baseline_copy.py` and `instructions_posture.claude_code_parses`). So
the writer puts a copy inside the project and points the include at it. An
include that resolves on this machine only because someone approved it is still
LOCALIZED: a committed machine path breaks every other checkout and worktree.

## Ordering and compare-and-apply

A project's copy is written and verified BEFORE any include is repointed at it.
If the copy fails, the old include is untouched; if the repoint fails, the old
include still works and an unused copy is left behind. The reverse order would
leave an include naming a file that does not exist.

A repoint carries the exact directive text and line it was planned from, and is
applied only if that directive is still there. A refresh carries the sha the
copy recorded, and is applied only if the copy still records it.

## What it will not do

- **It never deletes.** An obsolete direct baseline include is reported
  (`ADVISORY_DOUBLE_LOAD`), not removed.
- **It never rewrites authored prose.** A project whose `CLAUDE.md` tells the
  agent to reach for Grep first keeps saying so after wiring.
- **It never overwrites a `project-baseline.md` it did not write**, or one
  edited by hand, or one whose header is damaged.
- **It refuses rather than guesses.** Two `@BASIC_INSTRUCTIONS.md` lines, or two
  baseline includes, are not normalised silently.

## Bytes outside the managed line are preserved

Everything here reads with `newline=""` and inserts using the file's own
dominant terminator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from helpers import baseline_copy as bc
from helpers.instructions_posture import (
    ADVISORY_DUPLICATE_DIRECTIVE,
    ADVISORY_NONSTANDARD_CHAIN,
    BASIC_MD,
    CLAUDE_MD,
    DELIVERY_IN_PROJECT,
    DELIVERY_UNKNOWN,
    REACH_ABSENT,
    REACH_ORPHANED,
    REACH_RESOLVED,
    REACH_STALE,
    REACH_UNKNOWN,
    canonical,
    is_baseline,
    scan_text,
    unescape_target,
)
from helpers.io_utils import _atomic_write

# ── plan vocabulary ──────────────────────────────────────────────────────

#: Add `@BASIC_INSTRUCTIONS.md` to an existing `CLAUDE.md`.
ACTION_LINK_BASIC = "link_basic"
#: Create `CLAUDE.md` carrying only that link.
ACTION_CREATE_CLAUDE = "create_claude"
#: Create `BASIC_INSTRUCTIONS.md` from the template.
ACTION_CREATE_BASIC = "create_basic"
#: Add `@project-baseline.md` to an existing file.
ACTION_ADD_BASELINE = "add_baseline"
#: Create `<root>/project-baseline.md` from the template.
ACTION_WRITE_COPY = "write_copy"
#: Rewrite an outdated copy.
ACTION_REFRESH_COPY = "refresh_copy"
#: Repoint a baseline directive at the project's copy.
ACTION_LOCALIZE = "localize"

_LABELS = {
    ACTION_LINK_BASIC: "link BASIC_INSTRUCTIONS.md from CLAUDE.md",
    ACTION_CREATE_CLAUDE: "create CLAUDE.md",
    ACTION_CREATE_BASIC: "create BASIC_INSTRUCTIONS.md",
    ACTION_ADD_BASELINE: "add the baseline include",
    ACTION_WRITE_COPY: "write the project's copy of the baseline",
    ACTION_REFRESH_COPY: "update the project's copy of the baseline",
    ACTION_LOCALIZE: "point the baseline include at the project's copy",
}

_COPY_STEPS = (ACTION_WRITE_COPY, ACTION_REFRESH_COPY)

#: Copy states a plan must not write over, with the reason shown on the row.
_COPY_BLOCKS = {
    bc.COPY_EDITED: "project-baseline.md was edited by hand since the Manager "
                    "copied it - not overwritten",
    bc.COPY_INVALID: "project-baseline.md has a damaged Manager header - not "
                     "overwritten",
    bc.COPY_UNMANAGED: "a project-baseline.md the Manager did not write is "
                       "already in the project - not overwritten",
    bc.COPY_UNREADABLE: "project-baseline.md could not be read or judged",
}


@dataclass(frozen=True)
class WiringStep:
    action: str
    path: str
    detail: str = ""
    #: The precondition: a directive's exact raw target (LOCALIZE) or the sha a
    #: copy records (REFRESH_COPY).
    expect: str = ""
    lineno: int = 0

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

def _directive_target(posture, raw: str) -> str:
    spelled = unescape_target(raw)
    if os.path.isabs(spelled):
        return canonical(spelled)
    return canonical(os.path.join(posture.display_root, spelled))


def _localize_steps(posture) -> "tuple[list, str]":
    """Steps that leave the project on its own current copy, or a refusal."""
    blocked = _COPY_BLOCKS.get(posture.copy_state)
    if blocked:
        return [], blocked
    if not posture.baseline_directives:
        return [], ("the baseline include is not in %s or %s, so there is no "
                    "line the Manager knows to repoint" % (CLAUDE_MD, BASIC_MD))
    copy_path = canonical(os.path.join(posture.display_root, bc.COPY_BASENAME))
    foreign = [d for d in posture.baseline_directives
               if _directive_target(posture, d[2]) != copy_path]
    if len(posture.baseline_directives) > 1:
        return [], ("%d baseline includes - localizing them would load the "
                    "copy more than once" % len(posture.baseline_directives))

    steps: list = []
    if posture.copy_state == bc.COPY_OUTDATED:
        steps.append(WiringStep(ACTION_REFRESH_COPY, bc.COPY_BASENAME,
                                expect=posture.copy_recorded_sha))
    elif posture.copy_state != bc.COPY_CURRENT:
        steps.append(WiringStep(ACTION_WRITE_COPY, bc.COPY_BASENAME))
    for name, lineno, raw in foreign:
        steps.append(WiringStep(ACTION_LOCALIZE, name, "was @%s" % raw,
                                expect=raw, lineno=lineno))
    if any(d[0] == BASIC_MD for d in posture.baseline_directives) and \
            canonical(os.path.join(posture.display_root, BASIC_MD)) \
            not in posture.chain:
        steps.append(WiringStep(ACTION_LINK_BASIC, CLAUDE_MD))
    return steps, ""


def _absent_steps(posture, has_template: bool) -> "tuple[list, str]":
    blocked = _COPY_BLOCKS.get(posture.copy_state)
    if blocked:
        return [], blocked
    steps: list = []
    if posture.copy_state == bc.COPY_OUTDATED:
        steps.append(WiringStep(ACTION_REFRESH_COPY, bc.COPY_BASENAME,
                                expect=posture.copy_recorded_sha))
    elif posture.copy_state != bc.COPY_CURRENT:
        steps.append(WiringStep(ACTION_WRITE_COPY, bc.COPY_BASENAME))
    if posture.has_basic:
        steps.append(WiringStep(ACTION_ADD_BASELINE, BASIC_MD))
    elif has_template:
        steps.append(WiringStep(ACTION_CREATE_BASIC, BASIC_MD))
    else:
        # Nothing to create BASIC_INSTRUCTIONS.md from. Wire the one-hop shape
        # rather than refusing: `CLAUDE.md` -> baseline is a documented topology
        # too, and it needs no file the user did not ask for.
        steps.append(WiringStep(ACTION_ADD_BASELINE, CLAUDE_MD))
        return steps, ""
    steps.append(WiringStep(ACTION_LINK_BASIC, CLAUDE_MD))
    return steps, ""


def _refusal(posture) -> str:
    """Why nothing may be planned at all, or ""."""
    if getattr(posture, "excluded", False):
        # The guard belongs HERE and not only on the button: what this
        # exclusion protects is writing Manager-authored files into somebody
        # else's repository, which is not a mistake to leave one call site away.
        return posture.exclude_reason or "excluded from instruction wiring"
    if posture.reach == REACH_UNKNOWN:
        return posture.detail or "state could not be determined"
    if posture.delivery == DELIVERY_UNKNOWN:
        return ("could not determine whether Claude Code loads this chain: %s"
                % (posture.detail or "reason unrecorded"))
    if ADVISORY_DUPLICATE_DIRECTIVE in posture.advisories:
        return ("two %s includes in %s — repairing one and leaving the other "
                "would preserve the duplication" % (BASIC_MD, CLAUDE_MD))
    return ""


def plan_wiring(posture, has_template: bool = True) -> WiringPlan:
    """Decide the writes for one project from its posture. Pure.

    Takes an already-classified `ProjectInstructions` rather than re-reading
    the files, so there is exactly one classifier in the system.
    """
    refusal = _refusal(posture)
    if refusal:
        return WiringPlan(blocked=refusal)

    if posture.reach == REACH_RESOLVED:
        if posture.delivery == DELIVERY_IN_PROJECT:
            return WiringPlan()
        if ADVISORY_NONSTANDARD_CHAIN in posture.advisories:
            return WiringPlan(blocked="resolves through its own include chain, "
                                      "which the Manager does not rewrite")
        steps, blocked = _localize_steps(posture)
    elif posture.reach in (REACH_STALE, REACH_ORPHANED):
        steps, blocked = _localize_steps(posture)
    elif posture.reach == REACH_ABSENT:
        steps, blocked = _absent_steps(posture, has_template)
    else:
        return WiringPlan()
    if blocked:
        return WiringPlan(blocked=blocked)
    return WiringPlan(steps=tuple(steps))


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
    document.
    """
    return "# %s — Claude Instructions\n\n@%s\n" % (project_name, BASIC_MD)


def compute_link_basic(existing: str) -> "tuple[str, bool]":
    """Ensure `CLAUDE.md` includes `BASIC_INSTRUCTIONS.md`. Pure.

    Returns ``(text, changed)``; `changed` is False when the file already says
    this, which is what makes a re-run a genuine no-op.
    """
    if _has_directive(existing, BASIC_MD):
        return existing, False
    nl = dominant_newline(existing)
    line = "@%s%s" % (BASIC_MD, nl)
    if not existing.strip():
        return line, line != existing
    return line + nl + existing, True


def compute_add_baseline(existing: str,
                         baseline_line: str = bc.LOCAL_INCLUDE_LINE
                         ) -> "tuple[str, bool]":
    """Ensure a file carries the baseline include."""
    directives, _indented, _fence = scan_text(existing)
    if any(is_baseline(unescape_target(raw)) for _lineno, raw in directives):
        return existing, False
    nl = dominant_newline(existing)
    line = baseline_line.strip() + nl
    if not existing.strip():
        return line, True
    return line + nl + existing, True


def compute_localize(existing: str, lineno: int,
                     expect: str) -> "tuple[str, bool, str]":
    """Repoint the directive `@<expect>` to `@project-baseline.md`. Pure.

    Compare-and-apply: the directive must still read exactly *expect*, at
    *lineno* or, if lines moved, at exactly one other place. Anything else is
    `(existing, False, reason)` — the file changed since the plan, and line
    surgery on a guess is how a repair damages somebody's edit.
    """
    directives, _indented, _fence = scan_text(existing)
    hits = [n for n, raw in directives if raw == expect]
    if lineno in hits:
        target = lineno
    elif len(hits) == 1:
        target = hits[0]
    else:
        return existing, False, "the include changed since the plan"

    out = []
    for number, line in enumerate(existing.splitlines(keepends=True), 1):
        if number != target:
            out.append(line)
            continue
        tail = ""
        for ending in ("\r\n", "\n", "\r"):
            if line.endswith(ending):
                tail = ending
                break
        out.append(bc.LOCAL_INCLUDE_LINE + tail)
    return "".join(out), True, ""


# ── IO ───────────────────────────────────────────────────────────────────

def _read_preserving(path: str) -> "str | None":
    """Text with line endings intact, or None when unreadable."""
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
                 project_name: str, template_text: str) -> "tuple[str, bool, str]":
    """The text one step would write, whether it differs, and any refusal."""
    exists = os.path.isfile(path)

    if step.action == ACTION_LINK_BASIC:
        if not exists:
            return compute_created_claude(project_name), True, ""
        return compute_link_basic(existing) + ("",)

    if step.action == ACTION_CREATE_CLAUDE:
        return compute_created_claude(project_name), True, ""

    if step.action == ACTION_CREATE_BASIC:
        if exists:
            # Never overwrite a file the user may have filled in.
            return existing, False, ""
        return template_text, bool(template_text), ""

    if step.action == ACTION_ADD_BASELINE:
        return compute_add_baseline(existing) + ("",)

    if step.action == ACTION_LOCALIZE:
        return compute_localize(existing, step.lineno, step.expect)

    return existing, False, ""


def _stale_localize(project_root: str, steps: tuple) -> str:
    """Why a LOCALIZE step no longer applies, or ""."""
    for step in steps:
        if step.action != ACTION_LOCALIZE:
            continue
        existing = _read_preserving(os.path.join(project_root, step.path))
        if existing is None:
            return "could not read %s" % step.path
        _text, _changed, refusal = compute_localize(existing, step.lineno,
                                                    step.expect)
        if refusal:
            return "%s: %s" % (step.path, refusal)
    return ""


def _ordered(steps: tuple) -> list:
    """Copy steps first, whatever order a plan arrived in. See the docstring."""
    return ([s for s in steps if s.action in _COPY_STEPS]
            + [s for s in steps if s.action not in _COPY_STEPS])


def apply_wiring(project_root: str, plan: WiringPlan, project_name: str,
                 template_text: str = "",
                 baseline_template_text: str = "") -> WiringResult:
    """Execute *plan*, stopping at the first failure. Returns what changed.

    *template_text* is the BASIC_INSTRUCTIONS.md template; *baseline_template_text*
    is the shared baseline a copy is rendered from. Reports the files it
    CHANGED rather than the files it considered.
    """
    if plan.blocked:
        return WiringResult(ok=False, skipped=plan.blocked)
    if plan.is_noop:
        return WiringResult(ok=True)

    # Every repoint's precondition is checked before ANY write, so a project
    # edited since the plan gets nothing - not even the copy.
    stale = _stale_localize(project_root, plan.steps)
    if stale:
        return WiringResult(ok=False, error=stale)

    changed: list = []
    for step in _ordered(plan.steps):
        if step.action in _COPY_STEPS:
            expect = step.expect if step.action == ACTION_REFRESH_COPY else None
            written = bc.write_copy(project_root, baseline_template_text, expect)
            if not written.ok:
                return WiringResult(ok=False, changed_files=tuple(changed),
                                    error=written.error)
            if written.changed:
                changed.append(step.path)
            continue

        path = os.path.join(project_root, step.path)
        existing = ""
        if os.path.isfile(path):
            raw = _read_preserving(path)
            if raw is None:
                return WiringResult(ok=False, changed_files=tuple(changed),
                                    error="could not read %s" % step.path)
            existing = raw

        text, changed_now, refusal = _render_step(step, path, existing,
                                                  project_name, template_text)
        if refusal:
            return WiringResult(ok=False, changed_files=tuple(changed),
                                error="%s: %s" % (step.path, refusal))
        if not changed_now:
            continue
        ok, msg = _atomic_write(path, text, step.path)
        if not ok:
            return WiringResult(ok=False, changed_files=tuple(changed),
                                error=msg)
        if step.path not in changed:
            changed.append(step.path)

    return WiringResult(ok=True, changed_files=tuple(changed))


# ── applying to one project: a structured outcome, not a sentence ────────────
#
# What this replaces, and why it had to go before anything drove it over a whole
# fleet: the caller returned prose --
#
#     "skipped - state changed since preview"
#     "skipped - %s" % plan.blocked
#     "failed - %s"
#
# -- and the UI branched on `outcome == "wired"`. The safety case the
# three-stage bulk flow exists for, *this project moved since you looked, so
# nothing was written*, was therefore distinguishable from an ordinary refusal
# only by parsing a string the code had formatted itself. It worked, and it was
# one rename away from silently not working.
#
# The rendered sentence now derives from the outcome. Never the reverse.

#: Written, and re-read as healthy afterwards.
OUTCOME_WIRED = "wired"
#: Nothing to do; the chain already resolves inside the project.
OUTCOME_ALREADY_RESOLVED = "already_resolved"
#: The project changed between the preview and the click, so the plan built
#: from the preview was discarded unapplied. A DISTINCT member: this is the
#: safety net doing its job, not a refusal.
OUTCOME_SKIPPED_STATE_CHANGED = "skipped_state_changed"
#: The planner refused - a duplicate directive, an excluded project, an
#: undetermined state. Also a skip, and a different fact.
OUTCOME_SKIPPED_BLOCKED = "skipped_blocked"
#: The write itself failed.
OUTCOME_FAILED = "failed"
#: Written, but the chain still does not resolve inside the project. Neither
#: success nor failure, and collapsing it into either would be a lie.
OUTCOME_UNVERIFIED = "unverified"

_OUTCOME_TEXT = {
    OUTCOME_WIRED: "wired",
    OUTCOME_ALREADY_RESOLVED: "already resolved",
    OUTCOME_SKIPPED_STATE_CHANGED: "skipped - state changed since preview",
    OUTCOME_SKIPPED_BLOCKED: "skipped",
    OUTCOME_FAILED: "failed",
    OUTCOME_UNVERIFIED: "unverified",
}


@dataclass(frozen=True)
class ApplyOutcome:
    """What happened to ONE project, as data."""

    outcome: str
    reason: str = ""
    changed_files: tuple = ()

    @property
    def wrote(self) -> bool:
        return self.outcome == OUTCOME_WIRED

    @property
    def is_skip(self) -> bool:
        return self.outcome in (OUTCOME_SKIPPED_STATE_CHANGED,
                                OUTCOME_SKIPPED_BLOCKED)

    def render(self) -> str:
        """The sentence, DERIVED from the outcome."""
        text = _OUTCOME_TEXT.get(self.outcome, self.outcome)
        return "%s - %s" % (text, self.reason) if self.reason else text


def _state_key(posture) -> tuple:
    return (posture.reach, posture.delivery, posture.copy_state)


def apply_to_project(posture, baseline: str, template_dir: str,
                     template_text: str, has_template: bool,
                     baseline_template_text: str = "",
                     claude_projects=None) -> ApplyOutcome:
    """Re-read, re-plan, write, re-read. One project.

    The plan built for the preview is deliberately NOT reused: between the
    preview and the click the disk may have moved on, and writing a stale plan
    is how a bulk action damages a project somebody already fixed by hand.

    *claude_projects* is passed through to `read_project` when given (tests);
    by default the config is read fresh, because approval can change too.
    """
    from helpers.instructions_posture import read_project

    def reread():
        if claude_projects is None:
            return read_project(posture.display_root, posture.name,
                                template_dir, baseline, baseline_template_text)
        return read_project(posture.display_root, posture.name, template_dir,
                            baseline, baseline_template_text, "",
                            claude_projects)

    if baseline and not baseline_template_text:
        baseline_template_text = bc.read_template(baseline)

    fresh = reread()
    if _state_key(fresh) != _state_key(posture):
        return ApplyOutcome(OUTCOME_SKIPPED_STATE_CHANGED,
                            "was %s, now %s" % ("/".join(_state_key(posture)),
                                                "/".join(_state_key(fresh))))

    plan = plan_wiring(fresh, has_template=has_template)
    if plan.blocked:
        return ApplyOutcome(OUTCOME_SKIPPED_BLOCKED, plan.blocked)
    if plan.is_noop:
        return ApplyOutcome(OUTCOME_ALREADY_RESOLVED)

    result = apply_wiring(posture.display_root, plan, posture.name,
                          template_text, baseline_template_text)
    if not result.ok:
        return ApplyOutcome(OUTCOME_FAILED, result.error or result.skipped,
                            result.changed_files)

    after = reread()
    if after.healthy and after.copy_state == bc.COPY_CURRENT:
        return ApplyOutcome(OUTCOME_WIRED, changed_files=result.changed_files)
    return ApplyOutcome(OUTCOME_UNVERIFIED,
                        "still %s after writing" % "/".join(_state_key(after)),
                        result.changed_files)
