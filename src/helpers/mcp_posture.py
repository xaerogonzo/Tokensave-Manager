"""mcp_posture — what all the MCP wiring adds up to, in one answer.

## Why this exists

The MCP surface grew one panel per incident — desktop scope collision, trust
gate, binding-inert, duplicate keys, Cursor — and each panel is correct about
its own question. Nothing answered the question a user actually has, which is
*"can any project be answered from the wrong codebase, and is every project
served at all?"* Without that, the dialog's loudest control was an Apply button
on projects that were working fine.

## The correction this module encodes

Measured 2026-09-09 on a machine with sixteen indexed projects: four tokensave
servers were running, one per project, all spawned by Claude Code sessions
rather than by the wrapper — and one of them served a project with **no
`.mcp.json` at all**. A user-scoped bare ``serve`` is spawned per session with
that session's own cwd, so it resolves to that session's project.

Per-project binding is therefore a **determinism upgrade, not a prerequisite**.
The dialog nevertheless bucketed every unbound project under "needs binding",
which is true only once the fallback is gone.

## Two things kept apart, because conflating them is the whole bug class

**Tier is local configuration. Service is derived.** ``tier`` says what a
project's own files declare and nothing else; whether it is currently *served*
depends on a machine-wide fact — does the automatic fallback exist — that has
no business inside a per-project verdict. The same project is fine or broken
depending on it. Fold the fallback into the tier and an ``EXPLICIT_INERT`` row
can render a green "served" badge on a machine where it is served by nothing.

**Intent is not fact.** ``lifecycle_state`` already separates "the user retired
this" from "the entry is on disk"; :func:`classify_posture` reads serviceability
off the FACT (``PRESENT`` or ``RETURNED``) and never off the flag.

## Aggregation only

This module owns aggregation and presentation vocabulary. It does **not**
re-decide classification, trust, approval or project identity — those live in
``mcp_classify``, ``mcp_projects`` and ``mcp_approval``, and a second opinion
here would be one more hidden classifier in a feature whose entire point is
removing them. :func:`classify_posture` is pure and takes already-classified
facts; :func:`read_posture` is the only function that touches the filesystem,
and it gathers those facts exclusively through the existing readers.

Pure module apart from :func:`read_posture` — stdlib only, no Tkinter, safe
from any thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from helpers.mcp_paths import (
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETURNED,
    USER_SCOPE_RETIRED_KEY,
    lifecycle_state,
)

# ── vocabulary ───────────────────────────────────────────────────────────

#: Three-valued, matching `TRUST_*` and `EffectiveScope.is_known` rather than
#: `bool`. A source this module could not read must never become a confident
#: verdict — that is the "unknown is never zero" rule the rest of the project
#: already applies, and an aggregate is the easiest place to lose it.
VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_UNKNOWN = "unknown"

#: Whether each underlying fact was actually obtained. Kept per source so
#: "posture incomplete" can say WHICH source failed instead of shrugging.
READ_OK = "ok"
READ_UNREADABLE = "unreadable"
READ_MALFORMED = "malformed"

#: What a project's OWN configuration declares. Nothing machine-wide.
#: `.mcp.json` binds this project and would load.
TIER_EXPLICIT = "explicit"
#: `.mcp.json` is correct but is not loaded — untrusted folder, unapproved
#: server, or a local-scoped definition outranking it.
TIER_EXPLICIT_INERT = "explicit_inert"
#: `.mcp.json` binds a DIFFERENT project. Not merely inert: when it loads, it
#: answers from another codebase and looks entirely normal doing it.
TIER_MISBOUND = "misbound"
#: No project binding of its own.
TIER_NONE = "none"
#: A fact needed to decide was unreadable.
TIER_UNKNOWN = "unknown"

#: How a project is actually served, derived from tier + the machine fallback.
SERVICE_SELF = "self"
SERVICE_AUTOMATIC = "automatic"
SERVICE_WRONG = "wrong"
SERVICE_UNSERVED = "unserved"
SERVICE_UNKNOWN = "unknown"

#: Classifier states that mean "the file is correct but something outside it
#: stops it loading". Imported rather than restated so this module cannot
#: drift from the set the panels already act on.
from helpers.mcp_approval import ADVISORY_STATES  # noqa: E402

#: Classifier states where the file binds THIS project, whether or not it does
#: so in the portable form. `project_absolute` names the right project with a
#: machine-local path and `project_unbound` resolves by cwd, which at a project
#: root is this project — both are hygiene notes the details panel already
#: makes, not serving faults.
_SERVES_SELF_STATES = frozenset({"ok", "project_absolute", "project_unbound"})

#: No tokensave binding of this project's own. `missing` is a `.mcp.json` that
#: exists for other servers and names no tokensave, which is the same absence.
_NO_BINDING_STATES = frozenset({"no_file", "missing"})


# ── the facts, per project ───────────────────────────────────────────────

@dataclass(frozen=True)
class ProjectTier:
    """One project's own declaration, plus the identity to aggregate it by."""

    #: Canonical identity, from `normalize_project_key`. Aggregation counts
    #: are wrong if one directory reachable through two search roots produces
    #: two rows, so this is the key and `display_root` is for showing.
    root: str
    display_root: str
    name: str
    tier: str
    detail: str = ""


@dataclass(frozen=True)
class Posture:
    """What the machine's MCP wiring adds up to."""

    desktop_state: str
    desktop_read: str
    userscope_state: str
    userscope_read: str
    #: Is a machine-wide `tokensave` available to a project with no binding?
    #: Read off the FACT — an entry that came back after being retired is
    #: physically there and does serve, so `RETURNED` counts.
    automatic_fallback: bool
    #: Can any project be answered from another codebase?
    independent: str
    #: Does every project have some serving path?
    covered: str
    projects: tuple = ()

    @property
    def reads_ok(self) -> bool:
        return (self.desktop_read == READ_OK
                and self.userscope_read == READ_OK
                and not any(p.tier == TIER_UNKNOWN for p in self.projects))

    @property
    def headline_ok(self) -> bool:
        """May the overview show the green headline?

        Requires BOTH verdicts and successful reads. `independent` alone would
        go green on a machine where a project is served by nothing, and either
        would go green on a machine whose config could not be read at all.
        """
        return (self.independent == VERDICT_YES
                and self.covered == VERDICT_YES
                and self.reads_ok)

    def service(self, project: "ProjectTier") -> str:
        return service_of(project.tier, self.automatic_fallback)

    @property
    def unserved(self) -> tuple:
        return tuple(p for p in self.projects
                     if service_of(p.tier, self.automatic_fallback)
                     == SERVICE_UNSERVED)

    @property
    def misbound(self) -> tuple:
        return tuple(p for p in self.projects if p.tier == TIER_MISBOUND)


# ── pure classification ──────────────────────────────────────────────────

def tier_of(state: str, trusted: "bool | None") -> str:
    """Map one project's classifier state + trust to a tier. Pure.

    `trusted` is tri-state on purpose (`None` = could not tell). Claude Code
    does not load a project's `.mcp.json` in a folder it has not been trusted
    in, so a correct file in an untrusted folder is inert — and the difference
    between "untrusted" and "we could not read `~/.claude.json`" is the
    difference between a note and an admission.
    """
    if state == "unparseable":
        return TIER_UNKNOWN
    if state == "project_mismatch":
        return TIER_MISBOUND
    if state in _NO_BINDING_STATES:
        return TIER_NONE
    if state in _SERVES_SELF_STATES:
        if trusted is None:
            return TIER_UNKNOWN
        return TIER_EXPLICIT if trusted else TIER_EXPLICIT_INERT
    if state in ADVISORY_STATES:
        return TIER_EXPLICIT_INERT
    # A shape this module has no rule for. Saying UNKNOWN costs a grey row;
    # guessing would put a confident badge on a state nobody has looked at.
    return TIER_UNKNOWN


def service_of(tier: str, automatic_fallback: bool) -> str:
    """How a project is actually served. Pure, and the ONLY place the
    machine-wide fallback is allowed to touch a per-project verdict.

    Rendering reads this, never `tier` — which is what makes it structurally
    impossible for an `EXPLICIT_INERT` row to show a green "served" badge on a
    machine with no fallback.
    """
    if tier == TIER_UNKNOWN:
        return SERVICE_UNKNOWN
    if tier == TIER_EXPLICIT:
        return SERVICE_SELF
    if tier == TIER_MISBOUND:
        return SERVICE_WRONG
    # TIER_NONE and TIER_EXPLICIT_INERT both depend on the fallback, and for
    # the same reason: neither has a binding of its own that loads.
    return SERVICE_AUTOMATIC if automatic_fallback else SERVICE_UNSERVED


def classify_posture(*, desktop_state: str, desktop_read: str,
                     userscope_state: str, userscope_read: str,
                     project_tiers) -> "Posture":
    """Aggregate already-classified facts. Pure — reads nothing.

    Takes states and tiers, never raw config dictionaries, so it cannot
    quietly become a second source of truth for MCP state. Feeding it
    contradictory inputs produces a verdict about those inputs; it does not go
    back to disk to check.

    ``independent`` and ``covered`` are orthogonal and both are needed. A
    machine deliberately running explicit bindings only, with the fallback
    retired and every project bound, is independent AND covered. A machine
    that retired the fallback with two projects still unbound is independent
    but NOT covered — nothing can serve the wrong project there, and two
    projects are served by nothing at all. Collapsing them into one boolean
    would have to lie about one of those.
    """
    projects = tuple(project_tiers)
    automatic_fallback = userscope_state in (LIFECYCLE_PRESENT,
                                             LIFECYCLE_RETURNED)
    services = [service_of(p.tier, automatic_fallback) for p in projects]
    any_unknown = any(s == SERVICE_UNKNOWN for s in services)

    if desktop_read != READ_OK or userscope_read != READ_OK or any_unknown:
        independent = VERDICT_UNKNOWN
    elif desktop_state in (LIFECYCLE_PRESENT, LIFECYCLE_RETURNED):
        # Desktop spawns its `tokensave` app-level, so its cwd is the app and
        # every Desktop-hosted session inherits that one server whatever repo
        # it is in. Measured 2026-08-26.
        independent = VERDICT_NO
    elif any(s == SERVICE_WRONG for s in services):
        # The other way to be answered from the wrong codebase, and the one a
        # Desktop-only predicate would miss entirely: a `.mcp.json` with `-p`
        # naming a different project.
        independent = VERDICT_NO
    else:
        independent = VERDICT_YES

    if any_unknown or userscope_read != READ_OK:
        covered = VERDICT_UNKNOWN
    elif any(s == SERVICE_UNSERVED for s in services):
        covered = VERDICT_NO
    else:
        covered = VERDICT_YES

    return Posture(desktop_state=desktop_state, desktop_read=desktop_read,
                   userscope_state=userscope_state,
                   userscope_read=userscope_read,
                   automatic_fallback=automatic_fallback,
                   independent=independent, covered=covered,
                   projects=projects)


# ── the one IO boundary ──────────────────────────────────────────────────

def read_posture(cfg) -> "Posture":
    """Gather the facts through the EXISTING readers, then classify.

    Every fact here already has an owner, and this function calls that owner.
    It adds no parsing of its own: the moment it starts interpreting a config
    dict, it becomes the sixth thing that knows what an MCP entry means.

    Never raises. A reader that falls over degrades to a read status, which
    the overview renders as "posture incomplete" naming the failed source —
    strictly better than an exception taking the dialog down, and strictly
    better than a confident verdict built on a fact nobody obtained.
    """
    from helpers import mcp_desktop
    from helpers.mcp_classify import _classify_mcp_entry
    from helpers.mcp_approval import annotate_project_binding
    from helpers.mcp_paths import _mcp_code_cfg_path, _project_mcp_path
    from helpers.mcp_projects import (TRUST_TRUSTED, TRUST_UNKNOWN,
                                      normalize_project_key,
                                      project_trust_state,
                                      read_claude_projects)

    raw = cfg.raw if isinstance(getattr(cfg, "raw", None), dict) else {}

    # Desktop: intent from the flag, fact from the active installation only —
    # a Beta package's leftover entry cannot shadow the Desktop in use.
    try:
        desktop_state = lifecycle_state(
            mcp_desktop.desktop_entry_present(), mcp_desktop.is_retired(raw))
        desktop_read = READ_OK
    except Exception:                                        # noqa: BLE001
        desktop_state, desktop_read = "", READ_UNREADABLE

    # User scope: `~/.claude.json`. A file that does not exist is a legitimate
    # "no entry"; one that will not parse is a failure to read.
    try:
        info = _classify_mcp_entry(_mcp_code_cfg_path(), raw)
        userscope_read = (READ_MALFORMED if info["state"] == "unparseable"
                          else READ_OK)
        userscope_state = lifecycle_state(
            info.get("current") is not None,
            bool(raw.get(USER_SCOPE_RETIRED_KEY)))
    except Exception:                                        # noqa: BLE001
        userscope_state, userscope_read = "", READ_UNREADABLE

    try:
        claude_projects = read_claude_projects()
    except Exception:                                        # noqa: BLE001
        claude_projects = {}

    tiers = []
    seen = set()
    for proj in _discover(cfg):
        root = proj.get("path") if isinstance(proj, dict) else str(proj)
        if not root:
            continue
        # Only projects with an index of their own. Without one there is
        # nothing to serve, and the classifier (correctly) refuses project
        # scope for such a path.
        if not os.path.isdir(os.path.join(root, ".tokensave")):
            continue
        key = normalize_project_key(root)
        if key in seen:
            # One directory reachable through two search roots. Counting it
            # twice would inflate `unserved` and make the headline wrong about
            # a project that does not exist.
            continue
        seen.add(key)
        name = (proj.get("name") if isinstance(proj, dict) else "") \
            or os.path.basename(root) or root
        try:
            info = _classify_mcp_entry(_project_mcp_path(root), raw)
            info = annotate_project_binding(info, root,
                                            projects=claude_projects)
            trust = project_trust_state(root, projects=claude_projects)
            trusted = None if trust == TRUST_UNKNOWN else trust == TRUST_TRUSTED
            tier = tier_of(info["state"], trusted)
            detail = info.get("issue", "") or info.get("label", "")
        except Exception as exc:                             # noqa: BLE001
            tier, detail = TIER_UNKNOWN, "could not inspect: %s" % exc
        tiers.append(ProjectTier(root=key, display_root=root, name=name,
                                 tier=tier, detail=detail))

    return classify_posture(desktop_state=desktop_state,
                            desktop_read=desktop_read,
                            userscope_state=userscope_state,
                            userscope_read=userscope_read,
                            project_tiers=tiers)


def _discover(cfg) -> list:
    """Project discovery, degraded to empty rather than raising.

    Its own function so `read_posture` reads as a list of facts gathered, and
    so a discovery failure cannot be mistaken for "no projects are bound".
    """
    try:
        from helpers.project_discovery import find_projects
        return list(find_projects(cfg.search_roots))
    except Exception:                                        # noqa: BLE001
        return []
