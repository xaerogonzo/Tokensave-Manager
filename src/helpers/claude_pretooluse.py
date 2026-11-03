"""Undo tokensave doctor's PreToolUse rewrite when Cursor is also installed.

``tokensave doctor`` still treats a quoted
``"D:/path with spaces/tokensave.exe" hook-pre-tool-use`` as the legacy
single-string shape from upstream #81 and rewrites it to ``command`` +
``args``. Claude Code runs that; Cursor's agent reads only ``command`` and
then every shell call fails (measured 2026-10-31). Upstream ``doctor`` has
no ``--no-fix`` flag (tokensave 7.16.0), so the Manager puts the quoted
form back after its own Doctor run, and only when a Cursor CLI is
configured. Stop and UserPromptSubmit stay as ``args``: those were never
measured to break anything.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time

from helpers.agent_integration import SHAPE_ARGS, hook_shape
from helpers.mcp_paths import _write_json_atomic

EVENT = "PreToolUse"


@dataclasses.dataclass(frozen=True)
class RestoreResult:
    ok: bool
    changed: bool = False
    backup: str = ""
    error: str = ""
    detail: str = ""


def quoted_command(exe: str, extra: list) -> str:
    """One string Cursor and git-bash both keep as a single program path."""
    path = (exe or "").strip().strip('"').strip("'")
    args = " ".join(str(part) for part in extra if str(part).strip())
    if args:
        return '"%s" %s' % (path, args)
    return '"%s"' % path


def _tokensave_pretooluse_handlers(settings: dict):
    groups = ((settings.get("hooks") or {}).get(EVENT) or [])
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if (isinstance(entry, dict)
                    and entry.get("type") == "command"
                    and "tokensave" in str(entry.get("command") or "").lower()):
                yield entry


def _rewrite_args_form(entry: dict) -> bool:
    """True when this handler was the doctor-written command+args shape."""
    if hook_shape(entry) != SHAPE_ARGS:
        return False
    extra = list(entry.get("args") or [])
    if not extra:
        return False
    entry["command"] = quoted_command(str(entry.get("command") or ""), extra)
    entry.pop("args", None)
    return True


def restore_quoted_pretooluse(settings_path: str) -> RestoreResult:
    """Rewrite PreToolUse tokensave handlers from command+args to one string.

    Missing file: nothing to do. Unreadable file: refuse, do not write.
    """
    if not settings_path or not os.path.exists(settings_path):
        return RestoreResult(ok=True, detail="no settings file")
    try:
        with open(settings_path, encoding="utf-8-sig") as handle:
            settings = json.load(handle)
    except (OSError, ValueError) as exc:
        return RestoreResult(
            ok=False,
            error="settings unreadable (%s); not writing over them" % exc)
    if not isinstance(settings, dict):
        return RestoreResult(ok=False, error="settings JSON is not an object")

    rewritten = sum(1 for entry in _tokensave_pretooluse_handlers(settings)
                    if _rewrite_args_form(entry))
    if not rewritten:
        return RestoreResult(ok=True, detail="already quoted or no PreToolUse")

    backup = "%s.backup.%d" % (settings_path, int(time.time() * 1000))
    try:
        shutil.copy2(settings_path, backup)
    except OSError as exc:
        return RestoreResult(ok=False, error="could not back up: %s" % exc)
    ok, err = _write_json_atomic(settings_path, settings)
    if not ok:
        return RestoreResult(ok=False, backup=backup, error=err)
    return RestoreResult(
        ok=True, changed=True, backup=backup,
        detail="quoted %d PreToolUse handler(s)" % rewritten)


def restore_after_doctor(cfg) -> RestoreResult:
    """No-op unless this machine has a Cursor CLI configured."""
    if cfg is None:
        return RestoreResult(ok=True, detail="no config")
    from helpers.agent_cli import AGENT_CLIS
    spec = AGENT_CLIS.get("cursor")
    exe = str(getattr(cfg, spec.config_key_exe, "") or "") if spec else ""
    if not exe:
        return RestoreResult(ok=True, detail="Cursor CLI not configured")
    from helpers.claude_hooks import default_settings_path
    return restore_quoted_pretooluse(default_settings_path())
