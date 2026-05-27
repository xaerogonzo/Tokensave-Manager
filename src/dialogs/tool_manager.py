"""ToolManagerDialog — unified install/update/uninstall lifecycle for the
code-graph tools (v4.8).

Opened from Settings → tokensave or CodeGraph section ('🛠️ Open Tool
Manager…' shortcuts) OR from the Help tab header ('💾 Tool Manager…').

Per-tool row layout:
  * Binary status (path + version + latest-available comparison)
  * MCP wiring status
  * Three action buttons: Install / Update / Uninstall

Cascading uninstall (G-D non-fatal at MCP step):
  1. MCP cleanup via the tool's own ``uninstall`` subcommand.
     Failure → log warning, CONTINUE.
  2. Binary removal (npm uninstall for codegraph; shutil.rmtree of the
     manager-owned dir for tokensave-when-manager-installed).
     Failure → log error and bail.
  3. Always clear ``cfg.raw[<tool>_exe]`` + ``cfg.save()`` (G-F).
  4. Refresh row state.

Concurrency (G-G): every action handler synchronously calls
``_set_row_busy(tool_id, True)`` BEFORE spawning the worker thread, and
the worker's ``finally`` always restores button state via
``self.after(0, …)``.

Discovery surfaces:
  * Settings → tokensave section: '🛠️ Open Tool Manager…' button
  * Settings → CodeGraph section: '🛠️ Open Tool Manager…' button
  * Help tab header: '💾 Tool Manager…' button
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW
from helpers.detection import _detect_codegraph, _detect_npm
from helpers.install_codegraph import (
    codegraph_version,
    detect_codegraph_after_install,
    install_codegraph,
    uninstall_codegraph,
    update_codegraph,
)
from helpers.install_tokensave import (
    install_tokensave_via_download,
    is_manager_installed,
    manager_install_dir,
    releases_human_url,
)
from helpers.mcp import _claude_code_mcp_has_codegraph

if TYPE_CHECKING:
    from state import ManagerConfig


_TOKENSAVE_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")


def _tokensave_version(exe: str) -> str:
    """Quick `tokensave --version` probe; '' on any error."""
    if not exe or not os.path.isfile(exe):
        return ""
    try:
        r = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    m = _TOKENSAVE_VERSION_RE.search((r.stdout or "").strip())
    return m.group(1) if m else ""


def _persist_cfg_clear(cfg, key: str) -> None:
    """Clear a cfg key and save — used by both uninstall paths (G-F).

    Wrapping the clear+save pair in a single helper makes the discipline
    visible at every callsite: cfg.raw[key] = "" PLUS cfg.save() is
    the contract, not just the mutation.
    """
    try:
        cfg.raw[key] = ""
        cfg.save()
    except Exception:
        pass


class ToolManagerDialog(tk.Toplevel):
    """Install / Update / Uninstall lifecycle dialog for tokensave + codegraph."""

    def __init__(self, parent, cfg: "ManagerConfig") -> None:
        super().__init__(parent)
        self._parent = parent
        self._cfg = cfg
        self.title("💾 Tool Manager — TokenSave Manager")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 540)
        self.grab_set()

        # Per-tool widget bookkeeping (populated by _build_tool_row).
        self._tool_widgets: dict = {}
        # G-G concurrency: True while a worker is running for this tool.
        self._row_busy: dict = {"tokensave": False, "codegraph": False}

        self._build_header()
        self._build_action_bar()       # bottom, packed first
        self._build_log_pane()         # also packed bottom
        self._build_tool_row("tokensave", "tokensave",
                             "Code-graph indexer (per-project)")
        ttk.Separator(self, orient="horizontal").pack(
            fill=tk.X, padx=18, pady=(8, 8))
        self._build_tool_row("codegraph", "CodeGraph",
                             "Alternative code-graph tool")

        self._refresh_state()
        self._centre_on_parent(parent)

    # ── Section builders ──────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="💾  Tool Manager",
            bg=C["surface0"], fg=C["blue"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=("Install, update, or uninstall the code-graph tools "
                  "from one place. Each tool has its own row below with "
                  "current version + MCP-wiring status + the three "
                  "lifecycle actions. Uninstall is cascading — it strips "
                  "MCP wiring first, then removes the binary."),
            bg=C["surface0"], fg=C["text"], font=("Segoe UI", 9),
            wraplength=600, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        """Close button — packed BOTTOM before the rest (sticky-footer)."""
        ttk.Separator(self, orient="horizontal").pack(
            fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bar, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _build_log_pane(self) -> None:
        wrap = tk.LabelFrame(
            self, text="Output",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        wrap.pack(fill=tk.X, side=tk.BOTTOM, padx=18, pady=(4, 4))
        self._log_txt = tk.Text(
            wrap, height=8, font=("Consolas", 8),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, state=tk.DISABLED,
        )
        self._log_txt.pack(fill=tk.X, padx=8, pady=(6, 8))

    def _build_tool_row(self, tool_id: str, label: str, subtitle: str) -> None:
        wrap = tk.LabelFrame(
            self, text=f"{label}  —  {subtitle}",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.X, padx=18, pady=(8, 0))

        # Status rows (binary + MCP)
        bin_lbl = tk.Label(
            wrap, text="(checking…)",
            bg=C["base"], fg=C["text"],
            font=("Consolas", 9), anchor=tk.W, justify=tk.LEFT,
            wraplength=580,
        )
        bin_lbl.pack(anchor=tk.W, padx=12, pady=(6, 0))
        mcp_lbl = tk.Label(
            wrap, text="(checking…)",
            bg=C["base"], fg=C["text"],
            font=("Consolas", 9), anchor=tk.W, justify=tk.LEFT,
            wraplength=580,
        )
        mcp_lbl.pack(anchor=tk.W, padx=12, pady=(0, 4))

        # Action button row
        btn_row = tk.Frame(wrap, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=12, pady=(2, 8))
        install_btn = ttk.Button(
            btn_row, text="Install",
            command=lambda t=tool_id: self._on_install(t))
        install_btn.pack(side=tk.LEFT, padx=(0, 6))
        update_btn = ttk.Button(
            btn_row, text="Update",
            command=lambda t=tool_id: self._on_update(t))
        update_btn.pack(side=tk.LEFT, padx=(0, 6))
        uninstall_btn = ttk.Button(
            btn_row, text="Uninstall",
            command=lambda t=tool_id: self._on_uninstall(t))
        uninstall_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._tool_widgets[tool_id] = {
            "wrap":         wrap,
            "bin_lbl":      bin_lbl,
            "mcp_lbl":      mcp_lbl,
            "install_btn":  install_btn,
            "update_btn":   update_btn,
            "uninstall_btn": uninstall_btn,
        }

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 720, 640
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── State refresh ─────────────────────────────────────────────────────────

    def _refresh_state(self) -> None:
        """Single source of truth: read current binary + MCP state, update
        each row's labels + button enablement."""
        # ── tokensave ─────────────────────────────────────────────────────
        ts_exe = self._cfg.tokensave_exe
        ts_installed = bool(ts_exe and os.path.isfile(ts_exe))
        if ts_installed:
            ver = _tokensave_version(ts_exe) or "(version unknown)"
            mgmt = "manager-managed" if is_manager_installed(ts_exe) else "external"
            ts_bin_text = f"✓  v{ver} at {ts_exe}  ({mgmt})"
            ts_bin_fg = C["green"]
        else:
            ts_bin_text = "✗  not installed"
            ts_bin_fg = C["red"]
        # tokensave MCP status: easier to summarise via the same Claude
        # Code config — we already classify tokensave entries via
        # helpers/mcp.py's existing tokensave classifier surface, but
        # for this dialog a simple presence check is sufficient and
        # avoids reaching into the classifier's tokensave-wrapper
        # validation machinery.
        ts_mcp_wired = self._tokensave_mcp_wired()
        ts_mcp_text = (
            "✓  MCP wired for Claude Code"
            if ts_mcp_wired else
            "✗  MCP not wired for Claude Code"
        )
        ts_mcp_fg = C["green"] if ts_mcp_wired else C["overlay0"]
        self._apply_row_state(
            "tokensave", ts_installed,
            ts_bin_text, ts_bin_fg, ts_mcp_text, ts_mcp_fg)

        # ── codegraph ─────────────────────────────────────────────────────
        cg_exe = self._cfg.codegraph_exe or _detect_codegraph()
        cg_installed = bool(cg_exe and os.path.isfile(cg_exe))
        if cg_installed:
            ver = codegraph_version(cg_exe) or "(version unknown)"
            cg_bin_text = f"✓  v{ver} at {cg_exe}"
            cg_bin_fg = C["green"]
        else:
            cg_bin_text = "✗  not installed"
            cg_bin_fg = C["red"]
        cg_mcp_wired, cg_key = _claude_code_mcp_has_codegraph()
        cg_mcp_text = (
            f"✓  MCP wired for Claude Code (mcpServers.{cg_key})"
            if cg_mcp_wired else
            "✗  MCP not wired for Claude Code"
        )
        cg_mcp_fg = C["green"] if cg_mcp_wired else C["overlay0"]
        self._apply_row_state(
            "codegraph", cg_installed,
            cg_bin_text, cg_bin_fg, cg_mcp_text, cg_mcp_fg)

    def _tokensave_mcp_wired(self) -> bool:
        """Lightweight presence-check for the tokensave MCP entry in
        ~/.claude.json. Mirrors _claude_code_mcp_has_codegraph's shape
        but for tokensave; defers to a literal-key/command match rather
        than the full classifier (which is wrapper-aware)."""
        import json
        p = os.path.expanduser("~/.claude.json")
        if not os.path.isfile(p):
            return False
        try:
            with open(p, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False
        servers = (cfg or {}).get("mcpServers") or {}
        for key, entry in servers.items():
            if isinstance(key, str) and "tokensave" in key.lower():
                return True
            if isinstance(entry, dict):
                cmd = entry.get("command")
                if isinstance(cmd, str) and "tokensave" in cmd.lower():
                    return True
        return False

    def _apply_row_state(self, tool_id: str, installed: bool,
                         bin_text: str, bin_fg: str,
                         mcp_text: str, mcp_fg: str) -> None:
        widgets = self._tool_widgets[tool_id]
        widgets["bin_lbl"].configure(text=bin_text, fg=bin_fg)
        widgets["mcp_lbl"].configure(text=mcp_text, fg=mcp_fg)
        busy = self._row_busy[tool_id]
        widgets["install_btn"].configure(
            state=tk.NORMAL if (not installed and not busy) else tk.DISABLED)
        widgets["update_btn"].configure(
            state=tk.NORMAL if (installed and not busy) else tk.DISABLED)
        widgets["uninstall_btn"].configure(
            state=tk.NORMAL if (installed and not busy) else tk.DISABLED)

    def _set_row_busy(self, tool_id: str, busy: bool, label: str = "") -> None:
        """G-G: synchronously disable all action buttons + optionally retitle.

        Called BEFORE spawning a worker thread so a double-click can't
        spawn a second worker. The worker's ``finally`` block always
        calls this with busy=False on completion (success or exception).
        """
        self._row_busy[tool_id] = busy
        widgets = self._tool_widgets[tool_id]
        for k in ("install_btn", "update_btn", "uninstall_btn"):
            widgets[k].configure(state=tk.DISABLED if busy else tk.NORMAL)
        if busy and label:
            # Re-title the active button to make the in-flight action obvious.
            # (Best-effort — purely cosmetic.)
            for k in ("install_btn", "update_btn", "uninstall_btn"):
                if k.startswith(label.lower().rstrip("…")[:3]):
                    widgets[k].configure(text=f"{label}…")
                    break
        elif not busy:
            widgets["install_btn"].configure(text="Install")
            widgets["update_btn"].configure(text="Update")
            widgets["uninstall_btn"].configure(text="Uninstall")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log(self, line: str) -> None:
        try:
            self._log_txt.configure(state=tk.NORMAL)
            self._log_txt.insert(tk.END, line + "\n")
            self._log_txt.see(tk.END)
            self._log_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def _log_threadsafe(self, line: str) -> None:
        try:
            self.after(0, lambda l=line: self._log(l))
        except tk.TclError:
            pass

    # ── Action dispatchers ────────────────────────────────────────────────────

    def _on_install(self, tool_id: str) -> None:
        if tool_id == "tokensave":
            self._install_tokensave()
        elif tool_id == "codegraph":
            self._install_codegraph()

    def _on_update(self, tool_id: str) -> None:
        if tool_id == "tokensave":
            self._update_tokensave()
        elif tool_id == "codegraph":
            self._update_codegraph()

    def _on_uninstall(self, tool_id: str) -> None:
        if tool_id == "tokensave":
            self._uninstall_tokensave()
        elif tool_id == "codegraph":
            self._uninstall_codegraph()

    # ── Codegraph lifecycle ───────────────────────────────────────────────────

    def _install_codegraph(self) -> None:
        npm = _detect_npm()
        if not npm:
            messagebox.showerror(
                "npm not found",
                "npm is required to install codegraph. Install Node.js "
                "18+ first (https://nodejs.org) and reopen Tool Manager.",
                parent=self)
            return
        # G-G: disable BEFORE spawning the worker.
        self._set_row_busy("codegraph", True, "Install")
        self._log("--- codegraph: install ---")

        def _worker():
            try:
                ok, _ = install_codegraph(npm, on_log=self._log_threadsafe)
                if ok:
                    # G-B: use the fallback chain to find the new binary.
                    found = detect_codegraph_after_install(npm)
                    if found:
                        self._cfg.raw["codegraph_exe"] = found
                        try:
                            self._cfg.save()
                        except Exception:
                            pass
                        self._log_threadsafe(f"✓ codegraph_exe set to {found}")
                    else:
                        self._log_threadsafe(
                            "⚠ Install succeeded but binary not yet on PATH. "
                            "Restart the manager so PATH refreshes, then "
                            "click Check again in Settings.")
                else:
                    self._log_threadsafe(
                        "✗ codegraph install failed — see output above.")
            finally:
                self.after(0, lambda: self._set_row_busy("codegraph", False))
                self.after(0, self._refresh_state)

        threading.Thread(target=_worker, daemon=True).start()

    def _update_codegraph(self) -> None:
        npm = _detect_npm()
        if not npm:
            messagebox.showerror("npm not found",
                                 "npm not detected.", parent=self)
            return
        self._set_row_busy("codegraph", True, "Update")
        self._log("--- codegraph: update ---")

        def _worker():
            try:
                ok, _ = update_codegraph(npm, on_log=self._log_threadsafe)
                if not ok:
                    self._log_threadsafe("✗ Update failed.")
            finally:
                self.after(0, lambda: self._set_row_busy("codegraph", False))
                self.after(0, self._refresh_state)

        threading.Thread(target=_worker, daemon=True).start()

    def _uninstall_codegraph(self) -> None:
        npm = _detect_npm()
        if not npm:
            messagebox.showerror("npm not found",
                                 "npm not detected.", parent=self)
            return
        cg_exe = self._cfg.codegraph_exe or _detect_codegraph()
        if not cg_exe or not os.path.isfile(cg_exe):
            messagebox.showerror("Not installed",
                                 "codegraph is not currently installed.",
                                 parent=self)
            return
        if not messagebox.askyesno(
                "Uninstall CodeGraph?",
                "This will:\n"
                "  1. Remove CodeGraph from your AI agents' MCP servers\n"
                "  2. Uninstall the codegraph npm package\n"
                "  3. Clear the codegraph_exe path in Settings\n\n"
                "Per-project .codegraph/ indexes stay where they are — "
                "delete them manually if you also want to free that "
                "disk space.",
                parent=self, default="no"):
            return
        self._set_row_busy("codegraph", True, "Uninstall")
        self._log("--- codegraph: uninstall (cascading) ---")

        def _worker():
            try:
                # G-D step 1: MCP cleanup — NON-FATAL
                wired, _key = _claude_code_mcp_has_codegraph()
                if wired:
                    self._log_threadsafe(
                        "→ Stripping codegraph from AI-agent MCP configs…")
                    try:
                        r = subprocess.run(
                            [cg_exe, "uninstall"],
                            capture_output=True, text=True, timeout=60,
                            creationflags=CREATE_NO_WINDOW,
                            encoding="utf-8", errors="replace")
                        if r.returncode != 0:
                            self._log_threadsafe(
                                f"⚠ MCP cleanup failed (rc={r.returncode}): "
                                f"{(r.stdout or r.stderr or '').strip()[:200]}\n"
                                "  Continuing with binary uninstall — you "
                                "may need to manually remove the codegraph "
                                "entry from ~/.claude.json afterwards.")
                        else:
                            self._log_threadsafe("✓ MCP cleanup complete.")
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        self._log_threadsafe(
                            f"⚠ MCP cleanup error: {exc}. Continuing.")
                # G-D step 2: binary uninstall — FATAL
                self._log_threadsafe("→ Removing the codegraph npm package…")
                ok, _ = uninstall_codegraph(npm, on_log=self._log_threadsafe)
                if not ok:
                    self._log_threadsafe(
                        "✗ Binary uninstall failed. Stopping; "
                        "codegraph_exe path NOT cleared.")
                    return
                # G-F: explicit save
                _persist_cfg_clear(self._cfg, "codegraph_exe")
                self._log_threadsafe("✓ Uninstall complete.")
            finally:
                self.after(0, lambda: self._set_row_busy("codegraph", False))
                self.after(0, self._refresh_state)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Tokensave lifecycle ───────────────────────────────────────────────────

    def _install_tokensave(self) -> None:
        self._set_row_busy("tokensave", True, "Install")
        self._log("--- tokensave: install (GitHub-release download) ---")

        def _worker():
            try:
                ok, result = install_tokensave_via_download(
                    on_log=self._log_threadsafe)
                if ok:
                    # `result` is the path to tokensave.exe
                    self._cfg.raw["tokensave_exe"] = result
                    try:
                        self._cfg.save()
                    except Exception:
                        pass
                    self._log_threadsafe(
                        f"✓ tokensave_exe set to {result}")
                else:
                    # G-E friendly rate-limit message
                    if result == "rate_limit":
                        self.after(0, lambda: messagebox.showwarning(
                            "GitHub rate limit",
                            "GitHub is rate-limiting this IP "
                            "(anonymous 60 requests/hour).\n\n"
                            "Wait an hour or download manually from:\n\n"
                            f"  {releases_human_url()}",
                            parent=self))
                    elif result == "zip_slip":
                        self.after(0, lambda: messagebox.showerror(
                            "Refused to extract archive",
                            "The downloaded archive contained a "
                            "suspicious member (parent traversal or "
                            "absolute path). Install aborted as a safety "
                            "measure. Try again later — this typically "
                            "indicates an upstream issue, not a real "
                            "attack.",
                            parent=self))
                    elif result == "not_a_zip":
                        self.after(0, lambda: messagebox.showwarning(
                            "Bad download",
                            "Downloaded file is not a valid zip "
                            "(possibly a transient GitHub error). "
                            "Try again in a few moments.",
                            parent=self))
                    else:
                        self._log_threadsafe(
                            f"✗ Install failed: {result}")
            finally:
                self.after(0, lambda: self._set_row_busy("tokensave", False))
                self.after(0, self._refresh_state)

        threading.Thread(target=_worker, daemon=True).start()

    def _update_tokensave(self) -> None:
        ts_exe = self._cfg.tokensave_exe
        if not ts_exe or not os.path.isfile(ts_exe):
            messagebox.showerror("Not installed",
                                 "tokensave is not currently installed.",
                                 parent=self)
            return
        self._set_row_busy("tokensave", True, "Update")
        self._log("--- tokensave: upgrade ---")

        def _worker():
            try:
                # tokensave has its own self-upgrade command — reuse it
                # rather than re-downloading from GitHub.
                try:
                    proc = subprocess.Popen(
                        [ts_exe, "upgrade"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=CREATE_NO_WINDOW,
                    )
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            self._log_threadsafe(line.rstrip())
                    proc.wait(timeout=300)
                    ok = proc.returncode == 0
                except subprocess.TimeoutExpired:
                    proc.kill()
                    ok = False
                    self._log_threadsafe("[timed out after 300s]")
                except (FileNotFoundError, OSError) as exc:
                    ok = False
                    self._log_threadsafe(f"launch error: {exc}")
                if not ok:
                    self._log_threadsafe("✗ Upgrade failed.")
            finally:
                self.after(0, lambda: self._set_row_busy("tokensave", False))
                self.after(0, self._refresh_state)

        threading.Thread(target=_worker, daemon=True).start()

    def _uninstall_tokensave(self) -> None:
        ts_exe = self._cfg.tokensave_exe
        if not ts_exe or not os.path.isfile(ts_exe):
            messagebox.showerror("Not installed",
                                 "tokensave is not currently installed.",
                                 parent=self)
            return
        mgmt = is_manager_installed(ts_exe)
        msg = (
            "This will:\n"
            "  1. Remove tokensave from your AI agents' MCP servers\n"
            "  2. " + ("Delete the manager-managed binary at "
                         f"{ts_exe}\n" if mgmt else
                         "NOT delete the binary (it's outside the "
                         "manager-owned dir — delete it yourself if "
                         "you want)\n") +
            "  3. Clear the tokensave_exe path in Settings\n\n"
            "Per-project .tokensave/ indexes stay where they are."
        )
        if not messagebox.askyesno(
                "Uninstall tokensave?", msg,
                parent=self, default="no"):
            return
        self._set_row_busy("tokensave", True, "Uninstall")
        self._log("--- tokensave: uninstall (cascading) ---")

        def _worker():
            try:
                # G-D step 1: MCP cleanup via tokensave's own uninstall — NON-FATAL
                self._log_threadsafe(
                    "→ Stripping tokensave from AI-agent MCP configs…")
                try:
                    r = subprocess.run(
                        [ts_exe, "uninstall"],
                        capture_output=True, text=True, timeout=60,
                        creationflags=CREATE_NO_WINDOW,
                        encoding="utf-8", errors="replace")
                    if r.returncode != 0:
                        self._log_threadsafe(
                            f"⚠ MCP cleanup failed (rc={r.returncode}): "
                            f"{(r.stdout or r.stderr or '').strip()[:200]}\n"
                            "  Continuing with binary removal.")
                    else:
                        self._log_threadsafe("✓ MCP cleanup complete.")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self._log_threadsafe(
                        f"⚠ MCP cleanup error: {exc}. Continuing.")

                # G-D step 2: binary removal — FATAL only if manager-owned
                if mgmt:
                    self._log_threadsafe(
                        f"→ Removing manager-managed binary dir "
                        f"{manager_install_dir()}…")
                    try:
                        shutil.rmtree(manager_install_dir(),
                                       ignore_errors=False)
                        self._log_threadsafe("✓ Binary directory removed.")
                    except OSError as exc:
                        self._log_threadsafe(
                            f"✗ Could not remove binary dir: {exc}")
                        return
                else:
                    self._log_threadsafe(
                        f"→ Binary at {ts_exe} is outside the "
                        "manager-owned dir — not deleted. Clearing "
                        "cfg path only.")
                # G-F: explicit save
                _persist_cfg_clear(self._cfg, "tokensave_exe")
                self._log_threadsafe("✓ Uninstall complete.")
            finally:
                self.after(0, lambda: self._set_row_busy("tokensave", False))
                self.after(0, self._refresh_state)

        threading.Thread(target=_worker, daemon=True).start()
