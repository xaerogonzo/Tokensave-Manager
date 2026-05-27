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

    def __init__(self, parent, cfg: "ManagerConfig"):
        super().__init__(parent)
        self._cfg = cfg
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

    def _render_block(self, label: str, path: str):
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
        self._render_block_actions(frame, label, path, info)

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
        badge_colour = (C["green"] if state == "ok"
                        else C["peach"] if state in ("direct_serve", "wrong_wrapper")
                        else C["red"])
        tk.Label(head, text=info["label"],
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=badge_colour).pack(side=tk.RIGHT)

        issue_text = info["issue"] or "No action needed — already routes through the wrapper."
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

    def _render_block_actions(self, frame, label: str, path: str, info: dict):
        """Apply / Skip / Open buttons and the backup-notice strip."""
        actions = tk.Frame(frame, bg=C["base"])
        actions.pack(fill=tk.X, padx=8, pady=(2, 4))

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
                  "Other mcpServers entries in this file are preserved verbatim."),
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
