"""SyncStatusController — sync / status / pin commands for the Projects tab.

Extracted from ProjectsTabController (Round 5).

Dependency contract:
  • tab             — the Projects tk.Frame (after() + winfo_toplevel())
  • cfg             — read-only ManagerConfig (.tokensave_exe)
  • on_log          — thread-safe log callback  (msg: str, colour: str = "")
  • on_set_running  — (running: bool, label: str) -> None
  • on_set_proc     — (proc_or_none) -> None
  • on_refresh      — () -> None
  • on_run          — (args: list, cwd: str, label: str) -> None  (App._run)
  • on_run_capture  — (args: list, cwd: str, label: str) -> (raw, rc, elapsed)
  • get_projects    — () -> list[dict]
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

import tkinter as tk

from constants import C, CREATE_NO_WINDOW, _ANSI
from helpers.mcp import _mcp_configs, _classify_mcp_entry
from helpers.project_discovery import clear_pinned, set_pinned
from helpers.sync_service import (ShadowPrep, prepare_shadows,
                                  sync_argv)
from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


class SyncStatusController:
    """Handles sync / force-sync / status / set-active / auto commands."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_set_running: Callable[[bool, str], None],
        on_set_proc: Callable[[object], None],
        on_refresh: Callable[[], None],
        on_run: Callable,
        on_run_capture: Callable,
        get_projects: Callable,
    ) -> None:
        self._tab            = tab
        self._cfg            = cfg
        self._on_log         = on_log
        self._on_set_running = on_set_running
        self._on_set_proc    = on_set_proc
        self._on_refresh     = on_refresh
        self._on_run         = on_run
        self._on_run_capture = on_run_capture
        self._get_projects   = get_projects
        self._stop_requested: bool = False

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Commands ──────────────────────────────────────────────────────────────

    def cmd_set_active(self, path: str) -> None:
        """Pin *path*, then say what pinning it actually did.

        The branch is on **whether anything still reads the pin**, which is
        not the same question as "are the MCP configs healthy" — and asking
        the second one is the bug this replaces.

        The pin file has exactly one reader: ``src/tokensave-wrapper.py``,
        installed only as Claude Desktop's MCP command. Retire that entry and
        the pin decides nothing about MCP at all; it stays the manager's own
        default project, which is a real and useful thing, just a much smaller
        one than the old wording claimed.

        The previous guard read ``_classify_mcp_entry(...)["state"] == "ok"``
        across both configs and warned only when nothing was "ok". A RETIRED
        Desktop config classifies as ``ok`` — deliberately, because a chosen
        absence is not a defect and four surfaces depend on that reading — so
        on a machine that had completed the migration the warning could never
        fire, and the reassuring Desktop text printed instead. Measured
        2026-09-09: Desktop's ``mcpServers`` was empty, the pin still named a
        project, and the log said the pin set the default for Desktop's chats.

        The classifier is right and was left alone. Only the caller changed.
        """
        set_pinned(path)
        self._on_log(f"Pinned → {path}", C["green"])
        if self._wrapper_reads_the_pin():
            # The effect note is withheld when NOTHING is wired: the user's
            # problem is a step earlier, and a cross-project tip is noise to
            # someone whose tools do not run at all.
            if self._warn_about_broken_configs():
                self._log_pin_effect()
        else:
            self._log_manager_default_only()
        self._on_refresh()

    def _wrapper_reads_the_pin(self) -> bool:
        """Is there a Claude Desktop entry pointing at the wrapper?

        The same helper ``App._pin_tag`` already uses, so the badge and the log
        line cannot disagree. Errs toward the louder answer for the same
        reason it does: if we cannot tell, the message that says the pin MIGHT
        matter is the safer one to print.
        """
        try:
            from helpers import mcp_desktop
            return mcp_desktop.desktop_entry_present()
        except Exception:                                    # noqa: BLE001
            return True

    def _warn_about_broken_configs(self) -> bool:
        """Warn about half-wired configs. Returns whether the pin reaches any.

        Only meaningful while the pin is live, so only reached then. The
        return value preserves a distinction the original three-branch guard
        made and which is worth keeping: *some* wiring broken is a note beside
        the effect, while *all* of it broken means the effect has not happened
        at all and describing it would be describing nothing.

        Errs toward reporting the effect when it cannot probe: an unreadable
        config is a fact about this tool, and swallowing the explanation over
        it would be the worse half of the error.
        """
        try:
            configs = _mcp_configs()
            states = [_classify_mcp_entry(p, self._cfg.raw)["state"]
                      for _, p in configs]
        except Exception:                                    # noqa: BLE001
            return True
        if not states:
            return True
        if "ok" not in states:
            self._on_log(
                "  No MCP config currently routes through the wrapper — "
                "this pin won't take effect until you fix the MCP wiring "
                "AND restart Claude.  Settings → 🔌 Manage MCP wiring.",
                C["peach"])
            return False
        bad = [lbl for (lbl, _p), s in zip(configs, states) if s != "ok"]
        if bad:
            self._on_log(
                f"  Note: {', '.join(bad)} still needs its MCP wiring fixed "
                f"(Settings → 🔌 Manage MCP wiring) — the pin cannot "
                f"reach it.",
                C["peach"])
        return True

    def _log_manager_default_only(self) -> None:
        """What the pin means once nothing routes through the wrapper.

        Said plainly rather than softened. A user who has retired Desktop's
        entry is running the recommended posture, and the honest report is
        that this command is now a bookmark — it decides which project the
        manager's own tabs open against, and nothing else.

        The unserved list is computed only in the strictest case, where no
        wrapper AND no user-scoped fallback exist. That is the one posture in
        which "this project has no tokensave" is a live possibility, and it is
        rare enough to afford a scan on a menu click.
        """
        self._on_log(
            "  This is the manager's own default project — the ★ row, and the "
            "project the Git tab opens when nothing else is selected.",
            C["overlay0"])
        self._on_log(
            "  It decides NOTHING about MCP. Claude Desktop has no tokensave "
            "entry, so nothing reads this pin; every Claude Code session "
            "serves its own project either from that project's .mcp.json or "
            "from the user-scoped entry resolving in the session's folder.",
            C["overlay0"])
        self._on_log(
            "  Reading another project needs no restart anywhere: pass "
            "graph_root=<project path> on any tokensave call.  "
            "Reference tab → “🌐  Query another project”.",
            C["overlay0"])
        self._warn_if_nothing_serves_unbound_projects()

    def _warn_if_nothing_serves_unbound_projects(self) -> None:
        """Name the projects that no longer have any tokensave at all.

        Reached only when both global entries are gone, which is the posture
        where an unbound project is genuinely unserved rather than
        automatically served. Naming them beats a count: "two projects" sends
        the user to look, a name tells them whether they care.
        """
        try:
            from helpers.mcp_posture import read_posture
            posture = read_posture(self._cfg)
        except Exception:                                    # noqa: BLE001
            return
        if posture.automatic_fallback or not posture.unserved:
            return
        names = ", ".join(p.name for p in posture.unserved[:6])
        if len(posture.unserved) > 6:
            names += ", …"
        self._on_log(
            f"  ⚠ Both global tokensave entries are retired, so these have no "
            f"tokensave at all: {names}.  Bind them from Settings → "
            f"🔌 Manage MCP wiring, or skip them there.",
            C["peach"])

    def _log_pin_effect(self) -> None:
        """Say what the pin does -- and, more usefully, what it is not for.

        Reached only when Claude Desktop actually defines the wrapper entry;
        every sentence here is about Desktop, and printing it on a machine
        with no such entry is what made this text a lie. See
        :meth:`cmd_set_active`.

        This briefly claimed the change could be applied to a running Claude
        Desktop. It cannot: the pin watcher that promised it was removed after
        being measured, because killing Desktop's server does not make Desktop
        start another one. See docs/MCP_INTEGRATION_GOTCHAS.md.

        What replaced it is the more important fact, and the one a user
        reading this line actually wants: the pin only chooses the DEFAULT
        graph. Reading a different project needs no restart at all, because
        every tokensave tool takes ``graph_root``. Saying only "restart
        Claude" would be true and would still leave the user believing the
        restart is unavoidable, which is what made this worth switching
        projects over in the first place.

        It says "Desktop's own chats" rather than "Claude Desktop" on
        purpose. Claude Code runs *inside* the Desktop window, so the shorter
        phrasing reads as "this application" to someone working there -- and
        it is wrong for them, because a Claude Code session registers
        tokensave directly, binds to its own working directory, and never
        reads the pin at all. That ambiguity confused a real user three times
        in one session before the wording was changed.
        """
        self._on_log(
            "  This sets the DEFAULT project for Claude Desktop's own chats, "
            "which read it once when Desktop starts its tokensave server — so "
            "changing that default does need a Desktop restart.",
            C["overlay0"])
        self._on_log(
            "  Claude Code sessions are NOT affected — they bind to their own "
            "working directory and never read this pin, so there is nothing "
            "to restart there.",
            C["overlay0"])
        self._on_log(
            "  Either way, READING another project needs no restart: pass "
            "graph_root=<project path> on any tokensave call.  "
            "Reference tab → “🌐  Query another project”.",
            C["overlay0"])

    def cmd_auto(self) -> None:
        """Clear the pin. What that means depends on who was reading it.

        The old text described the wrapper's behaviour unconditionally, which
        is wrong in the same way :meth:`cmd_set_active` was: with no Desktop
        entry there is no wrapper to pick anything, and nothing to restart.
        """
        clear_pinned()
        if self._wrapper_reads_the_pin():
            self._on_log(
                "Auto-detect enabled — wrapper picks the most-recently-synced "
                "project at next launch.", C["sky"])
            self._on_log(
                "  Restart Claude Desktop to trigger a fresh auto-detect.",
                C["overlay0"])
        else:
            self._on_log(
                "Pin cleared — the manager falls back to the first project in "
                "the list as its own default.", C["sky"])
            self._on_log(
                "  Nothing reads this pin for MCP: Claude Desktop has no "
                "tokensave entry, and Claude Code sessions never read it.",
                C["overlay0"])
        self._on_refresh()

    def cmd_sync(self, path: str) -> None:
        self._refresh_shadows_if_enabled(path)
        self._on_run(sync_argv(), cwd=path, label=os.path.basename(path))

    def _refresh_shadows_if_enabled(self, path: str) -> None:
        """SL2: regenerate shadow links before the index is rebuilt.

        Opt-in per project. Files added since the last manual run are not
        shadowed, so tokensave silently stops seeing them -- a new `.zsc`
        just drops out of the index with no signal that anything is missing.

        Cost when disabled is one small file read: the walk and the hardlink
        probe are behind the flag, so projects that never turned this on pay
        nothing. Runs before the sync rather than after, so the new links are
        present for the indexer that is about to read them.
        """
        self._log_shadow_refresh(prepare_shadows(path), path)

    def _log_shadow_refresh(self, prep: "ShadowPrep", path: str) -> None:
        """Report only what the user can act on.

        A silent no-op on a volume without hardlink support is correct: that
        is a property of the disk, not an error, and repeating it on every
        sync would train the user to ignore the log. A FAILURE on a volume
        that does support them is the opposite -- a permissions or filesystem
        problem that would otherwise present as "auto-shadow appears to do
        nothing at all".
        """
        if not prep.worth_reporting:
            return
        if prep.failed:
            self._on_log(
                "  ⚠ shadow links: %d created, %d could not be created "
                "in %s" % (prep.created, prep.failed,
                           os.path.basename(path)),
                C["peach"])
        else:
            self._on_log("  + %d shadow link%s created" % (
                prep.created, "" if prep.created == 1 else "s"),
                C["overlay0"])

    # Operations safe to run unattended across many projects: they stream to
    # the log and open nothing. Status is deliberately the log-only variant —
    # the single-project command shows a popup, and N popups is not a feature.
    # Doctor is NOT here on purpose: it schedules follow-up dialogs (purge,
    # worktree repair) that would stack one per project.
    BATCH_OPS = {
        "sync":   (sync_argv(),             "Sync"),
        "force":  (sync_argv(force=True),   "Force re-sync"),
        "status": (["status"],          "Status"),
    }

    def run_batch(self, paths: list, op: str = "sync") -> None:
        """Run one tokensave op across *paths*, sequentially.

        Sequential on purpose: the controller tracks a single ``current_proc``
        so Stop can kill what is running, and N parallel subprocesses would
        leave all but one unkillable. It is also kinder to a machine already
        running several Claude sessions.
        """
        argv, label = self.BATCH_OPS.get(op, self.BATCH_OPS["sync"])
        if op == "force":
            n = len(paths)
            # Asked once for the batch. Confirming a full rebuild per project
            # would be worse than not asking at all.
            if not messagebox.askyesno(
                    "Force Re-sync",
                    f"Rebuild the code graph from scratch for {n} "
                    f"project{'s' if n != 1 else ''}?"
                    + chr(10) + chr(10) +
                    "Runs sequentially and may take several minutes.",
                    parent=self._root):
                return
        projects = [{"name": os.path.basename(p) or p, "path": p}
                    for p in paths]
        self._run_project_batch(projects, argv, label)

    def cmd_sync_all(self) -> None:
        projects = self._get_projects()
        if not projects:
            messagebox.showinfo("No Projects", "No projects found.", parent=self._root)
            return
        ts_projects = [p for p in projects if p.get("has_tokensave", True)]
        if not ts_projects:
            messagebox.showinfo(
                "No indexed projects",
                "None of your projects have a tokensave index yet.\n\n"
                "Right-click any project → ⚙ Retrofit… to add one.",
                parent=self._root)
            return
        count = len(ts_projects)
        skipped = len(projects) - count
        skip_note = (f"\n({skipped} git-only project{'s' if skipped != 1 else ''} "
                     f"will be skipped)") if skipped else ""
        if not messagebox.askyesno(
            "Sync All",
            f"Sync {count} indexed project{'s' if count != 1 else ''}?{skip_note}\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self._root,
        ):
            return

        self._run_project_batch(list(ts_projects), ["sync"], "Sync")

    def _run_project_batch(self, projects_snapshot: list, argv: list,
                           label: str) -> None:
        """Shared sequential runner: stream one op over N projects."""
        count = len(projects_snapshot)
        if not count:
            return

        def worker():
            self._stop_requested = False
            self._on_log(f"↺  {label} across {count} project{'s' if count != 1 else ''}…", C["blue"])
            log.info(f"BATCH {label.upper()} — {count} projects")
            self._tab.after(0, self._on_set_running, True,
                            f"{count} projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._stop_requested:
                    self._on_log(
                        f"  ■ {label} aborted after {i - 1}/{count}.",
                        C["red"])
                    log.info(f"BATCH {label} aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._on_log(f"[{i}/{count}] {name}", C["subtext"])
                # Already on a worker thread here, and _on_log is
                # thread-safe, so the same per-project refresh applies.
                if argv[:1] == ["sync"]:
                    self._refresh_shadows_if_enabled(path)
                log.info(f"  {label} {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [self._cfg.tokensave_exe, *argv], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._on_set_proc(proc)
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._on_log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._on_log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self._on_set_proc(None)

            summary = f"{label} done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._on_log(summary, C["green"] if not fail else C["peach"])
            log.info(f"BATCH {label} complete — ok={ok} fail={fail}")
            self._tab.after(0, self._on_set_running, False, "")
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self, path: str) -> None:
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._on_run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._on_log(f"  Status OK — {data.get('node_count')} nodes, "
                                 f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self._tab.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._on_log(line)
                self._on_log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self._tab.after(0, self._on_refresh)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def cmd_force_sync(self, path: str) -> None:
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self._root,
        ):
            self._on_run(["sync", "--force"], cwd=path, label=os.path.basename(path))

    def request_stop(self) -> None:
        """Signal the sync-all worker to abort after the current project."""
        self._stop_requested = True

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _show_status_popup(self, name: str, msg: str) -> None:
        win = tk.Toplevel(self._root)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()
        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self._root)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )
