"""GitHubSetupDialog — step-by-step GitHub setup wizard.

Walks first-time users through: git identity → GitHub account → create
repo → set remote URL → first push → optional GitHub Release. Each step
shows a live status indicator (✅ / ⬜ / ℹ️) based on the current git
state of the project.

Takes a `cfg: ManagerConfig` parameter (per Round 4 Phase C); reads
`self._cfg.git_exe` at execution time so a Settings → Save propagates
without restarting the dialog. NEVER snapshot a cfg field in __init__
(Rule 3 — caches go stale after a settings save).

Cross-class touch: this dialog uses `self._app._git.*` to fire off
push / refresh actions in the GitTabController. That's a back-reference
into App via the parent — it survives Phase C and gets cleaned up in
Phase D when GitTabController moves out and the controller-direct
callback pattern replaces the App lookup.
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

if TYPE_CHECKING:
    from state import ManagerConfig


class GitHubSetupDialog(tk.Toplevel):
    """Step-by-step GitHub setup wizard.

    Walks first-time users through: git identity → GitHub account →
    create repo → set remote URL → first push → optional GitHub Release.
    Each step shows a live status indicator (✅ / ⬜ / ℹ️) based on the
    current git state of the project.
    """

    def __init__(self, parent, path: str, cfg: "ManagerConfig"):
        super().__init__(parent)
        self._app  = parent   # App instance — gives access to _shell_capture, _log, etc.
        self._path = path
        self._cfg  = cfg
        self.title("GitHub Setup")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(480, 500)
        self.grab_set()

        self._name_var      = tk.StringVar()
        self._email_var     = tk.StringVar()
        self._remote_var    = tk.StringVar()
        self._tag_var       = tk.StringVar(value="v1.0.0")
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
                "GitHub Setup — build error",
                f"The wizard failed to render:\n\n{ex}\n\n{tb[-800:]}",
                parent=self)

        self.update_idletasks()
        # Open at content height, but never taller than parent window.
        content_h = self._body.winfo_reqheight() + 20
        max_h = max(400, parent.winfo_height() - 60)
        w, h = 520, min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── shell helper (fast config/remote queries — main-thread OK) ───────────

    def _sh(self, cmd) -> tuple:
        return self._app._shell_capture(cmd, self._path)

    # ── build ────────────────────────────────────────────────────────────────

    def _build(self):
        """Orchestrator — wizard step-by-step layout."""
        body = self._body   # all widgets pack into the scrollable canvas child frame
        self._build_header_section(body)
        self._build_step1_identity_section(body)
        self._build_step2_signin_section(body)
        self._build_step3_repo_section(body)
        self._build_step4_remote_section(body)
        self._build_step5_push_section(body)
        self._build_releases_section(body)
        self._build_close_section(body)

    def _build_header_section(self, body):
        """Wizard title + project name + divider."""
        P = dict(padx=20)
        tk.Label(body, text="🐙  GitHub Setup",
                 font=("Segoe UI", 12, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, pady=(16, 2), **P)
        tk.Label(body, text=os.path.basename(self._path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6), **P)
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

    def _build_step1_identity_section(self, body):
        """Step 1 — git config user.name + user.email + Save Identity."""
        self._s1_icon = self._step_header(body, "1",
            "Your name & email  (shown on every commit)")

        id_frame = tk.Frame(body, bg=C["surface0"], padx=10, pady=8)
        id_frame.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))

        for lbl_text, var in (("Name:", self._name_var), ("Email:", self._email_var)):
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
        """Step 2 — Sign in / Create account buttons + helper text."""
        self._s2_icon = self._step_header(body, "2",
            "Sign in to GitHub  (or create a free account)")
        s2 = tk.Frame(body, bg=C["base"])
        s2.pack(anchor=tk.W, padx=(44, 20), pady=(2, 4))
        ttk.Button(s2, text="Sign in to GitHub →",
                   command=lambda: os.startfile("https://github.com/login")).pack(
                   side=tk.LEFT, padx=(0, 8))
        ttk.Button(s2, text="Create free account →",
                   command=lambda: os.startfile("https://github.com/signup")).pack(side=tk.LEFT)
        tk.Label(body,
                 text="If you already have an account, just sign in — no need to create one.",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=(44, 20), pady=(0, 10))

    def _build_step3_repo_section(self, body):
        """Step 3 — instructions + Open github.com/new button."""
        self._s3_icon = self._step_header(body, "3",
            "Create a new repository on GitHub")
        s3 = tk.Frame(body, bg=C["base"])
        s3.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(s3,
                 text="Go to github.com/new, fill in the repo name, leave it Public.\n"
                      "Do NOT check 'Add README' or 'Add .gitignore' — you already\n"
                      "have those. Then copy the HTTPS URL it shows you.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        ttk.Button(s3, text="Open github.com/new →",
                   command=lambda: os.startfile("https://github.com/new")).pack(anchor=tk.W)

    def _build_step4_remote_section(self, body):
        """Step 4 — repo URL entry + Set button + example label."""
        self._s4_icon = self._step_header(body, "4",
            "Paste your repository URL here")
        s4 = tk.Frame(body, bg=C["base"])
        s4.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        url_row = tk.Frame(s4, bg=C["base"])
        url_row.pack(fill=tk.X)
        ttk.Entry(url_row, textvariable=self._remote_var, width=34,
                  font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(url_row, text="Set", command=self._set_remote).pack(side=tk.LEFT)
        tk.Label(s4,
                 text="e.g. https://github.com/you/my-project.git",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(4, 0))

    def _build_step5_push_section(self, body):
        """Step 5 — push instructions + Push button."""
        self._s5_icon = self._step_header(body, "5",
            "Upload your code to GitHub")
        s5 = tk.Frame(body, bg=C["base"])
        s5.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(s5,
                 text="This sends all your commits to GitHub. The first time, a\n"
                      "browser window will open asking you to log in — that's normal.\n"
                      "After that, pushes happen silently.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        self._push_btn = ttk.Button(s5, text="⬆  Push to GitHub",
                                    command=self._do_push)
        self._push_btn.pack(anchor=tk.W)

    def _build_releases_section(self, body):
        """Optional Releases section (gh-CLI gated) or install-gh prompt."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(10, 10))
        tk.Label(body, text="📦  GitHub Releases  (share your built .exe)",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["peach"]).pack(anchor=tk.W, padx=20, pady=(0, 4))
        tk.Label(body,
                 text="A Release lets anyone download your .exe without needing Python.\n"
                      "Build dist\\ first (run build.bat), then tag a release here.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        if shutil.which("gh"):
            self._build_releases_gh_form(body)
        else:
            self._build_releases_install_prompt(body)

    def _build_releases_gh_form(self, body):
        """Tag / Title entry grid + Create Release button (gh on PATH)."""
        rel_grid = tk.Frame(body, bg=C["base"])
        rel_grid.pack(anchor=tk.W, padx=20, pady=(0, 6))
        for col, (lbl, var, w) in enumerate([
                ("Tag:", self._tag_var, 9),
                ("Title:", self._rel_title_var, 22)]):
            tk.Label(rel_grid, text=lbl, bg=C["base"], fg=C["text"],
                     font=("Segoe UI", 9)).grid(
                     row=0, column=col*2, sticky=tk.W,
                     padx=(0 if col == 0 else 12, 4))
            ttk.Entry(rel_grid, textvariable=var, width=w,
                      font=("Segoe UI", 9)).grid(row=0, column=col*2+1, sticky=tk.W)
        ttk.Button(body, text="📦  Create Release",
                   command=self._create_release).pack(anchor=tk.W, padx=20, pady=(0, 4))

    def _build_releases_install_prompt(self, body):
        """Install-gh prompt (gh not on PATH)."""
        tk.Label(body,
                 text="Install GitHub CLI to enable one-click releases from here:",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20)
        ttk.Button(body, text="Get GitHub CLI  (cli.github.com) →",
                   command=lambda: os.startfile("https://cli.github.com")).pack(
                   anchor=tk.W, padx=20, pady=(4, 4))
        tk.Label(body,
                 text="After installing, re-open this dialog to enable releases.",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

    def _build_close_section(self, body):
        """Bottom divider + Close button."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(10, 10))
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

    # ── Refresh: query git and update step icons ─────────────────────────────

    def _refresh(self):
        # Step 1 — git identity
        name_out,  _ = self._sh([self._cfg.git_exe,"config", "--global", "user.name"])
        email_out, _ = self._sh([self._cfg.git_exe,"config", "--global", "user.email"])
        name  = name_out.strip()
        email = email_out.strip()
        if not self._name_var.get():
            self._name_var.set(name)
        if not self._email_var.get():
            self._email_var.set(email)
        self._s1_icon.config(text="✅" if (name and email) else "⚠️")

        # Steps 2 & 3 — can't detect automatically; show info marker
        self._s2_icon.config(text="ℹ️")
        self._s3_icon.config(text="ℹ️")

        # Step 4 — remote
        remote_out, rrc = self._sh(
            [self._cfg.git_exe,"-C", self._path, "remote", "get-url", "origin"])
        remote = remote_out.strip() if rrc == 0 else ""
        self._remote_var.set(remote)
        self._s4_icon.config(text="✅" if remote else "⬜")

        # Step 5 — push (only enabled when remote exists)
        self._s5_icon.config(text="⬜")
        self._push_btn.config(state=tk.NORMAL if remote else tk.DISABLED)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _save_identity(self):
        name  = self._name_var.get().strip()
        email = self._email_var.get().strip()
        if not name or not email:
            messagebox.showwarning("Incomplete",
                "Please enter both a name and an email address.", parent=self)
            return
        self._sh([self._cfg.git_exe,"config", "--global", "user.name",  name])
        self._sh([self._cfg.git_exe,"config", "--global", "user.email", email])
        self._refresh()
        messagebox.showinfo("Identity saved",
            f"Git will now sign commits as:\n{name} <{email}>", parent=self)

    def _set_remote(self):
        url = self._remote_var.get().strip()
        if not url:
            messagebox.showwarning("No URL",
                "Paste the HTTPS URL from your new GitHub repository.", parent=self)
            return
        if not (url.startswith("http") or url.startswith("git@")):
            messagebox.showwarning("Invalid URL",
                "The URL should start with https:// or git@", parent=self)
            return
        _, rrc = self._sh([self._cfg.git_exe,"-C", self._path, "remote", "get-url", "origin"])
        if rrc == 0:
            self._sh([self._cfg.git_exe,"-C", self._path, "remote", "set-url", "origin", url])
            self._app._log(f"  Remote updated: {url}", C["green"])
        else:
            self._sh([self._cfg.git_exe,"-C", self._path, "remote", "add", "origin", url])
            self._app._log(f"  Remote added: {url}", C["green"])
        self._refresh()
        self._app.after(0, self._app._git.refresh)

    def _do_push(self):
        self.destroy()
        self._app._git.set_active_path(self._path)
        self._app._git.cmd_git_push()

    def _create_release(self):
        tag   = self._tag_var.get().strip()
        title = self._rel_title_var.get().strip() or tag
        if not tag:
            messagebox.showwarning("No tag",
                "Enter a version tag, e.g. v1.0.0", parent=self)
            return
        # Collect .exe files from dist\
        dist_dir  = os.path.join(self._path, "dist")
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
        cmd = ["gh", "release", "create", tag,
               "--title", title, "--generate-notes"] + exe_files
        self.destroy()
        self._app._log(f"Creating GitHub release {tag}…", C["peach"])
        def worker():
            out, rc = self._app._shell_capture(cmd, self._path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-6:]:
                self._app._log(f"  {line}", col)
            if rc == 0:
                self._app._log(f"  ✓ Release {tag} created — check GitHub!", C["green"])
        threading.Thread(target=worker, daemon=True).start()
