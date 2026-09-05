"""Whether a project's .mcp.json has actually been approved.

Split out of helpers/mcp.py (Roadmap-16 god-file split).
Importable via the ``helpers.mcp`` facade, which re-exports
every name, so existing call sites and tests are unchanged.
This module must never import that facade.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time
from helpers.mcp_paths import (
    _write_json_atomic,
)
from helpers.mcp_projects import (
    _has_session,
    duplicate_project_keys,
    matching_project_keys,
    read_claude_projects,
)




def stale_duplicate_keys(claude_json_path: str = "",
                         projects: "dict | None" = None) -> dict:
    """Duplicate keys that hold nothing worth keeping.

    A duplicate is safe to drop when a sibling spelling has the real session
    history and this one records no session, no MCP approval and no allowed
    tools — i.e. it exists only because some tool ran `claude` in that
    directory once. This manager's own status checks were doing exactly that,
    so most of these are its litter.

    Returns `{normalised: [stale raw keys]}`, and only for groups where at
    least one sibling DOES have session history — without that the group has
    no obvious keeper and the choice is the user's.
    """
    if projects is None:
        projects = read_claude_projects(claude_json_path)
    out: dict = {}
    for norm, keys in duplicate_project_keys(projects=projects).items():
        if not any(_has_session(projects.get(k) or {}) for k in keys):
            continue
        # An approval here is only load-bearing while the project file does not
        # already record it. Once it does, this copy is a redundant duplicate
        # of something stored authoritatively elsewhere — and treating it as
        # precious is how a cleanup finds nothing: writing approvals into these
        # keys is exactly what made them look meaningful in the first place.
        local = None
        for key in keys:
            if os.path.isdir(key):
                local = local_settings_approval(key, server="tokensave")
                break

        stale = []
        for key in keys:
            entry = projects.get(key) or {}
            if _has_session(entry):
                continue
            if entry.get("disabledMcpjsonServers") or \
                    entry.get("allowedTools") or \
                    entry.get("enableAllProjectMcpServers"):
                continue                 # carries a real decision; keep it
            if entry.get("enabledMcpjsonServers") and \
                    local != APPROVAL_APPROVED:
                continue                 # this copy IS the approval; keep it
            stale.append(key)
        if stale:
            out[norm] = sorted(stale)
    return out



# ── has Claude Code approved this project's `.mcp.json`? ──────────────────

APPROVAL_APPROVED = "approved"

APPROVAL_PENDING = "pending"

APPROVAL_REJECTED = "rejected"

APPROVAL_AMBIGUOUS = "ambiguous"

APPROVAL_UNKNOWN = "unknown"



@dataclasses.dataclass(frozen=True)
class McpJsonApproval:
    """Whether Claude Code has approved a project's `.mcp.json` servers.

    Free to compute — one read of `~/.claude.json`, no subprocess — and it
    answers the question that precedes every other one: an unapproved
    project-scoped server is not competing for the name at all, so no amount of
    correct `.mcp.json` content makes it serve. Worth its own tier because
    `effective_scope` costs a CLI call per project, while this settles the
    common case for free across every row at once.
    """

    state: str
    keys: tuple = ()
    detail: str = ""

    @property
    def is_approved(self) -> bool:
        return self.state == APPROVAL_APPROVED

    @property
    def blocks_binding(self) -> bool:
        """True when the binding provably cannot be serving yet.

        `unknown` is excluded on purpose: no entry in `~/.claude.json` means
        Claude Code has never run in this project, which is not evidence of
        anything. `ambiguous` IS included — duplicate keys that disagree make
        the outcome depend on how the session is launched, and a row claiming
        "bound" there would be right only by luck.
        """
        return self.state in (APPROVAL_PENDING, APPROVAL_REJECTED,
                              APPROVAL_AMBIGUOUS)



def _settings_approval(data: dict, server: str) -> "str | None":
    """Approval recorded in one settings-shaped dict, or None for no opinion.

    "No opinion" is a distinct answer from "not approved", and conflating them
    is what made this reader wrong. `enabledMcpjsonServers: []` is an opinion —
    nothing is approved. The key being ABSENT is silence, and silence must not
    outvote a record that actually says something.
    """
    if not isinstance(data, dict):
        return None
    if data.get("enableAllProjectMcpServers") is True:
        return APPROVAL_APPROVED
    enabled = data.get("enabledMcpjsonServers")
    disabled = data.get("disabledMcpjsonServers")
    if isinstance(enabled, list) and server in enabled:
        return APPROVAL_APPROVED
    if isinstance(disabled, list) and server in disabled:
        return APPROVAL_REJECTED
    if isinstance(enabled, list):
        return APPROVAL_PENDING          # present but does not name it
    return None



def _entry_approval(entry: dict, server: str) -> str:
    """Approval in one `~/.claude.json` `projects[...]` entry.

    Kept returning a definite verdict for callers that want one; silence maps
    to PENDING here because an entry Claude Code created but never recorded an
    approval in has, in fact, not approved anything.
    """
    got = _settings_approval(entry, server)
    return got if got is not None else APPROVAL_PENDING



def local_settings_approval(project_root: str,
                            server: str = "tokensave") -> "str | None":
    """Approval from the project's own `.claude/settings*.json`, or None.

    **This is where Claude Code actually keeps it.** Measured 2026-08-25:
    approvals written into `~/.claude.json` were migrated out into
    `<project>/.claude/settings.local.json` within ~12 seconds, and the field
    was stripped from the duplicate path keys on the way. A reader that only
    consults `~/.claude.json` therefore reports stale state for every project
    Claude Code has touched since — which is how this function's absence made
    a working Fortuna Lab render as "approval depends on how you launch".

    `settings.local.json` is consulted before `settings.json`: it is the
    machine-local override, and it is the file Claude Code writes.
    """
    for name in ("settings.local.json", "settings.json"):
        path = os.path.join(project_root, ".claude", name)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        got = _settings_approval(data, server)
        if got is not None:
            return got
    return None



def mcpjson_approval(project_root: str, server: str = "tokensave",
                     claude_json_path: str = "",
                     projects: "dict | None" = None) -> "McpJsonApproval":
    """Read Claude Code's approval for `server` in `project_root`.

    The project's own `.claude/settings*.json` is authoritative and is checked
    FIRST, because that is where Claude Code migrates approvals to. Only when
    it has no opinion does this fall back to the `~/.claude.json` project keys.
    """
    local = local_settings_approval(project_root, server)
    if local is not None:
        return McpJsonApproval(
            local, detail="from .claude/settings.local.json")

    if projects is None:
        projects = read_claude_projects(claude_json_path)
    keys = matching_project_keys(project_root, projects)
    if not keys:
        return McpJsonApproval(
            APPROVAL_UNKNOWN,
            detail="Claude Code has no record of this project yet.")

    # Keys that record nothing are SKIPPED rather than counted as dissent.
    # Claude Code's migration strips `enabledMcpjsonServers` from the duplicate
    # path keys, so "the duplicates disagree" became the normal post-migration
    # state — and reporting it as ambiguity warned about projects that work.
    verdicts = {}
    for key in keys:
        got = _settings_approval(projects.get(key) or {}, server)
        if got is not None:
            verdicts[key] = got
    if not verdicts:
        return McpJsonApproval(
            APPROVAL_UNKNOWN, keys=tuple(sorted(keys)),
            detail="Claude Code has entries for this project but no recorded "
                   "approval either way.")

    distinct = set(verdicts.values())
    if len(distinct) == 1:
        return McpJsonApproval(distinct.pop(), keys=tuple(sorted(verdicts)))
    return McpJsonApproval(
        APPROVAL_AMBIGUOUS, keys=tuple(sorted(verdicts)),
        detail="; ".join("%s -> %s" % (k, v)
                         for k, v in sorted(verdicts.items())))



def local_scope_shadow(project_root: str, server: str = "tokensave",
                       claude_json_path: str = "",
                       projects: "dict | None" = None) -> list:
    """Keys defining `server` in their LOCAL-scoped `mcpServers`.

    The third shadow source, and the only one that outranks a project binding
    outright. Free to read alongside approval, so there is no reason to make
    the user spend a CLI call to discover it.
    """
    if projects is None:
        projects = read_claude_projects(claude_json_path)
    hits = []
    for key in matching_project_keys(project_root, projects):
        servers = (projects.get(key) or {}).get("mcpServers")
        if isinstance(servers, dict) and server in servers:
            hits.append(key)
    return sorted(hits)



def approve_project_binding(project_root: str, server: str = "tokensave"
                            ) -> "tuple[bool, str]":
    """Record approval for `server` in the project's own settings.local.json.

    Writes exactly what Claude Code's own approval prompt writes, in the file
    Claude Code writes it to — verified end to end on 2026-08-25, where a
    project approved this way served its own graph while the pin pointed
    elsewhere. Chosen over `~/.claude.json` deliberately: that layer gets
    migrated out from under you, and it is keyed by directory spelling, so an
    approval recorded under one spelling is invisible to a session launched
    with another. A project file has neither problem.

    Refuses rather than clobbers when the file exists and will not parse: the
    file carries the user's own permissions, and overwriting it to add one
    approval would be a far worse outcome than not approving.

    Returns `(ok, detail)`. Never raises.
    """
    if not project_root or not os.path.isdir(project_root):
        return False, "No such project directory: %s" % project_root

    folder = os.path.join(project_root, ".claude")
    path = os.path.join(folder, "settings.local.json")
    data: dict = {}
    backup = ""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return False, ("%s exists but could not be read (%s). Fix it by "
                           "hand — refusing to overwrite settings that may "
                           "hold your own permissions." % (path, exc))
        if not isinstance(data, dict):
            return False, "%s is not a JSON object; refusing to overwrite." % path
        try:
            backup = "%s.backup.%d" % (path, int(time.time() * 1000))
            shutil.copy2(path, backup)
        except OSError as exc:
            return False, "Could not back up %s: %s" % (path, exc)

    enabled = data.get("enabledMcpjsonServers")
    enabled = list(enabled) if isinstance(enabled, list) else []
    disabled = data.get("disabledMcpjsonServers")
    disabled = list(disabled) if isinstance(disabled, list) else []

    notes = []
    if server in enabled and server not in disabled:
        return False, "%s is already approved in %s" % (server, path)
    if server not in enabled:
        enabled.append(server)
    if server in disabled:
        # A rejection outranks the approval, so leaving it would write a file
        # that says yes and behaves like no.
        disabled = [s for s in disabled if s != server]
        notes.append("removed it from disabledMcpjsonServers")
        data["disabledMcpjsonServers"] = disabled
    data["enabledMcpjsonServers"] = enabled

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return False, "Could not create %s: %s" % (folder, exc)
    ok, err = _write_json_atomic(path, data)
    if not ok:
        return False, err

    detail = "Approved %s in %s" % (server, path)
    if notes:
        detail += " (%s)" % "; ".join(notes)
    if backup:
        detail += "\nBackup: %s" % os.path.basename(backup)
    return True, detail



# ── composing the file verdict with what `~/.claude.json` proves ──────────

#: The `.mcp.json` is correct and something OUTSIDE it blocks the binding.
#: These rows must not offer Apply: rewriting a file that already says the
#: right thing is a no-op dressed up as a fix, and it would leave the user
#: clicking a button that reports success while nothing changes.
#:
#: ``project_desktop_shadowed`` is the runtime member of this set, and the
#: only one no config file can reveal: Claude Desktop defines its own
#: ``tokensave`` in ``claude_desktop_config.json``, which ``claude mcp get``
#: never reads. See :mod:`helpers.mcp_shadow`.
#:
#: ``project_untrusted`` is the strongest member: Claude Code does not load a
#: project's ``.mcp.json`` in a folder it has not been trusted in, and trust
#: is granted interactively and cannot be written to a file at all. Offering
#: any button here would be offering one that cannot work.
ADVISORY_STATES = frozenset({
    "project_unapproved", "project_rejected", "project_key_ambiguous",
    "project_local_shadow", "project_shadowed", "project_desktop_shadowed",
    "project_untrusted",
})



def annotate_project_binding(info: dict, project_root: str,
                             server: str = "tokensave",
                             claude_json_path: str = "",
                             projects: "dict | None" = None) -> dict:
    """Downgrade a file-level "ok" using what `~/.claude.json` proves.

    `_classify_mcp_entry` reads `.mcp.json` and nothing else, so its "ok" means
    "this file says the right thing" — never "this is the server Claude Code
    runs". Presenting the first as the second is the specific failure this
    exists to stop: ten rows of "bound to this project" while every session was
    being answered by the user-scoped entry.

    Only ever downgrades. A verdict that is not "ok" already names a defect in
    the file itself, and that defect is what the user should fix first.
    """
    if info.get("state") != "ok":
        return info
    if projects is None:
        projects = read_claude_projects(claude_json_path)

    shadow = local_scope_shadow(project_root, server, projects=projects)
    if shadow:
        return {**info, "state": "project_local_shadow",
                "label": "\u26a0 overridden by a local-scoped entry",
                "issue": ("This file is correct, but %s also defines a "
                          "LOCAL-scoped `%s` for this project, and local scope "
                          "outranks project scope. Editing .mcp.json will not "
                          "change which server runs \u2014 remove the local "
                          "entry with `claude mcp remove %s -s local`."
                          % (", ".join(shadow), server, server))}

    got = mcpjson_approval(project_root, server, projects=projects)
    if got.state == APPROVAL_PENDING:
        return {**info, "state": "project_unapproved",
                "label": "\u26a0 written, not yet approved",
                "issue": ("This file is correct, but Claude Code has not "
                          "approved it, so the server is not in the running at "
                          "all and sessions here fall back to the user-scoped "
                          "entry. Run `claude` once in this project and approve "
                          "the .mcp.json server when prompted.")}
    if got.state == APPROVAL_REJECTED:
        return {**info, "state": "project_rejected",
                "label": "\u26a0 written, but rejected in Claude Code",
                "issue": ("This file is correct, but `%s` is listed in this "
                          "project's disabledMcpjsonServers, so Claude Code "
                          "will not load it. Re-approve it from a `claude` "
                          "session in this project." % server)}
    if got.state == APPROVAL_AMBIGUOUS:
        return {**info, "state": "project_key_ambiguous",
                "label": "\u26a0 approval depends on how you launch",
                "issue": ("This project is recorded more than once in "
                          "~/.claude.json under different spellings of the "
                          "same path, and they disagree about approval (%s). "
                          "Which one applies depends on the directory spelling "
                          "the session was started with." % got.detail)}
    return info


# ── Blanket auto-approval (measured 2026-09-04) ──────────────────────────

#: The key the Claude Code extension documents in its bundled
#: `claude-code-settings.schema.json` as "Whether to automatically approve
#: all MCP servers in the project".
AUTO_APPROVE_KEY = "enableAllProjectMcpServers"


def blanket_auto_approve() -> "tuple[bool, str]":
    """Is every project's `.mcp.json` auto-approved on this machine?

    Returns ``(enabled, detail)``. Detection only — nothing here writes the
    setting, and the Manager deliberately does not offer to.

    **Measured rather than read off the schema**, under a disposable HOME so
    the real config was never touched, with a probe `.mcp.json` and
    `claude mcp list` as the oracle:

    * a scratch server reports ``Pending approval``;
    * with the key in the project's ``.claude/settings.local.json`` --
      **still Pending**;
    * with it in the project's ``.claude/settings.json`` -- **still Pending**;
    * with it in ``~/.claude/settings.json`` -- approval is bypassed and the
      server is actually dialled.

    Removing it returned the server to Pending and restoring it flipped it
    again, so that is causation and not drift.

    Two consequences the schema's wording does not convey. It says "in the
    project", but the setting is **only honoured at user scope** -- writing it
    where a per-project tool would naturally put it does nothing at all, and
    does it silently. And it is **not per-project**: a second scratch project
    that had never been opened was auto-approved by the same flag, so this
    approves every MCP server in every repository on the machine, including
    ones cloned tomorrow.

    That makes it a security posture, not a convenience toggle, which is why
    this reports it and stops.
    """
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return False, ""
    if not isinstance(raw, dict) or not raw.get(AUTO_APPROVE_KEY):
        return False, ""
    return True, path
