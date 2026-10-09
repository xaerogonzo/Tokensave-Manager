"""instructions_migrate -- fold BASIC_INSTRUCTIONS.md into CLAUDE.md, in place.

The convention being retired::

    CLAUDE.md -> @BASIC_INSTRUCTIONS.md -> @project-baseline.md

The one it moves to::

    CLAUDE.md -> @project-baseline.md        (the project's own rules, inline)

## What a migration is, precisely

BASIC's text is **inlined at the line that included it**. Claude Code expands an
`@include` where the directive stands, so replacing that one line with the
file's text yields the same text in the same order. That is the whole safety
argument, and it is why this is a splice and not a merge: nothing is
reinterpreted, reordered or summarised.

The measured fleet (2026-10-09, 18 projects) is why it has to be careful. Of 14
projects on the chain, **11 hold authored rules in BASIC** (up to 70,947 B) and
only 3 hold the untouched template. "Mostly a rename" was a guess that the
measurement contradicted.

## What it will not do

- **It never deletes.** `BASIC_INSTRUCTIONS.md` is left exactly as it was; once
  nothing includes it, it is an ordinary unreferenced file for a person to
  remove. (Git still has it either way.)
- **It never pastes the untouched template.** A BASIC file equal to the shipped
  template (ignoring its include line) carries no authored content, so only its
  baseline include is kept. Pasting 2 KB of "[PROJECT NAME]" placeholders into a
  working CLAUDE.md would be the migration damaging the file it was moving to.
- **It refuses rather than guesses**: two includes of BASIC, an unclosed code
  fence in BASIC (inlined, it would swallow the rest of CLAUDE.md), no baseline
  anywhere, or a baseline that would then load twice.
- **It is compare-and-apply.** The plan carries the hash of both files it was
  made from; the write is refused if either has changed since.

Pure on text (`plan_migration`), IO in `apply_migration`, orchestration in
`migrate_project`, which mirrors `instructions_wiring.apply_to_project` so the
panel and the batch commit see one outcome shape.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass

from helpers import baseline_copy as bc
from helpers import batch_commit as bcm
from helpers import ignore_alignment as ia
from helpers.instructions_posture import (
    BASIC_MD,
    CLAUDE_MD,
    canonical,
    is_baseline,
    read_project,
    scan_text,
    unescape_target,
)
from helpers.instructions_wiring import (
    OUTCOME_FAILED,
    OUTCOME_SKIPPED_BLOCKED,
    OUTCOME_SKIPPED_STATE_CHANGED,
    OUTCOME_UNVERIFIED,
    ApplyOutcome,
    dominant_newline,
)
from helpers.io_utils import _atomic_write

#: The one new outcome. `ApplyOutcome.render` falls back to the raw string, so a
#: sentence is derived from it like every other, never the reverse.
OUTCOME_MIGRATED = "migrated"


@dataclass(frozen=True)
class MigrationPlan:
    """What would be written to `CLAUDE.md`, and the facts it was planned from."""

    blocked: str = ""
    new_claude_text: str = ""
    claude_sha: str = ""
    basic_sha: str = ""
    #: Bytes of BASIC's text that end up inside CLAUDE.md.
    carried_bytes: int = 0
    #: BASIC was the untouched template: only its baseline include was kept.
    dropped_template: bool = False

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)

    def preview(self) -> str:
        """One line a person can read before saying yes."""
        if self.blocked:
            return self.blocked
        if self.dropped_template:
            return ("BASIC_INSTRUCTIONS.md is the untouched template: only "
                    "its baseline include moves into CLAUDE.md")
        return ("%d B of BASIC_INSTRUCTIONS.md move into CLAUDE.md, in place"
                % self.carried_bytes)


# -- reading --------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: str) -> "str | None":
    """Text with line endings intact, or None when unreadable."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def read_pair(project_root: str) -> "tuple[str | None, str | None]":
    """`(CLAUDE.md text, BASIC text)`, each None when absent or unreadable."""
    return (_read(os.path.join(project_root, CLAUDE_MD)),
            _read(os.path.join(project_root, BASIC_MD)))


# -- planning (pure) ------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _without_directives(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if not line.startswith("@"))


def is_untouched_template(basic_text: str, placeholder_text: str) -> bool:
    """BASIC equals the shipped template, ignoring include lines.

    Include lines are ignored on both sides because the template ships with one
    spelling of the baseline path and a project's copy was rewritten to another;
    the prose around it is what says whether anybody wrote anything.
    """
    if not placeholder_text:
        return False
    return (_norm(_without_directives(basic_text))
            == _norm(_without_directives(placeholder_text)))


def _plain_basic_directive(raw: str) -> bool:
    """`@BASIC_INSTRUCTIONS.md` or `@./BASIC_INSTRUCTIONS.md`, nothing else."""
    target = unescape_target(raw).replace("\\", "/")
    return target in (BASIC_MD, "./" + BASIC_MD)


def _include_lines(text: str) -> str:
    """Just the directive lines of *text*, verbatim."""
    directives, _soft, _fence = scan_text(text)
    wanted = {number for number, _raw in directives}
    return "".join(line for number, line in
                   enumerate(text.splitlines(keepends=True), 1)
                   if number in wanted)


def _baseline_count(text: str) -> int:
    directives, _soft, _fence = scan_text(text)
    return sum(1 for _n, raw in directives if is_baseline(unescape_target(raw)))


def _replacement(basic_text: str, newline: str, placeholder_text: str
                 ) -> "tuple[str, bool]":
    """The text that stands where the directive was, and whether it is a drop."""
    dropped = is_untouched_template(basic_text, placeholder_text)
    body = _include_lines(basic_text) if dropped else basic_text
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return body.replace("\n", newline), dropped


def plan_migration(claude_text: str, basic_text: str,
                   placeholder_text: str = "") -> MigrationPlan:
    """Decide the new `CLAUDE.md`. Pure.

    *placeholder_text* is the shipped BASIC template, used only to recognise an
    untouched one.
    """
    directives, _soft, _fence = scan_text(claude_text)
    hits = [n for n, raw in directives if _plain_basic_directive(raw)]
    if not hits:
        return MigrationPlan(blocked="CLAUDE.md does not include "
                                     "BASIC_INSTRUCTIONS.md")
    if len(hits) > 1:
        return MigrationPlan(blocked="CLAUDE.md includes BASIC_INSTRUCTIONS.md "
                                     "more than once - not guessing which")
    if scan_text(basic_text)[2]:
        return MigrationPlan(blocked="BASIC_INSTRUCTIONS.md has an unclosed "
                                     "code fence; inlined, it would swallow "
                                     "the rest of CLAUDE.md")

    newline = dominant_newline(claude_text)
    replacement, dropped = _replacement(basic_text, newline, placeholder_text)
    lines = claude_text.splitlines(keepends=True)
    lines[hits[0] - 1] = replacement
    new_text = "".join(lines)

    baselines = _baseline_count(new_text)
    if baselines == 0:
        return MigrationPlan(blocked="neither file includes the baseline - "
                                     "wire the project first")
    if baselines > 1:
        return MigrationPlan(blocked="the baseline would load twice after "
                                     "migrating - not guessing which to keep")
    return MigrationPlan(
        new_claude_text=new_text, claude_sha=_sha(claude_text),
        basic_sha=_sha(basic_text), carried_bytes=len(replacement.encode()),
        dropped_template=dropped)


def eligible(posture) -> bool:
    """May the panel OFFER a migration for this project?

    Offering is cheaper than deciding: it needs the project to be healthy and
    to actually be on the three-file chain. Whether the text itself can be
    spliced is `plan_migration`'s answer, asked when somebody clicks.
    """
    if posture.excluded or not posture.has_basic or not posture.healthy:
        return False
    return canonical(os.path.join(posture.display_root, BASIC_MD)) in posture.chain


# -- applying -------------------------------------------------------------

def apply_migration(project_root: str, plan: MigrationPlan
                    ) -> "tuple[bool, str, bool]":
    """Write *plan*. Returns `(ok, error, stale)`; `stale` means a file moved.

    Both files are re-read and re-hashed first: the plan is a statement about
    two specific texts, not about whatever is on disk now.
    """
    if plan.is_blocked:
        return False, plan.blocked, False
    claude_text, basic_text = read_pair(project_root)
    if claude_text is None or basic_text is None:
        return False, "could not read CLAUDE.md or BASIC_INSTRUCTIONS.md", False
    if _sha(claude_text) != plan.claude_sha or _sha(basic_text) != plan.basic_sha:
        return False, "CLAUDE.md or BASIC_INSTRUCTIONS.md changed since the plan", True
    ok, message = _atomic_write(os.path.join(project_root, CLAUDE_MD),
                                plan.new_claude_text, CLAUDE_MD)
    return ok, "" if ok else message, False


def _gate(fresh) -> str:
    """Why this project may not migrate yet, or ""."""
    if not fresh.has_basic:
        return "no BASIC_INSTRUCTIONS.md to migrate"
    if not fresh.healthy:
        return "the chain does not resolve inside the project yet - repair first"
    if fresh.copy_state != bc.COPY_CURRENT:
        return "the project's baseline copy is not current - repair first"
    return ""


def migrate_project(posture, baseline: str, template_dir: str,
                    placeholder_text: str = "", baseline_template_text: str = "",
                    claude_projects=None, git_exe: str = "") -> ApplyOutcome:
    """Re-read, re-plan, write, re-read. One project. Never reuses a preview."""
    def reread():
        args = (posture.display_root, posture.name, template_dir, baseline,
                baseline_template_text, placeholder_text)
        return read_project(*args) if claude_projects is None \
            else read_project(*args, claude_projects)

    fresh = reread()
    before = (fresh.reach, fresh.delivery, fresh.copy_state)
    if before != (posture.reach, posture.delivery, posture.copy_state):
        return ApplyOutcome(OUTCOME_SKIPPED_STATE_CHANGED,
                            "was %s, now %s" % (
                                "/".join((posture.reach, posture.delivery,
                                          posture.copy_state)),
                                "/".join(before)))
    refusal = _gate(fresh)
    if refusal:
        return ApplyOutcome(OUTCOME_SKIPPED_BLOCKED, refusal)

    claude_text, basic_text = read_pair(fresh.display_root)
    if claude_text is None or basic_text is None:
        return ApplyOutcome(OUTCOME_FAILED, "could not read the instruction files")
    plan = plan_migration(claude_text, basic_text, placeholder_text)
    if plan.is_blocked:
        return ApplyOutcome(OUTCOME_SKIPPED_BLOCKED, plan.blocked)

    dirty_before = bcm.capture_dirty(git_exe, fresh.display_root) if git_exe else None
    ok, error, stale = apply_migration(fresh.display_root, plan)
    if not ok:
        return ApplyOutcome(OUTCOME_SKIPPED_STATE_CHANGED if stale
                            else OUTCOME_FAILED, error)

    after = reread()
    aligned = ia.align(after.display_root, git_exe, ia.companions_of(after)) \
        if git_exe else None
    still_wired = (after.healthy and after.reached_baseline == fresh.reached_baseline
                   and canonical(os.path.join(after.display_root, BASIC_MD)) not in after.chain)
    outcome = ApplyOutcome(
        OUTCOME_MIGRATED if still_wired else OUTCOME_UNVERIFIED,
        plan.preview() if still_wired else
        "still %s after writing" % "/".join((after.reach, after.delivery,
                                              after.copy_state)),
        changed_files=(CLAUDE_MD,), alignment=aligned)
    if git_exe:
        outcome = dataclasses.replace(outcome, evidence=bcm.make_evidence(
            git_exe, fresh.display_root, dirty_before, outcome.all_files))
    return outcome

