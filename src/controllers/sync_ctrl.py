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
from helpers.shadow_links import load_shadow_config, refresh_shadows
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
        set_pinned(path)
        self._on_log(f"Pinned → {path}", C["green"])
        try:
            configs = _mcp_configs()
            states = [_classify_mcp_entry(p, self._cfg.raw)["state"] for _, p in configs]
        except Exception:
            configs, states = [], []
        if "ok" in states and not all(s == "ok" for s in states):
            bad = [lbl for (lbl, p), s in zip(configs, states) if s != "ok"]
            self._on_log(
                f"  Note: {', '.join(bad)} still needs its MCP wiring fixed "
                f"(Settings → 🔌 Manage MCP wiring) — the pin cannot "
                f"reach it.",
                C["peach"])
            self._log_pin_effect()
        elif "ok" not in states:
            self._on_log(
                "  No MCP config currently routes through the wrapper — "
                "this pin won't take effect until you fix the MCP wiring "
                "AND restart Claude.  Settings → 🔌 Manage MCP wiring.",
                C["peach"])
        else:
            self._log_pin_effect()
        self._on_refresh()

    def _log_pin_effect(self) -> None:
        """Say what the pin does -- and, more usefully, what it is not for.

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
        """
        self._on_log(
            "  This sets the DEFAULT project for Claude Desktop, which reads "
            "it once when it starts its tokensave server — so changing that "
            "default does need a Desktop restart.",
            C["overlay0"])
        self._on_log(
            "  You do not need one to READ another project: pass "
            "graph_root=<project path> on any tokensave call.  "
            "Reference tab → “🌐  Query another project”.",
            C["overlay0"])

    def cmd_auto(self) -> None:
        clear_pinned()
        self._on_log(
            "Auto-detect enabled — wrapper picks the most-recently-synced project at next launch.",
            C["sky"])
        self._on_log(
            "  Restart Claude Desktop / Claude Code to trigger a fresh auto-detect.",
            C["overlay0"])
        self._on_refresh()

    def cmd_sync(self, path: str) -> None:
        self._refresh_shadows_if_enabled(path)
        self._on_run(["sync"], cwd=path, label=os.path.basename(path))

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
        config = load_shadow_config(path)
        if not (config and config.auto_shadow):
            return
        result = refresh_shadows(path, config.ext_map)
        self._log_shadow_refresh(result, path)

    def _log_shadow_refresh(self, result: dict, path: str) -> None:
        """Report only what the user can act on.

        A silent no-op on a volume without hardlink support is correct: that
        is a property of the disk, not an error, and repeating it on every
        sync would train the user to ignore the log. A FAILURE on a volume
        that does support them is the opposite -- a permissions or filesystem
        problem that would otherwise present as "auto-shadow appears to do
        nothing at all".
        """
        if not result["ran"]:
            return
        if result["failed"]:
            self._on_log(
                "  ⚠ shadow links: %d created, %d could not be created "
                "in %s" % (result["created"], result["failed"],
                           os.path.basename(path)),
                C["peach"])
        elif result["created"]:
            self._on_log("  + %d shadow link%s created" % (
                result["created"], "" if result["created"] == 1 else "s"),
                C["overlay0"])

    # Operations safe to run unattended across many projects: they stream to
    # the log and open nothing. Status is deliberately the log-only variant —
    # the single-project command shows a popup, and N popups is not a feature.
    # Doctor is NOT here on purpose: it schedules follow-up dialogs (purge,
    # worktree repair) that would stack one per project.
    BATCH_OPS = {
        "sync":   (["sync"],            "Sync"),
        "force":  (["sync", "--force"], "Force re-sync"),
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
