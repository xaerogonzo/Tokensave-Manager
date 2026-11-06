"""Compile Agent Policy into one Cursor User Rule card.

Cursor 3.29.x injects "Only create commits when requested by the user" from
its agent runtime, not from Customize → Rules. That synthetic user rule has
beaten workspace AGENTS.md. This module renders a real User Rule that points
at the TOKENSAVE-MANAGED git blocks, and upserts it only when a local JSON
store is supplied or discovered.

WHAT IT WILL NOT DO. It never writes Cursor.exe, never opens state.vscdb
(chat blobs, not the rule list), and never parses a policy out of the card.
Missing or unknown store → skip + paste, and the baseline apply still stands.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time

from helpers.agent_rules import _read, _write_atomic
from helpers.instruction_policy import InstructionPolicy

RULE_TITLE = "TokenSave git policy"
CARD_ID = "tokensave-git-policy"
MARKER = "<!-- TOKENSAVE-MANAGED: cursor-git-policy -->"
STORE_ENV = "TOKENSAVE_CURSOR_USER_RULES"

STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_SKIPPED = "skipped"

_INSTALL_MARK = "\\programs\\cursor\\"


@dataclasses.dataclass(frozen=True)
class ApplyResult:
    status: str
    reason: str = ""
    path: str = ""
    paste: str = ""
    backup_path: str = ""
    changed: bool = False


def render_body(policy: InstructionPolicy) -> str:
    """The card body. Both ON and OFF produce text — an off policy is still policy."""
    if policy.agent_may_commit:
        mid = (
            "Cursor injects a built-in \"Only create commits when requested "
            "by the user\" instruction that is not a Customize → Rules card. "
            "This user rule is the explicit exception that generator allows: "
            "when AGENTS.md says you may commit"
            + (" and may push" if policy.agent_may_push else "")
            + ", do that when the work is complete. Do not wait for another ask."
        )
        close = (
            "Never force-push, skip hooks, or rewrite commits that have "
            "already been pushed."
        )
    else:
        mid = (
            "Do not create commits and do not push unless those blocks say "
            "otherwise."
        )
        close = (
            "Cursor's built-in \"only create commits when requested\" already "
            "matches this policy. Keep following AGENTS.md if the two ever "
            "disagree."
        )
    return (
        "%s\n"
        "For TokenSave-wired projects, git commit and push follow the "
        "TOKENSAVE-MANAGED blocks in AGENTS.md.\n"
        "\n"
        "%s\n"
        "\n"
        "%s"
        % (MARKER, mid, close)
    )


def render_paste(policy: InstructionPolicy) -> str:
    return "%s\n\n%s\n" % (RULE_TITLE, render_body(policy))


def log_line(result: ApplyResult) -> str:
    if result.status == STATUS_SKIPPED:
        extra = result.reason or "unknown reason"
        return "cursor user rule: skipped (%s)" % extra
    where = (" path=%s" % result.path) if result.path else ""
    return "cursor user rule: %s%s" % (result.status, where)


def discover_store(environ: dict | None = None) -> str:
    """Only an explicit override. We do not invent a file Cursor will not read."""
    env = os.environ if environ is None else environ
    return str(env.get(STORE_ENV) or "").strip()


def _is_cursor_install(path: str) -> bool:
    norm = os.path.normpath(path).replace("/", "\\").lower()
    padded = norm if norm.endswith("\\") else norm + "\\"
    return _INSTALL_MARK in padded


def _is_ours(row) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("title") == RULE_TITLE:
        return True
    return MARKER in str(row.get("content") or "")


def _parse_store(existing: "str | None") -> "tuple[dict | None, str]":
    if not (existing or "").strip():
        return {"version": 1, "rules": []}, ""
    try:
        data = json.loads(existing)
    except json.JSONDecodeError as exc:
        return None, "could not parse JSON: %s" % exc
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return None, "unexpected schema: missing rules list"
    return data, ""


def plan_upsert(existing: "str | None", policy: InstructionPolicy
                ) -> "tuple[str | None, bool, str]":
    """``(new_text_or_None, changed, skip_reason)``. None text means skip."""
    invalid = policy.validate()
    if invalid:
        return None, False, invalid
    data, err = _parse_store(existing)
    if data is None:
        return None, False, err
    body = render_body(policy)
    rules = data["rules"]
    for index, row in enumerate(rules):
        if not _is_ours(row):
            continue
        if row.get("content") == body and row.get("title") == RULE_TITLE:
            return existing or "", False, ""
        updated = dict(row)
        updated["title"] = RULE_TITLE
        updated["content"] = body
        updated["id"] = updated.get("id") or CARD_ID
        rules[index] = updated
        break
    else:
        rules.append({"id": CARD_ID, "title": RULE_TITLE, "content": body})
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return text, True, ""


def needs_write(policy: InstructionPolicy, store_path: str = "",
                environ: dict | None = None) -> bool:
    path = store_path or discover_store(environ)
    if not path or _is_cursor_install(path):
        return False
    existing = _read(path) if os.path.exists(path) else None
    new_text, changed, _skip = plan_upsert(existing, policy)
    return bool(new_text) and changed


def preview_lines(policy: InstructionPolicy, store_path: str = "",
                  environ: dict | None = None) -> list:
    path = store_path or discover_store(environ)
    lines = ["Cursor User Rule",
             "  %-24s %s" % ("title", RULE_TITLE)]
    if path:
        lines.append("  %-24s %s" % ("store", path))
        lines.append("  %-24s %s" % ("apply will", "upsert the card"))
    else:
        lines.append("  %-24s %s" % ("store", "(none — paste fallback)"))
        lines.append("  %-24s %s" % (
            "apply will", "skip; card text goes to the log"))
    return lines


def _backup(path: str, now_ms: "int | None") -> "tuple[str, str]":
    target = f"{path}.backup.{now_ms if now_ms is not None else int(time.time() * 1000)}"
    try:
        shutil.copy2(path, target)
        with open(path, "rb") as src, open(target, "rb") as dst:
            if src.read() != dst.read():
                return "", "the backup %s does not match the original" % target
    except OSError as exc:
        return "", "could not back up %s: %s" % (path, exc)
    return target, ""


def apply(policy: InstructionPolicy, store_path: str = "",
          dry_run: bool = False, backup_dir: str = "",
          environ: dict | None = None, now_ms: "int | None" = None
          ) -> ApplyResult:
    """Upsert the managed card, or skip with paste. ``backup_dir`` is unused;
    backups sit next to the store like ``cursor_hooks`` (path.backup.<ms>)."""
    del backup_dir
    paste = render_paste(policy)
    path = store_path or discover_store(environ)
    if not path:
        return ApplyResult(STATUS_SKIPPED,
                           reason="no writable titled-rule store",
                           paste=paste)
    if _is_cursor_install(path):
        return ApplyResult(STATUS_SKIPPED,
                           reason="refusing Cursor install path",
                           path=path, paste=paste)

    existing = _read(path) if os.path.exists(path) else None
    new_text, changed, skip = plan_upsert(existing, policy)
    if new_text is None:
        return ApplyResult(STATUS_SKIPPED, reason=skip, path=path, paste=paste)
    if not changed:
        return ApplyResult(STATUS_UNCHANGED, path=path, paste=paste)
    if dry_run:
        return ApplyResult(STATUS_UPDATED, path=path, paste=paste, changed=True)

    backup = ""
    if existing is not None:
        backup, err = _backup(path, now_ms)
        if err:
            return ApplyResult(STATUS_SKIPPED, reason=err, path=path,
                               paste=paste)
    ok, err = _write_atomic(path, new_text)
    if not ok:
        return ApplyResult(STATUS_SKIPPED, reason=err, path=path, paste=paste,
                           backup_path=backup)
    return ApplyResult(STATUS_UPDATED, path=path, paste=paste,
                       backup_path=backup, changed=True)
