"""mcp_setup — what to do about a posture, described rather than done.

## What this is and, more importantly, what it is not

A planner. It turns a :class:`~helpers.mcp_posture.Posture` into an ordered
list of :class:`SetupStep`, and it writes nothing. Every step names an action
that an EXISTING writer already performs, complete with that writer's
timestamped backup, its show-diff confirmation and its running-Claude guard:

  ``retire_desktop``    -> :func:`helpers.mcp_desktop.retire`
  ``accept_desktop``    -> the retirement flag in ``manager-config.json``
  ``restore_userscope`` -> ``tokensave install --agent claude`` (its picker)
  ``bind_project``      -> :func:`helpers.mcp_classify._apply_mcp_fix`
  ``approve_project``   -> :func:`helpers.mcp_approval.approve_project_binding`
  ``trust_project``     -> nothing; a person must answer Claude Code's prompt
  ``strict_tree``       -> :func:`helpers.tokensave_config.set_strict_tree`

Keeping the description and the doing apart is deliberate. A "one-click setup"
that silently performed six configuration mutations would be the opposite of
what this whole change is for: the point is that the user can see what their
machine is doing. So the UI shows the plan, and each step still goes through
the writer that owns it.

## Steps that are alternatives, not stages

Some postures have two valid resolutions and no way to tell which the user
wants — a Desktop entry that came back may be an unwanted regression or a
deliberate reinstall, and only the user knows. Those steps share a ``group``,
and a group must be rendered as a choice. Rendering them as ordinary actions
invites clicking one after the other, which is self-contradictory.

## One step cannot be automated at all

Claude Code does not load a project's ``.mcp.json`` in a folder it has not been
trusted in, and trust is granted by a person answering "Do you trust the files
in this folder?" — it cannot be written to a file. ``automatable=False`` says
so, and it must never be quietly dropped from a plan to make the count look
smaller.

Pure module — stdlib only, no Tkinter, no filesystem. Every input arrives in
the posture.
"""

from __future__ import annotations

from dataclasses import dataclass

from helpers.mcp_paths import (
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETURNED,
)
from helpers.mcp_posture import (
    TIER_EXPLICIT,
    TIER_EXPLICIT_INERT,
    VERDICT_UNKNOWN,
)

#: Groups. Steps sharing one are alternatives; at most one may be
#: applied. Coverage is the only genuine fork left: restoring the shared
#: entry and binding each project reach the same coverage by different
#: routes, and which one a user wants is not derivable from here.
GROUP_COVERAGE = "coverage"


@dataclass(frozen=True)
class SetupStep:
    """One action, named — never performed here."""

    kind: str
    label: str
    detail: str = ""
    #: Project root, config path, or "" for a machine-wide action.
    target: str = ""
    #: False only for the trust prompt, which no file can grant.
    automatable: bool = True
    #: Steps sharing a non-empty group are mutually exclusive.
    group: str = ""


def plan_independence(posture) -> list:
    """What stands between this machine and independent, covered projects.

    Deterministic from the posture and nothing else: no probing, no
    filesystem, no "let me just check". Given the same posture it returns the
    same plan, which is what makes the mapping testable one row at a time.

    Returns ``[]`` for a machine that is already there — including the
    explicit-only posture, where the fallback is deliberately retired and
    every project carries its own binding. That is the strictest setup
    available, and proposing to "fix" it by re-adding a fallback would be
    undoing the user's decision.
    """
    # An unread fact is not a licence to act. Proposing a retirement because
    # the config could not be parsed would be acting on our own failure.
    if posture.independent == VERDICT_UNKNOWN or not posture.reads_ok:
        return [SetupStep(
            kind="explain", automatable=False,
            label="Posture incomplete — no action proposed",
            detail=("At least one fact this depends on could not be read, so "
                    "nothing is suggested. Acting here would be acting on a "
                    "gap in our own reading rather than on your setup."))]

    steps = []

    if posture.desktop_state in (LIFECYCLE_PRESENT, LIFECYCLE_RETURNED):
        # One step, not a choice, and the same one whichever way the entry got
        # there. RETURNED briefly offered a second option — "keep it, clear
        # the retirement flag" — which was not a step toward independence at
        # all but a decision NOT to be independent, phrased in terms of a
        # config key. The Claude Desktop chat switch expresses that decision
        # properly, in both directions, so a plan that is answering "what
        # stands between this machine and independent projects" has exactly
        # one thing to say here.
        steps.append(_retire_desktop_step())

    for project in posture.misbound:
        steps.append(SetupStep(
            kind="rebind_project", target=project.display_root,
            label="Rebind %s to itself" % project.name,
            detail=("Its .mcp.json binds tokensave to a different project, so "
                    "every query asked there is answered from that other tree "
                    "and looks entirely normal. Rewrites the binding to this "
                    "project in the portable form.")))

    steps.extend(_coverage_steps(posture))
    return steps


def _retire_desktop_step(group: str = "") -> "SetupStep":
    """The one action that buys independence, worded as the trade it is."""
    return SetupStep(
        kind="retire_desktop", group=group,
        label="Retire Claude Desktop's tokensave entry",
        detail=("Desktop spawns its server app-level, so its working "
                "directory is the app rather than your session — every "
                "Desktop-hosted Claude Code session inherits that one server "
                "whatever repo it is in, and it wins the `tokensave` name "
                "over each project's own binding. Retiring it is what makes "
                "sessions serve their own project. Claude Desktop's own chat "
                "then has no tokensave at all: that is the trade, not a "
                "side effect."))


def _coverage_steps(posture) -> list:
    """Two ways to serve a project that nothing currently serves.

    Alternatives, because they are: restore the machine-wide fallback and
    every unbound project is served automatically, or bind each one and they
    serve themselves. Both end with the same coverage and a different posture,
    and the choice is the user's.
    """
    unserved = posture.unserved
    if not unserved:
        return []
    names = ", ".join(p.name for p in unserved[:4])
    if len(unserved) > 4:
        names += ", …"
    steps = [SetupStep(
        kind="restore_userscope", group=GROUP_COVERAGE,
        label="Restore the machine-wide fallback",
        detail=("Runs `tokensave install --agent claude`, which writes a bare "
                "`serve` entry to ~/.claude.json. Each session spawns it in "
                "that session's own folder, so it resolves to that session's "
                "project — %s included. Projects keep their own bindings "
                "where they have them." % names))]
    for project in unserved:
        step = SetupStep(
            kind="bind_project", group=GROUP_COVERAGE,
            target=project.display_root,
            label="Bind %s to itself" % project.name,
            detail=("Writes a .mcp.json binding tokensave to this project. "
                    "Deterministic even from a subdirectory or a worktree — "
                    "and it needs the folder trusted before it loads."))
        steps.append(step)
    return steps


def plan_pin_down(posture, project) -> list:
    """Give one project an explicit binding, in the order the steps depend.

    Used for a project that is already served — by the machine-wide fallback —
    where the user wants determinism instead: a git worktree, a nested repo,
    or a session that starts in a subdirectory, all of which resolve by
    searching upward and can find the wrong index or none.

    The trust step is included even though nothing here can perform it.
    Dropping it would make the plan look complete while leaving the binding
    inert, which is the precise failure that made thirteen "bound" projects
    read as a finished migration while three of them were never loaded.
    """
    steps = []
    if project.tier not in (TIER_EXPLICIT, TIER_EXPLICIT_INERT):
        steps.append(SetupStep(
            kind="bind_project", target=project.display_root,
            label="Write %s/.mcp.json" % project.name,
            detail=("Binds tokensave to this project in the portable form, so "
                    "the file stays valid on anyone else's machine.")))
    steps.append(SetupStep(
        kind="approve_project", target=project.display_root,
        label="Approve the binding in %s" % project.name,
        detail=("Writes the `tokensave` server into this project's own "
                ".claude/settings.local.json — exactly what Claude Code "
                "records when you approve its prompt, and it authorises that "
                "one server by name.")))
    steps.append(SetupStep(
        kind="trust_project", target=project.display_root, automatable=False,
        label="Trust the folder (opens a Claude Code session)",
        detail=("Claude Code does not load a project's .mcp.json in a folder "
                "it has not been trusted in. Trust is granted by answering "
                "\"Do you trust the files in this folder?\" — it cannot be "
                "written to a file, so this step opens a session there and "
                "you answer the prompt.")))
    steps.append(SetupStep(
        kind="strict_tree", target=project.display_root,
        label="Enable strict_tree for %s" % project.name,
        detail=("Optional hardening, and NOT what makes the binding work: it "
                "turns an answer from the wrong working tree into an explicit "
                "refusal naming both roots, instead of a plausible answer "
                "about a checkout you are not in.")))
    return steps


def groups(steps) -> dict:
    """`{group: [steps]}` for the grouped steps only.

    A convenience for the renderer, and a place to state the rule once: a
    group is a choice. Anything that iterates a plan and renders every step as
    an available action will offer "retire this" beside "keep this".
    """
    out: dict = {}
    for step in steps:
        if step.group:
            out.setdefault(step.group, []).append(step)
    return out


def ungrouped(steps) -> list:
    """Steps that stand alone and may all be applied."""
    return [s for s in steps if not s.group]
