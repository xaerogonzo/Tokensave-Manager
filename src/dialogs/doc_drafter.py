"""DocDrafterDialog — tabbed AI doc-update drafter (Tier B of the doc-update
automation roadmap).

Two tabs: CHANGELOG and README.  Each tab is independent — its own worker
thread, its own ``stop_event``, its own draft cache.  A slow draft on one
tab never overwrites a fast result on another.

Flow per tab:
  1. User picks a commit range from the header dropdown (resolved once,
     shared across tabs).
  2. Click **Generate** → spawn worker → ``helpers.doc_drafter.dispatch_llm``
     → text appears in the editable area.
  3. Optionally edit the draft.
  4. Click **Apply** → spawn worker → ``ProposalBridge`` shows old-vs-new
     diff → on accept, the appropriate patcher writes atomically and the
     parent's commit-offer callback fires.

Closing the dialog (X button) signals stop_event on ALL tab workers and
destroys cleanly.  No leaked threads.

All Tk widget mutations from worker threads go through ``self.after(0, …)``
and are guarded with ``winfo_exists()`` + ``try/except TclError`` (same
pattern as the gitignore AI-Suggest worker).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.changelog_patch import update_unreleased, read_unreleased
from helpers.readme_patch import (
    update_readme_highlights, read_highlights,
    insert_readme_highlights_subsection,
)
from helpers.release import _classify_commits_for_changelog
from helpers import doc_drafter as dd

if TYPE_CHECKING:
    from typing import Callable
    from state import ManagerConfig


# Commit-range modes (mirrors helpers.doc_drafter.resolve_commit_range)
_RANGE_MODES = [
    ("Since last doc commit",    "since_last_doc"),
    ("Since last commit",        "since_last_commit"),
    ("Since last release tag",   "since_last_tag"),
    ("Custom range…",            "custom"),
]


class DocDrafterDialog(tk.Toplevel):
    """Tabbed doc-update drafter (CHANGELOG + README)."""

    def __init__(self, parent, project_path,
                 cfg: "ManagerConfig",
                 on_log: "Callable[[str, str | None], None] | None" = None,
                 on_commit_offer: "Callable[[str, str], None] | None" = None):
        super().__init__(parent)
        self._app             = parent
        self._project_path    = project_path
        self._cfg             = cfg
        self._on_log          = on_log or (lambda msg, c=None: None)
        self._on_commit_offer = on_commit_offer or (lambda path, label: None)

        self.title(f"📝 Draft Doc Updates — {os.path.basename(project_path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(720, 580)
        self.grab_set()
        self.transient(parent)

        # ── State ──────────────────────────────────────────────────────────
        self._range_data = None    # set by _refresh_range
        self._project_name = os.path.basename(project_path)
        self._project_desc = self._read_project_description()
        self._tab_state = {
            "changelog": {"stop": threading.Event(),
                          "thread": None, "draft": ""},
            "readme":    {"stop": threading.Event(),
                          "thread": None, "draft": ""},
        }
        self._tab_widgets: dict = {}   # populated by _build_tab

        # ── UI ─────────────────────────────────────────────────────────────
        self._build_header_section()
        self._build_notebook()
        self._build_footer_section()

        # Resolve initial range so tabs can Generate immediately.
        self._refresh_range()
        self._centre_on_parent(parent)

        # Close-button wiring — cancel all tab workers before destroy.
        orig_destroy = self.destroy
        def _safe_destroy():
            for state in self._tab_state.values():
                state["stop"].set()
            orig_destroy()
        self.protocol("WM_DELETE_WINDOW", _safe_destroy)

    # ── Header (range picker + backend label) ──────────────────────────────

    def _build_header_section(self):
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=12)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📝  Doc Updates", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)

        # Range row
        row = tk.Frame(hdr, bg=C["base"])
        row.pack(fill=tk.X, pady=(8, 4))
        tk.Label(row, text="Commit range:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._range_var = tk.StringVar(value=_RANGE_MODES[0][0])
        self._range_cb = ttk.Combobox(
            row, textvariable=self._range_var,
            values=[m[0] for m in _RANGE_MODES],
            state="readonly", width=28,
        )
        self._range_cb.pack(side=tk.LEFT)
        self._range_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_range())

        self._custom_var = tk.StringVar()
        self._custom_entry = ttk.Entry(row, textvariable=self._custom_var,
                                        width=22, font=("Consolas", 9))
        self._custom_entry.pack(side=tk.LEFT, padx=(6, 0))
        self._custom_entry.configure(state=tk.DISABLED)
        self._custom_entry.bind("<Return>", lambda _e: self._refresh_range())

        ttk.Button(row, text="⟳ Apply range",
                   command=self._refresh_range).pack(side=tk.LEFT, padx=(6, 0))

        # Range-resolution info
        self._range_info_var = tk.StringVar(value="(resolving…)")
        tk.Label(hdr, textvariable=self._range_info_var,
                 bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(anchor=tk.W, pady=(2, 0))

        # Backend label
        self._backend_var = tk.StringVar(value=self._backend_summary())
        tk.Label(hdr, textvariable=self._backend_var,
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(2, 0))

    def _backend_summary(self):
        cfg = self._llm_cfg_resolved()
        prov = cfg.get("provider", "(none configured)")
        model = cfg.get("model") or "(default)"
        which = "ask_tab_llm" if (self._cfg.raw or {}).get("ask_tab_llm") \
                else "commit_message_llm"
        return f"Backend: {which} → {prov} / {model}"

    def _llm_cfg_resolved(self):
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        return raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}

    def _refresh_range(self):
        """Resolve the selected range mode + update range_info + clear drafts.

        Called on dialog open AND every time the user changes the range
        dropdown / custom-ref / Apply range button.  Resolving a new range
        invalidates any in-flight drafts because their context is stale.
        """
        # Map label → mode token
        label = self._range_var.get()
        mode = next((m for lbl, m in _RANGE_MODES if lbl == label),
                    "since_last_doc")
        # Toggle custom-entry editability
        self._custom_entry.configure(
            state=(tk.NORMAL if mode == "custom" else tk.DISABLED))

        custom_ref = self._custom_var.get() if mode == "custom" else ""

        try:
            self._range_data = dd.resolve_commit_range(
                self._project_path, mode, custom_ref, self._cfg.git_exe)
        except Exception as exc:
            self._range_data = None
            self._range_info_var.set(f"Range resolution failed: {exc}")
            return

        rd = self._range_data
        n = len(rd.get("commits") or [])
        if n == 0:
            self._range_info_var.set(
                f"{rd['range_label']} — no commits in range")
        else:
            self._range_info_var.set(
                f"{rd['range_label']} — {n} commit(s); "
                f"sparse={dd.is_sparse(rd['commits'])}")

        # Cancel any in-flight workers (their context just became stale).
        for state in self._tab_state.values():
            state["stop"].set()

    # ── Notebook + per-tab construction ────────────────────────────────────

    def _build_notebook(self):
        nb_wrap = tk.Frame(self, bg=C["base"], padx=18)
        nb_wrap.pack(fill=tk.BOTH, expand=True)
        self._notebook = ttk.Notebook(nb_wrap)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        self._build_tab("changelog", "CHANGELOG.md", "Generate CHANGELOG")
        self._build_tab("readme",    "README.md",    "Generate README")

    def _build_tab(self, key, target_file, generate_label):
        frame = tk.Frame(self._notebook, bg=C["base"], padx=8, pady=8)
        self._notebook.add(frame, text=f"  {key.upper()}  ")

        # Target file label
        target_path = os.path.join(self._project_path, target_file)
        tk.Label(frame,
                 text=f"Target: {target_file}    ({target_path})",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(anchor=tk.W, pady=(0, 4))

        # Editable text area for the draft
        txt_wrap = tk.Frame(frame, bg=C["mantle"])
        txt_wrap.pack(fill=tk.BOTH, expand=True)
        txt = tk.Text(txt_wrap, font=("Consolas", 9),
                      bg=C["mantle"], fg=C["text"],
                      insertbackground=C["text"],
                      relief=tk.FLAT, padx=6, pady=4, wrap=tk.WORD,
                      height=20)
        vsb = ttk.Scrollbar(txt_wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Empty-state placeholder
        txt.insert("1.0",
                   "(no draft yet — click Generate to draft from the "
                   "selected commit range)")
        txt.tag_add("placeholder", "1.0", tk.END)
        txt.tag_configure("placeholder", foreground=C["overlay0"])

        # Buttons + status
        btn_row = tk.Frame(frame, bg=C["base"])
        btn_row.pack(fill=tk.X, pady=(6, 0))
        gen_btn = ttk.Button(btn_row, text=f"🔄 {generate_label}",
                             command=lambda k=key: self._on_generate(k))
        gen_btn.pack(side=tk.LEFT)
        copy_btn = ttk.Button(btn_row, text="📋 Copy",
                              command=lambda k=key: self._on_copy(k))
        copy_btn.pack(side=tk.LEFT, padx=(6, 0))
        apply_btn = ttk.Button(btn_row, text="✓ Apply via Proposal",
                               command=lambda k=key: self._on_apply(k))
        apply_btn.pack(side=tk.LEFT, padx=(6, 0))

        status_var = tk.StringVar(value="")
        status_lbl = tk.Label(btn_row, textvariable=status_var,
                              bg=C["base"], fg=C["overlay0"],
                              font=("Segoe UI", 8))
        status_lbl.pack(side=tk.LEFT, padx=(12, 0))

        self._tab_widgets[key] = {
            "frame":      frame,
            "text":       txt,
            "gen_btn":    gen_btn,
            "apply_btn":  apply_btn,
            "status_var": status_var,
            "status_lbl": status_lbl,
            "target":     target_file,
        }

    def _build_footer_section(self):
        ftr = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        ftr.pack(fill=tk.X)
        ttk.Button(ftr, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _centre_on_parent(self, parent):
        self.update_idletasks()
        w, h = 880, 660
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Project description (for prompt context) ───────────────────────────

    def _read_project_description(self):
        """First non-blank, non-header line of README.md, or empty string."""
        readme = os.path.join(self._project_path, "README.md")
        if not os.path.isfile(readme):
            return ""
        try:
            with open(readme, encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    return s[:200]
        except OSError:
            pass
        return ""

    # ── Generate flow ──────────────────────────────────────────────────────

    def _on_generate(self, key):
        """Spawn a background worker that builds the prompt + dispatches LLM."""
        if not self._range_data:
            self._set_status(key, "Resolve a commit range first.", C["red"])
            return
        rd = self._range_data
        if not rd.get("commits"):
            self._set_status(key, "No commits in range — nothing to draft.",
                             C["red"])
            return

        llm_cfg = self._llm_cfg_resolved()
        if not llm_cfg or not llm_cfg.get("provider"):
            self._set_status(key,
                "No AI configured — set a provider in Settings → Ask Tab AI.",
                C["red"])
            return

        # Cancel any in-flight generate for THIS tab.
        self._tab_state[key]["stop"].set()
        self._tab_state[key]["stop"] = threading.Event()
        stop_event = self._tab_state[key]["stop"]

        self._tab_widgets[key]["gen_btn"].configure(state=tk.DISABLED)
        self._set_status(key, "Drafting…", C["overlay0"])

        # Snapshot context for the worker.
        commits     = rd["commits"]
        classified  = _classify_commits_for_changelog(commits)
        boundary    = rd.get("boundary_note")
        range_spec  = rd.get("range_spec", "")
        project     = self._project_path

        def _worker():
            # Sparse-mode context-extras (only fetched if needed).
            changed_files = (dd.changed_file_paths(project, range_spec,
                                                    self._cfg.git_exe)
                             if dd.is_sparse(commits) else [])

            if key == "changelog":
                existing = read_unreleased(
                    os.path.join(project, "CHANGELOG.md"))
                system, user = dd.build_changelog_prompt(
                    commits, classified, existing,
                    self._project_name, self._project_desc,
                    changed_files, boundary)
            else:  # readme
                existing = read_highlights(
                    os.path.join(project, "README.md"))
                system, user = dd.build_readme_prompt(
                    commits, classified, existing,
                    self._project_name, self._project_desc,
                    changed_files, boundary)

            if stop_event.is_set():
                return

            text, err = dd.dispatch_llm(
                llm_cfg, system, user,
                claude_cli_exe=self._cfg.claude_cli_exe,
                cwd=project, timeout=120,
            )

            if stop_event.is_set():
                return

            if err or not text:
                self.after(0, lambda m=(err or "empty result"):
                           self._on_generate_error(key, m))
                return
            self.after(0, lambda t=text: self._on_generate_done(key, t))

        t = threading.Thread(target=_worker, daemon=True,
                              name=f"doc-drafter:{key}")
        self._tab_state[key]["thread"] = t
        t.start()

    def _on_generate_done(self, key, text):
        try:
            if not self.winfo_exists():
                return
            w = self._tab_widgets[key]
            txt = w["text"]
            txt.delete("1.0", tk.END)
            txt.insert("1.0", text.strip())
            self._tab_state[key]["draft"] = text.strip()
            w["gen_btn"].configure(state=tk.NORMAL)
            self._set_status(key, "Draft ready — review and Apply.",
                             C["green"])
        except (tk.TclError, RuntimeError):
            return

    def _on_generate_error(self, key, msg):
        try:
            if not self.winfo_exists():
                return
            self._tab_widgets[key]["gen_btn"].configure(state=tk.NORMAL)
            self._set_status(key, f"Error: {msg}", C["red"])
        except (tk.TclError, RuntimeError):
            return

    # ── Copy / Apply flow ──────────────────────────────────────────────────

    def _on_copy(self, key):
        try:
            txt = self._tab_widgets[key]["text"].get("1.0", tk.END).rstrip()
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.update()
            self._set_status(key, "Copied to clipboard.", C["overlay0"])
        except tk.TclError:
            pass

    def _on_apply(self, key):
        """Push the current draft text through ProposalBridge → patcher."""
        # Snapshot the user-edited text on the main thread.
        text = self._tab_widgets[key]["text"].get("1.0", tk.END).rstrip()
        if not text or text.startswith("(no draft yet"):
            self._set_status(key, "Nothing to apply — generate a draft first.",
                             C["red"])
            return

        target_file = self._tab_widgets[key]["target"]
        target_path = os.path.join(self._project_path, target_file)

        # Build the WriteProposal (old vs new).
        if key == "changelog":
            existing = read_unreleased(target_path)
            rationale = ("Draft [Unreleased] CHANGELOG bullets generated "
                         "from the selected commit range.")
        else:
            existing = read_highlights(target_path)
            rationale = ("Draft README 'Recent highlights (Unreleased)' "
                         "body generated from the selected commit range.")

        self._set_status(key, "Opening proposal…", C["overlay0"])
        self._tab_widgets[key]["apply_btn"].configure(state=tk.DISABLED)

        # Spawn worker — ProposalBridge.invoke() blocks on a threading.Event,
        # MUST NOT be called from the Tk main thread.
        def _worker():
            from dialogs.proposal import ProposalBridge, WriteProposal
            proposal = WriteProposal(
                filepath=target_path,
                original_content=existing or "(empty)",
                proposed_content=text,
                rationale=rationale,
            )
            bridge = ProposalBridge(self._app, proposal, timeout_s=300.0)
            accepted, final_content = bridge.invoke()
            if not accepted or not final_content:
                self.after(0, lambda: self._on_apply_result(
                    key, False, "Proposal rejected or timed out."))
                return

            # Apply the final (possibly user-edited in the proposal dialog)
            # content via the appropriate patcher.
            if key == "changelog":
                ok, msg = update_unreleased(target_path, final_content)
            else:
                # README uses append-only sub-section insertion to avoid
                # the small-model "drop existing sub-sections" failure mode.
                # Parse the first bold-header line as the sub-section name
                # and the rest as bullets.
                ok, msg = self._apply_readme_subsection(target_path,
                                                        final_content)
            self.after(0, lambda o=ok, m=msg:
                       self._on_apply_result(key, o, m))

        threading.Thread(target=_worker, daemon=True,
                         name=f"doc-drafter-apply:{key}").start()

    def _on_apply_result(self, key, ok, msg):
        try:
            if not self.winfo_exists():
                return
            self._tab_widgets[key]["apply_btn"].configure(state=tk.NORMAL)
            if ok:
                self._set_status(key, f"✓ {msg}", C["green"])
                target = self._tab_widgets[key]["target"]
                self._on_log(f"[doc-drafter] {target}: {msg}", C["green"])
                # Offer to commit the change (matches CHANGELOG drafter UX).
                self._on_commit_offer(self._project_path,
                                       f"{target} (doc-drafter)")
            else:
                self._set_status(key, f"✗ {msg}", C["red"])
                self._on_log(f"[doc-drafter] {self._tab_widgets[key]['target']}: "
                             f"{msg}", C["red"])
        except (tk.TclError, RuntimeError):
            return

    # ── README sub-section parser + dispatcher ─────────────────────────────

    def _apply_readme_subsection(self, target_path, draft_text):
        """Parse the first ``**Header**`` line as the sub-section header and
        dispatch to insert_readme_highlights_subsection.

        Robust to:
          - blank lines before the header
          - prose preamble before the header (silently skipped)
          - trailing whitespace
        Falls back to a clear error if no header line is found in the draft.
        """
        lines = draft_text.splitlines()
        header_idx = None
        for i, ln in enumerate(lines):
            stripped = ln.strip()
            if stripped.startswith("**") and stripped.endswith("**") \
               and len(stripped) > 4:
                header_idx = i
                break
            # Also accept "**Header**:" form
            if (stripped.startswith("**") and "**" in stripped[2:]
                    and stripped.endswith(":")):
                header_idx = i
                break
        if header_idx is None:
            return False, ("Draft is missing a `**Bold header**` line — "
                           "README mode requires a sub-section header as "
                           "the first non-blank line.")
        header_line = lines[header_idx].strip()
        bullets = "\n".join(lines[header_idx + 1:]).strip("\n")
        return insert_readme_highlights_subsection(
            target_path, header_line, bullets + "\n" if bullets else "\n")

    # ── Status helper ──────────────────────────────────────────────────────

    def _set_status(self, key, msg, fg):
        try:
            self._tab_widgets[key]["status_var"].set(msg)
            # Reach back to the status label widget for fg colour change.
            # Layout: btn_row → [gen_btn, copy_btn, apply_btn, status_lbl]
            #   status_lbl is the 4th child of the row.
            # Easier: just store a reference. Add it now.
            lbl = self._tab_widgets[key].get("status_lbl")
            if lbl is not None:
                lbl.configure(fg=fg)
        except (tk.TclError, RuntimeError):
            pass
