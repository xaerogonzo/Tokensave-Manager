"""GitPolicySection -- what the manager and your agents do to a git repo.

Moved out of `dialogs/settings.py` when Settings became a tab, with the
behaviour of every control unchanged. Four things live here because they
answer one question -- *what is allowed to touch git without you* -- even
though they act at three different layers:

  auto-commit after sync      the MANAGER commits
  .mcp.json in .gitignore     what a binding leaves behind in the repo
  pre-commit smoke tests      what runs before any commit
  Agent Policy...             what an AGENT may commit or push

THE PRE-COMMIT HOOK IS A SIDE EFFECT, NOT A SETTING, and it is deliberately
not part of `save_into`. `save_into` writes into a staging dict that the
controller may still discard; installing a file into `.git/hooks/` cannot be
staged or rolled back. So it runs as `apply_hook()` AFTER the configuration
has been committed, and its failure reports itself without undoing the save
-- unwinding your AI settings because a git hook could not be written would
be the more surprising outcome.

Agent Policy gets its own dialog rather than four more checkboxes here: those
toggles compile into `templates/project-baseline.md`, which every wired
project loads on every message, so they need a preview and an explicit apply
rather than a Save button shared with twenty unrelated paths.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import themed_checkbutton

if TYPE_CHECKING:
    from state import ManagerConfig


class GitPolicySection:
    """Auto-commit, gitignore binding, pre-commit hook, agent policy."""

    def __init__(self, host, body: tk.Frame, cfg: "ManagerConfig") -> None:
        self._host = host
        self._cfg = cfg
        self._build(body, cfg.raw)

    # ── Contract ─────────────────────────────────────────────────────────

    def save_into(self, raw: dict) -> bool:
        """Write the two persisted toggles. Always succeeds.

        The pre-commit hook is NOT written here -- see `apply_hook`.
        """
        from helpers.mcp import GITIGNORE_PROJECT_MCP_KEY

        raw["auto_commit_after_sync"] = self._var_autocommit.get()
        raw[GITIGNORE_PROJECT_MCP_KEY] = self._var_gitignore_mcp.get()
        return True

    def bind_dirty(self, callback) -> None:
        from dialogs.settings_section import bind_vars
        bind_vars(self, callback)

    def apply_hook(self) -> None:
        """Install or remove the pre-commit smoke-test hook.

        Called by the controller AFTER the configuration commit. Reports a
        failure and puts the checkbox back where it was, exactly as the
        dialog did; it never fails the save.
        """
        from helpers.smoke_runner import (
            is_hook_installed, install_pre_commit_hook,
            uninstall_pre_commit_hook,
        )

        project = self._active_project()
        if not project:
            return
        wanted = self._var_precommit_hook.get()
        currently = is_hook_installed(project)
        if wanted and not currently:
            ok, msg = install_pre_commit_hook(project)
            if not ok:
                messagebox.showwarning("Smoke-test hook", msg, parent=self._host)
                self._var_precommit_hook.set(False)
        elif not wanted and currently:
            ok, msg = uninstall_pre_commit_hook(project)
            if not ok:
                messagebox.showwarning("Smoke-test hook", msg, parent=self._host)
                self._var_precommit_hook.set(True)

    # ── Build ────────────────────────────────────────────────────────────

    def _active_project(self) -> str:
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        return (raw.get("projects") or [{}])[0].get("path") or ""

    def _toggle(self, body, text, var, caption):
        themed_checkbutton(
            body, text=text, variable=var,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body, text=caption,
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _build(self, body, raw):
        from helpers.mcp import GITIGNORE_PROJECT_MCP_KEY
        from helpers.smoke_runner import is_hook_installed

        self._var_autocommit = tk.BooleanVar(
            value=bool(raw.get("auto_commit_after_sync", False)))
        self._toggle(
            body,
            "Auto-commit after sync  (git add -A + git commit)",
            self._var_autocommit,
            "  Only fires when the project is a git repo and the working tree "
            "has changes.\n"
            "  Commit message: \"chore: tokensave sync\"  (or AI-generated if "
            "enabled under AI)")

        self._var_gitignore_mcp = tk.BooleanVar(
            value=bool(raw.get(GITIGNORE_PROJECT_MCP_KEY, True)))
        self._toggle(
            body,
            "Add .mcp.json to .gitignore when binding a project",
            self._var_gitignore_mcp,
            "  A project binding is portable on purpose, so it CAN be "
            "committed —\n"
            "  it holds no machine paths. But committing it gives everyone who\n"
            "  clones the repo a tokensave MCP server that only starts if they\n"
            "  have tokensave on PATH. Off = share it; on = keep it local.\n"
            "  Either way, git ignores nothing it is already tracking.")

        project = self._active_project()
        self._var_precommit_hook = tk.BooleanVar(
            value=is_hook_installed(project) if project else False)
        self._toggle(
            body,
            "Run smoke tests before commits  (pre-commit hook)",
            self._var_precommit_hook,
            "  Installs a .git/hooks/pre-commit script that runs the tests/ "
            "suite\n"
            "  before every commit.  Only affects the active project's git "
            "repo.")

        tk.Button(body, text="\U0001f39b Agent Policy…",
                  command=self._open_instruction_composer,
                  bg=C["surface1"], fg=C["text"], relief=tk.FLAT,
                  padx=12).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
                 text="  Whether agents may commit or push, as toggles. "
                      "Compiles into\n"
                      "  templates/project-baseline.md, which every wired "
                      "project loads.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _open_instruction_composer(self):
        """Lazy in-handler import -- cross-dialog dep, per the project rule."""
        from dialogs.instruction_composer import InstructionComposerDialog

        dialog = InstructionComposerDialog(self._host, self._cfg)
        dialog.transient(self._host)
        dialog.grab_set()
