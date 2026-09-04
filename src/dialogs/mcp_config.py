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

import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import UiPumpMixin, bind_mousewheel
from helpers.mcp import (
    _mcp_configs, _classify_mcp_entry, _apply_mcp_fix,
    _is_claude_running, _project_mcp_path, ADVISORY_STATES,
    annotate_project_binding, read_claude_projects,
)
from dialogs.mcp_desktop_panel import DesktopMigrationMixin
from dialogs.mcp_duplicates_panel import DuplicateKeysMixin
from dialogs.mcp_migration_panel import UserScopeMigrationMixin
from dialogs.mcp_blocks_panel import EntryBlocksMixin

if TYPE_CHECKING:
    from state import ManagerConfig


class MCPConfigDialog(DesktopMigrationMixin, DuplicateKeysMixin, UserScopeMigrationMixin, EntryBlocksMixin,
                     UiPumpMixin, tk.Toplevel):
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
        # Badge + issue widgets per row, so the verification pass can revise a
        # verdict in place instead of rebuilding the whole body underneath the
        # user's scroll position.
        self._row_widgets: dict[str, tuple] = {}
        # One read of ~/.claude.json per render, shared by every row.
        self._claude_projects: dict = {}
        # Bumped on every render so a verification still in flight from the
        # previous one cannot write a stale badge into fresh widgets.
        self._verify_gen = 0
        # Collapsed by default: it needs no action, and a permanently
        # expanded warning at the top of the dialog reads as a fault.
        self._show_dups = False
        # Running tokensave servers, for the runtime tier. None means "not
        # scanned yet"; the scan shells out to PowerShell, so it happens once
        # in the background and the render that follows reads the result.
        # Kept off the render path because a synchronous enumeration would
        # freeze the dialog for as long as CIM takes to answer.
        self._servers: "list | None" = None
        # `(running, detail)` from the same background scan — asking costs a
        # subprocess, so it never happens on the render path.
        self._desktop_running: "tuple | None" = None
        self._shadow_scanning = False
        self._start_ui_pump()
        self._render()

    # ── Rendering ───────────────────────────────────────────────────────

    def _render(self):
        """(Re-)build the per-config blocks. Called once at open and from
        Re-detect / after each Apply so the state badges stay fresh."""
        for child in self._body.winfo_children():
            child.destroy()
        self._row_widgets.clear()
        # Any verification still running belongs to the widgets just destroyed.
        self._verify_gen += 1
        self._claude_projects = read_claude_projects()

        # Warning banner — running Claude apps
        self._running = _is_claude_running()
        self._warn_lbl.configure(text=self._running_warning(self._running))

        for label, path in _mcp_configs():
            self._render_block(label, path)

        self._render_duplicate_keys()
        self._render_projects_section()

    @staticmethod
    def _running_warning(running: dict) -> str:
        """The banner text for whichever Claude apps are live.

        Names the FILE each app rewrites, not just the app. The previous
        wording joined both apps into one sentence — "Claude Desktop / Claude
        Code is currently running. It rewrites its own config file" — which is
        ungrammatical for two apps and, worse, left the reader to guess which
        file was at risk. The two apps own different files, and the one this
        dialog's migration button writes is Claude Code's.
        """
        parts = []
        if running.get("desktop"):
            parts.append(
                "Claude Desktop is running — it rewrites "
                "claude_desktop_config.json from its in-memory cache every "
                "1–2 minutes, so a change to that row reverts on its own.")
        if running.get("code"):
            detail = running.get("code_detail") or ""
            parts.append(
                "A Claude Code session is live%s — it rewrites ~/.claude.json "
                "continuously, so editing the Claude Code row OR removing the "
                "user-scoped entry can be undone without warning. This "
                "includes a session running inside the Claude desktop app, "
                "and any `claude` in a terminal."
                % (" (%s)" % detail if detail else ""))
        if not parts:
            return ""
        return "⚠  " + "\n⚠  ".join(parts)

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
            info = annotate_project_binding(
                info, root, projects=self._claude_projects)
            info = self._annotate_desktop_shadow(info, root)
            rows.append((name, root, info))
        if not rows:
            return

        # Three buckets, not two. An advisory row has a CORRECT file and an
        # external blocker, so it needs attention like a broken one but must
        # not offer Apply — rewriting a correct file changes nothing and would
        # report success for it.
        needs = [r for r in rows
                 if r[2]["state"] != "ok"
                 and r[2]["state"] not in ADVISORY_STATES]
        advisory = [r for r in rows if r[2]["state"] in ADVISORY_STATES]
        bound = [r for r in rows if r[2]["state"] == "ok"]

        # Skip is an ANSWER, and this view never honoured it. A skipped
        # project kept rendering under "needs binding" with a loud Apply
        # button, so clicking Skip appeared to do nothing at all — and for a
        # project already on the list it genuinely did nothing, because
        # `_skip` short-circuits when the path is present.
        raw_cfg = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        skips = raw_cfg.get("mcp_skip_warnings") or []
        skipped = [r for r in needs if _project_mcp_path(r[1]) in skips]
        needs = [r for r in needs if _project_mcp_path(r[1]) not in skips]

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
                               blocked_reason=blocked, project_root=root)

        for name, root, info in advisory:
            self._render_block("%s  —  bound, but not in effect" % name,
                               _project_mcp_path(root),
                               blocked_reason=blocked, project_root=root)

        self._render_skipped(skipped)

        if not bound:
            self._start_verification(rows)
            return
        if self._show_bound:
            for name, root, info in bound:
                self._render_block(name, _project_mcp_path(root),
                                   blocked_reason=blocked, project_root=root)
            self._render_migration(rows)
            self._start_verification(rows)
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
        self._start_verification(rows)

    def _render_skipped(self, skipped):
        """Unbound projects the user has explicitly answered "not this one" to.

        Shown, but quietly and without an Apply button. Hiding them entirely
        would make Skip look like it deleted the project; leaving them in the
        loud group made Skip look like it did nothing. One line each, and a way
        back, is the honest middle.
        """
        if not skipped:
            return
        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(10, 2))
        tk.Label(strip,
                 text="⤼  %d project%s skipped — not bound, and you asked not "
                      "to be warned" % (len(skipped),
                                        "" if len(skipped) == 1 else "s"),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
                 anchor=tk.W).pack(fill=tk.X)
        for name, root, _info in skipped:
            row = tk.Frame(self._body, bg=C["base"])
            row.pack(fill=tk.X, padx=16, pady=(2, 0))
            tk.Label(row, text="%s" % name, font=("Segoe UI", 9),
                     bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)
            tk.Label(row, text="  " + _project_mcp_path(root),
                     font=("Consolas", 8),
                     bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)
            ttk.Button(row, text="Un-skip",
                       command=lambda p=_project_mcp_path(root):
                           self._unskip(p)).pack(side=tk.RIGHT)

    def _unskip(self, cfg_path: str):
        """Take a project back off the skip list and re-render."""
        raw = self._cfg.raw
        skips = (raw.get("mcp_skip_warnings") or []) \
            if isinstance(raw, dict) else []
        if cfg_path in skips:
            skips = [s for s in skips if s != cfg_path]
            raw["mcp_skip_warnings"] = skips
            self._cfg.save()
            self._log_to_app("MCP: no longer skipping %s." % cfg_path,
                             C["overlay0"])
        self._render()

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

    # ── Actions ─────────────────────────────────────────────────────────

    def _log_to_app(self, text: str, colour: str) -> None:
        """Write to the persistent OUTPUT pane, silently ignored if unavailable."""
        try:
            self.master._log(text, colour)
        except (AttributeError, tk.TclError):
            pass

    def _desktop_running_guard(self, label: str) -> bool:
        """Refuse to write Desktop's config while Desktop is running.

        A hard refusal, because the outcome is certain: Desktop reads the file
        only at startup and writes its cache back every 1–2 minutes, so the
        write is guaranteed to be either ignored or reverted.
        """
        running = _is_claude_running()
        if not (label == "Claude Desktop" and running["desktop"]):
            return False
        self._log_to_app(
            "MCP Apply REFUSED: Claude Desktop is still running. No changes "
            "were written. Quit it (verify zero rows in Task Manager) then "
            "click Apply again.", C["red"])
        messagebox.showerror(
            "Apply refused — Claude Desktop is running",
            "Claude Desktop is reading the MCP config from its in-memory "
            "cache. Writing to the file now would either be ignored (it only "
            "reloads at startup) or silently reverted (it writes its cache "
            "back to disk every 1–2 minutes).\n\n"
            "★  NO CHANGES WERE WRITTEN  ★\n\n"
            "To fix:\n"
            "1. Fully quit Claude Desktop (tray icon → Quit).\n"
            "2. Verify ZERO rows for 'claude' in Task Manager.\n"
            "3. Wait ~5 seconds for stragglers (crashpad, renderer).\n"
            "4. Click Re-detect, then Apply this fix.",
            parent=self)
        self._render()
        return True

    def _code_running_guard(self, what: str) -> bool:
        """Confirm before writing `~/.claude.json` while a session looks live.

        Returns True when the caller should abandon the write.

        A confirmation rather than a refusal, deliberately, and the asymmetry
        with Desktop is the point. Desktop's outcome is certain, so refusing is
        doing the user a favour. Here the evidence is the config's mtime — see
        `claude_code_active` for why no process name can answer it — which
        means it can be a session that has just been closed, and the window is
        five minutes wide. A hard block would strand someone for five minutes
        after they did exactly what they were told, so the honest move is to
        state the risk and let them decide.
        """
        running = _is_claude_running()
        if not running.get("code"):
            return False
        detail = running.get("code_detail") or ""
        proceed = messagebox.askyesno(
            "A Claude Code session looks live",
            "%s writes ~/.claude.json, and a Claude Code session appears to "
            "be running%s.\n\n"
            "A live session rewrites that file from its own state, so this "
            "change can be undone within a minute or two — and it will look "
            "like the change silently failed rather than like it was "
            "reverted.\n\n"
            "This includes a session running inside the Claude desktop app, "
            "not just `claude` in a terminal.\n\n"
            "Recommended: exit every Claude Code session first, then click "
            "Re-detect.\n\n"
            "Proceed anyway?"
            % (what, " (%s)" % detail if detail else ""),
            default="no", parent=self)
        if proceed:
            self._log_to_app(
                "MCP: proceeding with %s while a Claude Code session looks "
                "live — re-check the row afterwards." % what, C["peach"])
            return False
        self._log_to_app(
            "MCP: %s cancelled — a Claude Code session looks live." % what,
            C["overlay0"])
        return True

    def _apply(self, cfg_path: str, label: str):
        if self._desktop_running_guard(label):
            return
        if label == "Claude Code" and self._code_running_guard("This fix"):
            return

        proposed = self._config_state[cfg_path]["proposed"]
        ok, msg = _apply_mcp_fix(cfg_path, proposed)
        if ok:
            msg += self._maybe_gitignore(cfg_path)
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

    def _maybe_gitignore(self, cfg_path: str) -> str:
        """Ignore a freshly written project `.mcp.json`, if the user wants that.

        Only ever touches a PROJECT binding — the two global configs live in
        Claude's own directories and are nobody's repository.

        The default is on, and it is a judgement call rather than an obvious
        one: the file is portable precisely so it *can* be committed, but
        committing it hands every collaborator an MCP server definition that
        only works if they happen to have tokensave on PATH. Opting people in
        silently is the ruder default. Anyone who wants it shared turns the
        setting off; anyone who already committed it is unaffected, since git
        ignores nothing it is already tracking.

        Returns a suffix for the Apply message, or "" when nothing happened.
        Never raises: failing to ignore is not a reason to report a successful
        write as failed.
        """
        if os.path.basename(cfg_path).lower() != ".mcp.json":
            return ""
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        from helpers.mcp import GITIGNORE_PROJECT_MCP_KEY
        if not raw.get(GITIGNORE_PROJECT_MCP_KEY, True):
            return ""
        try:
            from helpers.gitignore import ensure_pattern
            added, detail = ensure_pattern(
                os.path.dirname(cfg_path), ".mcp.json",
                comment="# tokensave MCP binding (TokenSave Manager)")
        except Exception as exc:                             # noqa: BLE001
            return "\n\n(could not update .gitignore: %s)" % exc
        return ("\n\n" + detail) if added else ""

    def _skip(self, cfg_path: str):
        """Record "not this one", then SHOW that it was recorded.

        Two bugs lived here. It never re-rendered, so the row it was clicked on
        stayed exactly as it was; and when the path was already on the list it
        short-circuited the write and still reported success, so on an
        already-skipped project the click did nothing whatsoever and said
        nothing about it. Both read as a dead button.
        """
        raw = self._cfg.raw
        skips = (raw.get("mcp_skip_warnings") or []) \
                if isinstance(raw, dict) else []
        already = cfg_path in skips
        if not already:
            skips.append(cfg_path)
            raw["mcp_skip_warnings"] = skips
            self._cfg.save()
            self._log_to_app("MCP: skipping %s from now on." % cfg_path,
                             C["overlay0"])
        messagebox.showinfo(
            "Already skipped" if already else "Skipped",
            ("%s was already on the skip list, so nothing changed.\n\n"
             if already else
             "Won't warn about %s on startup anymore.\n\n") % cfg_path
            + "It now appears under “skipped” below, with an "
              "Un-skip button if you change your mind.",
            parent=self)
        self._render()

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
