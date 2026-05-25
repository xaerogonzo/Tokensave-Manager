"""ReleaseWizardDialog — one-button release wizard for tagged GitHub releases.

Six stages stacked top-to-bottom in a scrollable canvas:

  1. Version          — auto-detected last tag + Patch/Minor/Major radio
                        with intelligent default + free-text override
  2. Title            — auto-filled from highest-priority commit
  3. Release notes    — auto-drafted from `<last_tag>..HEAD` via the
                        classifier; editable textarea
  4. Build step       — checkbox + auto-detect build.ps1 or build.bat
  5. Artefact         — read-only preview of dist/ contents + zip name
  6. CHANGELOG sync   — checkbox (only if CHANGELOG.md exists with the
                        `## [Unreleased]` anchor)

Publish runs everything locally in one threaded worker — zero LLM calls.
Errors short-circuit with copy-pasteable recovery commands so any
partial state can be cleaned up by hand.

The `_ReleaseCtx` dataclass at the top is the shared state bus passed
between the eight `_pub_*` pipeline step methods — keeps each step's
signature simple while letting the orchestrator inspect partial state
on failure.

Takes `cfg: ManagerConfig` per Round 4 Phase C. All git shell-outs use
`self._cfg.git_exe` at execution time (Rule 3 — no snapshot caches).
"""

from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from helpers.release import (
    _release_basename, _last_release_tag, _commits_since,
    _suggest_bump_kind, _bump_version,
    _classify_commits_for_changelog, _render_release_notes,
    _zip_dist, _fmt_size,
)
from helpers.changelog_patch import insert_changelog_release
from helpers.git import _fetch_tags, _git_tag, _git_push_with_tags

if TYPE_CHECKING:
    from state import ManagerConfig


@dataclasses.dataclass
class _ReleaseCtx:
    """Shared state bus passed between _pub_* step methods in ReleaseWizardDialog."""
    tag: str
    title: str
    notes: str
    zip_path: str | None = None
    notes_file: str | None = None
    staged_files: list = dataclasses.field(default_factory=list)


class ReleaseWizardDialog(tk.Toplevel):
    """One-button release wizard for tagged GitHub releases.

    Six stages stacked top-to-bottom in a scrollable canvas:

      1. Version          — auto-detected last tag + Patch/Minor/Major radio
                            with intelligent default + free-text override
      2. Title            — auto-filled from highest-priority commit
      3. Release notes    — auto-drafted from `<last_tag>..HEAD` via the
                            classifier; editable textarea
      4. Build step       — checkbox + auto-detect build.ps1 or build.bat
      5. Artefact         — read-only preview of dist/ contents + zip name
      6. CHANGELOG sync   — checkbox (only if CHANGELOG.md exists with the
                            `## [Unreleased]` anchor)

    Publish runs everything locally in one threaded worker — zero LLM calls.
    Errors short-circuit with copy-pasteable recovery commands so any
    partial state can be cleaned up by hand.

    Pre-flight (in `cmd_git_release`, before constructing this dialog):
      • gh on PATH
      • _is_local_git_repo(path)
      • has remote
      • working tree clean (CHANGELOG.md is fine to be dirty — we own it)
    """

    def __init__(self, parent, path: str, cfg: "ManagerConfig"):
        super().__init__(parent)
        self._app  = parent
        self._path = path
        self._cfg  = cfg
        # _repo_name is shown in the dialog title / header — keep the
        # human-readable folder name. _release_basename is used for the
        # release zip filename and is derived from the git remote so it
        # matches the GitHub repo name (no spaces).
        self._repo_name        = os.path.basename(path)
        self._release_basename = _release_basename(path, self._cfg.git_exe)

        self.title(f"Release Wizard — {self._repo_name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 600)
        self.grab_set()
        self.transient(parent)

        # ── Discover state up front ─────────────────────────────────────────
        # Sync tags from origin first so the detected "last tag" reflects any
        # releases created remotely (e.g. legacy `gh release create` calls
        # that never tagged locally). Silent + 5s timeout — never blocks
        # dialog open on flaky networks.
        _fetch_tags(path, self._cfg.git_exe)
        self._last_tag       = _last_release_tag(path, self._cfg.git_exe)
        self._commits        = _commits_since(path, self._last_tag, self._cfg.git_exe)
        self._suggested_kind = _suggest_bump_kind(self._commits)
        self._build_script   = self._detect_build_script()
        self._changelog_path = os.path.join(path, "CHANGELOG.md")
        self._has_changelog  = (os.path.isfile(self._changelog_path)
                                and self._changelog_has_unreleased())

        # ── State vars ──────────────────────────────────────────────────────
        self._bump_var       = tk.StringVar(value=self._suggested_kind)
        self._override_var   = tk.StringVar(value="")
        self._title_var      = tk.StringVar(value="")
        self._run_build_var  = tk.BooleanVar(value=bool(self._build_script))
        self._sync_cl_var    = tk.BooleanVar(value=self._has_changelog)
        self._publishing     = False    # guard against double-clicks

        # ── Scrollable canvas (mirrors GitHubSetupDialog pattern) ───────────
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
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
        self.bind("<Destroy>",
            lambda e: self._canvas.unbind_all("<MouseWheel>"))

        try:
            self._build_ui()
            self._regenerate_notes()
        except Exception as ex:
            import traceback
            messagebox.showerror(
                "Release Wizard — build error",
                f"The wizard failed to render:\n\n{ex}\n\n"
                f"{traceback.format_exc()[-800:]}",
                parent=self)

        self.update_idletasks()
        # Open at content height, but never taller than parent.
        content_h = self._body.winfo_reqheight() + 30
        max_h = max(500, parent.winfo_height() - 40)
        w, h = 680, min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── Discovery helpers ──────────────────────────────────────────────────

    def _detect_build_script(self) -> str | None:
        """Return ``build.ps1`` or ``build.bat`` if either exists in the repo
        root, preferring .ps1. Returns None if no script is found."""
        for name in ("build.ps1", "build.bat"):
            if os.path.isfile(os.path.join(self._path, name)):
                return name
        return None

    def _changelog_has_unreleased(self) -> bool:
        try:
            with open(self._changelog_path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return False
        return re.search(r"(?m)^## \[Unreleased\]", text) is not None

    def _next_tag(self) -> str:
        """Compute the version tag from the override box or the bump radio."""
        override = self._override_var.get().strip()
        if override:
            # Ensure leading 'v' for consistency with existing tags.
            return override if override.startswith("v") else f"v{override}"
        prior = self._last_tag or "v0.0.0"
        bumped = _bump_version(prior, self._bump_var.get())
        # On a fresh repo with no tags, the first release should default to
        # v0.1.0 rather than v0.0.1 (which a patch-bump of v0.0.0 would give).
        if self._last_tag is None:
            return "v0.1.0"
        return bumped

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        """Orchestrator — each numbered wizard step is its own builder.

        Split per on-screen section (semantic grouping per BASIC_INSTRUCTIONS
        Rule A — never geometry-based chunks). The helper text and bottom
        publish row bracket the 6 numbered steps in screen order.
        """
        body = self._body
        self._build_header_section(body)
        self._build_version_section(body)
        self._build_title_section(body)
        self._build_notes_section(body)
        self._build_build_section(body)
        self._build_artefact_section(body)
        self._build_changelog_section(body)
        self._build_publish_section(body)

    def _build_header_section(self, body):
        """Title, repo name, commit-count line, divider."""
        P = dict(padx=20)
        tk.Label(body, text="📦  Release Wizard",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, pady=(16, 0), **P)
        tk.Label(body, text=self._repo_name,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 4), **P)
        n = len(self._commits)
        prior = self._last_tag or "(none — first release)"
        tk.Label(body,
                 text=f"{n} commit{'s' if n != 1 else ''} since {prior}",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 10), **P)
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 12))

    def _build_version_section(self, body):
        """Step 1 — bump radios + custom tag entry + live resolved-tag preview."""
        self._section_header(body, "1", "Version")
        ver_frame = tk.Frame(body, bg=C["base"])
        ver_frame.pack(fill=tk.X, padx=(44, 20), pady=(0, 4))

        base_for_bump = self._last_tag or "v0.0.0"
        suggestions = [
            ("patch",  _bump_version(base_for_bump, "patch")),
            ("minor",  _bump_version(base_for_bump, "minor")),
            ("major",  _bump_version(base_for_bump, "major")),
            ("hotfix", _bump_version(base_for_bump, "hotfix")),
        ]
        hotfix_labels = {
            "patch":  "Patch",
            "minor":  "Minor",
            "major":  "Major",
            "hotfix": "Hotfix",
        }
        hotfix_blurb = {
            "patch":  "(bug fixes — new patch series)",
            "minor":  "(new feature, no breaking changes)",
            "major":  "(breaking changes)",
            "hotfix": "(small tweak on top of the current release — 4-part)",
        }
        for kind, candidate in suggestions:
            text = f"{hotfix_labels[kind]}  ({candidate})  {hotfix_blurb[kind]}"
            ttk.Radiobutton(ver_frame, text=text,
                            variable=self._bump_var, value=kind,
                            command=self._refresh_resolved_tag).pack(anchor=tk.W)

        ov_row = tk.Frame(body, bg=C["base"])
        ov_row.pack(fill=tk.X, padx=(44, 20), pady=(6, 2))
        tk.Label(ov_row, text="Custom tag:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        custom_entry = ttk.Entry(ov_row, textvariable=self._override_var,
                                 width=18, font=("Consolas", 9))
        custom_entry.pack(side=tk.LEFT)
        tk.Label(ov_row, text="e.g. v1.0.5 or 1.0.5 or v1.0.4.1",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(side=tk.LEFT, padx=(8, 0))

        # Live "Will publish as" preview — updates as the user types in the
        # custom field OR switches radios. Removes the "do I add the v?"
        # ambiguity entirely because the resolved tag is right there.
        self._resolved_lbl = tk.Label(body, text="",
                                       font=("Segoe UI", 9, "bold"),
                                       bg=C["base"], fg=C["green"])
        self._resolved_lbl.pack(anchor=tk.W, padx=(44, 20), pady=(2, 12))

        # Watch the override entry — every keystroke refreshes the preview.
        self._override_var.trace_add("write",
            lambda *_: self._refresh_resolved_tag())

    def _build_title_section(self, body):
        """Step 2 — title entry."""
        self._section_header(body, "2", "Title")
        ttk.Entry(body, textvariable=self._title_var, font=("Segoe UI", 10)
                  ).pack(fill=tk.X, padx=(44, 20), pady=(0, 12))

    def _build_notes_section(self, body):
        """Step 3 — helper text + regenerate/copy buttons + notes textarea."""
        self._section_header(body, "3", "Release notes  (editable)")

        # Helper text BELOW the section header but ABOVE the textarea, so
        # the instruction never leaks INTO the textarea (and from there into
        # the published release on GitHub).
        tk.Label(body,
                 text="The textarea is your release body verbatim. Edit before "
                      "publishing — add a one-line summary at the top, tweak "
                      "bullets, drop sections you don't want shipped.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 wraplength=600, justify=tk.LEFT
                 ).pack(anchor=tk.W, padx=(44, 20), pady=(0, 4))

        notes_row = tk.Frame(body, bg=C["base"])
        notes_row.pack(fill=tk.X, padx=(44, 20), pady=(0, 4))
        ttk.Button(notes_row, text="🔄  Regenerate from commits",
                   command=self._regenerate_notes).pack(side=tk.LEFT)
        ttk.Button(notes_row, text="📋  Copy to clipboard",
                   command=self._copy_notes).pack(side=tk.LEFT, padx=(6, 0))

        nt_wrap = tk.Frame(body, bg=C["mantle"])
        nt_wrap.pack(fill=tk.BOTH, expand=False, padx=(44, 20), pady=(4, 12))
        self._notes_txt = tk.Text(nt_wrap, height=14, font=("Consolas", 9),
                                  bg=C["mantle"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief=tk.FLAT, padx=8, pady=6, wrap=tk.WORD)
        nt_vsb = ttk.Scrollbar(nt_wrap, orient="vertical",
                               command=self._notes_txt.yview)
        self._notes_txt.configure(yscrollcommand=nt_vsb.set)
        self._notes_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        nt_vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_build_section(self, body):
        """Step 4 — optional build-script checkbox (or "no build script" notice)."""
        self._section_header(body, "4", "Build step")
        build_frame = tk.Frame(body, bg=C["base"])
        build_frame.pack(fill=tk.X, padx=(44, 20), pady=(0, 12))

        if self._build_script:
            cmd_preview = (
                f"powershell -ExecutionPolicy Bypass -File {self._build_script}"
                if self._build_script.endswith(".ps1")
                else f"cmd.exe /c {self._build_script}"
            )
            ttk.Checkbutton(build_frame,
                            text=f"Run build before release  ({self._build_script})",
                            variable=self._run_build_var).pack(anchor=tk.W)
            tk.Label(build_frame, text=f"Will invoke: {cmd_preview}",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, pady=(2, 0))
        else:
            self._run_build_var.set(False)
            tk.Label(build_frame,
                     text="No build.ps1 / build.bat found in repo root.",
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W)
            tk.Label(build_frame,
                     text="Release will package whatever is in dist/ as-is.",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W)

    def _build_artefact_section(self, body):
        """Step 5 — zip artefact preview label."""
        self._section_header(body, "5", "Artefact")
        self._artefact_lbl = tk.Label(body, text="(will be computed at publish time)",
                                       font=("Segoe UI", 9), bg=C["base"],
                                       fg=C["subtext"], justify=tk.LEFT)
        self._artefact_lbl.pack(anchor=tk.W, padx=(44, 20), pady=(0, 12))
        self._refresh_artefact_preview()

    def _build_changelog_section(self, body):
        """Step 6 — CHANGELOG.md sync checkbox (or "no changelog" notice)."""
        self._section_header(body, "6", "CHANGELOG.md sync")
        cl_frame = tk.Frame(body, bg=C["base"])
        cl_frame.pack(fill=tk.X, padx=(44, 20), pady=(0, 12))
        if self._has_changelog:
            ttk.Checkbutton(cl_frame,
                            text="Update CHANGELOG.md with the new version section",
                            variable=self._sync_cl_var).pack(anchor=tk.W)
            tk.Label(cl_frame,
                     text="Inserts (or replaces) the [<version>] section directly "
                          "below [Unreleased], then commits as `chore: release "
                          "prep for <tag>`.",
                     font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=520).pack(anchor=tk.W, pady=(2, 0))
        else:
            self._sync_cl_var.set(False)
            tk.Label(cl_frame,
                     text="CHANGELOG.md not found or missing the `## [Unreleased]` "
                          "anchor — sync disabled.",
                     font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=520).pack(anchor=tk.W)

    def _build_publish_section(self, body):
        """Bottom row — Publish + Cancel buttons + status label."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 12))
        btn_row = tk.Frame(body, bg=C["base"])
        btn_row.pack(anchor=tk.W, padx=20, pady=(0, 18))
        self._publish_btn = ttk.Button(btn_row, text="🚀  Publish",
                                       style="Primary.TButton",
                                       command=self._on_publish)
        self._publish_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Status label below buttons — updated as the worker progresses.
        self._status_lbl = tk.Label(body, text="",
                                     font=("Segoe UI", 9), bg=C["base"],
                                     fg=C["overlay0"], justify=tk.LEFT,
                                     wraplength=600)
        self._status_lbl.pack(anchor=tk.W, padx=20, pady=(0, 18))

    def _section_header(self, parent, num: str, text: str):
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, padx=20, pady=(4, 4))
        tk.Label(row, text=f"{num}.", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=text, bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)

    # ── Auto-fill / regenerate ─────────────────────────────────────────────

    def _regenerate_notes(self):
        """(Re)populate the notes textarea from the commit classifier.

        Does NOT inject any placeholder summary text — the helper label
        above the textarea is the user-facing instruction, and a published
        release should contain only the user's content. (Earlier versions
        injected an "Edit this summary…" line that some users shipped to
        GitHub by accident.)
        """
        sections = _classify_commits_for_changelog(self._commits)
        from datetime import datetime as _dt
        date_str = _dt.now().strftime("%Y-%m-%d")
        tag_clean = self._next_tag().lstrip("v")
        notes = _render_release_notes(tag_clean, date_str, sections)
        self._notes_txt.delete("1.0", tk.END)
        self._notes_txt.insert("1.0", notes)
        # Cascade — title, resolved-tag preview, and artefact label all
        # depend on the resolved tag. _refresh_resolved_tag handles them all.
        self._refresh_resolved_tag()

    def _refresh_title(self):
        # Default title: "v1.0.4 — <first non-summary heading or top item>".
        tag = self._next_tag()
        first = ""
        sections = _classify_commits_for_changelog(self._commits)
        for sec in ("Breaking", "Added", "Fixed", "Changed", "Docs", "Other"):
            if sections.get(sec):
                first = sections[sec][0]
                break
        if first:
            # Take the first 60 chars of the first commit subject as a hint.
            self._title_var.set(f"{tag} — {first[:60]}")
        else:
            self._title_var.set(f"{tag} — release")

    def _refresh_resolved_tag(self):
        """Update the live "Will publish as" preview + title + artefact name.

        Called whenever the user changes a version control (radio or custom
        entry). Centralises the "what tag are we actually publishing?"
        question — no matter how they pick the version, the resolved tag
        is visible in one place and propagates downstream automatically.
        """
        tag = self._next_tag()
        # Show the resolved tag prominently in green.
        if self._override_var.get().strip():
            self._resolved_lbl.config(
                text=f"  → Will publish as:  {tag}   (custom)",
                fg=C["green"])
        else:
            kind = self._bump_var.get()
            self._resolved_lbl.config(
                text=f"  → Will publish as:  {tag}   ({kind} bump)",
                fg=C["green"])
        # Cascade — title and artefact preview depend on the resolved tag.
        self._refresh_title()
        if hasattr(self, "_artefact_lbl"):   # guard during __init__ build order
            self._refresh_artefact_preview()

    def _refresh_artefact_preview(self):
        """Refresh the artefact preview label.

        Layout intent: surface the IMPORTANT files first — `.exe` artefacts
        are what users actually download — then directories, then everything
        else. Plain alphabetical sort buried the exes after `templates/` in
        the manager's own dist/ which was misleading. Sizes use `_fmt_size`
        so a 12 KB file shows as `12 KB`, not `0.0 MB`.
        """
        dist_dir = os.path.join(self._path, "dist")
        tag = self._next_tag()
        zip_name  = f"{self._release_basename}-{tag}-windows.zip"
        if not os.path.isdir(dist_dir):
            self._artefact_lbl.config(
                text=f"dist/ not found yet — will be created by the build step.\n"
                     f"Zip: {zip_name}")
            return
        try:
            all_entries = sorted(os.listdir(dist_dir))
        except OSError:
            all_entries = []
        if not all_entries:
            self._artefact_lbl.config(
                text=f"dist/ is empty (will be populated by build).\n"
                     f"Zip: {zip_name}")
            return

        # Priority bucket order: .exe → other files → directories.
        # Within each bucket, alphabetical.
        exes, files, dirs = [], [], []
        for name in all_entries:
            full = os.path.join(dist_dir, name)
            if os.path.isdir(full):
                dirs.append(name)
            elif name.lower().endswith(".exe"):
                exes.append(name)
            else:
                files.append(name)
        ordered = exes + files + dirs

        # Show first 8 entries; if more, append "+ N more".
        SHOW = 8
        bits = []
        for name in ordered[:SHOW]:
            full = os.path.join(dist_dir, name)
            if os.path.isdir(full):
                bits.append(f"{name}/")
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                bits.append(f"{name} ({_fmt_size(size)})")
        more = f"  + {len(ordered) - SHOW} more" if len(ordered) > SHOW else ""

        # Two lines for legibility — exes get their own line so they stand out.
        if exes:
            exe_bits  = [b for b in bits if any(b.startswith(e) for e in exes)]
            rest_bits = [b for b in bits if b not in exe_bits]
            lines = ["Files:"]
            lines.append(f"  {', '.join(exe_bits)}")
            if rest_bits:
                lines.append(f"  {', '.join(rest_bits)}{more}")
            elif more:
                lines.append(f"  {more.strip()}")
            lines.append(f"Zip: {zip_name}")
            self._artefact_lbl.config(text="\n".join(lines))
        else:
            # No exes found in dist/ yet (probably pre-build) — keep the
            # original single-line layout, but the build step will produce them.
            self._artefact_lbl.config(
                text=f"Files: {', '.join(bits)}{more}\n"
                     f"(no .exe yet — will be produced by the build step)\n"
                     f"Zip: {zip_name}")

    def _copy_notes(self):
        text = self._notes_txt.get("1.0", "end-1c")
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status_lbl.config(text="✔ Release notes copied to clipboard.",
                                     fg=C["green"])
        except tk.TclError as exc:
            self._status_lbl.config(text=f"Could not copy: {exc}", fg=C["red"])

    # ── Publish pipeline ───────────────────────────────────────────────────

    def _on_publish(self):
        if self._publishing:
            return
        tag = self._next_tag()
        title = self._title_var.get().strip() or tag
        notes = self._notes_txt.get("1.0", "end-1c").strip()
        if not tag.lstrip("v"):
            messagebox.showwarning("Tag required",
                "Pick a bump radio or enter a custom tag.", parent=self)
            return
        if not notes:
            messagebox.showwarning("Notes required",
                "Release notes cannot be empty. Click 🔄 Regenerate or edit "
                "the textarea.", parent=self)
            return

        # Confirm
        confirm = messagebox.askyesno(
            "Publish release",
            f"Publish {tag} to GitHub?\n\n"
            f"This will:\n"
            f"  • Run the build  ({'yes' if self._run_build_var.get() else 'no'})\n"
            f"  • Zip dist/ as the release artefact\n"
            f"  • {'Update CHANGELOG.md, commit, ' if self._sync_cl_var.get() else ''}"
            f"create local tag {tag}, push, then call gh release create.",
            parent=self)
        if not confirm:
            return

        self._publishing = True
        self._publish_btn.configure(state=tk.DISABLED)
        self._app._git._git_begin_op()

        threading.Thread(
            target=self._publish_worker,
            args=(tag, title, notes),
            daemon=True,
        ).start()

    def _set_status(self, text: str, fg: str = None):
        """Thread-safe status update."""
        def _do():
            self._status_lbl.config(text=text, fg=fg or C["subtext"])
        try:
            self.after(0, _do)
        except tk.TclError:
            pass  # dialog already destroyed

    def _fail(self, message: str, *, keep_temp: bool = False,
              temp_path: str | None = None):
        """Surface a copy-pasteable recovery message and end the op."""
        self._set_status(message, fg=C["red"])
        self._app._log("  ✗ Release aborted", C["red"])
        for line in message.splitlines():
            if line.strip():
                self._app._log(f"    {line}", C["red"])
        if keep_temp and temp_path:
            self._app._log(f"    Notes preserved at: {temp_path}", C["red"])
        # Re-enable buttons
        self._publishing = False
        def _reenable():
            try:
                self._publish_btn.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
            self._app._git._git_end_op()
        try:
            self.after(0, _reenable)
        except tk.TclError:
            pass

    # ── Publish pipeline step methods ─────────────────────────────────────────

    def _pub_build(self, ctx: "_ReleaseCtx") -> bool:
        """Step 1: run the optional build script. Returns False on failure."""
        if not (self._run_build_var.get() and self._build_script):
            return True
        log = self._app._log
        sh  = self._app._shell_capture
        self._set_status(f"Building via {self._build_script}…  "
                         "(this can take 3–8 minutes)", fg=C["peach"])
        log(f"Release {ctx.tag}: running {self._build_script}…", C["peach"])
        if self._build_script.endswith(".ps1"):
            build_cmd = ["powershell", "-ExecutionPolicy", "Bypass",
                         "-File", self._build_script]
        else:
            # .bat needs cmd.exe /c — running the .bat directly raises
            # WinError 193 ("not a valid Win32 application") on Windows.
            build_cmd = ["cmd.exe", "/c", self._build_script]
        out, rc = sh(build_cmd, self._path)
        for line in out.strip().splitlines()[-12:]:
            log(f"  {line}", C["overlay0"])
        if rc != 0:
            self._fail("Build failed — release aborted, no state changed.\n"
                       "See log panel for build tail.")
            return False
        return True

    def _pub_zip(self, ctx: "_ReleaseCtx") -> bool:
        """Step 2: zip dist/. Populates ctx.zip_path on success."""
        self._set_status("Zipping dist/…", fg=C["peach"])
        dist_dir = os.path.join(self._path, "dist")
        zip_name = f"{self._release_basename}-{ctx.tag}-windows.zip"
        zip_path = os.path.join(self._path, zip_name)
        zip_out  = _zip_dist(dist_dir, zip_path)
        if not zip_out:
            self._fail("Zip step failed — dist/ missing or empty.\n"
                       "Re-run with 'Run build' enabled, or build manually first.")
            return False
        try:
            mb = os.path.getsize(zip_out) / 1024 / 1024
        except OSError:
            mb = 0.0
        self._app._log(f"  zipped: {os.path.basename(zip_out)} ({mb:.1f} MB)",
                       C["green"])
        ctx.zip_path = zip_out
        return True

    def _pub_write_notes(self, ctx: "_ReleaseCtx") -> bool:
        """Step 3: write release notes to a temp file. Populates ctx.notes_file."""
        notes_fd, notes_file = tempfile.mkstemp(
            prefix=f"release-notes-{ctx.tag}-", suffix=".md", text=True)
        try:
            with os.fdopen(notes_fd, "w", encoding="utf-8") as f:
                f.write(ctx.notes)
        except OSError as exc:
            self._fail(f"Could not write temporary notes file: {exc}")
            return False
        ctx.notes_file = notes_file
        return True

    def _pub_patch_changelog(self, ctx: "_ReleaseCtx") -> bool:
        """Step 4: optionally stamp CHANGELOG.md with the release version."""
        if not (self._sync_cl_var.get() and self._has_changelog):
            return True
        self._set_status("Patching CHANGELOG.md…", fg=C["peach"])
        ok, msg = insert_changelog_release(
            self._changelog_path,
            ctx.tag.lstrip("v"),
            ctx.notes,
        )
        if not ok:
            self._fail(f"CHANGELOG patch failed: {msg}",
                       keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log(f"  CHANGELOG.md {msg}", C["green"])
        ctx.staged_files.append("CHANGELOG.md")
        return True

    def _pub_stage_commit(self, ctx: "_ReleaseCtx") -> bool:
        """Step 5: stage and commit only the files we touched (e.g. CHANGELOG)."""
        if not ctx.staged_files:
            return True
        sh   = self._app._shell_capture
        path = self._path
        git  = self._cfg.git_exe
        self._set_status("Committing release-prep changes…", fg=C["peach"])
        out, rc = sh([git, "-C", path, "add", "--"] + ctx.staged_files, path)
        if rc != 0:
            self._fail(f"git add CHANGELOG.md failed (rc={rc}).\n{out[-300:]}",
                       keep_temp=True, temp_path=ctx.notes_file)
            return False
        commit_cmd = ([git, "-C", path, "commit",
                       "-m", f"chore: release prep for {ctx.tag}", "--"]
                      + ctx.staged_files)
        out, rc = sh(commit_cmd, path)
        if rc != 0:
            self._fail(f"Commit failed (rc={rc}).\n{out[-300:]}\n\n"
                       "Recover: git restore --staged CHANGELOG.md",
                       keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log("  committed release-prep changes", C["green"])
        return True

    def _pub_tag(self, ctx: "_ReleaseCtx") -> bool:
        """Step 6: create a local annotated tag."""
        self._set_status(f"Creating local tag {ctx.tag}…", fg=C["peach"])
        out, rc = _git_tag(self._path, ctx.tag, ctx.title, self._cfg.git_exe)
        if rc != 0:
            self._fail(
                f"git tag {ctx.tag} failed — possibly already exists locally.\n"
                f"Recover: git tag -d {ctx.tag}\n\n"
                f"git output:\n{out[-300:]}",
                keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log(f"  created annotated tag {ctx.tag}", C["green"])
        return True

    def _pub_push(self, ctx: "_ReleaseCtx") -> bool:
        """Step 7: push commits + tag to origin in one round-trip."""
        self._set_status(f"Pushing {ctx.tag} to origin…", fg=C["peach"])
        out, rc = _git_push_with_tags(self._path, self._cfg.git_exe)
        if rc != 0:
            self._fail(
                f"Push failed (rc={rc}).\n{out[-300:]}\n\n"
                f"Recover local state with:\n"
                f"  git tag -d {ctx.tag}                # remove local tag\n"
                f"  git reset --soft HEAD~1         # if release-prep commit was made",
                keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log(f"  pushed HEAD + {ctx.tag} to origin", C["green"])
        return True

    def _pub_gh_release(self, ctx: "_ReleaseCtx") -> bool:
        """Step 8: create the GitHub Release via `gh release create`."""
        # After this point, tag + commit are already on the remote, so a
        # failure here means manual recovery is needed.
        self._set_status(f"Creating GitHub release {ctx.tag}…", fg=C["peach"])
        gh_cmd = ["gh", "release", "create", ctx.tag,
                  "--title", ctx.title,
                  "--notes-file", ctx.notes_file,
                  ctx.zip_path]
        out, rc = self._app._shell_capture(gh_cmd, self._path)
        for line in out.strip().splitlines()[-8:]:
            self._app._log(f"  {line}", C["overlay0"])
        if rc != 0:
            cmd_str = (f"gh release create {ctx.tag} --title \"{ctx.title}\" "
                       f"--notes-file \"{ctx.notes_file}\" \"{ctx.zip_path}\"")
            self._fail(
                f"GitHub release creation failed AFTER the tag was pushed.\n\n"
                f"Local repo and remote ARE in sync — only the GitHub Release\n"
                f"page wasn't created. Recover with:\n\n"
                f"  {cmd_str}\n\n"
                f"Notes file preserved at: {ctx.notes_file}",
                keep_temp=True, temp_path=ctx.notes_file)
            return False
        return True

    def _publish_worker(self, tag: str, title: str, notes: str):
        """Sequenced publish pipeline. Runs entirely on a background thread."""
        ctx = _ReleaseCtx(tag=tag, title=title, notes=notes)
        steps = [
            self._pub_build,
            self._pub_zip,
            self._pub_write_notes,
            self._pub_patch_changelog,
            self._pub_stage_commit,
            self._pub_tag,
            self._pub_push,
            self._pub_gh_release,
        ]
        for step in steps:
            if not step(ctx):
                return

        # Step 9: cleanup notes temp file
        if ctx.notes_file:
            try:
                os.unlink(ctx.notes_file)
            except OSError:
                pass

        # Step 10: done — refresh git tab and close wizard
        self._app._log(f"  ✓ Release {ctx.tag} published — check GitHub!", C["green"])
        self._set_status(f"✔ Release {ctx.tag} published.", fg=C["green"])
        def _close():
            self._publishing = False
            self._app._git._git_end_op()
            self.destroy()
        try:
            self.after(2000, _close)
        except tk.TclError:
            pass
