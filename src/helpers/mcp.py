"""MCP configuration logic — re-export facade.

Roadmap-16 god-file split: 1590 lines became six family modules. This
facade re-exports **every** name, private ones included, because 20 modules
import from here and several take privates directly
(`dialogs/mcp_config.py` alone pulls seven). A public-only facade would have
turned a mechanical split into a 20-file change.

The dependency direction is one-way and must stay that way: this facade
imports the family modules, and no family module imports this facade.
`helpers/mcp_shadow.py` and `helpers/mcp_desktop.py` therefore import
`helpers.mcp_paths` directly. `tests/test_graph_trust.py`-style guards in
`tests/test_mcp_split.py` fail if either rule is broken.

New code may import from a family module directly; nothing has to.
"""

from __future__ import annotations

from helpers.mcp_paths import (
    _resolve_desktop_cfg_path,
    _mcp_desktop_cfg_path,
    _mcp_code_cfg_path,
    _mcp_configs,
    _wrapper_path,
    PROJECT_PATH_ARG,
    GITIGNORE_PROJECT_MCP_KEY,
    USER_SCOPE_RETIRED_KEY,
    DESKTOP_SCOPE_RETIRED_KEY,
    _project_mcp_path,
    _same_project,
    _write_json_atomic,
    _DRIVE_RE,
    _claude_json_path,
)
from helpers.mcp_projects import (
    normalize_project_key,
    read_claude_projects,
    matching_project_keys,
    _SESSION_FIELDS,
    _has_session,
    canonical_launch_dir,
    duplicate_project_keys,
)
from helpers.mcp_classify import (
    _canonical_mcp_entry,
    _canonical_project_entry,
    _McpCtx,
    _chk_bundled_wrapper,
    _chk_python_wrapper,
    _chk_project_scoped,
    _chk_direct_serve,
    _MCP_CMD_CHECKERS,
    _retired_absence,
    _classify_mcp_entry,
    _apply_mcp_fix,
    remove_mcp_entry,
)
from helpers.mcp_approval import (
    stale_duplicate_keys,
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    APPROVAL_AMBIGUOUS,
    APPROVAL_UNKNOWN,
    McpJsonApproval,
    _settings_approval,
    _entry_approval,
    local_settings_approval,
    mcpjson_approval,
    local_scope_shadow,
    approve_project_binding,
    ADVISORY_STATES,
    annotate_project_binding,
)
from helpers.mcp_scope import (
    SCOPE_PROJECT,
    SCOPE_USER,
    SCOPE_LOCAL,
    SCOPE_ABSENT,
    SCOPE_UNKNOWN,
    EffectiveScope,
    _parse_mcp_get,
    describe_effective,
    effective_scope,
    _CLAUDE_JSON_ACTIVE_SECS,
    claude_code_active,
    _is_claude_running,
)
from helpers.mcp_agents import (
    _claude_code_mcp_has_codegraph,
    _CODEGRAPH_AGENTS,
    _codegraph_agent_destination_path,
    _codegraph_agent_installed,
    _TOKENSAVE_AGENTS,
    _TOKENSAVE_AGENT_PATHS,
    _tokensave_agent_path_candidates,
    _tokensave_agent_destination_path,
    _tokensave_agent_installed,
    _tokensave_agent_wired,
)

__all__ = [
    "_resolve_desktop_cfg_path",
    "_mcp_desktop_cfg_path",
    "_mcp_code_cfg_path",
    "_mcp_configs",
    "_wrapper_path",
    "_canonical_mcp_entry",
    "PROJECT_PATH_ARG",
    "GITIGNORE_PROJECT_MCP_KEY",
    "USER_SCOPE_RETIRED_KEY",
    "DESKTOP_SCOPE_RETIRED_KEY",
    "_project_mcp_path",
    "_canonical_project_entry",
    "_same_project",
    "_McpCtx",
    "_chk_bundled_wrapper",
    "_chk_python_wrapper",
    "_chk_project_scoped",
    "_chk_direct_serve",
    "_MCP_CMD_CHECKERS",
    "_retired_absence",
    "_classify_mcp_entry",
    "_apply_mcp_fix",
    "_write_json_atomic",
    "remove_mcp_entry",
    "_DRIVE_RE",
    "_claude_json_path",
    "normalize_project_key",
    "read_claude_projects",
    "matching_project_keys",
    "_SESSION_FIELDS",
    "_has_session",
    "stale_duplicate_keys",
    "canonical_launch_dir",
    "duplicate_project_keys",
    "APPROVAL_APPROVED",
    "APPROVAL_PENDING",
    "APPROVAL_REJECTED",
    "APPROVAL_AMBIGUOUS",
    "APPROVAL_UNKNOWN",
    "McpJsonApproval",
    "_settings_approval",
    "_entry_approval",
    "local_settings_approval",
    "mcpjson_approval",
    "local_scope_shadow",
    "approve_project_binding",
    "ADVISORY_STATES",
    "annotate_project_binding",
    "SCOPE_PROJECT",
    "SCOPE_USER",
    "SCOPE_LOCAL",
    "SCOPE_ABSENT",
    "SCOPE_UNKNOWN",
    "EffectiveScope",
    "_parse_mcp_get",
    "describe_effective",
    "effective_scope",
    "_CLAUDE_JSON_ACTIVE_SECS",
    "claude_code_active",
    "_is_claude_running",
    "_claude_code_mcp_has_codegraph",
    "_CODEGRAPH_AGENTS",
    "_codegraph_agent_destination_path",
    "_codegraph_agent_installed",
    "_TOKENSAVE_AGENTS",
    "_TOKENSAVE_AGENT_PATHS",
    "_tokensave_agent_path_candidates",
    "_tokensave_agent_destination_path",
    "_tokensave_agent_installed",
    "_tokensave_agent_wired",
]
