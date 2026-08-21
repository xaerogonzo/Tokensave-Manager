"""RemoteSetupDialog — the first-push wizard, for any forge.

Walks a first-time user through: git identity → forge account → create repo →
set remote URL → first push → optional release. Each step shows a live status
indicator (✅ / ⬜ / ℹ️) read from the project's current git state.

This is ``dialogs/github_setup.py`` with the GitHub-specific parts lifted out
into ``helpers/remote_providers.py``. The dialog asks the provider what to
say and which command to run; it contains no ``if provider == ...``. Adding a
forge is an entry in that registry, not a branch in here — which is the whole
reason the split was worth doing, since the alternative smears three-way
branching across every one of these builders.

``GitHubSetupDialog`` remains as a thin subclass so existing callers are
untouched, and the argv it produces is pinned by
``tests/test_remote_providers.py``.

Takes ``cfg: ManagerConfig`` and reads ``cfg.git_exe`` at execution time, so a
Settings → Save propagates without reopening (Rule 3 — never snapshot a cfg
field in ``__init__``).
"""

from __future__ import annotations

import os
import shutil
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import bind_mousewheel
from helpers.remote_providers import (
    AUTH_CREDENTIAL_HELPER,
    GITHUB,
    RemoteProvider,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class RemoteSetupDialog(tk.Toplevel):
    """Step-by-step setup wizard for one git forge."""

    def __init__(self, parent, path: str, cfg: "ManagerConfig",
                 provider: "RemoteProvider | None" = None):
        super().__init__(parent)
        self._app = parent      # App — gives _shell_capture / _log / _git
        self._path = path
        self._cfg = cfg
        self._provider = provider or GITHUB
        self.title("%s Setup" % self._provider.display_name)
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(480, 500)
        self.grab_set()

        self._name_var = tk.StringVar()
        self._email_var = tk.StringVar()
        self._remote_var = tk.StringVar()
        self._tag_var = tk.StringVar(value="v1.0.0")
        self._rel_title_var = tk.StringVar(value="Release")

        # Scrollable area: canvas + scrollbar wrap the body Frame.
        # body is a child of self (not canvas) — keeps Windows rendering happy.
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
        bind_mousewheel(self._canvas)
        _vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._body = tk.Frame(self, bg=C["base"])
        self._body_id = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._body_id, width=e.width))
        self._body.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

        try:
            self._build()
            self._refresh()
        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            messagebox.showerror(
                "%s Setup — build error" % self._provider.display_name,
                f"The wizard failed to render:\n\n{ex}\n\n{tb[-800:]}",
                parent=self)

        self.update_idletasks()
        # Open at content height, but never taller than parent window.
        content_h = self._body.winfo_reqheight() + 20
        max_h = max(400, parent.winfo_height() - 60)
        w, h = 520, min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width() - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── shell helper (fast config/remote queries — main-thread OK) ────────

    def _sh(self, cmd) -> tuple:
        return self._app._shell_capture(cmd, self._path)

    # ── build ────────────────────────────────────────────────────────────

    def _build(self):
        body = self._body
        self._build_header_section(body)
        self._build_step1_identity_section(body)
        self._build_step2_signin_section(body)
        self._build_step3_repo_section(body)
        self._build_step4_remote_section(body)
        self._build_step5_push_section(body)
        self._build_releases_section(body)
        self._build_close_section(body)

    def _build_header_section(self, body):
        P = dict(padx=20)
        tk.Label(body, text="%s  %s Setup" % (self._provider.icon,
                                              self._provider.display_name),
                 font=("Segoe UI", 12, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, pady=(16, 2), **P)
        tk.Label(body, text=os.path.basename(self._path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6), **P)
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20,
                                                      pady=(0, 10))

    def _build_step1_identity_section(self, body):
        """Step 1 — git identity. The one step no provider changes."""
        self._s1_icon = self._step_header(
            body, "1", "Your name & email  (shown on every commit)")
        id_frame = tk.Frame(body, bg=C["surface0"], padx=10, pady=8)
        id_frame.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        for lbl_text, var in (("Name:", self._name_var),
                              ("Email:", self._email_var)):
            row = tk.Frame(id_frame, bg=C["surface0"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=lbl_text, width=7, anchor=tk.W,
                     bg=C["surface0"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=30,
                      font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(id_frame, text="Save Identity",
                   command=self._save_identity).pack(anchor=tk.W, pady=(6, 0))

    def _build_step2_signin_section(self, body):
        name = self._provider.display_name
        self._s2_icon = self._step_header(
            body, "2", "Sign in to %s  (or create a free account)" % name)
        row = tk.Frame(body, bg=C["base"])
        row.pack(anchor=tk.W, padx=(44, 20), pady=(2, 4))
        ttk.Button(row, text="Sign in to %s →" % name,
                   command=lambda: self._open(self._provider.login_url)).pack(
            side=tk.LEFT, padx=(0, 8))
        ttk.Button(row, text="Create free account →",
                   command=lambda: self._open(self._provider.signup_url)).pack(
            side=tk.LEFT)
        hint = ("If you already have an account, just sign in — no need to "
                "create one.")
        if self._provider.auth_strategy == AUTH_CREDENTIAL_HELPER:
            # Worth saying plainly: this forge has no CLI here, so pushing
            # relies on git's credential helper rather than a login button.
            hint = self._provider.signin_hint
        tk.Label(body, text=hint, font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"], wraplength=420,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=(44, 20), pady=(0, 10))

    def _build_step3_repo_section(self, body):
        self._s3_icon = self._step_header(
            body, "3", "Create a new repository on %s"
                       % self._provider.display_name)
        sec = tk.Frame(body, bg=C["base"])
        sec.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(sec, text=self._provider.repo_advice,
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        ttk.Button(sec, text="Open %s →" % self._short_url(
            self._provider.new_repo_url),
            command=lambda: self._open(self._provider.new_repo_url)).pack(
            anchor=tk.W)

    def _build_step4_remote_section(self, body):
        self._s4_icon = self._step_header(
            body, "4", "Paste your repository URL here")
        sec = tk.Frame(body, bg=C["base"])
        sec.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        url_row = tk.Frame(sec, bg=C["base"])
        url_row.pack(fill=tk.X)
        ttk.Entry(url_row, textvariable=self._remote_var, width=34,
                  font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(url_row, text="Set",
                   command=self._set_remote).pack(side=tk.LEFT)
        tk.Label(sec, text="e.g. %s" % self._provider.example_remote_url,
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(4, 0))

    def _build_step5_push_section(self, body):
        name = self._provider.display_name
        self._s5_icon = self._step_header(
            body, "5", "Upload your code to %s" % name)
        sec = tk.Frame(body, bg=C["base"])
        sec.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(sec,
                 text=("This sends all your commits to %s. The first time, a\n"
                       "browser window will open asking you to log in — that's\n"
                       "normal. After that, pushes happen silently." % name),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        self._push_btn = ttk.Button(sec, text="⬆  Push to %s" % name,
                                    command=self._do_push)
        self._push_btn.pack(anchor=tk.W)

    def _build_releases_section(self, body):
        provider = self._provider
        if not provider.release_support:
            return          # nothing honest to offer — see CODEBERG
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20,
                                                      pady=(10, 10))
        tk.Label(body, text="📦  %s %ss  (share your built .exe)"
                            % (provider.display_name, provider.release_noun),
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["peach"]).pack(anchor=tk.W, padx=20,
                                                   pady=(0, 4))
        tk.Label(body,
                 text=("A release lets anyone download your .exe without "
                       "needing Python.\nBuild dist\\ first (run build.bat), "
                       "then tag a release here."),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))
        if shutil.which(provider.cli_name):
            self._build_releases_form(body)
        else:
            self._build_releases_install_prompt(body)

    def _build_releases_form(self, body):
        grid = tk.Frame(body, bg=C["base"])
        grid.pack(anchor=tk.W, padx=20, pady=(0, 6))
        for col, (lbl, var, w) in enumerate([("Tag:", self._tag_var, 9),
                                             ("Title:", self._rel_title_var, 22)]):
            tk.Label(grid, text=lbl, bg=C["base"], fg=C["text"],
                     font=("Segoe UI", 9)).grid(
                row=0, column=col * 2, sticky=tk.W,
                padx=(0 if col == 0 else 12, 4))
            ttk.Entry(grid, textvariable=var, width=w,
                      font=("Segoe UI", 9)).grid(row=0, column=col * 2 + 1,
                                                 sticky=tk.W)
        ttk.Button(body, text="📦  Create %s" % self._provider.release_noun,
                   command=self._create_release).pack(anchor=tk.W, padx=20,
                                                      pady=(0, 4))

    def _build_releases_install_prompt(self, body):
        provider = self._provider
        tk.Label(body,
                 text="Install %s to enable one-click releases from here:"
                      % provider.cli_display_name,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["text"]).pack(anchor=tk.W, padx=20)
        ttk.Button(body, text="Get %s →" % provider.cli_display_name,
                   command=lambda: self._open(provider.cli_download_url)).pack(
            anchor=tk.W, padx=20, pady=(4, 4))
        tk.Label(body,
                 text="After installing, re-open this dialog to enable releases.",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

    def _build_close_section(self, body):
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20,
                                                      pady=(10, 10))
        ttk.Button(body, text="Close", command=self.destroy).pack(
            anchor=tk.E, padx=20, pady=(0, 16))

    def _step_header(self, parent, num: str, text: str) -> tk.Label:
        """Numbered step row — returns the icon label so caller can update it."""
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, padx=20, pady=(0, 2))
        icon = tk.Label(row, text="⬜", bg=C["base"], font=("Segoe UI", 10))
        icon.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=f"Step {num} — {text}",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        return icon

    @staticmethod
    def _short_url(url: str) -> str:
        return url.split("://", 1)[-1]

    @staticmethod
    def _open(url: str) -> None:
        if not url:
            return
        try:
            os.startfile(url)                       # noqa: S606 (Windows)
        except (AttributeError, OSError):
            import webbrowser
            webbrowser.open(url)

    # ── live state ───────────────────────────────────────────────────────

    def _refresh(self):
        git = self._cfg.git_exe
        name_out, _ = self._sh([git, "config", "--global", "user.name"])
        email_out, _ = self._sh([git, "config", "--global", "user.email"])
        name, email = name_out.strip(), email_out.strip()
        if not self._name_var.get():
            self._name_var.set(name)
        if not self._email_var.get():
            self._email_var.set(email)
        self._s1_icon.config(text="✅" if (name and email) else "⚠️")

        # Steps 2 and 3 happen in a browser; nothing local to detect.
        self._s2_icon.config(text="ℹ️")
        self._s3_icon.config(text="ℹ️")

        remote_out, rrc = self._sh(
            [git, "-C", self._path, "remote", "get-url", "origin"])
        remote = remote_out.strip() if rrc == 0 else ""
        self._remote_var.set(remote)
        self._s4_icon.config(text="✅" if remote else "⬜")

        self._s5_icon.config(text="⬜")
        self._push_btn.config(state=tk.NORMAL if remote else tk.DISABLED)

    # ── actions ──────────────────────────────────────────────────────────

    def _save_identity(self):
        name = self._name_var.get().strip()
        email = self._email_var.get().strip()
        if not name or not email:
            messagebox.showwarning(
                "Incomplete", "Please enter both a name and an email address.",
                parent=self)
            return
        git = self._cfg.git_exe
        self._sh([git, "config", "--global", "user.name", name])
        self._sh([git, "config", "--global", "user.email", email])
        self._refresh()
        messagebox.showinfo(
            "Identity saved",
            "Git will now sign commits as:\n%s <%s>" % (name, email),
            parent=self)

    def _set_remote(self):
        url = self._remote_var.get().strip()
        ok, reason = self._provider.validate_remote_url(url)
        if not ok:
            messagebox.showwarning("Check the URL", reason, parent=self)
            return
        git = self._cfg.git_exe
        _, rrc = self._sh([git, "-C", self._path, "remote", "get-url", "origin"])
        if rrc == 0:
            self._sh([git, "-C", self._path, "remote", "set-url", "origin", url])
            self._app._log("  Remote updated: %s" % url, C["green"])
        else:
            self._sh([git, "-C", self._path, "remote", "add", "origin", url])
            self._app._log("  Remote added: %s" % url, C["green"])
        self._refresh()
        self._app.after(0, self._app._git.refresh)

    def _do_push(self):
        self.destroy()
        self._app._git.set_active_path(self._path)
        self._app._git.cmd_git_push()

    def _create_release(self):
        tag = self._tag_var.get().strip()
        title = self._rel_title_var.get().strip() or tag
        if not tag:
            messagebox.showwarning("No tag", "Enter a version tag, e.g. v1.0.0",
                                   parent=self)
            return
        dist_dir = os.path.join(self._path, "dist")
        exe_files = []
        if os.path.isdir(dist_dir):
            exe_files = [os.path.join(dist_dir, f)
                         for f in os.listdir(dist_dir) if f.endswith(".exe")]
        if not exe_files:
            if not messagebox.askyesno(
                    "No .exe files found",
                    "No .exe files found in dist\\\n\n"
                    "Run build.bat first to compile them.\n\n"
                    "Create a release without uploading any files anyway?",
                    parent=self):
                return
        cmd = self._provider.create_release_argv(tag, title, exe_files)
        if not cmd:
            messagebox.showinfo(
                "Not supported",
                "%s releases cannot be created from here."
                % self._provider.display_name, parent=self)
            return
        self.destroy()
        self._app._log("Creating %s release %s…"
                       % (self._provider.display_name, tag), C["peach"])

        def worker():
            out, rc = self._app._shell_capture(cmd, self._path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-6:]:
                self._app._log("  %s" % line, col)
            if rc == 0:
                self._app._log("  ✓ Release %s created." % tag, C["green"])

        threading.Thread(target=worker, daemon=True).start()
