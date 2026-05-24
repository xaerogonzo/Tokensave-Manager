"""ManagerConfig — the runtime-mutable settings store.

App holds one instance; future controllers and dialogs receive it via
`__init__(cfg: ManagerConfig)` and read live values through the property
getters (`cfg.git_exe`, `cfg.tokensave_exe`, etc.). When the user saves
Settings, `App._on_settings_saved` mutates `cfg.raw` in place and calls
`cfg.refresh_derived()` — all holders of the instance see the new values
without anyone having to re-bind a global.

Round 4 Phase A locks in two rules from `docs/plans/...`:

  * **Rule 3 — no caching of derived values in caller __init__.**
    Read `self._cfg.git_exe` at execution time, never snapshot it at
    construction. Cached snapshots become stale after a Settings save.

  * **Rule 5 — derived fields are read-only `@property`.** Direct
    assignment (`cfg.git_exe = "..."`) raises AttributeError. The ONLY
    supported mutation path is:
        ``cfg.raw.update(new_values); cfg.save(); cfg.refresh_derived()``

The class is dataclass-style so callers can construct one cheaply in
tests with `ManagerConfig(raw={"git_exe": "/usr/bin/git", ...})`, but
real production usage goes through `ManagerConfig.load()` which reads
+ migrates `manager-config.json` from disk.

This module imports from `helpers.detection` and `helpers.config` *lazily*
inside `refresh_derived()` and `load()` / `save()` — never at module
load — so `state.py` stays free of circular-import risk. Other modules
can `from state import ManagerConfig` for `TYPE_CHECKING` type hints
without dragging the helper graph along with them.
"""

from __future__ import annotations

import dataclasses
import os


@dataclasses.dataclass
class ManagerConfig:
    """Runtime-mutable settings, backed by a JSON dict on disk.

    The only writable surface is `self.raw` (the underlying dict).
    All derived fields are read-only `@property` getters that read from
    `self.raw` plus a small cache for fields that require an expensive
    fallback (subprocess shells for `_detect_git` / `_detect_codegraph`).
    """

    raw: dict

    # ── Direct dict-lookup properties (cheap, no caching needed) ──────────

    @property
    def tokensave_exe(self) -> str:
        """Path to tokensave.exe (empty string if not configured)."""
        return self.raw.get("tokensave_exe", "")

    @property
    def template_dir(self) -> str:
        """Folder holding template files (BASIC_INSTRUCTIONS, baseline gitignore, etc.).

        Falls back to `<repo>/templates` when not explicitly configured.
        """
        from constants import _BASE_DIR
        return self.raw.get("template_dir", "") or os.path.join(_BASE_DIR, "templates")

    @property
    def search_roots(self) -> list:
        """List of search-root entries (each entry is str or {"path","label"})."""
        return self.raw.get("search_roots", [])

    @property
    def basic_instructions_template(self) -> str:
        """Absolute path to the BASIC_INSTRUCTIONS.md template (derives from template_dir)."""
        return os.path.join(self.template_dir, "claude-md-template.md")

    @property
    def baseline_include_line(self) -> str:
        """The `@<path>\\project-baseline.md` include line written into BASIC_INSTRUCTIONS.md."""
        return f"@{self.template_dir}\\project-baseline.md"

    # ── Cached properties (cache values from refresh_derived) ─────────────

    @property
    def git_exe(self) -> str:
        """Absolute path to git.exe (or bare 'git' fallback)."""
        return self._cached_git_exe

    @property
    def codegraph_exe(self) -> str:
        """Absolute path to codegraph CLI (empty string if not installed)."""
        return self._cached_codegraph_exe

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def __post_init__(self):
        # Populate the cached fields once so the property accessors are usable
        # immediately after construction.
        self.refresh_derived()

    def refresh_derived(self) -> None:
        """Recompute cached derived fields from self.raw.

        Called after disk reload (in `load`) and after any code path that
        mutates `self.raw` (most notably `App._on_settings_saved`).
        """
        # Lazy import — keeps state.py at the bottom of the import graph.
        from helpers.detection import _detect_git, _detect_codegraph
        self._cached_git_exe        = self.raw.get("git_exe")        or _detect_git()
        self._cached_codegraph_exe  = self.raw.get("codegraph_exe")  or _detect_codegraph()

    # ── Disk I/O ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "ManagerConfig":
        """Read + migrate manager-config.json from disk, return a fresh instance."""
        # Lazy import — same isolation rationale as refresh_derived.
        from helpers.config import _load_config, _migrate_config
        return cls(raw=_migrate_config(_load_config()))

    def save(self) -> None:
        """Persist `self.raw` back to manager-config.json (no derived-field refresh)."""
        from helpers.config import _save_config
        _save_config(self.raw)
