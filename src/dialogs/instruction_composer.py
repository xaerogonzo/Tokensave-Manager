"""Toggle what the fleet is told, and compile it into the baseline.

The page is a thin view over `InstructionPolicy`. It stages changes, shows the
policy delta ABOVE the artifact delta — because the Markdown is the output, not
the thing being edited — and writes only on an explicit Apply.

THREE THINGS IT REFUSES TO DO, each with its reason.

* **It does not write a template another installation owns.** Ownership is read
  before anything else, and on anything but `OWNED_HERE` no proposal is built at
  all. This machine has had a source checkout and a `dist/` build whose
  baselines differed by four months; a policy artifact is the worst possible
  file to write into the wrong one.
* **It does not recompile on open.** Drift is reported and a recompile is
  offered. Recompiling automatically would turn a `git pull` — the compiled
  baseline is version-controlled — into a silent fleet-wide policy change.
* **It does not exceed the review budget.** A compile that would breach it is
  refused with its arithmetic, rather than being discovered later by the guard
  test in CI.

AND THE CLAIM IT MAKES, phrased carefully on screen as well as here: a rendered
instruction is what projects are TOLD. Whether an agent obeys it is not
something this Manager can observe, so nothing here says it will.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING

from constants import C
from theme import UiPumpMixin
from helpers import baseline_blocks as bb
from helpers.instruction_policy import RENDERED_KEYS

if TYPE_CHECKING:                                    # pragma: no cover
    from state import ManagerConfig


#: Keys that COMPILE INTO the baseline — text every wired project loads.
_LABELS = {
    "agent_may_commit": ("Allow automatic commits",
                         "Whether agents may run git commit themselves."),
    "agent_may_push":   ("Allow automatic pushes",
                         "Requires commits. Never force-push, tags or "
                         "branch deletion."),
}

#: Keys that change what this MANAGER does rather than what projects are told.
#: They cost no baseline bytes. A toggle only appears once the feature it
#: controls exists — a control that does nothing is the same
#: configuration-is-not-behaviour failure this dialog exists to avoid.
_OPERATIONAL = {
    "enable_session_note": (
        "Session note on exit",
        "A Stop hook records your prompts beside each repo, so later drafts "
        "can say why a change happened."),
    "enable_session_grounding": (
        "Use session context in drafts",
        "Attaches what you asked for to PR drafts. Falls back to reading "
        "transcripts when a project has no note yet."),
    "session_grounding_prompts": (
        "    · include your prompts",
        "Your own words, and the only authoritative source. ~1,200 tokens a "
        "day measured."),
    "session_grounding_prose": (
        "    · include assistant prose",
        "Richer but ~20,200 tokens a day measured — the prompt weight that "
        "was already measured to degrade small local models. Capped hard."),
}


class InstructionComposerDialog(UiPumpMixin, tk.Toplevel):
    """Agent policy, compiled into the shared baseline."""

    def __init__(self, parent, cfg: "ManagerConfig", on_log=None):
        super().__init__(parent)
        self.title("🎛 Agent Policy")
        self.configure(bg=C["base"])
        # Tall enough for both toggle groups AND the preview without clipping
        # the footer: measured against a driven window, where the first size
        # cut the Apply row in half.
        self.geometry("760x790")
        self._cfg = cfg
        self._on_log = on_log or (lambda *a, **k: None)
        self._staged = cfg.instruction_policy
        self._ownership = None
        self._projects = 0
        self._vars: dict = {}

        self._build_header()
        self._build_toggles()
        self._build_preview()
        self._build_footer()
        self._set_busy("Checking which installation owns this baseline…")
        # Widgets exist, no thread has started yet: the documented order.
        self._start_ui_pump()
        threading.Thread(target=self._ownership_worker, daemon=True,
                         name="composer-ownership").start()

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        tk.Label(self, text="What every wired project is told",
                 font=("Segoe UI", 12, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=18,
                                                  pady=(14, 0))
        tk.Label(self,
                 text="These settings are the policy. templates/"
                      "project-baseline.md is compiled from them.",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=18)
        tk.Label(self,
                 text="The Manager controls what projects are told. Whether an "
                      "agent obeys is not something it can observe.",
                 font=("Segoe UI", 8, "italic"), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=18, pady=(0, 10))

    def _build_toggles(self) -> None:
        self._toggle_group(" Agent behaviour — compiled into every project ",
                           RENDERED_KEYS, _LABELS)
        self._toggle_group(" Agent context — what this Manager does ",
                           tuple(_OPERATIONAL), _OPERATIONAL,
                           footer="These cost no baseline bytes — they change what "
                                  "this Manager does, not what projects are told.")

    def _toggle_group(self, title: str, keys, labels, footer: str = "") -> None:
        from theme import themed_checkbutton

        box = tk.LabelFrame(self, text=title, bg=C["base"], fg=C["mauve"],
                            font=("Segoe UI", 9, "bold"))
        box.pack(fill=tk.X, padx=18, pady=(0, 8))
        for key in keys:
            label, blurb = labels[key]
            var = tk.BooleanVar(value=bool(getattr(self._staged, key)))
            self._vars[key] = var
            row = tk.Frame(box, bg=C["base"])
            row.pack(fill=tk.X, padx=10, pady=(6, 0))
            themed_checkbutton(row, text=label, variable=var,
                               bg=C["base"], fg=C["text"],
                               font=("Segoe UI", 10),
                               command=lambda k=key: self._stage(k)).pack(
                                   anchor=tk.W)
            tk.Label(row, text=blurb, font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"], justify=tk.LEFT,
                     wraplength=640).pack(anchor=tk.W, padx=(26, 0))
        if footer:
            tk.Label(box, text=footer, font=("Segoe UI", 8, "italic"),
                     bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, padx=10,
                                                          pady=(6, 8))

    def _build_preview(self) -> None:
        box = tk.LabelFrame(self, text=" What Apply would do ", bg=C["base"],
                            fg=C["mauve"], font=("Segoe UI", 9, "bold"))
        box.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        self._preview = tk.Text(box, height=10, wrap="word", bd=0,
                                bg=C["mantle"], fg=C["text"],
                                font=("Consolas", 9), padx=10, pady=8)
        self._preview.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._preview.configure(state=tk.DISABLED)

    def _build_footer(self) -> None:
        row = tk.Frame(self, bg=C["base"])
        row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._apply_btn = tk.Button(row, text="Apply", command=self._apply,
                                    bg=C["blue"], fg=C["crust"],
                                    font=("Segoe UI", 9, "bold"),
                                    relief=tk.FLAT, padx=16, state=tk.DISABLED)
        self._apply_btn.pack(side=tk.RIGHT)
        tk.Button(row, text="Close", command=self.destroy, bg=C["surface1"],
                  fg=C["text"], relief=tk.FLAT,
                  padx=14).pack(side=tk.RIGHT, padx=(0, 8))

    # ── Ownership, read before anything else ──────────────────────────────

    def _ownership_worker(self) -> None:
        """Off the main thread, so every widget touch goes through `_post`.

        Never `self.after(...)` from here: on Windows that usually works, on
        Linux it BLOCKS without raising, and the surrounding `except` would
        swallow the one case that does raise.
        """
        try:
            from helpers.detection import _root_path
            from helpers.install_identity import read_ownership
            from helpers.instructions_posture import read_posture

            roots = [_root_path(r) for r in self._cfg.raw.get("search_roots", [])]
            fleet = read_posture(roots, self._cfg)
            owned = read_ownership(fleet.projects, self._cfg.template_dir)
            reaching = sum(o.count for o in owned.owners
                           if o.canonical_dir == _canonical(self._cfg.template_dir))
        except Exception as exc:                       # pragma: no cover - UI path
            # Bind now: Python clears the exception variable at the end of the
            # block, so a deferred lambda closing over `exc` would NameError.
            err = "%s: %s" % (exc.__class__.__name__, exc)
            self._post(lambda m=err: self._set_busy(
                "Could not determine ownership (%s). No changes offered." % m))
            return
        self._post(lambda o=owned, n=reaching: self._on_ownership(o, n))

    def _on_ownership(self, owned, reaching: int) -> None:
        if not self.winfo_exists():
            return
        from helpers.install_identity import OWNED_HERE, OWNERSHIP_UNOWNED

        self._ownership = owned
        self._projects = reaching
        if owned.state not in (OWNED_HERE, OWNERSHIP_UNOWNED):
            self._set_busy(
                "This baseline is owned by another installation — %s.\n\n"
                "No changes are offered here. Open the Manager that owns it, "
                "or point this one's template_dir at the baseline you mean to "
                "edit. Writing policy into another install's artifact would "
                "change the wrong fleet."
                % owned.summary())
            return
        self._refresh()

    # ── Staging and preview ───────────────────────────────────────────────

    def _stage(self, key: str) -> None:
        self._staged = self._staged.with_change(key, self._vars[key].get())
        self._refresh()

    def _refresh(self) -> None:
        text, can_apply = self._describe()
        self._write_preview(text)
        self._apply_btn.configure(state=tk.NORMAL if can_apply else tk.DISABLED)

    def _describe(self) -> "tuple[str, bool]":
        """The preview: policy delta first, artifact delta second."""
        invalid = self._staged.validate()
        if invalid:
            return ("Not a valid combination.\n\n%s" % invalid), False

        path = self._baseline_path()
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                current = handle.read()
        except OSError as exc:
            return ("Cannot read the baseline at\n  %s\n\n%s"
                    % (path, exc)), False

        compiled, refusal = bb.compile_baseline(current, self._staged)
        lines = ["Policy"]
        committed = self._cfg.instruction_policy
        changed = False
        for key, labels in ((RENDERED_KEYS, _LABELS), (tuple(_OPERATIONAL),
                                                       _OPERATIONAL)):
            for name in key:
                was, now = getattr(committed, name), getattr(self._staged, name)
                mark = "  →  " if was != now else "     "
                changed = changed or was != now
                lines.append("  %-24s %s%s%s"
                             % (labels[name][0], _onoff(was), mark, _onoff(now)))

        hook_lines, hook_pending = self._hook_lines()
        lines += hook_lines
        changed = changed or hook_pending

        if refusal:
            lines += ["", refusal]
            return "\n".join(lines), False

        before, after = bb.budget_bytes(current), bb.budget_bytes(compiled)
        delta = after - before
        lines += [
            "",
            "Compiled baseline",
            "  %-24s %d → %d B  (headroom %d of %d)"
            % ("size", before, after, bb.MAX_BASELINE_BYTES - after,
               bb.MAX_BASELINE_BYTES),
            "  %-24s %+d B × %d project%s ≈ %+d B per session-pass"
            % ("fleet exposure", delta, self._projects,
               "" if self._projects == 1 else "s", delta * self._projects),
            "",
            "Editing",
            "  %s" % path,
        ]
        states = bb.block_states(current, committed)
        drifted = [k for k, v in states.items() if v != bb.CURRENT]
        if drifted:
            lines += ["", "The file does not currently match the stored policy "
                          "(%s). Apply recompiles it." % ", ".join(drifted)]
        if bb.findings(current):
            lines += ["", "The superseded hand-written instruction is still "
                          "present outside the managed blocks; Apply removes "
                          "that one line and nothing else."]
        if not changed and not drifted and not bb.findings(current):
            lines += ["", "Nothing to apply — the baseline already matches."]
            return "\n".join(lines), False
        return "\n".join(lines), True

    def _hook_lines(self) -> "tuple[list, bool]":
        """Policy state and ARTIFACT state, separately. `(lines, pending)`.

        Two facts, not one. A toggle that says ON beside a hook that is absent
        is the case worth seeing — and the one a single combined label would
        hide, which is how "turning it off" quietly stops meaning anything.
        """
        from helpers import session_note

        state, detail = session_note.installed_state()
        wanted = bool(self._staged.enable_session_note)
        installed = state == session_note.CURRENT
        pending = wanted != installed
        lines = ["", "Session note hook",
                 "  %-24s %s" % ("policy", "ON" if wanted else "OFF"),
                 "  %-24s %s%s" % ("hook", state,
                                   "  (%s)" % detail if detail and
                                   state != session_note.ABSENT else "")]
        if pending:
            lines.append("  %-24s %s" % (
                "apply will",
                "install it" if wanted else "remove our entry and script"))
        return lines, pending

    def _write_preview(self, text: str) -> None:
        self._preview.configure(state=tk.NORMAL)
        self._preview.delete("1.0", tk.END)
        self._preview.insert("1.0", text)
        self._preview.configure(state=tk.DISABLED)

    def _set_busy(self, message: str) -> None:
        self._write_preview(message)
        self._apply_btn.configure(state=tk.DISABLED)

    # ── Apply ─────────────────────────────────────────────────────────────

    def _baseline_path(self) -> str:
        # normpath so the shown path does not mix separators: template_dir is
        # stored with forward slashes and os.path.join adds a backslash, which
        # renders as "…/templates\project-baseline.md" and reads like a bug.
        return os.path.normpath(os.path.join(self._cfg.template_dir or "",
                                             "project-baseline.md"))

    def _apply(self) -> None:
        path = self._baseline_path()
        if not messagebox.askyesno(
                "Apply agent policy?",
                "This rewrites the managed blocks in\n\n%s\n\nwhich every "
                "wired project loads on every message. Authored text outside "
                "those blocks is not touched.\n\nApply now?" % path,
                parent=self):
            return
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                current = handle.read()
            compiled, refusal = bb.compile_baseline(current, self._staged)
            if refusal:
                messagebox.showerror("Not applied", refusal, parent=self)
                return
            saved = self._cfg.save_instruction_policy(self._staged)
            if saved:
                messagebox.showerror("Not applied", saved, parent=self)
                return
            _write_atomic(path, compiled)
            hook_error = self._reconcile_hook()
            if hook_error:
                # The policy and baseline DID land; only the hook did not.
                # Saying so beats a blanket "not applied" that would send the
                # user looking for a change that is already on disk.
                messagebox.showwarning(
                    "Policy applied, hook not changed", hook_error, parent=self)
        except OSError as exc:
            err = "%s: %s" % (exc.__class__.__name__, exc)
            messagebox.showerror("Not applied", err, parent=self)
            return

        self._on_log("Agent policy applied to %s" % path, C["green"])
        self._refresh()          # re-read from disk and re-report
        messagebox.showinfo(
            "Applied",
            "Policy saved and the baseline recompiled.\n\nClaude Code reads "
            "instructions when a session starts, so sessions already running "
            "keep the previous text until they are restarted.", parent=self)


    def _reconcile_hook(self) -> str:
        """Make the artifact match the policy. `""`, or why it could not.

        ON installs; OFF **uninstalls** — entry and script both. A toggle that
        only stops future installs is a preference wearing a switch's clothes.
        """
        from helpers import session_note

        wanted = bool(self._staged.enable_session_note)
        state, _detail = session_note.installed_state()
        if wanted and state == session_note.CURRENT:
            return ""
        if not wanted and state == session_note.ABSENT:
            return ""
        if wanted:
            ok, error, actions = session_note.install(
                python_exe=self._cfg.raw.get("python_exe", ""))
        else:
            ok, error, actions = session_note.uninstall()
        for action in actions:
            self._on_log("  %s" % action, C["overlay0"])
        return "" if ok else error


def _onoff(value: bool) -> str:
    return "ON " if value else "OFF"


def _canonical(path: str) -> str:
    from helpers.instructions_posture import canonical

    return canonical(path or "")


def _write_atomic(path: str, text: str) -> None:
    """Temp + replace. A half-written baseline is worse than an unchanged one.

    No `.bak` is left behind: this file is version-controlled, so git already
    holds its history, and a litter of backups beside a template that fifteen
    projects read would be its own problem.
    """
    import tempfile

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".baseline_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
