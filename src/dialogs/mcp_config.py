"""MCPConfigDialog — manage tokensave's MCP wiring into Claude Desktop / Code.

Shows one block per config file (Claude Desktop + Claude Code) with:
  - Path of the file
  - Current state badge (✓ correct / ⚠ drift / ✗ missing)
  - Diff between current and proposed entries
  - Apply / Skip / Open file actions
  - Big warning if the owning Claude app is currently running

Per the show-diff-and-ask protocol in CLAUDE.md: each config gets its own
Apply button (no "Fix all"), each Apply writes a timestamped backup
before mutating, and the diff is rendered in plain text so the user can
read it without leaving the dialog.

This dialog is the one place in the manager that mutates Claude's MCP
configs. It also mutates `_cfg["mcp_skip_warnings"]` (a raw list — NOT
a derived ManagerConfig field). After a mutation, `self._cfg.save()`
alone is sufficient: no `refresh_derived()` is needed because no
ManagerConfig @property getter depends on `mcp_skip_warnings`.

Takes `cfg: ManagerConfig` per Round 4 Phase C; reads `cfg.raw` for
the dict view that `_classify_mcp_entry` and `_save_config` expect.
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import bind_mousewheel
from helpers.mcp import (
    _mcp_configs, _classify_mcp_entry, _apply_mcp_fix, _is_claude_running,
    _mcp_code_cfg_path, _project_mcp_path, effective_scope,
    remove_mcp_entry,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class MCPConfigDialog(tk.Toplevel):
    """Manage tokensave entries in Claude Desktop's and Claude Code's MCP
    config files.

    Shows one block per config file with:
      - Path of the file
      - Current state badge (✓ correct / ⚠ drift / ✗ missing)
      - Diff between current and proposed entries
      - Apply / Skip / Open file actions
      - Big warning if the owning Claude app is currently running

    Per the show-diff-and-ask protocol in CLAUDE.md: each config gets its
    own Apply button (no "Fix all"), each Apply writes a timestamped
    backup before mutating, and the diff is rendered in plain text so the
    user can read it without leaving the dialog.

    The dialog is the one place in the manager that touches Claude's MCP
    configs. Other callers (startup banner, Settings button) only LAUNCH
    this dialog — they never edit the JSON themselves.
    """

    def __init__(self, parent, cfg: "ManagerConfig", focus_project: str = ""):
        super().__init__(parent)
        self._cfg = cfg
        # Set when opened from the Projects tab: that project renders
        # expanded even if it is already bound, because the user asked
        # about it specifically.
        self._focus_project = focus_project or ""
        # Bound projects collapse by default. With seventeen of them the
        # useful axis is "what needs attention", not "show everything".
        self._show_bound = bool(focus_project)
        self.title("MCP Integration")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(680, 520)
        self.geometry("820x680")
        self.grab_set()

        # Header
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=18, pady=(14, 4))
        tk.Label(hdr, text="🔌  MCP Integration",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        tk.Label(hdr, text="Manage tokensave's wiring into Claude's MCP system.",
                 font=("Segoe UI", 9, "italic"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT, padx=(10, 0))

        # Running-Claude warning banner (populated on detect)
        self._warn_lbl = tk.Label(
            self, text="", font=("Segoe UI", 9),
            bg=C["base"], fg=C["red"],
            justify=tk.LEFT, anchor=tk.W, wraplength=760)
        self._warn_lbl.pack(fill=tk.X, padx=18, pady=(2, 4))

        # Scrollable body — same Canvas+Frame pattern as SettingsDialog
        wrap = tk.Frame(self, bg=C["base"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(2, 4))
        self._canvas = tk.Canvas(wrap, bg=C["base"], highlightthickness=0)
        bind_mousewheel(self._canvas)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._body = tk.Frame(self._canvas, bg=C["base"])
        self._body_win = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")
        self._body.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(
                self._body_win, width=e.width))
        for w in (self._canvas, self._body):
            w.bind(
                "<MouseWheel>",
                lambda e: self._canvas.yview_scroll(
                    int(-1 * (e.delta / 120)), "units"))

        # Footer
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        ttk.Button(btn_row, text="↻ Re-detect",
                   command=self._render).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

        # Per-config-path state for the renderer
        self._config_state: dict[str, dict] = {}
        self._render()

    # ── Rendering ───────────────────────────────────────────────────────

    def _render(self):
        """(Re-)build the per-config blocks. Called once at open and from
        Re-detect / after each Apply so the state badges stay fresh."""
        for child in self._body.winfo_children():
            child.destroy()

        # Warning banner — running Claude apps
        running = _is_claude_running()
        if running["desktop"] or running["code"]:
            apps = []
            if running["desktop"]:
                apps.append("Claude Desktop")
            if running["code"]:
                apps.append("Claude Code")
            self._warn_lbl.configure(
                text=("⚠  " + " / ".join(apps) + " is currently running. "
                      "It rewrites its own config file every 1–2 minutes, "
                      "which will silently revert any fix you apply here. "
                      "Fully quit the app before clicking Apply on its row."))
        else:
            self._warn_lbl.configure(text="")

        for label, path in _mcp_configs():
            self._render_block(label, path)

        self._render_projects_section()

    def _render_projects_section(self):
        """Per-project `.mcp.json` bindings, grouped by whether they need work.

        A Claude Code session reads the `.mcp.json` in its project root, and a
        binding there is what makes the session serve THAT project rather than
        whatever the user-scoped entry happens to resolve to. Rendered through
        the same block helpers as the two global configs, so these rows inherit
        the diff, the backup and the per-row Apply rather than growing a second
        write path.
        """
        try:
            from helpers.project_discovery import find_projects
            projects = find_projects(self._cfg.search_roots)
        except Exception:                                    # noqa: BLE001
            return                       # never let discovery break the dialog

        rows = []
        for proj in projects:
            root = proj.get("path") if isinstance(proj, dict) else str(proj)
            if not root:
                continue
            # Only projects with a real tokensave index. Without one there
            # is nothing to bind a server TO -- and the classifier
            # (correctly) refuses project scope for such a path, so the row
            # would fall through to the GLOBAL wrapper proposal and offer to
            # write this machine's absolute paths into a shared project
            # file. That is the precise outcome this feature exists to
            # prevent. Filtered here rather than by loosening the scope
            # rule, which would let any stray .mcp.json be judged by
            # project rules.
            if not os.path.isdir(os.path.join(root, ".tokensave")):
                continue
            name = (proj.get("name") if isinstance(proj, dict) else "") \
                or os.path.basename(root) or root
            info = _classify_mcp_entry(_project_mcp_path(root), self._cfg.raw)
            rows.append((name, root, info))
        if not rows:
            return

        needs = [r for r in rows if r[2]["state"] != "ok"]
        bound = [r for r in rows if r[2]["state"] == "ok"]

        # A project entry says `"command": "tokensave"` so the file stays
        # portable, which makes PATH resolution a prerequisite rather than
        # a detail. Offering Apply while it is unmet would write a config
        # that parses fine and cannot start.
        from helpers import path_setup
        path_state = path_setup.read_state(self._cfg.raw)
        blocked = "" if path_state.is_ready else (
            "Blocked: `tokensave` does not resolve as a command yet — "
            "see the note above. Binding now would write a config that "
            "cannot start.")

        tk.Label(self._body,
                 text="  Per-project bindings  (Claude Code)",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(
            anchor=tk.W, padx=4, pady=(16, 0))
        tk.Label(self._body,
                 text=("  Each Claude Code session reads its own project's "
                       ".mcp.json. Without one it falls back to the user-scoped "
                       "entry above, which resolves by searching upward from "
                       "whatever directory the session started in."),
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
            fill=tk.X, padx=4, pady=(0, 4))

        self._render_path_prerequisite(path_state)

        for name, root, info in needs:
            self._render_block("%s  —  needs binding" % name,
                               _project_mcp_path(root),
                               blocked_reason=blocked)

        if not bound:
            return
        if self._show_bound:
            for name, root, info in bound:
                self._render_block(name, _project_mcp_path(root),
                                   blocked_reason=blocked)
            self._render_migration(rows)
            return

        self._render_migration(rows)

        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(6, 2))
        tk.Label(strip,
                 text="\u2713  %d project%s already bound" % (
                     len(bound), "" if len(bound) == 1 else "s"),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["green"]).pack(
            side=tk.LEFT)
        ttk.Button(strip, text="show",
                   command=self._toggle_bound).pack(side=tk.LEFT, padx=(10, 0))

    def _render_path_prerequisite(self, state):
        """Show whether `tokensave` runs as a bare command, and offer the fix.

        Three states, and the last two must not be conflated: a binary that is
        installed but unreachable is a setup step, while a missing one is an
        installation problem with a different remedy. Telling someone to
        reinstall software they already have is its own bug.
        """
        if state.is_ready:
            tk.Label(self._body,
                     text="  ✓  `tokensave` resolves on PATH — project "
                          "bindings can start.",
                     font=("Segoe UI", 9), bg=C["base"], fg=C["green"],
                     anchor=tk.W).pack(fill=tk.X, padx=4, pady=(2, 6))
            return

        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(2, 8), ipady=6)
        tk.Label(box, text="  ⚠  Prerequisite: tokensave is not on PATH",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["surface0"], fg=C["peach"], anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(box, text="  " + state.detail,
                 font=("Segoe UI", 9), bg=C["surface0"], fg=C["text"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8)
        tk.Label(box,
                 text=("  A project binding uses the bare command `tokensave` "
                       "on purpose: a .mcp.json is shared through version "
                       "control, and an absolute path would only work on this "
                       "machine."),
                 font=("Segoe UI", 8, "italic"),
                 bg=C["surface0"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(2, 4))

        if not state.is_fixable:
            return
        ttk.Button(box, text="Add tokensave to PATH…",
                   style="Primary.TButton",
                   command=lambda d=state.exe_dir: self._add_to_path(d)).pack(
            anchor=tk.W, padx=8, pady=(2, 2))

    def _add_to_path(self, directory: str):
        """Its own confirmed action, never folded into a project Apply.

        Shows the exact directory and the current value before touching
        anything, and reports the previous value afterwards: there is no file
        to back up for an environment variable, so the old value has to be
        surfaced somewhere the user can copy it from.
        """
        from helpers import path_setup

        current = path_setup.user_path()
        if not messagebox.askyesno(
                "Add tokensave to PATH",
                "Append this folder to your USER PATH?\n\n"
                "    %s\n\n"
                "Current user PATH:\n%s\n\n"
                "User scope only — no administrator rights, and nothing "
                "other accounts can see. Programs already running keep their "
                "old environment, so Claude Code must be restarted before a "
                "project binding will start."
                % (directory, current or "(empty)"),
                parent=self):
            return

        ok, detail = path_setup.add_to_user_path(directory)
        self._log_to_app(
            ("PATH: " + detail.splitlines()[0]) if detail else "PATH unchanged",
            C["green"] if ok else C["peach"])
        messagebox.showinfo(
            "PATH updated" if ok else "PATH unchanged",
            detail + ("\n\nRestart Claude Code (and any open terminals) "
                      "before binding a project — running processes keep "
                      "the environment they started with." if ok else ""),
            parent=self)
        self._render()

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
        bound, skipped, remaining = [], [], []
        for name, root, info in rows:
            if info["state"] == "ok":
                bound.append((name, root))
            elif _project_mcp_path(root) in skips:
                skipped.append((name, root))
            else:
                remaining.append((name, root))
        return {"bound": bound, "skipped": skipped, "remaining": remaining,
                "ready": not remaining and bool(bound)}

    def _render_migration(self, rows):
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
        tk.Label(box,
                 text="  %d bound · %d skipped · %d still to bind"
                      % (len(st["bound"]), len(st["skipped"]),
                         len(st["remaining"])),
                 font=("Consolas", 9), bg=C["surface0"], fg=C["text"],
                 anchor=tk.W).pack(fill=tk.X, padx=8)
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

        ok, detail = remove_mcp_entry(code_cfg)
        if not ok:
            self._log_to_app("MCP migration FAILED: %s" % detail, C["red"])
            messagebox.showerror("Removal failed", detail, parent=self)
            self._render()
            return

        self._log_to_app("MCP migration: removed the user-scoped tokensave "
                         "entry.", C["green"])
        self._verify_migration(detail)
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

    def _toggle_bound(self):
        self._show_bound = not self._show_bound
        self._render()

    def _render_block(self, label: str, path: str, blocked_reason: str = ""):
        info = _classify_mcp_entry(path, self._cfg.raw)
        self._config_state[path] = info

        frame = tk.LabelFrame(
            self._body, text=f"  {label}  ",
            bg=C["base"], fg=C["text"],
            font=("Segoe UI", 10, "bold"),
            bd=1, relief=tk.GROOVE)
        frame.pack(fill=tk.X, padx=4, pady=(8, 4), ipady=4)

        self._render_block_header(frame, label, path, info)
        if info["state"] != "ok":
            self._render_block_diff(frame, info)
        self._render_block_actions(frame, label, path, info, blocked_reason)

    def _render_block_header(self, frame, label: str, path: str, info: dict):
        """Path label, optional UWP tag, and status badge."""
        head = tk.Frame(frame, bg=C["base"])
        head.pack(fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(head, text=path, font=("Consolas", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)

        # UWP / traditional install indicator — only meaningful for the
        # Desktop row.  Users hitting the UWP path-redirection footgun
        # need to SEE that the manager is targeting the package-internal
        # config, not %APPDATA%\Claude\.  Knowing this prevents a whole
        # category of "I edited the file but Desktop ignores my fix"
        # confusion.
        if label == "Claude Desktop":
            is_uwp = "\\Packages\\Claude_" in path
            install_tag = ("(UWP / Store install)" if is_uwp
                           else "(Traditional install)")
            tag_colour = C["blue"] if is_uwp else C["overlay0"]
            tk.Label(head, text="  " + install_tag,
                     font=("Segoe UI", 8, "italic"),
                     bg=C["base"], fg=tag_colour).pack(side=tk.LEFT)

        state = info["state"]
        # project_mismatch is red with the unreadable/missing cases, not
        # amber with the drift ones: a binding pointed at ANOTHER project
        # answers every query from the wrong codebase and looks normal.
        badge_colour = (C["green"] if state == "ok"
                        else C["peach"] if state in (
                            "direct_serve", "wrong_wrapper",
                            "project_unbound", "project_absolute")
                        else C["red"])
        tk.Label(head, text=info["label"],
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=badge_colour).pack(side=tk.RIGHT)

        # The default only made sense for the two global configs; a project
        # binding deliberately does NOT route through the wrapper.
        _ok_default = ("No action needed — bound to this project."
                       if os.path.basename(path).lower() == ".mcp.json"
                       else "No action needed — already routes through the wrapper.")
        issue_text = info["issue"] or _ok_default
        tk.Label(frame, text=issue_text,
                 font=("Segoe UI", 9),
                 bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(0, 4))

    def _render_block_diff(self, frame, info: dict):
        """Diff text box showing current vs proposed JSON — only for non-ok states."""
        diff_box = tk.Text(
            frame, height=8, font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=8, pady=6, wrap=tk.NONE,
            state=tk.NORMAL)
        diff_box.tag_configure("old", foreground="#f38ba8")
        diff_box.tag_configure("new", foreground="#a6e3a1")
        diff_box.tag_configure("hdr", foreground=C["overlay0"],
                                font=("Consolas", 9, "italic"))

        if info["current"] is None:
            diff_box.insert(tk.END, "  (no current entry — will be added)\n", "hdr")
        else:
            diff_box.insert(tk.END, "  --- current ---\n", "hdr")
            for line in json.dumps(info["current"], indent=2).splitlines():
                diff_box.insert(tk.END, "  - " + line + "\n", "old")
        diff_box.insert(tk.END, "  +++ proposed +++\n", "hdr")
        for line in json.dumps(info["proposed"], indent=2).splitlines():
            diff_box.insert(tk.END, "  + " + line + "\n", "new")

        line_count = int(diff_box.index("end-1c").split(".")[0])
        diff_box.configure(height=min(max(line_count + 1, 6), 18),
                           state=tk.DISABLED)
        diff_box.pack(fill=tk.X, padx=8, pady=(2, 4))

    def _render_block_actions(self, frame, label: str, path: str, info: dict,
                              blocked_reason: str = ""):
        """Apply / Skip / Open buttons and the backup-notice strip."""
        actions = tk.Frame(frame, bg=C["base"])
        actions.pack(fill=tk.X, padx=8, pady=(2, 4))

        if blocked_reason and info["state"] != "ok":
            # No Apply button rather than a disabled one: the reason is the
            # useful part, and a greyed control invites clicking to find
            # out why it is greyed.
            tk.Label(actions, text=blocked_reason,
                     font=("Segoe UI", 9), bg=C["base"], fg=C["peach"],
                     justify=tk.LEFT, wraplength=700, anchor=tk.W).pack(
                fill=tk.X)
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(
                side=tk.LEFT, pady=(4, 0))
            return

        if info["state"] == "ok":
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(side=tk.LEFT)
        else:
            ttk.Button(
                actions, text="Apply this fix",
                style="Primary.TButton",
                command=lambda p=path, l=label: self._apply(p, l)).pack(side=tk.LEFT)
            ttk.Button(actions, text="Skip (don't warn again)",
                       command=lambda p=path: self._skip(p)).pack(
                side=tk.LEFT, padx=(8, 0))
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(
                side=tk.LEFT, padx=(8, 0))

        tk.Label(frame,
            text=("  A timestamped backup is written before any change. "
                  "Other mcpServers entries in this file are preserved "
                  "(as data \u2014 formatting and comments are not)."),
            font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(0, 4))

    # ── Actions ─────────────────────────────────────────────────────────

    def _log_to_app(self, text: str, colour: str) -> None:
        """Write to the persistent OUTPUT pane, silently ignored if unavailable."""
        try:
            self.master._log(text, colour)
        except (AttributeError, tk.TclError):
            pass

    def _apply_running_guard(self, label: str) -> bool:
        """Return True and surface an error if the target Claude app is running.

        Writing the MCP config while Claude is live is either ignored (Desktop
        only reloads at startup) or silently reverted (Desktop writes its cache
        back to disk every 1–2 minutes). The guard prevents silent no-ops.
        """
        running = _is_claude_running()
        if not ((label == "Claude Desktop" and running["desktop"]) or
                (label == "Claude Code" and running["code"])):
            return False
        self._log_to_app(
            f"MCP Apply REFUSED: {label} is still running. "
            f"No changes were written. Quit {label} (verify zero "
            f"rows in Task Manager) then click Apply again.",
            C["red"])
        messagebox.showerror(
            f"Apply refused — {label} is running",
            f"{label} is currently running and is reading the MCP "
            "config from its in-memory cache.  Writing to the file now "
            "would either be ignored (Desktop only reloads at startup) "
            "or silently reverted (Desktop writes its cache back to "
            "disk every 1–2 minutes).\n\n"
            "★  NO CHANGES WERE WRITTEN  ★\n\n"
            f"To fix:\n"
            f"1. Fully quit {label} (tray icon → Quit).\n"
            f"2. Verify ZERO rows for 'claude' in Task Manager.\n"
            f"3. Wait ~5 seconds for stragglers (crashpad, renderer).\n"
            "4. Click Re-detect, then Apply this fix.",
            parent=self)
        self._render()
        return True

    def _apply(self, cfg_path: str, label: str):
        if self._apply_running_guard(label):
            return

        proposed = self._config_state[cfg_path]["proposed"]
        ok, msg = _apply_mcp_fix(cfg_path, proposed)
        if ok:
            self._log_to_app(
                f"MCP Apply OK: wrote canonical tokensave entry to "
                f"{label} config.  {msg}",
                C["green"])
            messagebox.showinfo(
                "Fix applied", f"{msg}\n\n"
                "Status row below has been refreshed.",
                parent=self)
            raw = self._cfg.raw
            skips = (raw.get("mcp_skip_warnings") or []) \
                    if isinstance(raw, dict) else []
            if cfg_path in skips:
                skips.remove(cfg_path)
                raw["mcp_skip_warnings"] = skips
                self._cfg.save()
        else:
            self._log_to_app(
                f"MCP Apply FAILED: {label} — {msg}", C["red"])
            messagebox.showerror("Fix failed", msg, parent=self)
        self._render()

    def _skip(self, cfg_path: str):
        raw = self._cfg.raw
        skips = (raw.get("mcp_skip_warnings") or []) \
                if isinstance(raw, dict) else []
        if cfg_path not in skips:
            skips.append(cfg_path)
            raw["mcp_skip_warnings"] = skips
            self._cfg.save()
        messagebox.showinfo(
            "Skipped",
            f"Won't warn about {cfg_path} on startup anymore.\n\n"
            "Open this dialog from Settings → MCP integration to revisit.",
            parent=self)

    def _open_file(self, cfg_path: str):
        """Open the config file in the user's default editor, or its parent
        folder if the file doesn't exist yet."""
        try:
            if os.path.isfile(cfg_path):
                os.startfile(cfg_path)
            else:
                parent_dir = os.path.dirname(cfg_path)
                if os.path.isdir(parent_dir):
                    os.startfile(parent_dir)
                else:
                    messagebox.showwarning(
                        "Not found",
                        f"Neither {cfg_path} nor its parent directory exists.",
                        parent=self)
        except OSError as e:
            messagebox.showerror("Could not open", str(e), parent=self)
