"""Renders and drives the user-scope tokensave retirement, including
the background verification pass.

Split out of ``dialogs/mcp_config.py`` (Roadmap-16 god-class
split), following the `DesktopMigrationMixin` precedent that
file already set rather than introducing a second pattern.
"""

from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from constants import C
from helpers.mcp import (
    ADVISORY_STATES,
    USER_SCOPE_RETIRED_KEY,
    _apply_mcp_fix,
    _canonical_mcp_entry,
    _classify_mcp_entry,
    _mcp_code_cfg_path,
    _project_mcp_path,
    annotate_project_binding,
    approve_project_binding,
    describe_effective,
    effective_scope,
    remove_mcp_entry,
)


class UserScopeMigrationMixin:
    """Renders and drives the user-scope tokensave retirement, including
the background verification pass.

    A mixin, so it reads ``self`` attributes the host
    dialog owns (``_body``, ``_cfg``, ``_render()``,
    ``_post()``, ``_log_to_app()``). It is never
    instantiated on its own.
    """

    def _migration_status(self, rows) -> dict:
        """Counts for the migration guard. Reads files only — no CLI calls.

        Readiness deliberately does NOT mean "verified serving". While the
        user-scoped entry still exists it is the thing that may be shadowing a
        binding, so demanding proof of correct service *before* removing it
        would be asking for an outcome the removal itself produces.
        Verification happens after, on a representative bound project.
        """
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        skips = raw.get("mcp_skip_warnings") or []
        bound, skipped, remaining, approved = [], [], [], []
        for name, root, info in rows:
            # Advisory states count as bound here, and must. Their files are
            # correct by construction, and the thing blocking them is usually
            # the user-scoped entry this migration removes — treating them as
            # unbound would withhold the button precisely when it is the fix,
            # which is the same "demand the outcome beforehand" trap the
            # readiness rule already refuses for shadowing.
            if info["state"] == "ok" or info["state"] in ADVISORY_STATES:
                bound.append((name, root))
                if info["state"] != "project_unapproved":
                    approved.append((name, root))
            elif _project_mcp_path(root) in skips:
                skipped.append((name, root))
            else:
                remaining.append((name, root))
        return {"bound": bound, "skipped": skipped, "remaining": remaining,
                "approved": approved,
                "ready": not remaining and bool(bound)}

    def _render_migration(self, rows):
        """Both retirement migrations, in the order they must be done.

        Two different global `tokensave` definitions can shadow a project
        binding, and retiring one says nothing about the other. Claude Code's
        user-scoped entry lives in `~/.claude.json`; Claude Desktop's lives in
        `claude_desktop_config.json`, which `claude mcp get` does not read —
        so the second was invisible to every check this dialog had, and
        outlived the first migration by months.
        """
        self._render_user_scope_migration(rows)
        self._render_desktop_migration(rows)

    def _render_user_scope_migration(self, rows):
        """Where the user-scoped fallback stands, and the action to retire it.

        Stated rather than hidden: once the user-scoped entry is gone, a
        project with no binding has no tokensave at all. That is the deliberate
        trade — determinism instead of a fallback that is usually right — and
        it is not something to discover afterwards.
        """
        st = self._migration_status(rows)
        code_cfg = _mcp_code_cfg_path()
        info = _classify_mcp_entry(code_cfg, self._cfg.raw)
        still_there = info.get("current") is not None

        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(12, 4), ipady=6)

        if not still_there:
            tk.Label(box,
                     text="  ✓  Migration complete — no user-scoped tokensave "
                          "entry remains. Each project serves its own graph.",
                     font=("Segoe UI", 9, "bold"),
                     bg=C["surface0"], fg=C["green"], anchor=tk.W,
                     justify=tk.LEFT, wraplength=740).pack(fill=tk.X, padx=8)
            return

        tk.Label(box, text="  ⚠  User-scoped fallback is still active",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["surface0"], fg=C["peach"], anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(4, 2))
        unapproved = len(st["bound"]) - len(st["approved"])
        tk.Label(box,
                 text="  %d bound · %d approved · %d skipped · %d still to bind"
                      % (len(st["bound"]), len(st["approved"]),
                         len(st["skipped"]), len(st["remaining"])),
                 font=("Consolas", 9), bg=C["surface0"], fg=C["text"],
                 anchor=tk.W).pack(fill=tk.X, padx=8)

        # Approval is the half of this the guard cannot speak for. "All bound"
        # does not mean "all working": an unapproved binding is not in the
        # running at all, and removing the fallback does not approve it — so
        # retiring now with nothing approved leaves every project without
        # tokensave until each one is visited. Said before the button, because
        # afterwards it reads as the migration having broken something.
        if unapproved:
            worst = not st["approved"]
            tk.Label(
                box,
                text=("  %s  %d bound project%s %s not approved yet. An "
                      "unapproved binding does not load, and removing the "
                      "user-scoped entry will not approve it%s"
                      % ("⚠" if worst else "•", unapproved,
                         "" if unapproved == 1 else "s",
                         "is" if unapproved == 1 else "are",
                         " — every project would be left with no tokensave "
                         "at all until you approve it."
                         if worst else ". Approve them first.")),
                font=("Segoe UI", 9, "bold" if worst else "normal"),
                bg=C["surface0"], fg=C["red"] if worst else C["peach"],
                justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(4, 2))
            ttk.Button(box,
                       text="Approve %d binding%s now…" % (
                           unapproved, "" if unapproved == 1 else "s"),
                       command=self._approve_all).pack(
                anchor=tk.W, padx=8, pady=(0, 4))
        tk.Label(box,
                 text=("  Claude Code dedupes MCP servers by name, so while a "
                       "user-scoped `tokensave` exists it can shadow a project "
                       "binding. Removing it makes each project's own binding "
                       "authoritative — and leaves any UNBOUND project with no "
                       "tokensave at all. Bind or skip each project first."),
                 font=("Segoe UI", 8), bg=C["surface0"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(2, 4))

        if st["ready"]:
            ttk.Button(box, text="Remove user-scoped tokensave…",
                       style="Primary.TButton",
                       command=self._remove_user_scoped).pack(
                anchor=tk.W, padx=8, pady=(2, 2))
        else:
            names = ", ".join(n for n, _ in st["remaining"][:4])
            more = "" if len(st["remaining"]) <= 4 else " …"
            tk.Label(box,
                     text=("  Not offered yet — %s%s %s still unbound and not "
                           "skipped." % (names, more,
                                         "is" if len(st["remaining"]) == 1
                                         else "are"))
                          if st["remaining"] else
                          "  Not offered yet — bind at least one project first.",
                     font=("Segoe UI", 9), bg=C["surface0"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(0, 4))

    def _remove_user_scoped(self):
        """Its own reviewed operation: diff, backup, apply, then VERIFY.

        Never a side effect of binding one project. And the result is not
        assumed — after removal a representative bound project is asked what
        Claude Code now serves, because "the file changed" and "the right
        server runs" are different claims. That distinction is the whole
        lesson of the pin watcher.
        """
        code_cfg = _mcp_code_cfg_path()
        info = _classify_mcp_entry(code_cfg, self._cfg.raw)
        current = info.get("current")
        if current is None:
            messagebox.showinfo("Nothing to remove",
                                "No user-scoped tokensave entry found in\n%s"
                                % code_cfg, parent=self)
            return

        if not messagebox.askyesno(
                "Remove user-scoped tokensave",
                "Remove this entry from\n%s ?\n\n%s\n\n"
                "After this, a Claude Code session only gets tokensave if its "
                "project has its own .mcp.json binding. Projects you skipped "
                "will have no tokensave at all.\n\n"
                "A timestamped backup is written first, and the removed entry "
                "is shown afterwards so it can be restored by hand."
                % (code_cfg, json.dumps(current, indent=2)),
                parent=self):
            return

        # Asked AFTER the diff, not before: this is the more specific of the
        # two questions, and putting it first would make the user answer
        # "is a session live" before they had seen what is being removed.
        if self._code_running_guard("Removing the user-scoped entry"):
            return

        ok, detail = remove_mcp_entry(code_cfg)
        if not ok:
            self._log_to_app("MCP migration FAILED: %s" % detail, C["red"])
            messagebox.showerror("Removal failed", detail, parent=self)
            self._render()
            return

        # Record the DECISION, not just its effect. Without this the next
        # render reads the absence as a missing entry and offers to put it
        # back — in the same dialog that just reported the migration complete.
        raw = self._cfg.raw
        if isinstance(raw, dict):
            raw[USER_SCOPE_RETIRED_KEY] = True
            self._cfg.save()

        self._log_to_app("MCP migration: removed the user-scoped tokensave "
                         "entry.", C["green"])
        self._verify_migration(detail)
        self._render()

    def _approve_one(self, project_root: str):
        """Approve this project's binding, then re-render."""
        ok, detail = approve_project_binding(project_root)
        self._log_to_app(
            ("MCP: %s" % detail) if ok else ("MCP approve failed: %s" % detail),
            C["green"] if ok else C["peach"])
        if not ok:
            messagebox.showwarning("Not approved", detail, parent=self)
        self._render()

    def _approve_all(self):
        """Approve every bound-but-unapproved project in one action.

        Confirmed first, and the consequence is stated rather than implied:
        this authorises the `tokensave` server by name in each project, which
        is what Claude Code's own prompt would record.
        """
        rows = self._unapproved_roots()
        if not rows:
            messagebox.showinfo("Nothing to approve",
                                "Every bound project is already approved.",
                                parent=self)
            return
        listing = "\n".join("  • %s" % n for n, _r in rows[:12])
        if len(rows) > 12:
            listing += "\n  … and %d more" % (len(rows) - 12)
        if not messagebox.askyesno(
                "Approve %d binding%s" % (len(rows),
                                          "" if len(rows) == 1 else "s"),
                "Write approval for the `tokensave` server into each "
                "project's own .claude/settings.local.json:\n\n%s\n\n"
                "This is exactly what Claude Code records when you approve "
                "its prompt, and it authorises that ONE server by name — any "
                "other server in a .mcp.json still gets asked about.\n\n"
                "An existing settings file is backed up first, and its other "
                "settings are preserved. Proceed?" % listing,
                parent=self):
            return

        ok_n, fails = 0, []
        for name, root in rows:
            ok, detail = approve_project_binding(root)
            if ok:
                ok_n += 1
            else:
                fails.append("%s: %s" % (name, detail))
        self._log_to_app(
            "MCP: approved %d project binding%s%s."
            % (ok_n, "" if ok_n == 1 else "s",
               ", %d failed" % len(fails) if fails else ""),
            C["green"] if not fails else C["peach"])
        if fails:
            messagebox.showwarning(
                "Approved %d, %d failed" % (ok_n, len(fails)),
                "\n\n".join(fails[:6]), parent=self)
        else:
            messagebox.showinfo(
                "Approved",
                "%d project binding%s approved.\n\nRestart any running Claude "
                "Code session in those projects before relying on it."
                % (ok_n, "" if ok_n == 1 else "s"), parent=self)
        self._render()

    def _unapproved_roots(self) -> list:
        """(name, root) for every bound project that is not yet approved."""
        out = []
        try:
            from helpers.project_discovery import find_projects
            for proj in find_projects(self._cfg.search_roots):
                root = proj.get("path") if isinstance(proj, dict) else str(proj)
                if not root or not os.path.isdir(
                        os.path.join(root, ".tokensave")):
                    continue
                info = annotate_project_binding(
                    _classify_mcp_entry(_project_mcp_path(root),
                                        self._cfg.raw),
                    root, projects=self._claude_projects)
                if info["state"] == "project_unapproved":
                    name = (proj.get("name") if isinstance(proj, dict) else "") \
                        or os.path.basename(root)
                    out.append((name, root))
        except Exception:                                    # noqa: BLE001
            return []
        return out

    def _unretire_user_scoped(self):
        """Put the user-scoped entry back, as an explicit undo.

        The retirement flag would otherwise be a one-way door with no way out
        of the UI. Offered as undoing a migration rather than as "Apply this
        fix", because that is what it is — every unbound project starts
        resolving through the fallback again.
        """
        if not messagebox.askyesno(
                "Re-add the user-scoped entry",
                "This undoes the migration.\n\n"
                "A user-scoped `tokensave` outranks nothing, but Claude Code "
                "dedupes MCP servers by name — so it can shadow a project "
                "binding again, and every project will resolve through it "
                "rather than through its own .mcp.json.\n\n"
                "Re-add it?",
                default="no", parent=self):
            return
        if self._code_running_guard("Re-adding the user-scoped entry"):
            return
        raw = self._cfg.raw
        if isinstance(raw, dict):
            raw[USER_SCOPE_RETIRED_KEY] = False
            self._cfg.save()
        code_cfg = _mcp_code_cfg_path()
        ok, msg = _apply_mcp_fix(code_cfg, _canonical_mcp_entry(raw))
        self._log_to_app(
            ("MCP: re-added the user-scoped tokensave entry. %s" % msg) if ok
            else ("MCP: re-add FAILED — %s" % msg),
            C["peach"] if ok else C["red"])
        (messagebox.showinfo if ok else messagebox.showerror)(
            "Re-added" if ok else "Re-add failed", msg, parent=self)
        self._render()

    def _verify_migration(self, removal_detail: str):
        """Ask Claude Code what a bound project now serves.

        A failure here is reported as `verification_failed`, not as success
        with a caveat: the user needs to know the difference between "the file
        changed" and "the right server runs".
        """
        rows = []
        try:
            from helpers.project_discovery import find_projects
            for proj in find_projects(self._cfg.search_roots):
                root = proj.get("path") if isinstance(proj, dict) else str(proj)
                if not root:
                    continue
                if _classify_mcp_entry(_project_mcp_path(root),
                                       self._cfg.raw)["state"] == "ok":
                    rows.append(root)
        except Exception:                                    # noqa: BLE001
            rows = []

        if not rows:
            messagebox.showinfo(
                "Removed — not verified",
                removal_detail + "\n\nNo bound project was available to check "
                "against, so this was not verified end to end.",
                parent=self)
            return

        root = rows[0]
        got = effective_scope(root)
        if got.is_project or got.pending_approval:
            messagebox.showinfo(
                "Migration verified",
                removal_detail
                + "\n\nClaude Code now reports the PROJECT-scoped definition "
                  "in:\n%s\n\nRestart any running Claude Code session before "
                  "relying on it." % root,
                parent=self)
            self._log_to_app("MCP migration verified against %s." % root,
                             C["green"])
            return

        self._log_to_app(
            "MCP migration verification_failed: %s reports scope=%s."
            % (root, got.scope), C["peach"])
        messagebox.showwarning(
            "Removed, but verification failed",
            removal_detail
            + "\n\nverification_failed: after removal, %s still reports "
              "scope=%s rather than the project definition.\n\nThe file "
              "changed but the expected server is not the one serving. Do not "
              "assume the migration succeeded."
            % (root, got.scope),
            parent=self)

    # ── verification: what is Claude Code ACTUALLY serving? ─────────────

    def _start_verification(self, rows):
        """Ask Claude Code about each rendered row, in the background.

        The file check and the approval check together still cannot answer
        "which server runs" — only the client can, and asking costs a
        subprocess per project. So it runs off the Tk thread and revises each
        badge as its answer lands, rather than making the dialog sit blank for
        the length of ten CLI calls.

        Rows the free tier already disqualified are skipped: their verdict is
        settled, and spending a CLI call to re-derive a known answer would just
        delay the rows that are still in question.
        """
        targets = [(_project_mcp_path(root), root) for _n, root, info in rows
                   if info["state"] == "ok"
                   and _project_mcp_path(root) in self._row_widgets]
        if not targets:
            return

        for path, _root in targets:
            self._mark_verifying(path)

        gen = self._verify_gen

        def _worker():
            for path, root in targets:
                if gen != self._verify_gen:
                    return              # a re-render replaced these widgets
                try:
                    got = effective_scope(root)
                except Exception:                            # noqa: BLE001
                    continue            # one project must not stop the rest
                self._post(self._apply_verification, gen, path, got)

        threading.Thread(target=_worker, daemon=True).start()

    def _mark_verifying(self, path: str):
        """Say an answer is being fetched, so the row is not read as final."""
        widgets = self._row_widgets.get(path)
        if not widgets:
            return
        badge, _issue = widgets
        try:
            badge.configure(text="⋯ checking with Claude Code…",
                            fg=C["overlay0"])
        except tk.TclError:
            pass

    def _apply_verification(self, gen: int, path: str, got):
        """Write one verification result into its row. Tk thread only."""
        if gen != self._verify_gen:
            return
        widgets = self._row_widgets.get(path)
        if not widgets:
            return
        badge, issue = widgets

        # Hand the cheap tier's verdict across: `claude mcp get` does not read
        # .claude/settings.local.json, so left alone it would downgrade a
        # correctly-approved row to "not yet approved".
        from helpers.mcp import mcpjson_approval
        root = os.path.dirname(path)
        try:
            approval = mcpjson_approval(root,
                                        projects=self._claude_projects).state
        except Exception:                                    # noqa: BLE001
            approval = None
        verdict = describe_effective(got, approval=approval)
        if verdict is None:
            # Could not tell. Restore the file-level verdict rather than
            # leaving "checking…" on screen forever or inventing a failure:
            # an unreachable `claude` is a fact about our tooling, not about
            # the user's binding.
            info = self._config_state.get(path) or {}
            state = info.get("state", "ok")
            label = info.get("label", "")
            text = info.get("issue", "")
        else:
            state, label, text = verdict
            self._config_state[path] = {**(self._config_state.get(path) or {}),
                                        "state": state, "label": label,
                                        "issue": text}
        try:
            badge.configure(text=label, fg=self._badge_colour(state))
            if text:
                issue.configure(text=text)
        except tk.TclError:
            pass                         # row destroyed between post and run
