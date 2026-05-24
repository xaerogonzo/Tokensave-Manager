"""Load / save / migrate the manager-config.json file on disk.

This module owns the raw JSON I/O. Higher-level callers (state.ManagerConfig
in particular) wrap these in a richer object that recomputes derived fields
when the disk file changes.

Migration is idempotent — re-running on already-migrated configs is a no-op.
Saves back to disk only if anything actually changed.
"""

from __future__ import annotations

import json
import os

from constants import _CONFIG_PATH


def _load_config() -> dict:
    """Return the manager-config.json contents as a dict, or {} if missing."""
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM if present
        return json.load(f)


def _save_config(cfg: dict) -> None:
    """Persist `cfg` to manager-config.json (overwrites whatever's there)."""
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _migrate_config(cfg: dict) -> dict:
    """Bump old config defaults to current ones, on first load after upgrade.

    Why this exists: tk Settings dialog uses ``setdefault`` which only fills
    MISSING keys. Users upgrading from v1.0.x have ``timeout_seconds: 12``
    AND ``max_diff_chars: 8000`` already present in their saved config —
    those keys exist, so setdefault no-ops, and the user keeps the old slow
    defaults until they edit the JSON by hand. This migration treats values
    AT-OR-BELOW the previous default as 'never explicitly chosen by the user'
    and bumps them to the current default. Users who deliberately set
    intermediate values (e.g. timeout=60, max_diff_chars=16000) keep those.

    Migration is idempotent: re-running on already-migrated configs is a no-op.
    Saves back to disk only if anything changed.
    """
    changed = False
    llm = cfg.get("commit_message_llm")
    if isinstance(llm, dict):
        # timeout_seconds: old default was 12, current is 90; bump anything <30
        if int(llm.get("timeout_seconds", 90)) < 30:
            llm["timeout_seconds"] = 90
            changed = True
        # max_diff_chars: old default was 8000, current is 24000; bump anything <16000
        if int(llm.get("max_diff_chars", 24000)) < 16000:
            llm["max_diff_chars"] = 24000
            changed = True
    # MCP-config skip list — added [Unreleased]. Empty list means "warn me
    # whenever any Claude MCP config drifts from the canonical wrapper-based
    # shape". Each entry is an absolute path to a config file the user has
    # told us to stop warning about.
    if "mcp_skip_warnings" not in cfg:
        cfg["mcp_skip_warnings"] = []
        changed = True
    if changed:
        _save_config(cfg)
    return cfg
