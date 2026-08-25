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
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import UiPumpMixin, bind_mousewheel
from helpers.mcp import (
    _mcp_configs, _classify_mcp_entry, _apply_mcp_fix, _is_claude_running,
    _mcp_code_cfg_path, _project_mcp_path, effective_scope,
    remove_mcp_entry, ADVISORY_STATES, USER_SCOPE_RETIRED_KEY,
    approve_project_binding,
    _canonical_mcp_entry, annotate_project_binding,
    describe_effective, duplicate_project_keys, read_claude_projects,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class MCPConfigDialog(UiPumpMixin, tk.Toplevel):
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

    def _render_duplicate_keys(self):
        """Note — quietly — that one directory is recorded under several spellings.

        This was a ⚠ block listing every duplicate, and it dominated the top of
        the dialog while describing something that needs no action. Two reasons
        it is now collapsed and informational:

        Its severity was inherited from a claim that has since stopped being
        true. It warned that duplicates mean "two independent sets of MCP
        approvals" — but approval lives in each project's own
        `.claude/settings.local.json`, not in these keys, so the part that
        actually mattered does not depend on the spelling any more. What is
        left is the trust flag, allowed-tools and session history: a re-prompt
        at worst.

        And nothing is being asked of the user. The manager no longer mints
        duplicates, and merging existing ones means choosing whose settings
        survive — a decision for the show-diff protocol, not a status render.
        A red block with no action attached trains people to ignore red blocks.
        """
        dups = duplicate_project_keys(projects=self._claude_projects)
        if not dups:
            return

        if not self._show_dups:
            strip = tk.Frame(self._body, bg=C["base"])
            strip.pack(fill=tk.X, padx=4, pady=(8, 2))
            tk.Label(strip,
                     text="•  %d project%s recorded under more than one path "
                          "spelling in ~/.claude.json — no action needed"
                          % (len(dups), "" if len(dups) == 1 else "s"),
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"], anchor=tk.W).pack(side=tk.LEFT)
            ttk.Button(strip, text="details",
                       command=self._toggle_dups).pack(
                side=tk.LEFT, padx=(10, 0))
            return

        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(10, 2), ipady=6)
        head = tk.Frame(box, bg=C["surface0"])
        head.pack(fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(head,
                 text="•  %d project%s recorded under more than one path "
                      "spelling" % (len(dups), "" if len(dups) == 1 else "s"),
                 font=("Segoe UI", 10, "bold"),
                 bg=C["surface0"], fg=C["blue"], anchor=tk.W).pack(side=tk.LEFT)
        ttk.Button(head, text="hide", command=self._toggle_dups).pack(
            side=tk.RIGHT)
        tk.Label(box,
                 text=("  Claude Code keys per-project state by the directory "
                       "a session started in, spelled however the launcher "
                       "spelled it. This does NOT affect MCP approval — that "
                       "lives in each project's own .claude/settings.local.json. "
                       "What is split across spellings is the trust flag, "
                       "allowed-tools and session history, so an unfamiliar "
                       "spelling may re-ask the trust question once."),
                 font=("Segoe UI", 9), bg=C["surface0"], fg=C["text"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8)

        # Say which of these are nobody's real project. Most were created by
        # tools running `claude` in a directory Claude Code had not seen — this
        # manager's own status checks included — so "are these supposed to
        # exist?" has an answer, and it is mostly "no".
        from helpers.mcp import stale_duplicate_keys
        stale = stale_duplicate_keys(projects=self._claude_projects)
        n_stale = sum(len(v) for v in stale.values())
        if n_stale:
            tk.Label(
                box,
                text=("  %d of these hold no session and no settings a "
                      "sibling spelling does not already have. Those are "
                      "leftovers from something running `claude` in the "
                      "directory once — this manager's own status checks "
                      "included. Deleting one costs at most the trust "
                      "question being asked again, once, if you ever launch "
                      "a session with that exact spelling."
                      % n_stale),
                font=("Segoe UI", 9), bg=C["surface0"], fg=C["subtext"],
                justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(4, 0))

        # A fixed-height scroller rather than one Label per path. Expanded, the
        # per-Label version grew with the duplicate count until it pushed the
        # bindings off the page and scrolled its own `hide` button out of
        # reach — so the only way to close a panel needing no action was to
        # scroll back up hunting for it. Bounded here, the whole panel stays
        # about twelve lines whatever the count, and because it scrolls, every
        # group can be listed instead of truncating at six.
        listing = tk.Frame(box, bg=C["surface0"])
        listing.pack(fill=tk.X, padx=8, pady=(4, 2))
        text = tk.Text(listing, height=8, font=("Consolas", 8),
                       bg=C["mantle"], fg=C["subtext"], relief=tk.FLAT,
                       padx=8, pady=6, wrap=tk.NONE)
        bar = ttk.Scrollbar(listing, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        for group in dups.values():
            for key in group:
                text.insert(tk.END, key + "\n")
            text.insert(tk.END, "\n")
        # Swallow the wheel so it scrolls this box and not the dialog behind
        # it — the SettingsDialog rule for any nested scroller.
        text.bind("<MouseWheel>",
                  lambda e, w=text: (w.yview_scroll(
                      int(-1 * (e.delta / 120)), "units"), "break")[1])
        text.configure(state=tk.DISABLED)

        tk.Label(box,
                 text=("  The manager now launches `claude` using a spelling "
                       "Claude Code already has on file, so it no longer adds "
                       "new ones. Collapsing the existing duplicates means "
                       "deciding which side's settings survive, so it is left "
                       "to you — edit the file directly if you want them "
                       "merged. Leaving them alone is a fine answer."),
                 font=("Segoe UI", 8, "italic"),
                 bg=C["surface0"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(2, 2))
        ttk.Button(box, text="Open ~/.claude.json",
                   command=lambda: self._open_file(
                       os.path.join(os.path.expanduser("~"),
                                    ".claude.json"))).pack(
            anchor=tk.W, padx=8, pady=(2, 4))

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

    def _toggle_dups(self):
        self._show_dups = not self._show_dups
        self._render()

    def _toggle_bound(self):
        self._show_bound = not self._show_bound
        self._render()

    def _render_block(self, label: str, path: str, blocked_reason: str = "",
                      project_root: str = ""):
        info = _classify_mcp_entry(path, self._cfg.raw)
        if project_root:
            info = annotate_project_binding(
                info, project_root, projects=self._claude_projects)
        self._config_state[path] = info

        frame = tk.LabelFrame(
            self._body, text=f"  {label}  ",
            bg=C["base"], fg=C["text"],
            font=("Segoe UI", 10, "bold"),
            bd=1, relief=tk.GROOVE)
        frame.pack(fill=tk.X, padx=4, pady=(8, 4), ipady=4)

        self._render_block_header(frame, label, path, info)
        # No diff for an advisory row: the file already matches the proposal,
        # so a current-vs-proposed box would show two identical blocks and
        # imply there is an edit to make.
        if info["state"] != "ok" and info["state"] not in ADVISORY_STATES:
            self._render_block_diff(frame, info)
        self._render_block_actions(frame, label, path, info, blocked_reason,
                                   project_root)

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
        badge = tk.Label(head, text=info["label"],
                         font=("Segoe UI", 9, "bold"),
                         bg=C["base"], fg=self._badge_colour(state))
        badge.pack(side=tk.RIGHT)

        # The default only made sense for the two global configs; a project
        # binding deliberately does NOT route through the wrapper. "bound to
        # this project" is deliberately NOT claimed here any more: this row is
        # rendered from the file alone, and the file cannot know whether Claude
        # Code is serving it. The verification pass upgrades the wording once
        # something has actually been asked.
        _ok_default = ("The file binds tokensave to this project."
                       if os.path.basename(path).lower() == ".mcp.json"
                       else "No action needed — already routes through the wrapper.")
        issue_text = info["issue"] or _ok_default
        issue = tk.Label(frame, text=issue_text,
                         font=("Segoe UI", 9),
                         bg=C["base"], fg=C["overlay0"],
                         justify=tk.LEFT, wraplength=720, anchor=tk.W)
        issue.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._row_widgets[path] = (badge, issue)

    @staticmethod
    def _badge_colour(state: str) -> str:
        """Colour for a status badge.

        project_mismatch sits with the unreadable/missing cases rather than the
        drift ones: a binding pointed at ANOTHER project answers every query
        from the wrong codebase and looks entirely normal doing it.
        """
        if state == "ok":
            return C["green"]
        if state in ("direct_serve", "wrong_wrapper", "project_unbound",
                     "project_absolute") or state in ADVISORY_STATES:
            return C["peach"]
        return C["red"]

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
                              blocked_reason: str = "",
                              project_root: str = ""):
        """Apply / Skip / Open buttons and the backup-notice strip."""
        actions = tk.Frame(frame, bg=C["base"])
        actions.pack(fill=tk.X, padx=8, pady=(2, 4))

        # An advisory row has nothing to apply, so a "binding is blocked"
        # notice would name a prerequisite for work that is already done.
        if (blocked_reason and info["state"] != "ok"
                and info["state"] not in ADVISORY_STATES):
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

        if info["state"] == "ok" or info["state"] in ADVISORY_STATES:
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(side=tk.LEFT)
            # The retired row is the one "ok" state a user might want to leave,
            # and the flag that produces it has no other exit from the UI.
            if "retired" in info.get("label", ""):
                ttk.Button(actions, text="Re-add user-scoped entry…",
                           command=self._unretire_user_scoped).pack(
                    side=tk.LEFT, padx=(8, 0))
            # An unapproved row is the one advisory state the manager can
            # actually resolve itself, rather than only explaining.
            if info["state"] == "project_unapproved" and project_root:
                ttk.Button(
                    actions, text="Approve this binding",
                    style="Primary.TButton",
                    command=lambda r=project_root: self._approve_one(r)).pack(
                    side=tk.LEFT, padx=(8, 0))
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
