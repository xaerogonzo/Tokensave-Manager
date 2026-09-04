"""mcp_shadow — is Claude Desktop's global tokensave server shadowing *this* project?

## The failure this exists to name

Measured 2026-08-26 while planning it. A session working in Token Save Manager
Source asked ``tokensave_status`` over MCP and was told 741 files / 24,530
nodes / branch ``joback-thermophysical``. The CLI, asked about the same
directory, said 301 files / 9,568 nodes / branch ``Roadmap-11``. The MCP
answer's ``db_size_bytes`` was 93,863,936 — a byte-exact match for a
*different repo's* ``.tokensave/tokensave.db``.

Nothing was stale. Claude Desktop registers ``tokensave`` in its own
``claude_desktop_config.json`` and spawns the manager's wrapper **app-level**
(both live wrappers had ``ppid`` = the Desktop process), so the wrapper's cwd
can never identify the session's project. It picks one project from the global
pin, and every Desktop-hosted Claude Code session inherits that one server —
whichever repo the session is actually in. The project's own ``.mcp.json``
server was running correctly at the same time; it simply lost the name
collision.

## Why the verdict must be relative to a project

A running Desktop wrapper is **not inherently a fault**. If it serves project A
and you are inspecting project A, it is doing exactly the right thing. It
becomes a fault only when it wins the ``tokensave`` name for a project while
serving a *different* one.

Getting this wrong would make the new Doctor rule fire against the one project
that is working — and this Doctor has already had to be fixed once for nagging.
:func:`classify_shadow` therefore takes the inspected project and returns one
of five states, never a bare boolean.

## Evidence discipline

Attribution comes from :mod:`helpers.tokensave_daemon`, which ranks its
evidence ``wrapper record / -p argument`` above ``-shm mtime correlation``. A
``-shm`` match names a project as a *guess*, so this module reports UNCERTAIN
rather than promoting it to a confirmed shadow. Wording never outruns
evidence: "cannot determine which project it serves" is a different sentence
from "is serving OpenChem Studio instead", and only the second accuses
anything.
"""

from __future__ import annotations

from dataclasses import dataclass

# From the family leaf, not the `helpers.mcp` facade: the facade imports
# every family module, so importing it back from a sibling would close a
# runtime cycle. See scripts/split_mcp.py.
from helpers.mcp_paths import _same_project

#: Selection reasons the manager's wrapper writes into its run record. A
#: server carrying one of these was spawned by the wrapper — i.e. by Claude
#: Desktop — rather than by a Claude Code session's own ``.mcp.json``. Servers
#: Claude Code started carry ``""``.
WRAPPER_SELECTIONS = frozenset({"pin", "most-recent-index", "none"})

#: No ``tokensave`` entry in Claude Desktop's config: nothing can shadow.
SHADOW_NONE = "none"
#: The entry exists but no wrapper server is running. A config-level fact, not
#: a runtime fault — Desktop is closed, or has not started the server yet.
SHADOW_DORMANT = "dormant"
#: A wrapper server is running and serving the project being inspected.
#: Active, and correct for *this* project.
SHADOW_SERVING_THIS = "serving_this"
#: A wrapper server is running and serving a different project. The fault.
SHADOW_ACTIVE = "active"
#: A wrapper server is running but its project cannot be established, or rests
#: only on the ``-shm`` guess. Reported as unknown, never as a shadow.
SHADOW_UNCERTAIN = "uncertain"

#: States that belong in a needs-attention area. UNCERTAIN earns its place —
#: "we cannot tell" is actionable — but it must not be worded as a shadow.
ATTENTION_STATES = frozenset({SHADOW_ACTIVE, SHADOW_UNCERTAIN})

_SELECTION_WHY = {
    "pin": "the pinned project",
    "most-recent-index": ("the most recently indexed project -- nothing was "
                          "pinned, so this moves whenever another project "
                          "syncs"),
    "none": "no project at all -- the wrapper found none to serve",
}


@dataclass(frozen=True)
class ShadowVerdict:
    """What Claude Desktop's tokensave is doing *to the inspected project*."""

    state: str
    #: The project the live wrapper server is serving, when known.
    served_project: "str | None" = None
    inspected_project: str = ""
    pid: "int | None" = None
    #: The wrapper's own reason for choosing that project ("pin", ...).
    selection: str = ""
    attribution: str = ""
    label: str = ""
    detail: str = ""

    @property
    def is_fault(self) -> bool:
        """Only a confirmed wrong-tree shadow is a fault."""
        return self.state == SHADOW_ACTIVE

    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION_STATES

    @property
    def is_runtime(self) -> bool:
        """Is a wrapper server actually running right now?

        Separates configuration evidence from runtime evidence, which the UI
        must keep apart: a dormant entry is not a running server.
        """
        return self.state in (SHADOW_SERVING_THIS, SHADOW_ACTIVE,
                              SHADOW_UNCERTAIN)


def wrapper_servers(servers: list) -> list:
    """The subset of *servers* that Claude Desktop's wrapper spawned.

    Identified by the wrapper's own run record rather than by process
    ancestry, because the record survives the wrapper exiting and is the
    evidence the daemon helper already collects.
    """
    return [s for s in (servers or [])
            if getattr(s, "selection", "") in WRAPPER_SELECTIONS]


def _live_wrapper_server(servers: list):
    """The wrapper server a session would be talking to — the newest one.

    Several can be alive at once (one per Desktop-hosted session). They all
    resolve the same pin, so any of them answers the question "which project
    is Desktop serving"; the newest is chosen so the reported PID is the one
    the user is most likely to see.
    """
    candidates = wrapper_servers(servers)
    if not candidates:
        return None
    return max(candidates, key=lambda s: getattr(s, "started_at", 0.0))


def classify_shadow(inspected_project: str,
                    *,
                    desktop_entry_present: bool,
                    servers: "list | None" = None) -> ShadowVerdict:
    """Classify Desktop's tokensave **relative to** *inspected_project*.

    Pure: every input is passed in, so each of the five states can be tested
    against constructed servers rather than whatever happens to be running.
    """
    if not desktop_entry_present:
        return ShadowVerdict(
            SHADOW_NONE, inspected_project=inspected_project,
            label="no Claude Desktop tokensave",
            detail=("Claude Desktop does not define a `tokensave` server, so "
                    "nothing of its own can shadow a project binding."))

    srv = _live_wrapper_server(servers or [])
    if srv is None:
        return ShadowVerdict(
            SHADOW_DORMANT, inspected_project=inspected_project,
            label="Desktop tokensave defined, not running",
            detail=("Claude Desktop defines a `tokensave` server but none is "
                    "running. It will shadow project bindings again the next "
                    "time Desktop starts one."))

    common = dict(inspected_project=inspected_project,
                  pid=getattr(srv, "pid", None),
                  selection=getattr(srv, "selection", ""),
                  attribution=getattr(srv, "attribution", ""))
    served = getattr(srv, "project", None)

    if not served or getattr(srv, "is_guess", False):
        return ShadowVerdict(
            SHADOW_UNCERTAIN, served_project=served,
            label="Desktop tokensave running — project unknown",
            detail=("Claude Desktop is running a `tokensave` server, but the "
                    "manager cannot establish which project it serves, so it "
                    "cannot say whether this binding is being shadowed."),
            **common)

    if _same_project(served, inspected_project):
        return ShadowVerdict(
            SHADOW_SERVING_THIS, served_project=served,
            label="Desktop tokensave is serving this project",
            detail=("Claude Desktop's global server happens to be serving "
                    "this project, so sessions here get the right tree. It "
                    "serves exactly one project at a time, so every OTHER "
                    "project is being shadowed right now."),
            **common)

    why = _SELECTION_WHY.get(getattr(srv, "selection", ""), "")
    return ShadowVerdict(
        SHADOW_ACTIVE, served_project=served,
        label="shadowed by Claude Desktop's global tokensave",
        detail=("This project's binding is correct, but Claude Desktop runs "
                "its own `tokensave` server and that one wins the name. It is "
                "serving %s%s, so questions asked here are answered from that "
                "tree." % (served, " -- chosen as %s" % why if why else "")),
        **common)


def structural_note(project_count: int) -> str:
    """The machine-wide fact, true whichever project is selected.

    Kept separate from :func:`classify_shadow` because it is not a per-project
    verdict: it is the reason the per-project verdict can never be good for
    everyone at once.
    """
    if project_count <= 1:
        return ""
    return ("Claude Desktop runs one `tokensave` server for the whole machine "
            "and it can serve only one project at a time, so at most one of "
            "your %d bound projects is ever answered correctly."
            % project_count)
