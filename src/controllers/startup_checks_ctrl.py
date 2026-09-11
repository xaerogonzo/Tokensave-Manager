"""startup_checks_ctrl.py - what the Manager checks shortly after launch.

Extracted from `App` (2026-09-11), which had 53 direct methods against a cap
of 40. With the IPC inbox already out this was the last four, taking the
class to 37 - comfortably under rather than exactly on the line.

Three checks, and **the stagger between them is the point of the class**:

  300 ms   config      may open Settings or the MCP dialog
 1200 ms   worktrees   staggered so the two checks' log lines do not
                       interleave mid-write
 2500 ms   identity    last, so a relocation dialog does not land on top of
                       whatever the earlier two opened

That ordering used to live as three `self.after` calls and two comments in
`App.__init__`, where it read as scheduling trivia. It is not: each delay is
there because a dialog would otherwise cover another, or two writers would
interleave. Keeping the reasons beside the calls is the reason this is a
class rather than four loose functions.

Callback injection, as with the other sub-controllers. `post` is the
parent's `UiPumpMixin._post`: one window, one pump, and a class that mixes
the mixin in without starting it raises AttributeError on a worker thread
where nothing is watching.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, TYPE_CHECKING

from constants import C, _BASE_DIR
from helpers.mcp import _mcp_configs, _classify_mcp_entry
from helpers.worktree_health import find_orphaned_worktrees
from dialogs.settings import SettingsDialog
from dialogs.mcp_config import MCPConfigDialog

if TYPE_CHECKING:
    from state import ManagerConfig


class StartupChecksController:
    """The post-launch checks, and the stagger that keeps them apart."""

    def __init__(
        self,
        root,
        cfg: "ManagerConfig",
        on_log: Callable,
        post: Callable,
        on_settings_saved: Callable,
        get_project_list: Callable,
    ) -> None:
        self._root              = root
        self._cfg               = cfg
        self._log               = on_log
        self._post              = post
        self._on_settings_saved = on_settings_saved
        # App's `projects` LIST (not `_projects`, the tab controller). Reached
        # through a callable because the original read it as
        # `getattr(self, "projects", [])` -- string-based, so a move to any
        # other object silently returns the default forever instead of
        # raising. Found by reading the moved body; no test failed on it.
        self._get_project_list  = get_project_list

    def schedule(self) -> None:
        """Queue the three checks, staggered - see the module docstring."""
        self._root.after(300, self._check_config)
        self._root.after(1200, self._check_worktree_health)
        self._root.after(2500, self._check_install_identity)

    def _check_config(self):
        problems = []
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            problems.append("tokensave.exe path is missing or invalid")
        if not self._cfg.template_dir or not os.path.isdir(self._cfg.template_dir):
            problems.append("Template directory is missing or invalid")

        # MCP-config drift detection — opens the configurator instead of
        # Settings when there are no other problems, since that's the most
        # actionable thing the user can do.
        skips = (self._cfg.raw.get("mcp_skip_warnings") or []) \
                if isinstance(self._cfg.raw, dict) else []
        mcp_drift = []
        for label, path in _mcp_configs():
            if path in skips:
                continue
            try:
                info = _classify_mcp_entry(path, self._cfg.raw)
            except Exception:
                # Defensive — never crash startup just because we can't read
                # a Claude config file. The dialog can surface details.
                continue
            if info["state"] != "ok":
                mcp_drift.append((label, info))

        if not problems and not mcp_drift:
            return

        if problems:
            # Existing path: paths broken, open Settings as before.
            note = "Please set the correct paths before using the manager."
            self._log("Config problem: " + " | ".join(problems), C["red"])
            SettingsDialog(
                self, self._cfg, self._cfg.save, self._on_settings_saved,
                startup_note=(note + "\n\n"
                              + "\n".join(f"• {p}" for p in problems)))
            return

        # Pure MCP drift — log it, open the configurator dialog directly.
        # Don't auto-pop in a modal way; the user just launched the manager
        # and wants to see the project list. A log line + a non-modal dialog
        # gives them the choice.
        for label, info in mcp_drift:
            self._log(
                f"MCP: {label} {info['label']} ({info['cfg_path']}). "
                f"Open Settings → MCP integration to fix.",
                C["peach"] if info["state"] in
                ("direct_serve", "wrong_wrapper") else C["red"])

        # Open the configurator after a short delay so the main window has
        # finished laying out — feels less like an interruption.
        self._root.after(800, lambda: MCPConfigDialog(self, self._cfg))

    def _check_worktree_health(self):
        """Log (never dialog) any git worktree with no tokensave index of its
        own — see helpers/worktree_health.py for why this matters: without
        one, tokensave answers questions asked there using a SIBLING
        checkout's index instead, confidently and about the wrong branch.

        Deliberately quiet — unlike _check_config, this never opens a dialog
        at launch. Real repair is a deliberate action via Doctor (🔍 Doctor →
        one click repairs every orphaned worktree for that project); this
        sweep exists so an orphaned worktree is never silently sitting there
        unnoticed between Doctor runs.
        """
        orphans = find_orphaned_worktrees(
            self._get_project_list(), self._cfg.git_exe)
        if not orphans:
            return
        self._log(
            f"⚠ {len(orphans)} git worktree"
            f"{'s' if len(orphans) != 1 else ''} found with no tokensave "
            "index of its own — run 🔍 Doctor on the parent project to "
            "repair:", C["peach"])
        for o in orphans:
            self._log(
                f"    {o['project_name']}: '{o['branch'] or o['head']}' "
                f"at {o['worktree_path']}", C["overlay0"])

    def _check_install_identity(self):
        """Where am I, and who owns the fleet? Two questions, one sweep.

        Not "a launch check" for its own sake. The trigger is *this
        installation's identity, or the fleet's ownership, no longer matches* —
        and launch is simply the only moment the Manager can observe it, since
        nothing runs while it is closed. It is re-run after a Settings save,
        where `template_dir` can change underneath it.

        Identity is a string comparison and free. Ownership needs to read every
        project, and that is affordable **because** the instruction split took
        the fleet's always-loaded text from 1,924,101 B to 409,375 B: the
        largest single `CLAUDE.md` is now about 20 KB.

        Modelled on `_check_worktree_health` — a worker, never blocking launch —
        but unlike that one it offers the repair, the way MCP drift already
        opens its configurator. A move is exactly the moment to be interrupted.
        """
        roots = list(self._cfg.raw.get("search_roots") or [])
        cfg = self._cfg
        raw = dict(cfg.raw)
        base_dir = _BASE_DIR

        def worker():
            try:
                from helpers.install_identity import (
                    read_identity, read_ownership, relocation_plan,
                )
                from helpers.instructions_posture import read_posture
                identity = read_identity(raw, base_dir)
                fleet = read_posture(roots, cfg)
                ownership = read_ownership(fleet.projects, cfg.template_dir)
                plan = relocation_plan(identity, ownership, raw,
                                       cfg.template_dir)
            except Exception:       # noqa: BLE001 — never break startup
                return
            self._post(lambda: self._report_install_identity(plan))

        threading.Thread(target=worker, daemon=True).start()

    def _report_install_identity(self, plan) -> None:
        """Log what was found; offer the repair only when one is available.

        The healthy case says nothing at all. `SAME` + `OWNED_ELSEWHERE` — I
        have not moved, another installation owns these projects — is REPORTED
        and never turned into an offer: this cannot tell a deliberate handover
        from a second install quietly taking them, and treating a state as an
        instruction is how a report becomes a silent transfer of fifteen
        repositories.
        """
        from helpers.install_identity import (
            OWNED_HERE, OWNERSHIP_SPLIT, OWNERSHIP_UNOWNED,
        )
        ownership = plan.ownership
        if ownership.state in (OWNED_HERE, OWNERSHIP_UNOWNED) \
                and not plan.identity.moved:
            return

        if plan.identity.moved:
            self._log("Instructions: this installation moved — %s -> %s"
                      % (plan.identity.recorded_display,
                         plan.identity.current_display), C["peach"])
        if ownership.state == OWNERSHIP_SPLIT:
            self._log("Instructions: the fleet points at %d different "
                      "baselines — %s"
                      % (len(ownership.owners), ownership.summary()), C["peach"])
        elif ownership.state != OWNED_HERE:
            self._log("Instructions: the fleet's baseline is %s"
                      % ownership.summary(), C["peach"])
        if ownership.unresolved:
            self._log("    %d project(s) reach no baseline at all: %s"
                      % (len(ownership.unresolved),
                         ", ".join(ownership.unresolved[:4])), C["overlay0"])
        if plan.blocked:
            self._log("    no single repair: %s" % plan.blocked, C["overlay0"])
            return
        if not plan.offers_bulk:
            return

        from dialogs.relocate import RelocateDialog
        self._root.after(800, lambda: RelocateDialog(self, self._cfg, plan,
                                               on_log=self._log))

