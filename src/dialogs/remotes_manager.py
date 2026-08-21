"""RemotesManagerDialog — choose which remotes a push goes to.

Opened from the Git tab. Two things are being configured, and conflating them
is the bug this dialog exists to avoid:

* **Push targets** — every remote ticked receives the push.
* **The tracking remote** — exactly one remote (or none) that ``git push -u``
  applies to. Looping ``-u`` over several remotes leaves the branch tracking
  whichever ran last, which is a silent edit to the user's git config made by
  a button that said "push".

The saved selection is reconciled against ``git remote`` every time the dialog
opens, so a remote renamed or removed outside the manager drops out rather
than failing at push time. Git's view is authoritative; the saved list is a
preference.

URLs are shown redacted — a remote configured as
``https://user:token@host/...`` would otherwise print its token into this
window and every log line that quotes it.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from constants import C
from theme import bind_mousewheel, themed_checkbutton
from helpers.multi_remote import list_remotes, reconcile_selection
from helpers.remote_providers import provider_for_url

if TYPE_CHECKING:
    from state import ManagerConfig

# Config keys, per project path, in manager-config.json:
#
#   "selected_push_remotes": {"<project path>": ["origin", "codeberg"]}
#       Remotes a push is sent to. Absent means "never configured", which
#       defaults to origin so an untouched project behaves as it always did.
#       An empty list is different: the user unticked everything.
#
#   "upstream_remote": {"<project path>": "origin"}
#       The single remote `git push -u` applies to, or "" for none. Stored
#       apart from the selection because tracking and delivery are different
#       questions -- see the module docstring.
#
# Both are reconciled against `git remote` on read, so a name that no longer
# exists is dropped rather than pushed to.
_SELECTION_KEY = "selected_push_remotes"
_UPSTREAM_KEY = "upstream_remote"


def load_selection(cfg: "ManagerConfig", path: str, remotes) -> tuple:
    """Saved push targets, reconciled against what git actually has.

    Defaults to ``origin`` when nothing is saved: that is what the previous
    single-remote button did, so an untouched project behaves exactly as
    before.
    """
    saved = cfg.raw.get(_SELECTION_KEY, {}).get(path)
    if saved is None:
        names = [r.name for r in remotes]
        return ("origin",) if "origin" in names else tuple(names[:1])
    return reconcile_selection(saved, remotes)


def load_upstream(cfg: "ManagerConfig", path: str, remotes) -> str:
    """The remote ``-u`` applies to. Empty means "leave tracking alone"."""
    saved = cfg.raw.get(_UPSTREAM_KEY, {}).get(path)
    names = [r.name for r in remotes]
    if saved is not None:
        return saved if saved in names else ""
    return "origin" if "origin" in names else ""


def save_selection(cfg: "ManagerConfig", path: str,
                   selected, upstream: str) -> None:
    cfg.raw.setdefault(_SELECTION_KEY, {})[path] = list(selected)
    cfg.raw.setdefault(_UPSTREAM_KEY, {})[path] = upstream
    cfg.save()


class RemotesManagerDialog(tk.Toplevel):
    """Tick the remotes to push to; pick at most one to track."""

    def __init__(self, parent, path: str, cfg: "ManagerConfig",
                 on_saved: "Callable[[], None] | None" = None) -> None:
        super().__init__(parent)
        self._path = path
        self._cfg = cfg
        self._on_saved = on_saved
        self.title("Push targets")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 420)
        self.grab_set()

        self._remotes = list_remotes(cfg.git_exe, path)
        self._vars: dict = {}
        self._upstream_var = tk.StringVar(
            value=load_upstream(cfg, path, self._remotes))

        self._build_header()
        self._build_action_bar()
        self._build_list()
        self._centre_on_parent(parent)

    # ── layout ───────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Which remotes receive a push?",
                 bg=C["surface0"], fg=C["text"],
                 font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=("Push and Force Push send to every remote ticked here.\n"
                  "\"Track\" is separate: it is the one remote your branch "
                  "follows for ahead/behind counts, and only it receives "
                  "-u. Setting it on several would leave the branch tracking "
                  "whichever finished last."),
            bg=C["surface0"], fg=C["subtext"], font=("Segoe UI", 9),
            wraplength=500, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(bar, text="Save", style="Primary.TButton",
                   command=self._on_save).pack(side=tk.RIGHT, padx=(0, 6))

    def _build_list(self) -> None:
        wrap = tk.LabelFrame(self, text="Remotes", fg=C["subtext"],
                             bg=C["base"], font=("Segoe UI", 9, "bold"))
        wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(8, 4))

        canvas = tk.Canvas(wrap, bg=C["base"], highlightthickness=0)
        bind_mousewheel(canvas)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(canvas, bg=C["base"])
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_id, width=e.width))
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        if not self._remotes:
            tk.Label(body,
                     text=("  No remotes configured — add one with "
                           "\"Set remote…\" on the Git tab."),
                     bg=C["base"], fg=C["overlay0"],
                     font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=8)
            return

        selected = set(load_selection(self._cfg, self._path, self._remotes))
        for remote in self._remotes:
            self._add_row(body, remote, remote.name in selected)

    def _add_row(self, parent, remote, checked: bool) -> None:
        row = tk.Frame(parent, bg=C["mantle"])
        row.pack(fill=tk.X, padx=4, pady=2)

        var = tk.BooleanVar(value=checked)
        self._vars[remote.name] = var
        themed_checkbutton(row, text=remote.name, variable=var,
                           bg=C["mantle"], fg=C["text"],
                           activebackground=C["mantle"],
                           activeforeground=C["text"],
                           font=("Segoe UI", 10, "bold")).pack(
            side=tk.LEFT, padx=(8, 4), pady=6)

        ttk.Radiobutton(row, text="Track", value=remote.name,
                        variable=self._upstream_var).pack(side=tk.RIGHT,
                                                          padx=8)

        info = tk.Frame(row, bg=C["mantle"])
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        provider = provider_for_url(" ".join(remote.destinations))
        label = provider.display_name if provider else "other"
        for url in remote.safe_destinations:
            tk.Label(info, text=url, bg=C["mantle"], fg=C["overlay0"],
                     font=("Consolas", 8)).pack(anchor=tk.W)
        if len(remote.destinations) > 1:
            # Git pushes to every configured pushurl, which surprises people
            # who assume a remote is one place.
            tk.Label(info,
                     text="pushes to all %d destinations"
                          % len(remote.destinations),
                     bg=C["mantle"], fg=C["peach"],
                     font=("Segoe UI", 8)).pack(anchor=tk.W)
        tk.Label(info, text=label, bg=C["mantle"], fg=C["overlay0"],
                 font=("Segoe UI", 8)).pack(anchor=tk.W)

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 600, 460
        try:
            px = parent.winfo_x() + (parent.winfo_width() - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── save ─────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        selected = tuple(name for name, var in self._vars.items()
                         if var.get())
        upstream = self._upstream_var.get()
        # Tracking a remote that will not be pushed to is a contradiction the
        # user is unlikely to want, and git would never update the tracking
        # ref anyway.
        if upstream and upstream not in selected:
            upstream = ""
        save_selection(self._cfg, self._path, selected, upstream)
        if self._on_saved:
            self._on_saved()
        self.destroy()
