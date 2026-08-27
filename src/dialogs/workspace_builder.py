"""WorkspaceBuilderDialog — assemble a VS Code `.code-workspace` descriptor.

The generator behind this dialog has existed and been tested since
Roadmap-12; what was missing was the question only a person can answer.
"Which of these projects belong in one workspace?" is not derivable from the
Manager's registry: that registry is a *discovery* result — every repo found
under the configured search roots — and generating across all of it produces a
workspace of unrelated code. So there is a picker, and it is the whole reason
this dialog exists.

Two rules it enforces rather than intends:

**What you are shown is what gets written.** The folder list comes from
``helpers.vscode_tasks.preview_workspace`` and the merge from
``plan_workspace_merge``; the dialog renders their output and writes their
document. It does not compose a second version of either, which is how a
preview and a file start disagreeing.

**An existing descriptor is merged, never replaced.** A `.code-workspace` is
hand-edited: it carries `settings`, and VS Code keeps launch configuration and
extension recommendations in there too. Folders already present are ticked when
the dialog opens, so saving an unchanged selection changes nothing, and
anything the Manager does not own is carried across untouched.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from constants import C
from helpers.vscode_tasks import (plan_workspace_merge, read_workspace,
                                  write_merged_workspace)
from theme import bind_mousewheel, themed_checkbutton


class WorkspaceBuilderDialog(tk.Toplevel):
    """Pick projects, preview the result, write a `.code-workspace`."""

    def __init__(self, parent, projects: list, on_log=None,
                 initial_target: str = ""):
        """`projects` is a list of absolute project roots to offer."""
        super().__init__(parent)
        self.title("Generate VS Code workspace")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.grab_set()

        self._projects = list(projects)
        self._on_log = on_log or (lambda *_a, **_k: None)
        self._rows: list = []            # (BooleanVar, absolute path)
        self._target = tk.StringVar(value=initial_target)

        tk.Label(self, text="\U0001F5C2  VS Code workspace",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor="w", padx=20, pady=(14, 2))
        tk.Label(self,
                 text="Tick the projects that belong together. The Manager "
                      "knows which repos exist, not which ones are one piece "
                      "of work.",
                 wraplength=520, justify="left",
                 bg=C["base"], fg=C["subtext0"]).pack(anchor="w", padx=20)

        self._build_picker()
        self._build_target_row()
        self._build_preview()
        self._build_buttons()

        self._refresh_preview()

    # ── layout ────────────────────────────────────────────────────────────

    def _build_picker(self) -> None:
        """A scrollable checkbox list. Scrollable because the registry can hold
        dozens of projects and a fixed frame would push the buttons off-screen —
        the failure the v4.10 chrome sweep went through every dialog to fix."""
        wrap = tk.Frame(self, bg=C["base"])
        wrap.pack(fill="both", expand=True, padx=20, pady=(10, 4))

        canvas = tk.Canvas(wrap, bg=C["mantle"], highlightthickness=0, height=180)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=C["mantle"])

        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        bind_mousewheel(canvas)

        for path in self._projects:
            var = tk.BooleanVar(value=False)
            row = tk.Frame(body, bg=C["mantle"])
            row.pack(fill="x", padx=6, pady=1)
            themed_checkbutton(row, text=os.path.basename(path) or path,
                               variable=var,
                               command=self._refresh_preview).pack(side="left")
            tk.Label(row, text=path, bg=C["mantle"], fg=C["overlay1"]
                     ).pack(side="left", padx=(8, 0))
            self._rows.append((var, path))

    def _build_target_row(self) -> None:
        row = tk.Frame(self, bg=C["base"])
        row.pack(fill="x", padx=20, pady=(6, 2))
        tk.Label(row, text="Save to:", bg=C["base"], fg=C["text"]).pack(side="left")
        tk.Entry(row, textvariable=self._target, bg=C["surface0"],
                 fg=C["text"], insertbackground=C["text"]
                 ).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Browse…", command=self._choose_target).pack(side="left")
        self._target.trace_add("write", lambda *_a: self._refresh_preview())

    def _build_preview(self) -> None:
        tk.Label(self, text="What will be written:", bg=C["base"], fg=C["text"]
                 ).pack(anchor="w", padx=20, pady=(8, 2))
        self._preview = tk.Text(self, height=8, wrap="none", bg=C["mantle"],
                                fg=C["text"], relief="flat")
        self._preview.pack(fill="both", expand=True, padx=20)
        self._preview.configure(state="disabled")

    def _build_buttons(self) -> None:
        row = tk.Frame(self, bg=C["base"])
        row.pack(fill="x", padx=20, pady=12)
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right")
        self._write_button = ttk.Button(row, text="Write workspace",
                                        command=self._on_write)
        self._write_button.pack(side="right", padx=(0, 8))

    # ── behaviour ─────────────────────────────────────────────────────────

    def _selected(self) -> list:
        return [path for var, path in self._rows if var.get()]

    def _choose_target(self) -> None:
        chosen = filedialog.asksaveasfilename(
            parent=self, title="Save workspace as",
            defaultextension=".code-workspace",
            filetypes=[("VS Code workspace", "*.code-workspace")])
        if chosen:
            self._target.set(chosen)
            self._preselect_existing(chosen)

    def _preselect_existing(self, target: str) -> None:
        """Tick whatever the chosen descriptor already contains.

        So that opening an existing workspace and saving it back is a no-op
        unless the user actively unticks something. Without this, the first
        save would silently drop every folder the picker did not happen to
        list.
        """
        existing = read_workspace(target)
        if not existing:
            return
        plan = plan_workspace_merge(existing, [path for _v, path in self._rows],
                                    target)
        already = {os.path.normcase(os.path.abspath(p))
                   for p in plan["retained"]}
        for var, path in self._rows:
            if os.path.normcase(os.path.abspath(path)) in already:
                var.set(True)
        self._refresh_preview()

    def _plan(self) -> "dict | None":
        target = self._target.get().strip()
        if not target:
            return None
        existing = read_workspace(target) if os.path.isfile(target) else None
        return plan_workspace_merge(existing, self._selected(), target)

    def _refresh_preview(self) -> None:
        plan = self._plan()
        lines: list = []
        if plan is None:
            lines.append("Choose where to save the workspace.")
        elif not self._selected():
            lines.append("Tick at least one project.")
        else:
            for label, key in (("+ added", "added"),
                               ("  kept ", "retained"),
                               ("- REMOVED", "removed")):
                for entry in plan[key]:
                    lines.append(f"{label}  {entry}")
            kept = [k for k in plan["document"]
                    if k not in ("folders", "settings")]
            if plan["document"].get("settings"):
                kept.append("settings")
            if kept:
                lines.append("")
                lines.append("preserved untouched: " + ", ".join(sorted(kept)))
        self._preview.configure(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", "\n".join(lines))
        self._preview.configure(state="disabled")

    def _on_write(self) -> None:
        target = self._target.get().strip()
        if not target:
            messagebox.showwarning("Generate VS Code workspace",
                                   "Choose where to save the workspace first.",
                                   parent=self)
            return
        if not self._selected():
            messagebox.showwarning("Generate VS Code workspace",
                                   "Tick at least one project.", parent=self)
            return
        if os.path.isfile(target) and read_workspace(target) is None:
            # Unreadable is not the same as absent. Overwriting a descriptor we
            # could not parse would destroy hand-written content to no purpose.
            messagebox.showerror(
                "Generate VS Code workspace",
                f"{os.path.basename(target)} exists but could not be read as "
                "JSON. Fix or move it first — overwriting it would discard "
                "whatever it contains.", parent=self)
            return

        plan = self._plan()
        if plan["removed"] and not messagebox.askyesno(
                "Generate VS Code workspace",
                "This removes these folders from the workspace:\n\n  "
                + "\n  ".join(plan["removed"])
                + "\n\nContinue?", parent=self):
            return

        ok, message = write_merged_workspace(target, plan["document"])
        self._on_log(f"[vscode] {message}",
                     C["green"] if ok else C["peach"])
        if ok:
            self.destroy()
        else:
            messagebox.showerror("Generate VS Code workspace", message,
                                 parent=self)
