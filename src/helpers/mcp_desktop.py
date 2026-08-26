"""mcp_desktop — retire the ``tokensave`` entry Claude Desktop defines globally.

## What this migration is, and what it is not

Claude Desktop registers its own ``tokensave`` MCP server pointing at the
manager's wrapper. The wrapper picks ONE project (from the global pin) and
Desktop spawns it app-level, so every Desktop-hosted Claude Code session
inherits that single server no matter which repository the session is in.
Claude Code dedupes MCP servers by NAME, so this entry wins over each
project's own correct ``.mcp.json`` binding. Measured 2026-08-26: the project
server was running and correct at the same moment its session was being
answered from a different repo's index.

Removing the entry is therefore the fix for multi-project isolation — **and it
is also a deliberate feature removal**, because Claude Desktop chat then has
no tokensave at all. That is why this is its own migration with its own
confirmation, and never a side effect of binding a project. Binding a project
must never imply retiring this entry.

## Four things that would each have made it unsafe

1. **Desktop rewrites its own config from memory every 1-2 minutes.** Writing
   while it runs is silently reverted, which reads as a manager bug. Hence the
   hard gate in :func:`desktop_app_running` — and it must tell Desktop from
   Claude Code, which is *also* ``claude.exe``. Only the executable path
   separates them, so a name-based check would block the migration forever
   (the user is running Claude Code to perform it).
2. **The UWP/non-UWP path asymmetry.** A Store-installed Desktop reads
   ``%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\``;
   a non-UWP process opening ``%APPDATA%\\Claude\\`` sees a *different physical
   file with the same path string*. Both exist. The manager is non-UWP, so
   editing only what it can see leaves Desktop's copy untouched — which is
   exactly how an earlier Apply wrote canonical content nobody ever read.
3. **Blanket removal would hit unrelated installs.** Beta/alternate packages
   are separate Desktop installations, not extra views of the active one.
   :func:`change_set` names both lists so the confirmation can say what will
   and will not be touched.
4. **The file can change between preview and click.** :func:`retire` re-reads
   and re-digests every target immediately before writing, and refuses if
   anything moved.

## The flag records intent, not absence

``DESKTOP_SCOPE_RETIRED_KEY`` means "the user ran this migration", not "no
entry exists". Those differ: a user may never have had the entry, may have
removed it by hand, or may have had it recreated by a Desktop update. Only the
last is a regression worth reporting, and only the recorded intent can tell it
from the others. See :func:`lifecycle_state`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

from helpers.mcp import (_resolve_desktop_cfg_path, _write_json_atomic,
                         _mcp_desktop_cfg_path)
#: Set when the Desktop-scoped ``tokensave`` entry is retired through this
#: migration, so its later ABSENCE reads as a completed decision. Mirrors
#: ``USER_SCOPE_RETIRED_KEY``, and defined in :mod:`helpers.mcp` beside it
#: because `_classify_mcp_entry` reads it too — defining it here and importing
#: it back would be a cycle. Re-exported so callers can keep asking the module
#: that owns the migration.
from helpers.mcp import DESKTOP_SCOPE_RETIRED_KEY

#: The verbatim entry that was removed, plus the digests written, so Undo can
#: restore exactly what was there instead of a reconstructed canonical entry.
DESKTOP_RETIRED_RECORD_KEY = "mcp_desktop_retired_record"

#: Lifecycle states -- see :func:`lifecycle_state`.
LIFECYCLE_ABSENT = "absent"
LIFECYCLE_PRESENT = "present"
LIFECYCLE_RETIRED = "retired"
LIFECYCLE_RETURNED = "returned"

#: A ``claude.exe`` under this path fragment is Claude Code, not Desktop.
_CODE_PATH_MARKER = "claude-code"
#: Path fragments that positively identify the Desktop app's own executable.
_DESKTOP_PATH_MARKERS = ("windowsapps", os.path.join("programs", "claude"))


# ── discovery ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DesktopConfig:
    """One physical ``claude_desktop_config.json`` on this machine."""

    path: str
    install_id: str = ""
    #: Does this file belong to the Desktop installation currently in use?
    is_active: bool = False
    exists: bool = False
    #: The verbatim ``mcpServers.<server>`` object, or None.
    entry: "dict | None" = None
    #: sha256 of the file bytes, for the stale-state guard.
    digest: str = ""
    error: str = ""

    @property
    def has_entry(self) -> bool:
        return self.entry is not None


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _install_id(path: str) -> str:
    """Name the installation a physical config belongs to.

    UWP packages are identified by their package directory, which is what
    distinguishes Stable from Beta. Everything else is the traditional install.

    Only DIRECTORY components are considered. The file itself is called
    ``claude_desktop_config.json``, so scanning the whole path matched the
    basename and labelled the traditional config as its own UWP package —
    which then failed the active-installation test and quietly dropped the
    ``%APPDATA%`` view out of the change set, leaving the exact half-applied
    migration this module is supposed to prevent.
    """
    parts = os.path.normpath(path).split(os.sep)[:-1]
    for part in parts:
        if part.lower().startswith("claude_"):
            return "uwp:%s" % part
    return "traditional"


def _read_entry(path: str, server: str) -> "tuple[dict | None, str]":
    """``(entry, error)`` for one config file. An absent entry is not an error."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        return None, "could not read: %s" % exc
    except json.JSONDecodeError as exc:
        return None, "not valid JSON: %s" % exc
    if not isinstance(data, dict):
        return None, "config root is not a JSON object"
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None, ""
    entry = servers.get(server)
    return (entry if isinstance(entry, dict) else None), ""


def _candidate_paths() -> list:
    """Every physical Desktop config path worth looking at."""
    import glob as _glob

    out = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        try:
            out.extend(sorted(_glob.glob(os.path.join(
                local, "Packages", "Claude_*", "LocalCache", "Roaming",
                "Claude", "claude_desktop_config.json"))))
        except OSError:
            pass
    traditional = os.path.join(os.environ.get("APPDATA", ""), "Claude",
                               "claude_desktop_config.json")
    if traditional not in out:
        out.append(traditional)
    return out


def discover_desktop_configs(server: str = "tokensave") -> list:
    """Every candidate Desktop config, with its entry and active-install flag.

    "Active" is decided by :func:`helpers.mcp._resolve_desktop_cfg_path`, which
    already encodes the UWP-preference rule the rest of the manager uses — this
    module must not invent a second answer to the same question.

    The traditional ``%APPDATA%`` file is ALSO marked active when the resolved
    config is a UWP one, because on such a machine the two are one logical
    configuration seen from two process contexts. That is the case this whole
    module exists for, so it is asserted rather than inferred per call.
    """
    active_path = ""
    try:
        active_path = os.path.normcase(_resolve_desktop_cfg_path())
    except OSError:
        active_path = ""
    active_is_uwp = "packages" in active_path and "claude_" in active_path

    out = []
    for path in _candidate_paths():
        exists = os.path.isfile(path)
        entry, error = _read_entry(path, server) if exists else (None, "")
        install = _install_id(path)
        is_active = os.path.normcase(path) == active_path or (
            active_is_uwp and install == "traditional" and exists)
        out.append(DesktopConfig(
            path=path, install_id=install, is_active=is_active,
            exists=exists, entry=entry,
            digest=_digest(path) if exists else "", error=error))
    return out


def change_set(configs: list, server: str = "tokensave") -> "tuple[list, list]":
    """``(will_change, will_not_change)`` for the confirmation dialog.

    Only files that belong to the ACTIVE installation and actually carry the
    entry are changed. A Beta package's config is listed in the second group
    with its reason, so the dialog can show what it is deliberately leaving
    alone rather than staying silent about files it found.
    """
    will, wont = [], []
    for cfg in configs or []:
        if cfg.is_active and cfg.has_entry:
            will.append(cfg)
        elif cfg.exists and cfg.has_entry:
            wont.append(cfg)
    return will, wont


# ── the hard gate ──────────────────────────────────────────────────────────

def _split_claude_processes(procs: list) -> "tuple[list, list]":
    """``(desktop, unrecognised)`` from a list of ``claude``-ish processes.

    Claude Code is filtered out by path, not by name: both ship as
    ``claude.exe``, and a name-based rule would report Claude Code sessions as
    Claude Desktop — permanently blocking a migration the user runs Claude
    Code to perform.
    """
    desktop, unknown = [], []
    for proc in procs:
        exe = (proc.get("exe") or "").lower()
        if not exe.endswith("claude.exe") or _CODE_PATH_MARKER in exe:
            continue
        target = desktop if any(m in exe for m in _DESKTOP_PATH_MARKERS) \
            else unknown
        target.append(proc)
    return desktop, unknown


def desktop_app_running() -> "tuple[bool | None, str]":
    """Is Claude **Desktop** running? ``(True | False | None, detail)``.

    ``None`` means the question could not be answered, which is reported as
    such rather than collapsed into either answer: this gates a write that
    Desktop would silently revert, so "we could not tell" must not read as
    "safe to proceed", and must not read as a permanent block either.

    Claude Code ships as ``claude.exe`` too. Distinguishing them by name is
    what would make this gate useless — the user runs Claude Code to perform
    the migration — so the executable PATH decides, and an unrecognised
    ``claude.exe`` counts as Desktop because blocking is the safe error.
    """
    if sys.platform != "win32":
        return False, "not Windows -- Claude Desktop does not run here"
    try:
        from helpers.tokensave_daemon import _enumerate_processes
        procs = _enumerate_processes("claude", strict=True)
    except Exception as exc:                      # noqa: BLE001 - fail-honest
        # Named, not generalised. "no process information was returned" was
        # what this said when PowerShell simply was not on the manager's PATH,
        # and it sent the reader looking for a problem with Claude Desktop
        # instead of with the question we had failed to ask.
        return None, "could not enumerate processes -- %s" % exc

    desktop, unknown = _split_claude_processes(procs)
    if not (desktop or unknown):
        # A successful enumeration that found nothing IS an answer. Only a
        # failed one is unknown, and that path returned above.
        return False, "Claude Desktop is not running"

    if desktop:
        return True, ("Claude Desktop is running (PID %s)"
                      % desktop[0].get("pid", "?"))
    if unknown:
        return True, ("a claude.exe is running from an unrecognised location "
                      "(%s) -- treated as Claude Desktop"
                      % (unknown[0].get("exe") or "?"))
    return False, "Claude Desktop is not running"


# ── retirement ─────────────────────────────────────────────────────────────

@dataclass
class RetireResult:
    """Outcome of one retirement attempt."""

    ok: bool = False
    changed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    entry: "dict | None" = None
    record: dict = field(default_factory=dict)
    detail: str = ""


def _strip_entry(path: str, server: str) -> "tuple[bool, str, dict | None]":
    """Remove one server from one config. ``(ok, detail, removed_entry)``."""
    try:
        backup = path + ".backup." + str(int(time.time() * 1000))
        shutil.copy2(path, backup)
    except OSError as exc:
        return False, "could not write backup: %s" % exc, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return False, "could not parse: %s" % exc, None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server not in servers:
        return False, "no '%s' entry" % server, None
    removed = servers.pop(server)
    ok, err = _write_json_atomic(path, data)
    if not ok:
        return False, err, None
    return True, os.path.basename(backup), removed


def retire(configs: list, server: str = "tokensave") -> RetireResult:
    """Remove *server* from the active installation's config file(s).

    Every target is re-read and re-digested immediately before writing. If any
    changed since discovery the whole operation is refused rather than
    partially applied — Desktop rewrites this file on its own schedule, and a
    half-applied migration across two physical views of one configuration is
    worse than none.
    """
    will, _ = change_set(configs, server)
    if not will:
        return RetireResult(detail="No active Claude Desktop config carries a "
                                   "'%s' entry -- nothing to retire." % server)

    fresh = {c.path: c for c in discover_desktop_configs(server)}
    for cfg in will:
        now = fresh.get(cfg.path)
        if now is None or not now.exists:
            return RetireResult(
                detail="%s disappeared since this preview. Re-scan before "
                       "retiring." % cfg.path)
        if now.digest != cfg.digest:
            return RetireResult(
                detail="Claude Desktop configuration changed since this "
                       "preview (%s). Re-scan before retiring."
                       % os.path.basename(cfg.path))

    result = RetireResult(entry=will[0].entry)
    backups = {}
    for cfg in will:
        ok, detail, removed = _strip_entry(cfg.path, server)
        if ok:
            result.changed.append(cfg.path)
            backups[cfg.path] = detail
            if result.entry is None:
                result.entry = removed
        else:
            result.failed.append((cfg.path, detail))

    result.ok = bool(result.changed) and not result.failed
    after = {c.path: c.digest for c in discover_desktop_configs(server)}
    result.record = {
        "entry": result.entry,
        "files": {p: after.get(p, "") for p in result.changed},
        "backups": backups,
        "retired_at": time.time(),
        "server": server,
    }
    result.detail = _retire_detail(result, server)
    return result


def _retire_detail(result: RetireResult, server: str) -> str:
    lines = []
    for path in result.changed:
        lines.append("Removed '%s' from %s" % (server, path))
    for path, why in result.failed:
        lines.append("FAILED %s: %s" % (path, why))
    if result.changed:
        lines.append("")
        lines.append("Claude Desktop must be restarted before this takes "
                     "effect: a running server resolved its project at "
                     "startup and keeps serving it until then.")
    return "\n".join(lines)


def _restore_one(path: str, expected_digest: str, entry: dict,
                 server: str) -> "tuple[bool, str]":
    """Put *entry* back into one config, unless that file has moved on.

    The digest check is what keeps Undo honest: it restores what the manager
    retired, and declines a file somebody has edited since rather than
    overwriting their change with a stale snapshot.
    """
    if not os.path.isfile(path):
        return False, "file no longer exists"
    if expected_digest and _digest(path) != expected_digest:
        return False, "changed since retirement -- left alone"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return False, "could not parse: %s" % exc
    if not isinstance(data, dict):
        return False, "config root is not a JSON object"
    data.setdefault("mcpServers", {})[server] = entry
    ok, err = _write_json_atomic(path, data)
    return (True, "") if ok else (False, err)


def restore(record: dict, server: str = "tokensave") -> RetireResult:
    """Put back exactly what :func:`retire` removed.

    Guarded both ways. The entry restored is the verbatim object from the
    record, because the user may have had custom ``command``/``args`` that a
    reconstructed canonical entry would silently replace. And a file that has
    changed since the retirement is left alone rather than overwritten, so
    Undo restores what the manager retired without discarding unrelated edits
    made afterwards.
    """
    entry = (record or {}).get("entry")
    files = (record or {}).get("files") or {}
    if not isinstance(entry, dict) or not files:
        return RetireResult(detail="No recorded Desktop entry to restore.")

    result = RetireResult(entry=entry)
    for path, expected_digest in files.items():
        ok, why = _restore_one(path, expected_digest, entry, server)
        if ok:
            result.changed.append(path)
        else:
            result.failed.append((path, why))

    result.ok = bool(result.changed) and not result.failed
    result.detail = "\n".join(
        ["Restored '%s' to %s" % (server, p) for p in result.changed] +
        ["FAILED %s: %s" % (p, why) for p, why in result.failed])
    return result


# ── lifecycle ──────────────────────────────────────────────────────────────

def is_retired(raw: "dict | None") -> bool:
    """Did the user run this migration?

    Reads the recorded INTENT, never the absence of an entry. Every caller
    wants this question rather than the raw key, and routing them through one
    accessor keeps "was it retired" and "is it missing" from being conflated
    by whichever surface asks next.
    """
    return bool((raw or {}).get(DESKTOP_SCOPE_RETIRED_KEY))


def lifecycle_state(entry_present: bool, retired_flag: bool) -> str:
    """Where this machine sits in the migration, from intent plus fact.

    The interesting cell is RETURNED: the entry is back although the user
    retired it, which a Desktop update or a hand edit can do. Reporting that
    is the difference between a durable migration and a cleanup that silently
    undoes itself.
    """
    if entry_present:
        return LIFECYCLE_RETURNED if retired_flag else LIFECYCLE_PRESENT
    return LIFECYCLE_RETIRED if retired_flag else LIFECYCLE_ABSENT


def desktop_entry_present(server: str = "tokensave",
                          configs: "list | None" = None) -> bool:
    """Does the ACTIVE Desktop installation define *server*?

    Deliberately ignores inactive installations: a Beta package's leftover
    entry cannot shadow anything in the Desktop the user is actually running.
    """
    if configs is None:
        configs = discover_desktop_configs(server)
    return any(c.is_active and c.has_entry for c in configs)


def config_path() -> str:
    """The active Desktop config path, for messages that name one file."""
    return _mcp_desktop_cfg_path()
