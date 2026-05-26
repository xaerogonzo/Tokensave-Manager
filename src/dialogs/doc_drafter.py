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
import re

from helpers.changelog_patch import (
    read_unreleased, insert_unreleased_bullets,
    read_section_bullets,
)
from helpers.readme_patch import (
    read_highlights,
    insert_readme_highlights_subsection, read_subsection_bullets,
)


# ── Bullet-quality helpers (truncation + redundancy filters) ────────────────
#
# These run on the AI's output BEFORE the patcher applies it.  Catch the two
# Ollama failure modes observed during dogfooding:
#   1. Truncation — bullet ends mid-clause with no closing punctuation and
#      a trailing stop-word ("for", "the", "to", ...)
#   2. Redundancy — bullet semantically restates an existing entry that the
#      patcher would happily duplicate.
#
# Same prompt-then-code-defence pattern as the gitignore AI Suggest dedup.

_TRUNCATION_TRAILING = {
    "for", "the", "to", "with", "and", "or", "of",
    "in", "on", "at", "by", "as", "is", "a", "an",
}

_STOP_WORDS = {
    # articles / conjunctions
    "the", "a", "an", "and", "or", "but", "if", "then", "than",
    # prepositions (commonly bloat dedup denominator without semantic weight)
    "of", "to", "in", "on", "at", "by", "as", "for", "with", "from", "into",
    "via", "per", "between", "through", "across", "over", "under",
    "after", "before", "during", "while", "when",
    # copulas / pronouns
    "be", "is", "are", "was", "were", "it", "its", "this", "that",
    # adverbs that surface a lot in commit prose with no scope info
    "also", "now", "still", "even", "just", "only",
}


# Placeholder bullets the model writes despite the prompt telling it to omit
# empty sections.  ``### Fixed\n- None`` happens regularly with small models;
# anchor with ``^...$`` so legitimate bullets that merely START with one of
# these words ("- None of the existing patches handle case X") stay through.
_NOOP_BULLET_PATTERNS = [
    re.compile(
        r"^-?\s*(none|n/?a|nothing|tbd|no\s+changes?"
        r"|nothing\s+to\s+(add|report|note|do)"
        r"|n\.?a\.?|empty|placeholder)\s*\.?$",
        re.IGNORECASE,
    ),
]


def _is_noop_bullet(bullet):
    """True if the bullet is a literal placeholder with no content.

    Catches: ``- None``, ``- N/A``, ``- Nothing``, ``- TBD``,
    ``- no changes``, ``- nothing to add``, ``- nothing to report``,
    ``- (none)``.  Conservative — the regex is anchored with ``^...$`` so a
    bullet that merely STARTS with one of these words is preserved (e.g.
    ``- None of the existing patches handle case X``).

    The normalisation chain MUST start with .strip() (NOT .lstrip("-")) so
    Windows CRLF carriage returns are removed BEFORE the dash-strip.
    Ollama on Windows occasionally emits ``\\r\\n`` line endings inside
    the stream.
    """
    s = (bullet or "").strip().lstrip("-").strip().strip("()").strip()
    return any(p.match(s) for p in _NOOP_BULLET_PATTERNS)


def _looks_truncated(bullet):
    """Return True if ``bullet`` looks cut off mid-sentence.

    Conservative — only flags clear truncation signatures:
      - line does NOT end with closing punctuation ``.`` ``)`` `` ` `` `` ' ``
        `` " `` ``:``
      - AND the final word is a stop-word fragment ("for", "the", ...) that
        almost certainly precedes an object that wasn't generated

    A bullet ending in a parenthetical citation (``"... (helpers/foo.py)"``)
    is correctly NOT flagged (ends with ``)``).  A bullet ending in a
    non-stop-word like ``"... see issue #42"`` is also not flagged — only
    obvious mid-sentence cuts trip.
    """
    s = (bullet or "").rstrip()
    if not s:
        return False
    if s.endswith((".", ")", "`", '"', "'", ":", "!", "?")):
        return False
    last_word = s.split()[-1].lower().rstrip(",;:")
    return last_word in _TRUNCATION_TRAILING


def _token_set(bullet):
    """Lowercase-tokenise + strip stop-words + drop short tokens.

    Used by ``_is_duplicate``.  Length filter (>2 chars) discards noise like
    digits, single letters, and one-char punctuation residue without losing
    meaningful short tokens (gitignore semantics catches "AI", "PR", "UI",
    "CLI" at length 2-3 — keep >2 threshold).
    """
    s = re.sub(r"[^\w\s]", " ", (bullet or "").lower())
    return {t for t in s.split() if t not in _STOP_WORDS and len(t) > 2}


def _is_duplicate(new_bullet, existing_bullet):
    """Combined Jaccard + containment check against ONE existing bullet.

    Jaccard ≥ 0.6 catches "mostly the same words" cases.  The containment
    (overlap) coefficient at 0.70 catches the asymmetric case where one
    bullet is much shorter than the other but its tokens are largely a
    SUBSET of the larger one — plain Jaccard misses these because the
    union inflates the denominator.

    Threshold 0.65 was chosen by dogfooding against the realistic failure
    case: existing 13-token detailed bullet, new 6-token generic summary
    bullet describing the same feature ('add automated CHANGELOG updates
    via AI'), shared 4 tokens → 4/6 = 0.67 overlap.  Stricter 0.85 missed
    every paraphrase-style failure; lower than 0.60 starts catching
    unrelated bullets that happen to share scope prefix words.  0.65 is
    the sweet spot from the live test data.
    """
    a = _token_set(new_bullet)
    b = _token_set(existing_bullet)
    if not a or not b:
        return False
    union = a | b
    jaccard = len(a & b) / len(union) if union else 0.0
    overlap = len(a & b) / min(len(a), len(b))
    return jaccard >= 0.6 or overlap >= 0.65


def _sanitise_raw_draft(text):
    """Strip head/tail artefacts the model sometimes appends before parsing.

    Local models occasionally wrap their output with markdown code fences,
    italic 'generated by AI' footers, HTML comments, or horizontal-rule
    separators.  None of these are bullets, and if they slip past the
    section parser into a bullet body they distort the tokeniser /
    truncation guard / dedup math (e.g. ``- ``` text`` would not be
    flagged as truncated because it ends in a backtick).

    Conservative — only strips lines that are *clearly* not bullets and
    only at the head/tail.  In-body content is untouched, so a bullet
    that legitimately mentions a code fence inside its text survives.

    Patterns removed (lines, after .strip()):
      - empty / whitespace-only
      - ``\\`\\`\\``` opener / closer (with or without language hint)
      - ``<!--`` HTML comment opener
      - ``---`` / ``***`` / ``___`` horizontal rules
      - lines starting with ``*generated``, ``_generated``, ``*draft``,
        ``*ai-generated`` (italic AI-footer markers)
    """
    lines = text.splitlines()
    # Trailing junk
    while lines:
        last = lines[-1].strip()
        if (not last
                or last.startswith("```")
                or last.startswith("<!--")
                or last in {"---", "***", "___"}
                or last.lower().startswith(("*generated", "_generated",
                                            "*draft", "*ai-generated"))):
            lines.pop()
            continue
        break
    # Leading junk (e.g. ``` opener or blank lines)
    while lines:
        first = lines[0].strip()
        if not first or first.startswith("```"):
            lines.pop(0)
            continue
        break
    return "\n".join(lines)


def _filter_bullets(bullets_md, existing_bullets, *,
                    dedup_against_existing=True):
    """Apply truncation + dedup + noop-placeholder filters to a bullet block.

    Args:
        bullets_md:               newline-separated bullet block from the LLM
        existing_bullets:         list of existing bullet lines to dedup against
        dedup_against_existing:   keyword-only.

            **True (CHANGELOG mode, default)** — drop any new bullet that
            semantically duplicates anything in ``existing_bullets``.  The
            CHANGELOG drafter prompt asks the model to output ONLY new
            content under append-only semantics, so any output bullet that
            mirrors an existing on-disk bullet is unwanted duplication.

            **False (README mode)** — the README drafter prompt asks the
            model to mirror ALL existing bullets PLUS new ones, because the
            ``insert_readme_highlights_subsection`` patcher REPLACES the
            whole sub-section (omitting a mirrored bullet deletes it from
            the file).  Dropping bullets that match existing would be
            DESTRUCTIVE.  Instead, dedup is performed against ``kept``
            (the bullets already accepted from THIS DRAFT) so the model
            duplicating its OWN new bullets is still caught.  Quality-swap
            precedence: when a duplicate is detected, the LONGER bullet
            wins by +8 character slack — prevents a truncated paraphrase
            earlier in the stream from displacing a polished later version.

    Returns ``(kept_md, truncated_n, duplicate_n, noop_n)`` where
    ``kept_md`` is the filtered bullets joined back into a newline-separated
    string.  Counters track each rejection class so the status bar can
    surface a per-class summary.
    """
    kept = []
    truncated_n = 0
    duplicate_n = 0
    noop_n = 0
    for line in bullets_md.splitlines():
        stripped = line.lstrip()
        # Pass through non-bullet lines untouched (lets prose interleave —
        # not common in our output but harmless).
        if not (stripped.startswith("- ") or stripped.startswith("* ")):
            kept.append(line)
            continue
        if _looks_truncated(stripped):
            truncated_n += 1
            continue
        if _is_noop_bullet(stripped):
            noop_n += 1
            continue
        if dedup_against_existing:
            # CHANGELOG mode — drop if it matches anything on disk.
            if any(_is_duplicate(stripped, eb) for eb in existing_bullets):
                duplicate_n += 1
                continue
            kept.append(line)
        else:
            # README mode — dedup only against bullets we've already kept
            # FROM THIS SAME DRAFT.  Existing-on-disk bullets are NOT a
            # target; the model is supposed to mirror them back so the
            # REPLACE patcher preserves them.  Quality-swap when matched:
            # longer (more detailed) bullet wins.
            is_dup = False
            for idx, kb in enumerate(kept):
                kb_stripped = kb.lstrip()
                if not kb_stripped.startswith(("- ", "* ")):
                    continue
                if _is_duplicate(stripped, kb_stripped):
                    is_dup = True
                    # Swap when the incoming bullet is materially longer
                    # (~ a phrase more detail).  +8 slack avoids
                    # thrashing on single-word differences.  Ties favour
                    # the earlier-kept line for stable diff order.
                    if len(stripped) > len(kb_stripped) + 8:
                        kept[idx] = line
                    duplicate_n += 1
                    break
            if not is_dup:
                kept.append(line)
    return "\n".join(kept).strip(), truncated_n, duplicate_n, noop_n
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
        # Populated by _apply_changelog_bullets / _apply_readme_subsection
        # right before they return.  _on_apply_result reads + clears.
        self._last_filter_stats: dict = {}

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

        # <<Modified>> binding — user-typed edits re-enable Apply if the
        # buffer holds non-placeholder content.  Apply may have been hard-
        # disabled by a previous "all filtered" generate result; without
        # this binding the user couldn't rescue the situation by typing
        # their own bullet.  Tk fires <<Modified>> once per flag toggle;
        # _on_text_modified resets the flag so subsequent edits also fire.
        txt.bind("<<Modified>>",
                  lambda _e, k=key: self._on_text_modified(k))
        # Clear the modified flag set by the programmatic insert above so
        # the first USER edit (not our setup insert) fires the binding.
        try:
            txt.edit_modified(False)
        except tk.TclError:
            pass

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
        # wraplength keeps long filter messages from pushing buttons off-screen
        # on narrow window sizes; status text wraps inside the row instead.
        status_lbl = tk.Label(btn_row, textvariable=status_var,
                              bg=C["base"], fg=C["overlay0"],
                              font=("Segoe UI", 8),
                              wraplength=420, justify=tk.LEFT)
        status_lbl.pack(side=tk.LEFT, padx=(12, 0), fill=tk.X, expand=True)

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

    def _filter_draft_text(self, key, raw_text):
        """Filter raw LLM output at generate time.

        Pipeline: sanitise (strip head/tail artefacts) → parse per-tab format
        → per-section filter (truncation + dedup + noop) → reassemble into
        clean markdown.  Returns ``(filtered_text, trunc_n, dup_n, noop_n)``.

        For empty results the caller (``_on_generate_done``) is responsible
        for displaying a placeholder phrase AND disabling the Apply button —
        the placeholder must never reach the patcher.

        Reassembly is whitespace-clean: ``\\n\\n`` separates sections, no
        accumulated blank-line padding when sections drop out.  Verified
        by the multi-section reassembly verification case.
        """
        target_path = os.path.join(self._project_path,
                                    self._tab_widgets[key]["target"])
        sanitised = _sanitise_raw_draft(raw_text or "")
        total_trunc = total_dup = total_noop = 0

        if key == "changelog":
            # CHANGELOG: grouped "### Section / - bullet" output
            pairs = self._parse_grouped_bullets(sanitised)
            kept_pairs = []
            for section, bullets in pairs:
                existing = read_section_bullets(target_path, section)
                filtered, t_n, d_n, n_n = _filter_bullets(bullets, existing)
                total_trunc += t_n
                total_dup += d_n
                total_noop += n_n
                if filtered.strip():
                    kept_pairs.append((section, filtered.strip()))
            # Compact reassembly: no leading blanks, single blank between sections
            chunks = [f"### {sec}\n{body}" for sec, body in kept_pairs]
            return "\n\n".join(chunks), total_trunc, total_dup, total_noop

        # README: single sub-section with **Header** + bullets
        parsed = self._split_readme_subsection(sanitised)
        if parsed is None:
            # Can't parse — pass raw through and let the apply path surface
            # the "missing **Bold header**" error message.  No filter stats.
            return sanitised, 0, 0, 0
        header_line, bullets = parsed
        existing = read_subsection_bullets(target_path, header_line)
        # README mode: the patcher REPLACES the matching sub-section, so the
        # model is told to mirror existing bullets back. Dropping bullets
        # that match existing-on-disk would DELETE them from the file.
        # Filter only catches truncation, noop, and self-duplicates here.
        filtered, total_trunc, total_dup, total_noop = _filter_bullets(
            bullets, existing, dedup_against_existing=False)
        if filtered.strip():
            return (f"{header_line}\n{filtered.strip()}",
                    total_trunc, total_dup, total_noop)
        return "", total_trunc, total_dup, total_noop

    def _on_generate_done(self, key, raw_text):
        try:
            if not self.winfo_exists():
                return
            # Filter raw model output against existing target content
            filtered_text, t_n, d_n, n_n = self._filter_draft_text(
                key, raw_text)
            w = self._tab_widgets[key]
            txt = w["text"]
            txt.delete("1.0", tk.END)
            has_content = bool(filtered_text.strip())
            if has_content:
                txt.insert("1.0", filtered_text.strip())
                self._tab_state[key]["draft"] = filtered_text.strip()
            else:
                # Visual placeholder ONLY — Apply button is hard-disabled
                # below so this string never reaches the patchers.  README
                # gets an extra-explicit message because its REPLACE patcher
                # would delete the existing sub-section if applied with
                # nothing.  CHANGELOG's append-only patcher is recoverable
                # (no destructive failure mode).
                if key == "readme":
                    placeholder = (
                        "(all bullets filtered — README's REPLACE patcher "
                        "would DELETE the existing sub-section if Applied "
                        "with no bullets.  Apply is disabled.  Click "
                        "Generate to retry.)"
                    )
                else:
                    placeholder = ("(all bullets filtered — click "
                                   "Generate to retry)")
                txt.insert("1.0", placeholder)
                txt.tag_add("placeholder", "1.0", tk.END)
                self._tab_state[key]["draft"] = ""
            # Reset the text-widget modified flag so the <<Modified>> binding
            # fires on subsequent USER edits (not on our programmatic insert).
            try:
                txt.edit_modified(False)
            except tk.TclError:
                pass
            w["gen_btn"].configure(state=tk.NORMAL)
            # LOAD-BEARING: Apply enabled only when filtered text has content.
            # Empty filtered result means the visible text widget holds the
            # placeholder phrase — applying that would splice the literal
            # placeholder string into CHANGELOG.md / README.md.
            w["apply_btn"].configure(
                state=tk.NORMAL if has_content else tk.DISABLED)
            suffix = self._format_filter_suffix(t_n, d_n, n_n)
            if has_content:
                base = "Draft ready — review and Apply."
                self._set_status(key, base + suffix, C["green"])
            else:
                self._set_status(
                    key,
                    f"All bullets filtered.{suffix}  Click Generate to retry.",
                    C["red"])
        except (tk.TclError, RuntimeError):
            return

    def _on_text_modified(self, key):
        """Re-enable Apply when the user manually types non-placeholder content.

        Without this binding, a user faced with an "all filtered" state who
        wants to write their own bullet would find Apply locked because the
        disable was set by the worker and never re-evaluated.

        Tk only fires <<Modified>> once per modified-flag toggle; we have to
        reset edit_modified(False) explicitly to keep receiving events.
        """
        try:
            if not self.winfo_exists():
                return
            w = self._tab_widgets[key]
            txt = w["text"]
            if not txt.edit_modified():
                return
            current = txt.get("1.0", "end-1c").strip()
            is_placeholder = (current.startswith("(all bullets filtered")
                              or current.startswith("(no draft yet"))
            if current and not is_placeholder:
                w["apply_btn"].configure(state=tk.NORMAL)
            txt.edit_modified(False)
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
            # content via the appropriate patcher.  Both paths use
            # append-only insertion to avoid the small-model "drop existing
            # content" failure mode discovered during dogfooding — see
            # memory/doc_drafter.md for the locked design rationale.
            if key == "changelog":
                ok, msg = self._apply_changelog_bullets(target_path,
                                                         final_content)
            else:
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
            # Pop and clear filter stats so we don't leak counts across Applies
            stats = self._last_filter_stats or {}
            self._last_filter_stats = {}
            trunc_n = stats.get("truncated", 0)
            dup_n   = stats.get("duplicates", 0)
            noop_n  = stats.get("noop", 0)
            filter_suffix = self._format_filter_suffix(trunc_n, dup_n, noop_n)
            target = self._tab_widgets[key]["target"]
            if ok:
                status_msg = f"✓ {msg}{filter_suffix}"
                self._set_status(key, status_msg, C["green"])
                self._on_log(f"[doc-drafter] {target}: {msg}{filter_suffix}",
                             C["green"])
                # Offer to commit the change (matches CHANGELOG drafter UX).
                self._on_commit_offer(self._project_path,
                                       f"{target} (doc-drafter)")
            else:
                status_msg = f"✗ {msg}{filter_suffix}"
                self._set_status(key, status_msg, C["red"])
                self._on_log(f"[doc-drafter] {target}: {msg}{filter_suffix}",
                             C["red"])
        except (tk.TclError, RuntimeError):
            return

    @staticmethod
    def _format_filter_suffix(trunc_n, dup_n, noop_n=0):
        """Format filter counts as a compact tail suffix.

        Bounded length so an unexpected 45+ count doesn't push UI off-screen.
        Returns empty string when ALL counts are zero (clean drafts get no
        noise).  ``noop_n`` defaults to 0 for backward compat with any older
        call sites that haven't been updated to the 4-tuple filter return.
        """
        if not trunc_n and not dup_n and not noop_n:
            return ""
        bits = []
        if trunc_n:
            bits.append(f"{trunc_n} truncated")
        if dup_n:
            bits.append(f"{dup_n} duplicate{'s' if dup_n != 1 else ''}")
        if noop_n:
            bits.append(f"{noop_n} placeholder{'s' if noop_n != 1 else ''}")
        return f"  ⚠ {', '.join(bits)} dropped — Regenerate to retry"

    # ── CHANGELOG grouped-bullets parser + dispatcher ──────────────────────

    @staticmethod
    def _parse_grouped_bullets(draft_text):
        """Parse '### Header / - bullet ...' grouped output into (header, body) pairs.

        Walks lines; whenever it sees ``^### `` it starts a new section.
        Bullets are everything between two ``^### `` markers (or to EOF).
        Empty sections (header with no bullets) are dropped — keeps the
        output free of stray ``### Added`` headers with nothing under them.

        Returns ``[(section_heading_raw, bullets_text), ...]``.  The
        downstream patcher canonicalises section names, so raw headers
        like ``"fixed"`` / ``"FIXED"`` / ``"Fixes"`` all collapse correctly.
        """
        pairs = []
        current_section = None
        current_lines = []
        for ln in draft_text.splitlines():
            stripped = ln.strip()
            if stripped.startswith("### "):
                if current_section is not None:
                    body = "\n".join(current_lines).strip()
                    if body:
                        pairs.append((current_section, body))
                current_section = stripped[4:].strip()
                current_lines = []
            elif current_section is not None:
                current_lines.append(ln)
        # Flush the final section.
        if current_section is not None:
            body = "\n".join(current_lines).strip()
            if body:
                pairs.append((current_section, body))
        return pairs

    def _apply_changelog_bullets(self, target_path, draft_text):
        """Parse grouped output, filter truncated + duplicate bullets, dispatch.

        Per-section filtering catches the two Ollama failure modes (truncation,
        redundancy hallucination) BEFORE the patcher runs.  Suppressed counts
        are stored in self._last_filter_stats so _on_apply_result can surface
        them in the status bar.

        On the first patcher failure, returns immediately with the section
        that failed in the message.  Earlier successful sections are NOT
        rolled back — append-only is naturally partial-fail-tolerant; the
        user can re-Apply to retry the remainder.
        """
        pairs = self._parse_grouped_bullets(draft_text)
        if not pairs:
            return False, ("Draft is missing '### Section' headers — "
                           "CHANGELOG mode requires bullets grouped under "
                           "### Added / ### Fixed / ### Changed / ### Removed.")
        applied = []
        total_truncated = 0
        total_duplicates = 0
        total_noop = 0
        empty_after_filter = 0
        for section, bullets in pairs:
            existing = read_section_bullets(target_path, section)
            filtered, trunc_n, dup_n, noop_n = _filter_bullets(bullets, existing)
            total_truncated += trunc_n
            total_duplicates += dup_n
            total_noop += noop_n
            if not filtered.strip():
                empty_after_filter += 1
                continue                # nothing left to insert for this section
            ok, msg = insert_unreleased_bullets(target_path, section, filtered)
            if not ok:
                return False, f"{section}: {msg} (applied so far: {applied})"
            applied.append(section)
        self._last_filter_stats = {
            "truncated":  total_truncated,
            "duplicates": total_duplicates,
            "noop":       total_noop,
        }
        if not applied:
            return False, ("All bullets filtered out "
                           f"({total_truncated} truncated, "
                           f"{total_duplicates} duplicates, "
                           f"{total_noop} placeholders).  Click "
                           "Regenerate to retry.")
        summary = f"appended to {len(applied)} section(s): {', '.join(applied)}"
        if empty_after_filter:
            summary += f" ({empty_after_filter} section(s) emptied by filter)"
        return True, summary

    # ── README sub-section parser + dispatcher ─────────────────────────────

    @staticmethod
    def _split_readme_subsection(draft_text):
        """Extract first ``**Header**`` line + remaining bullets from a draft.

        Returns ``(header_line, bullets_md)`` on success, or ``None`` if no
        bold-header line was found.  Shared by both the apply path and the
        filter-at-generate path so they parse the same way.
        """
        lines = draft_text.splitlines()
        header_idx = None
        for i, ln in enumerate(lines):
            stripped = ln.strip()
            if (stripped.startswith("**") and stripped.endswith("**")
                    and len(stripped) > 4):
                header_idx = i
                break
            # Also accept "**Header**:" form
            if (stripped.startswith("**") and "**" in stripped[2:]
                    and stripped.endswith(":")):
                header_idx = i
                break
        if header_idx is None:
            return None
        header_line = lines[header_idx].strip()
        bullets = "\n".join(lines[header_idx + 1:]).strip("\n")
        return header_line, bullets

    def _apply_readme_subsection(self, target_path, draft_text):
        """Parse the first ``**Header**`` line as the sub-section header and
        dispatch to insert_readme_highlights_subsection.

        Robust to:
          - blank lines before the header
          - prose preamble before the header (silently skipped)
          - trailing whitespace
        Falls back to a clear error if no header line is found in the draft.
        """
        parsed = self._split_readme_subsection(draft_text)
        if parsed is None:
            return False, ("Draft is missing a `**Bold header**` line — "
                           "README mode requires a sub-section header as "
                           "the first non-blank line.")
        header_line, bullets = parsed

        # Filter at Apply-time too (safety net for user-edited content).
        # README mode: the patcher REPLACES the matching sub-section, so the
        # model is expected to mirror existing bullets back as part of its
        # output.  Dropping bullets that match existing-on-disk would DELETE
        # preserved content — use dedup_against_existing=False so the filter
        # only catches truncation, noop placeholders, and bullets that
        # duplicate something already kept FROM THIS DRAFT (self-dedup).
        existing = read_subsection_bullets(target_path, header_line)
        filtered, trunc_n, dup_n, noop_n = _filter_bullets(
            bullets, existing, dedup_against_existing=False)
        self._last_filter_stats = {
            "truncated":  trunc_n,
            "duplicates": dup_n,
            "noop":       noop_n,
        }
        if not filtered.strip():
            # README replace semantics: applying empty bullets would DELETE
            # the existing sub-section.  Refuse to write.
            return False, ("All bullets filtered out "
                           f"({trunc_n} truncated, {dup_n} self-duplicates, "
                           f"{noop_n} placeholders).  README's REPLACE "
                           "patcher would DELETE the existing sub-section "
                           "if applied with no bullets.  Click Regenerate.")
        return insert_readme_highlights_subsection(
            target_path, header_line,
            filtered + "\n" if filtered else "\n")

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
