"""mcp_desktop_panel — the Claude Desktop retirement UI for the MCP dialog.

Split out of :mod:`dialogs.mcp_config` rather than added to it. That file was
already 1,397 lines against the Doctor's 1,500-line cap, and this panel is a
self-contained migration with its own confirmation, its own hard gate and its
own undo -- exactly the seam the anti-monolith audit exists to find.

A mixin, not a helper module, because every method here is a renderer that
needs the dialog's own scrolling ``_body``, its ``_cfg``, its ``_render`` and
the ``UiPumpMixin`` marshalling. Passing all four through free functions would
be the same coupling with more ceremony.

What the panel is FOR is documented on :meth:`_render_desktop_migration`; the
short version is that Claude Desktop defines its own app-level ``tokensave``
which ``claude mcp get`` never reads, so it outlived the user-scoped migration
and kept shadowing every project binding on the machine.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C
from helpers import mcp_desktop
from helpers.mcp_shadow import SHADOW_ACTIVE, classify_shadow, structural_note


class DesktopMigrationMixin:
    """Renders and drives the Claude Desktop tokensave retirement.

    Expects the host to provide ``_body``, ``_cfg``, ``_servers``,
    ``_shadow_scanning``, ``_render()``, ``_post()``, ``_log_to_app()`` and
    ``_migration_status()``.
    """

    def _annotate_desktop_shadow(self, info: dict, root: str) -> dict:
        """Demote a correct binding that Claude Desktop is out-serving.

        Only a row whose file is already right can be shadowed — anything else
        has a nearer problem to fix first, and saying "shadowed" over it would
        bury the actionable verdict. The result is an ADVISORY state, so the
        row renders in the needs-attention area with no Apply button: the
        `.mcp.json` is correct, and rewriting it would be a no-op reported as
        a fix.

        Requires the runtime scan to have finished. Until then the row keeps
        its file verdict rather than guessing, on the same principle that
        stops `describe_effective` contradicting a true badge with "could not
        verify".
        """
        if info.get("state") != "ok" or self._servers is None:
            return info
        try:
            if not mcp_desktop.desktop_entry_present():
                return info
            verdict = classify_shadow(root, desktop_entry_present=True,
                                      servers=self._servers)
        except Exception:                                    # noqa: BLE001
            return info
        if verdict.state != SHADOW_ACTIVE:
            return info
        out = dict(info)
        out["state"] = "project_desktop_shadowed"
        out["label"] = "⚠ shadowed by Claude Desktop's tokensave"
        out["issue"] = (
            "This file is correct. Claude Desktop runs its own `tokensave` "
            "server for the whole app and Claude Code dedupes by name, so "
            "that one answers here instead — it is serving %s (PID %s). "
            "Editing this file will not change which server runs; retire the "
            "Desktop entry below." % (verdict.served_project, verdict.pid))
        return out

    # ── Claude Desktop's global tokensave ────────────────────────────────

    def _render_desktop_migration(self, rows):
        """The second shadow: Claude Desktop's own app-level `tokensave`.

        Desktop spawns the manager's wrapper for the whole app, not per
        session, so the wrapper cannot know which project a session is in and
        resolves one from the global pin. Every Desktop-hosted Claude Code
        session then inherits that single server whatever repository it is
        working in — measured 2026-08-26, a session in this repo answered
        from another project's graph while this project's own server was
        running correctly beside it.

        Silent when there is no entry and none was ever retired: a machine
        that never had this problem must not be shown a migration for it.
        """
        try:
            configs = mcp_desktop.discover_desktop_configs()
            present = mcp_desktop.desktop_entry_present(configs=configs)
        except Exception:                                    # noqa: BLE001
            return
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        retired = bool(raw.get(mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY))
        state = mcp_desktop.lifecycle_state(present, retired)

        if state == mcp_desktop.LIFECYCLE_ABSENT:
            return

        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(8, 4), ipady=6)

        if state == mcp_desktop.LIFECYCLE_RETIRED:
            tk.Label(box,
                     text="  ✓  Claude Desktop's tokensave entry is retired — "
                          "each project's own binding is authoritative.",
                     font=("Segoe UI", 9, "bold"), bg=C["surface0"],
                     fg=C["green"], anchor=tk.W, justify=tk.LEFT,
                     wraplength=740).pack(fill=tk.X, padx=8)
            ttk.Button(box, text="Re-add Desktop entry…",
                       command=self._unretire_desktop).pack(
                anchor=tk.W, padx=8, pady=(4, 2))
            return

        if state == mcp_desktop.LIFECYCLE_RETURNED:
            tk.Label(box,
                     text="  ⚠  Claude Desktop's tokensave entry has come "
                          "BACK after you retired it",
                     font=("Segoe UI", 10, "bold"), bg=C["surface0"],
                     fg=C["red"], anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(4, 2))
            tk.Label(box,
                     text=("  An app update or a hand edit can recreate it. "
                           "Until it is retired again it wins the `tokensave` "
                           "name over every project binding on this machine."),
                     font=("Segoe UI", 8), bg=C["surface0"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
                fill=tk.X, padx=8)
        else:
            tk.Label(box,
                     text="  ⚠  Claude Desktop defines its own tokensave",
                     font=("Segoe UI", 10, "bold"), bg=C["surface0"],
                     fg=C["peach"], anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(4, 2))
            tk.Label(box,
                     text=("  Claude Desktop starts one tokensave server for "
                           "the whole app and picks its project from the pin, "
                           "so every Desktop-hosted Claude Code session gets "
                           "that one project whichever repository it is in. "
                           "Claude Code dedupes MCP servers by name, so it "
                           "wins over each project's own .mcp.json."),
                     font=("Segoe UI", 8), bg=C["surface0"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
                fill=tk.X, padx=8)

        self._render_desktop_runtime(box, rows)
        self._render_desktop_action(box, rows)

    def _render_desktop_runtime(self, box, rows):
        """What the live server is actually serving — the runtime tier.

        Configuration and runtime are reported as separate facts. The entry
        existing is not the same claim as a server running, and neither is the
        same as that server answering for a given project; conflating them is
        how a dormant config becomes a false alarm.
        """
        if self._servers is None:
            tk.Label(box, text="  ⋯ checking which project it is serving…",
                     font=("Consolas", 9), bg=C["surface0"],
                     fg=C["overlay0"], anchor=tk.W).pack(fill=tk.X, padx=8)
            self._start_shadow_scan()
            return

        verdict = classify_shadow("", desktop_entry_present=True,
                                  servers=self._servers)
        if verdict.served_project:
            tk.Label(box,
                     text="  Currently serving:  %s   (PID %s)"
                          % (verdict.served_project, verdict.pid),
                     font=("Consolas", 9), bg=C["surface0"], fg=C["peach"],
                     anchor=tk.W).pack(fill=tk.X, padx=8, pady=(2, 0))
        elif verdict.is_runtime:
            tk.Label(box,
                     text="  A Desktop tokensave server is running, but which "
                          "project it serves could not be established.",
                     font=("Consolas", 9), bg=C["surface0"],
                     fg=C["overlay0"], anchor=tk.W, justify=tk.LEFT,
                     wraplength=740).pack(fill=tk.X, padx=8, pady=(2, 0))
        else:
            # Do not infer "Desktop is closed" from the absence of a server.
            # An earlier version did, and printed it beside a gate that said
            # the opposite — because the enumeration behind BOTH had failed,
            # and only one of them admitted it.
            running = (self._desktop_running or (None, ""))[0]
            why = {True: " — Claude Desktop is running but has not started one",
                   False: " — Claude Desktop is closed"}.get(running, "")
            tk.Label(box,
                     text="  No Desktop tokensave server is running right "
                          "now%s." % why,
                     font=("Consolas", 9), bg=C["surface0"],
                     fg=C["overlay0"], anchor=tk.W, justify=tk.LEFT,
                     wraplength=740).pack(fill=tk.X, padx=8, pady=(2, 0))

        note = structural_note(len(self._migration_status(rows)["bound"]))
        if note:
            tk.Label(box, text="  " + note,
                     font=("Segoe UI", 8), bg=C["surface0"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(2, 2))

    def _desktop_gate(self, rows, *, live: bool = False) -> "tuple[bool, str]":
        """May the Desktop entry be retired right now? ``(allowed, reason)``.

        A hard gate, not a banner. Claude Desktop rewrites
        `claude_desktop_config.json` from its in-memory cache every 1-2
        minutes, so a removal performed while it runs is silently restored —
        which would read as this manager failing rather than as a race.

        "Could not determine" also blocks, and says so. The alternative is
        treating an unanswered question as a yes, on the one operation in this
        dialog that removes a feature.

        ``live`` asks the process list again instead of reading the cached
        answer. Rendering uses the cache — the check spawns a subprocess, and
        a render must never block on one — but the WRITE re-asks, because a
        gate answered a minute ago is not a gate.
        """
        st = self._migration_status(rows)
        if not st["ready"]:
            return False, ("Bind or skip every project first — the same "
                           "readiness rule as the user-scoped migration.")
        if live:
            running, detail = mcp_desktop.desktop_app_running()
        elif self._desktop_running is None:
            return False, "Checking whether Claude Desktop is running…"
        else:
            running, detail = self._desktop_running
        if running is None:
            return False, ("Could not determine whether Claude Desktop is "
                           "running (%s). Retiring now risks the edit being "
                           "silently reverted." % detail)
        if running:
            return False, ("Quit Claude Desktop first — it rewrites its own "
                           "config from memory every 1–2 minutes, so this "
                           "change would revert on its own. (%s)" % detail)
        return True, ""

    def _render_desktop_action(self, box, rows):
        allowed, reason = self._desktop_gate(rows)
        if allowed:
            ttk.Button(box, text="Retire Desktop tokensave…",
                       style="Primary.TButton",
                       command=lambda: self._retire_desktop(rows)).pack(
                anchor=tk.W, padx=8, pady=(4, 2))
            return
        row = tk.Frame(box, bg=C["surface0"])
        row.pack(fill=tk.X, padx=8, pady=(4, 2))
        btn = ttk.Button(row, text="Retire Desktop tokensave…")
        btn.state(["disabled"])
        btn.pack(side=tk.LEFT)
        ttk.Button(row, text="↻ Re-check",
                   command=self._rescan_shadow).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(box, text="  " + reason,
                 font=("Segoe UI", 8), bg=C["surface0"], fg=C["peach"],
                 justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(0, 4))

    def _retire_desktop(self, rows):
        """Remove Claude Desktop's tokensave, behind its own confirmation.

        Never reached from binding a project. This removes a feature — Claude
        Desktop chat loses tokensave entirely — so the confirmation says that
        in those words rather than describing the change as a cleanup.
        """
        allowed, reason = self._desktop_gate(rows, live=True)
        if not allowed:
            messagebox.showwarning("Not yet", reason, parent=self)
            self._render()
            return

        configs = mcp_desktop.discover_desktop_configs()
        will, wont = mcp_desktop.change_set(configs)
        if not will:
            messagebox.showinfo(
                "Nothing to remove",
                "No active Claude Desktop config carries a tokensave entry.",
                parent=self)
            self._render()
            return

        changed = "\n".join("    %s" % c.path for c in will)
        untouched = "\n".join("    %s   (%s)" % (c.path, c.install_id)
                              for c in wont)
        if not messagebox.askyesno(
                "Retire Desktop tokensave",
                "Will change:\n%s\n\n%s"
                "Removed entry:\n%s\n\n"
                "After this, Claude Desktop CHAT has no tokensave at all — "
                "that is the deliberate trade for making every Claude Code "
                "session serve its own project.\n\n"
                "A timestamped backup is written first, and the exact entry "
                "is recorded so it can be put back."
                % (changed,
                   ("Will NOT change:\n%s\n\n" % untouched) if wont else "",
                   json.dumps(will[0].entry, indent=2)),
                parent=self):
            return

        result = mcp_desktop.retire(configs)
        if not result.ok:
            self._log_to_app("Desktop MCP retirement FAILED: %s"
                             % result.detail, C["red"])
            messagebox.showerror("Retirement failed", result.detail,
                                 parent=self)
            self._render()
            return

        raw = self._cfg.raw
        if isinstance(raw, dict):
            # The flag records the DECISION. Absence alone cannot tell a
            # completed migration from a machine that never had the entry,
            # and only the decision makes a later reappearance a regression.
            raw[mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY] = True
            raw[mcp_desktop.DESKTOP_RETIRED_RECORD_KEY] = result.record
            self._cfg.save()

        self._log_to_app("MCP: retired Claude Desktop's tokensave entry.",
                         C["green"])
        messagebox.showinfo(
            "Desktop tokensave retired",
            "%s\n\nThe config is retired now, but a server Claude Desktop "
            "already started keeps serving its old project until Desktop is "
            "restarted — it resolves the project once, at startup.\n\n"
            "Restart Claude Desktop, then open a session in a bound project "
            "and check that tokensave_status matches that project."
            % result.detail, parent=self)
        self._servers = None            # runtime facts are now stale
        self._desktop_running = None
        self._render()

    def _unretire_desktop(self):
        """Put back exactly what was removed, if the files still match.

        Restores the recorded entry verbatim rather than a canonical one: the
        user may have had a custom command or arguments, and quietly
        substituting the manager's idea of the entry would be a different
        change wearing the word "undo".
        """
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        record = raw.get(mcp_desktop.DESKTOP_RETIRED_RECORD_KEY) or {}
        entry = record.get("entry")
        if not entry:
            messagebox.showinfo(
                "Nothing recorded",
                "No retired Claude Desktop entry was recorded, so there is "
                "nothing to restore automatically. Add it back through "
                "Claude Desktop's own settings.", parent=self)
            return

        running, detail = mcp_desktop.desktop_app_running()
        if running is not False:
            messagebox.showwarning(
                "Quit Claude Desktop first",
                "Claude Desktop rewrites its config from memory, so this "
                "change would revert on its own. (%s)" % detail, parent=self)
            return

        if not messagebox.askyesno(
                "Re-add Desktop tokensave",
                "Restore this entry to Claude Desktop's config?\n\n%s\n\n"
                "Claude Desktop chat gets tokensave back — and it will "
                "shadow every project binding again."
                % json.dumps(entry, indent=2), parent=self):
            return

        result = mcp_desktop.restore(record)
        if not result.ok:
            messagebox.showwarning("Not restored", result.detail, parent=self)
            self._log_to_app("Desktop MCP restore: %s" % result.detail,
                             C["peach"])
            self._render()
            return

        raw[mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY] = False
        raw.pop(mcp_desktop.DESKTOP_RETIRED_RECORD_KEY, None)
        self._cfg.save()
        self._log_to_app("MCP: restored Claude Desktop's tokensave entry.",
                         C["green"])
        self._servers = None
        self._render()

    def _rescan_shadow(self):
        """Drop the cached runtime facts and look again."""
        self._servers = None
        self._desktop_running = None
        self._render()

    def _start_shadow_scan(self):
        """Enumerate running tokensave servers once, off the Tk thread.

        Guarded by ``_shadow_scanning`` rather than the render generation: the
        answer is about the machine, not about any widget, so a re-render
        during the scan should reuse it rather than start a second one.
        """
        if self._shadow_scanning:
            return
        self._shadow_scanning = True

        def _worker():
            servers = []
            try:
                from helpers.project_discovery import find_projects
                from helpers.tokensave_daemon import list_tokensave_servers
                projects = [p["path"] for p in
                            find_projects(self._cfg.search_roots)]
                servers = list_tokensave_servers(
                    tokensave_exe=self._cfg.tokensave_exe,
                    known_projects=projects)
            except Exception:                                # noqa: BLE001
                servers = []
            # Asked here too, on the same thread, for the same reason: it
            # spawns a process, and the gate that reads it is rendered.
            try:
                running = mcp_desktop.desktop_app_running()
            except Exception as exc:                         # noqa: BLE001
                running = (None, "could not check: %s" % exc)
            self._post(self._apply_shadow_scan, servers, running)

        threading.Thread(target=_worker, daemon=True,
                         name="mcp-shadow-scan").start()

    def _apply_shadow_scan(self, servers, running=None):
        """Store the scan and re-render once. Tk thread only."""
        self._shadow_scanning = False
        self._servers = servers
        self._desktop_running = running
        self._render()
