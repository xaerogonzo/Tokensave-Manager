"""OverviewMixin — the one screen that says what all the wiring adds up to.

## Why the dialog needed a front door

Five panels, three verification tiers, a skip list and a migration counter,
each correct about its own question, and nothing that answered the user's:
*can any project be answered from the wrong codebase, and is every project
served at all?* The result was a dialog whose loudest control was an Apply
button on projects that were working fine.

## Three layers, three questions

Kept apart deliberately, because merging them is how the maze reappears in a
new shape:

  Headline  — can any project be served from the WRONG codebase?
  Table     — how is THIS project served?
  Details   — what hardening and configuration exists?

``strict_tree`` belongs to the third and only the third. It is genuine
hardening — it turns a wrong-tree answer into a refusal — but it is not what
makes projects independent: it was ``true`` in both projects during the
desktop-collision incident and did nothing, because it guards ``graph_root``
redirection inside a server rather than which server a client talks to. A
badge reading "AUTOMATIC / hardened" would rebuild the confusion this replaces.

## The badge comes from service, never from tier

``EXPLICIT_INERT`` — a correct ``.mcp.json`` in an untrusted folder — is
harmless while a fallback exists and fatal once it does not. Reading the badge
off ``tier`` would put a green checkmark on a project served by nothing. See
:mod:`helpers.mcp_posture`, where the two are separated for exactly this
reason.

A mixin, so it reads ``self`` attributes the host dialog owns (``_body``,
``_cfg``, ``_posture``, ``_render()``, ``_log_to_app()``). It is never
instantiated on its own.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from helpers.mcp_posture import (
    READ_MALFORMED,
    READ_OK,
    SERVICE_AUTOMATIC,
    SERVICE_SELF,
    SERVICE_UNKNOWN,
    SERVICE_UNSERVED,
    SERVICE_WRONG,
    VERDICT_NO,
)
from helpers.mcp import (_apply_mcp_fix, _classify_mcp_entry,
                         _project_mcp_path, remove_mcp_entry)
from helpers.mcp_setup import groups, plan_independence, ungrouped
from helpers.mcp_paths import (
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETURNED,
    USER_SCOPE_RETIRED_KEY,
)

#: `(badge, colour key, what it means)` per service. The wording says how the
#: project is served rather than what its files contain — a user reading this
#: row wants to know whether to act, and "no binding of its own" is not an
#: answer to that.
_SERVICE_ROWS = {
    SERVICE_SELF: ("✓", "green", "bound to itself"),
    SERVICE_AUTOMATIC: ("✓", "sky",
                        "served automatically from the session's folder"),
    SERVICE_WRONG: ("✗", "red", "bound to a DIFFERENT project"),
    SERVICE_UNSERVED: ("✗", "red", "no tokensave at all"),
    SERVICE_UNKNOWN: ("?", "overlay0", "could not determine"),
}


class OverviewMixin:
    """Renders the posture headline, the per-project table and the toggle."""

    # ── headline ─────────────────────────────────────────────────────────

    def _render_overview(self):
        posture = getattr(self, "_posture", None)
        if posture is None:
            return

        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(4, 2), ipady=8)

        headline, colour, detail = self._headline(posture)
        tk.Label(box, text="  " + headline, font=("Segoe UI", 12, "bold"),
                 bg=C["surface0"], fg=C[colour], anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(box, text="  " + detail, font=("Segoe UI", 9),
                 bg=C["surface0"], fg=C["text"], justify=tk.LEFT,
                 wraplength=740, anchor=tk.W).pack(fill=tk.X, padx=8)
        tk.Label(box, text="  " + self._counts(posture),
                 font=("Consolas", 9), bg=C["surface0"], fg=C["subtext"],
                 anchor=tk.W).pack(fill=tk.X, padx=8, pady=(4, 2))

        self._render_incomplete_note(box, posture)
        self._render_mode_toggle(box, posture)
        self._render_plan(box, posture)
        self._render_project_table(posture)
        self._render_details_toggle()

    @staticmethod
    def _headline(posture) -> tuple:
        """`(headline, colour key, detail)` — the only thing above the fold.

        Green requires `headline_ok`, which is `independent` AND `covered` AND
        every source actually read. Any one of those alone would go green on a
        machine with a real problem: independence says nothing about a project
        served by nothing, coverage says nothing about routing, and both are
        worthless if `~/.claude.json` could not be parsed.
        """
        if posture.headline_ok:
            return ("✅  Your projects are independent", "green",
                    "Every Claude Code session serves its own project — from "
                    "that project's own .mcp.json where it has one, otherwise "
                    "resolved automatically from the session's folder. "
                    "Nothing can answer from another codebase.")

        if posture.independent == VERDICT_NO:
            if posture.misbound:
                names = ", ".join(p.name for p in posture.misbound[:4])
                return ("⚠  A project is bound to the wrong codebase", "peach",
                        "%s has a .mcp.json binding tokensave to a different "
                        "project. Every query asked there is answered from "
                        "that other tree and looks completely normal doing "
                        "it." % names)
            return ("⚠  One project at a time", "peach",
                    "Claude Desktop defines its own `tokensave`. It spawns "
                    "that server app-level, so its working directory is the "
                    "app rather than your session — every Desktop-hosted "
                    "Claude Code session inherits one server whatever repo it "
                    "is in, and it wins the name over each project's own "
                    "binding.")

        if posture.covered == VERDICT_NO:
            names = ", ".join(p.name for p in posture.unserved[:4])
            more = "" if len(posture.unserved) <= 4 else ", …"
            return ("⚠  %d project%s has no tokensave at all"
                    % (len(posture.unserved),
                       "" if len(posture.unserved) == 1 else "s"), "peach",
                    "Nothing can answer questions in %s%s. Both global "
                    "entries are retired, so a project without its own "
                    ".mcp.json gets no server — which is the deliberate trade "
                    "of that posture, not a fault, as long as you meant it."
                    % (names, more))

        return ("?  Posture incomplete", "overlay0",
                "One of the facts this depends on could not be read, so no "
                "claim is made either way. The note below says which.")

    @staticmethod
    def _counts(posture) -> str:
        """A population line, so a silently-empty table cannot read as good news."""
        by = {}
        for project in posture.projects:
            by[posture.service(project)] = by.get(posture.service(project), 0) + 1
        parts = ["%d project%s" % (len(posture.projects),
                                   "" if len(posture.projects) == 1 else "s")]
        for service, label in ((SERVICE_SELF, "bound to themselves"),
                               (SERVICE_AUTOMATIC, "served automatically"),
                               (SERVICE_WRONG, "MISBOUND"),
                               (SERVICE_UNSERVED, "unserved"),
                               (SERVICE_UNKNOWN, "unknown")):
            if by.get(service):
                parts.append("%d %s" % (by[service], label))
        return "  ·  ".join(parts)

    # ── the two ways a fact can be missing or stale ──────────────────────

    def _render_incomplete_note(self, box, posture):
        """Name the source that failed. "Could not verify" without a subject
        is a complaint about our tooling dressed up as a finding."""
        if posture.reads_ok:
            return
        problems = []
        if posture.desktop_read != READ_OK:
            problems.append(
                "Claude Desktop's config is %s"
                % ("malformed" if posture.desktop_read == READ_MALFORMED
                   else "unreadable"))
        if posture.userscope_read != READ_OK:
            problems.append(
                "~/.claude.json is %s"
                % ("malformed" if posture.userscope_read == READ_MALFORMED
                   else "unreadable"))
        unknown = [p.name for p in posture.projects
                   if posture.service(p) == SERVICE_UNKNOWN]
        if unknown:
            problems.append("could not inspect %s" % ", ".join(unknown[:4]))
        tk.Label(box,
                 text="  ?  Posture incomplete — " + "; ".join(problems)
                      + ". No verdict is claimed over a fact that was not read.",
                 font=("Segoe UI", 9), bg=C["surface0"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(2, 2))

    # ── the two switches a user actually has ─────────────────────────────

    def _render_mode_toggle(self, box, posture):
        """Both switches, in the words a first-time reader needs.

        Everything else on this page is a report. These are the only two
        settings, and each was previously reachable only as a one-way
        "migration" buried in a different panel — one of which could not be
        undone from the UI at all, and one of which announced itself as a
        warning about a *flag* rather than as a choice about serving.

        Written as ON/OFF with both states spelled out, because a user who
        cannot see what they would be giving up is being asked to take the
        manager's word for it, and these are the two decisions they should
        least have to.
        """
        self._render_desktop_switch(box, posture)
        self._render_fallback_switch(box, posture)

    def _render_switch(self, box, *, title, on, on_text, off_text, command,
                       footer, note=""):
        """One labelled two-state switch. Shared so both read identically.

        The inactive state is dimmed rather than hidden: a switch that shows
        only the state you are in makes you flip it to find out what the other
        one does, which for these two means a config write to satisfy
        curiosity.
        """
        frame = tk.Frame(box, bg=C["surface1"])
        frame.pack(fill=tk.X, padx=8, pady=(10, 4), ipady=6)

        head = tk.Frame(frame, bg=C["surface1"])
        head.pack(fill=tk.X, padx=8, pady=(4, 0))
        tk.Label(head, text=title, font=("Segoe UI", 10, "bold"),
                 bg=C["surface1"], fg=C["text"]).pack(side=tk.LEFT)
        tk.Label(head, text=("  \u25cf  ON" if on else "  \u25cb  OFF"),
                 font=("Segoe UI", 10, "bold"), bg=C["surface1"],
                 fg=C["peach"] if on else C["overlay0"]).pack(side=tk.LEFT)
        ttk.Button(head, text="Turn OFF\u2026" if on else "Turn ON\u2026",
                   style="Primary.TButton",
                   command=command).pack(side=tk.RIGHT)

        for label, text, live in (("ON", on_text, on),
                                  ("OFF", off_text, not on)):
            row = tk.Frame(frame, bg=C["surface1"])
            row.pack(fill=tk.X, padx=8, pady=(4, 0))
            tk.Label(row, text=("  %s   " % label),
                     font=("Consolas", 9, "bold"), bg=C["surface1"],
                     fg=C["text"] if live else C["overlay0"], width=6,
                     anchor=tk.W).pack(side=tk.LEFT, anchor=tk.N)
            tk.Label(row, text=text, font=("Segoe UI", 9), bg=C["surface1"],
                     fg=C["text"] if live else C["overlay0"],
                     justify=tk.LEFT, wraplength=660, anchor=tk.W).pack(
                side=tk.LEFT)
        if note:
            tk.Label(frame, text="  " + note, font=("Segoe UI", 8),
                     bg=C["surface1"], fg=C["subtext"], justify=tk.LEFT,
                     wraplength=720, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(6, 0))
        tk.Label(frame, text="  " + footer,
                 font=("Segoe UI", 8, "italic"), bg=C["surface1"],
                 fg=C["overlay0"], justify=tk.LEFT, wraplength=720,
                 anchor=tk.W).pack(fill=tk.X, padx=8, pady=(4, 2))

    def _render_desktop_switch(self, box, posture):
        on = posture.desktop_state in (LIFECYCLE_PRESENT, LIFECYCLE_RETURNED)
        self._render_switch(
            box, title="Claude Desktop chat", on=on,
            command=self._toggle_desktop_chat,
            on_text=("Claude Desktop's own chat window can use tokensave "
                     "\u2014 but it serves ONE project for the whole machine, "
                     "the one you pin with \u2605 Set as Active. Claude Code "
                     "sessions running inside the Desktop window are answered "
                     "from that project too, whichever repo they are actually "
                     "in."),
            off_text=("Claude Desktop's chat window has no tokensave at all. "
                      "In exchange, every Claude Code session serves its own "
                      "project and you can work in several at once. \u2605 Set "
                      "as Active disappears from the menu, because nothing "
                      "reads it."),
            note=self._returned_note(
                posture.desktop_state,
                "Claude Desktop's entry", "a Claude Desktop update"),
            footer=("You are on %s. Switching is reversible either way, and "
                    "Claude Desktop must be closed for the change to stick."
                    % ("ON" if on else "OFF")))

    def _render_fallback_switch(self, box, posture):
        """The user-scoped entry, described by what it DOES.

        This used to render as a warning headed *"This machine recorded
        retiring the user-scoped entry — but it is back"*, with buttons
        offering to "retire it again" or "clear the retirement flag". Every
        noun in that was the manager's own bookkeeping: "user-scoped",
        "retiring", "the retirement flag". It announced a disagreement between
        a config key and a file — which is true, and is not a thing a user has
        any reason to care about — while never saying what the entry actually
        does or which of their projects depend on it.

        What it does is the whole story: it serves any project that has no
        binding of its own. So it gets the same switch as Claude Desktop chat,
        and the ON text names the projects currently relying on it.
        """
        on = posture.automatic_fallback
        relying = [p.name for p in posture.projects
                   if posture.service(p) == SERVICE_AUTOMATIC]
        if relying:
            names = ", ".join(relying[:5])
            if len(relying) > 5:
                names += ", \u2026"
            depends = (" Right now %d project%s rel%s on it: %s."
                       % (len(relying), "" if len(relying) == 1 else "s",
                          "ies" if len(relying) == 1 else "y", names))
        else:
            depends = (" Every project currently has its own binding, so "
                       "nothing depends on it today.")
        self._render_switch(
            box, title="Automatic serving", on=on,
            command=self._toggle_automatic_serving,
            on_text=("A project with no .mcp.json of its own is still served: "
                     "one shared `tokensave` entry is started inside each "
                     "session's own folder, so it resolves to that session's "
                     "project." + depends),
            off_text=("Only projects with their own .mcp.json get tokensave. "
                      "Any other project gets none at all \u2014 the strictest "
                      "setup, and the right one if you would rather a missing "
                      "binding failed loudly than worked by accident."),
            note=self._returned_note(
                posture.userscope_state, "this entry",
                "`tokensave install --agent claude` or `tokensave doctor`, "
                "both of which write it by design"),
            footer=("You are on %s. Switching is reversible either way."
                    % ("ON" if on else "OFF")))

    @staticmethod
    def _returned_note(state: str, what: str, likely: str) -> str:
        """One quiet line when a switch was turned off and came back on.

        Worth saying, because it will happen again and the user is entitled to
        know their choice was undone. NOT worth a warning box with buttons:
        nothing is broken, the switch above already shows the true state, and
        the stale flag underneath is inert while the entry exists. Presenting
        bookkeeping as a fault is what made the old banner unreadable.
        """
        if state != LIFECYCLE_RETURNED:
            return ""
        return ("Note: you turned this off before and %s is back \u2014 most "
                "likely re-added by %s. The switch above shows what is "
                "actually in effect." % (what, likely))

    def _toggle_desktop_chat(self):
        """Flip the mode through the writer that owns each direction.

        Both directions already existed as separate migrations with separate
        confirmations, backups and running-Desktop guards. This adds no third
        write path — it picks which of the two to call.
        """
        posture = getattr(self, "_posture", None)
        on = getattr(posture, "desktop_state", "") in (LIFECYCLE_PRESENT,
                                                       LIFECYCLE_RETURNED)
        if on:
            self._retire_desktop(self._desktop_rows())
        else:
            self._unretire_desktop()

    def _toggle_automatic_serving(self):
        """Turn the shared entry on or off, naming who it would strand.

        Turning it OFF is the one action on this page that can leave a project
        with no tokensave at all, and the check for that is not the old
        migration's "is every project bound or skipped" — it is simply which
        projects are being served by this entry right now, which the posture
        already lists. Named, not counted: "4 projects" sends the user to go
        and look.
        """
        posture = getattr(self, "_posture", None)
        if posture is None:
            return
        if not posture.automatic_fallback:
            from dialogs.tokensave_mcp_picker import TokensaveMCPPickerDialog
            TokensaveMCPPickerDialog(self, self._cfg)
            # Saying "on" IS the decision, so the stale off-flag stops being a
            # disagreement the moment they say it. Cleared silently rather than
            # asked about: there is nothing here the user could usefully
            # answer differently.
            raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
            if raw.get(USER_SCOPE_RETIRED_KEY):
                raw[USER_SCOPE_RETIRED_KEY] = False
                self._cfg.save()
            self._render()
            return

        relying = [p.name for p in posture.projects
                   if posture.service(p) == SERVICE_AUTOMATIC]
        if relying:
            listing = "\n".join("  \u2022  %s" % n for n in relying[:12])
            if len(relying) > 12:
                listing += "\n  \u2026 and %d more" % (len(relying) - 12)
            if not messagebox.askyesno(
                    "Turn automatic serving off",
                    "These %d project%s ha%s no binding of their own, so they "
                    "are being served by the shared entry right now:\n\n%s\n\n"
                    "Turning it off leaves them with no tokensave at all "
                    "until each one is bound.\n\nTurn it off anyway?"
                    % (len(relying), "" if len(relying) == 1 else "s",
                       "s" if len(relying) == 1 else "ve", listing),
                    default="no", parent=self):
                return
        self._remove_user_scoped()

    def _desktop_rows(self):
        """`(name, root, info)` rows in the shape `_migration_status` expects.

        Built from the posture rather than re-classified: the gate's coverage
        half now reads `posture.unserved` directly, so these carry only the
        names the status counters render.
        """
        posture = getattr(self, "_posture", None)
        return [(p.name, p.display_root, {"state": "ok"})
                for p in getattr(posture, "projects", ()) or ()]

    # ── the plan ─────────────────────────────────────────────────────────

    #: Steps whose writer needs a gate this panel cannot answer, so the button
    #: opens Details rather than acting. Retiring Desktop's entry re-asks
    #: whether Desktop is running IMMEDIATELY before writing — a gate answered
    #: a minute ago is not a gate — and that check runs in the background scan
    #: the details panel owns. Reproducing it here would be a second copy of
    #: the one guard whose failure silently reverts the change.
    _DEFERRED_KINDS = frozenset({"retire_desktop"})

    def _render_plan(self, box, posture):
        """What this machine needs, if anything — and nothing if it needs nothing.

        A plan of zero steps renders as no plan at all, deliberately. The
        common case here is a machine that is already right, and inventing a
        "everything looks good, but…" section for it is how a dashboard turns
        back into a chore list.
        """
        steps = plan_independence(posture)
        if not steps:
            return
        if len(steps) == 1 and steps[0].kind == "explain":
            return               # already said by the incomplete-posture note

        tk.Label(box, text="  What this needs", font=("Segoe UI", 10, "bold"),
                 bg=C["surface0"], fg=C["blue"], anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(10, 2))

        for step in ungrouped(steps):
            self._render_step(box, step)
        for name, members in groups(steps).items():
            tk.Label(box,
                     text="  Pick one — these are alternatives, not steps:",
                     font=("Segoe UI", 9, "italic"),
                     bg=C["surface0"], fg=C["overlay0"], anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(8, 0))
            for step in members:
                self._render_step(box, step)

    def _render_step(self, box, step):
        """One step: what it does, why, and the button that performs it.

        The detail is shown rather than hidden behind a tooltip. Every one of
        these changes what Claude serves, and a user who cannot see the
        consequence before clicking is exactly the user this dialog keeps
        producing.
        """
        row = tk.Frame(box, bg=C["surface0"])
        row.pack(fill=tk.X, padx=8, pady=(6, 0))
        deferred = step.kind in self._DEFERRED_KINDS
        if not step.automatable and step.kind == "explain":
            text = ""
        elif deferred:
            text = "Show details ▾"
        elif not step.automatable:
            text = "Open a session…"
        else:
            text = "Do this…"
        if text:
            ttk.Button(row, text=text,
                       command=lambda s=step: self._run_step(s)).pack(
                side=tk.RIGHT)
        tk.Label(row, text="•  " + step.label,
                 font=("Segoe UI", 9, "bold"),
                 bg=C["surface0"], fg=C["text"], anchor=tk.W,
                 justify=tk.LEFT, wraplength=560).pack(side=tk.LEFT)
        tk.Label(box, text="     " + step.detail, font=("Segoe UI", 8),
                 bg=C["surface0"], fg=C["overlay0"], justify=tk.LEFT,
                 wraplength=720, anchor=tk.W).pack(fill=tk.X, padx=8)
        if not step.automatable and step.kind != "explain":
            tk.Label(box,
                     text="     This one cannot be done by editing a file.",
                     font=("Segoe UI", 8, "italic"), bg=C["surface0"],
                     fg=C["peach"], anchor=tk.W).pack(fill=tk.X, padx=8)

    def _run_step(self, step):
        """Route one step to the writer that owns it. Performs nothing itself.

        Every branch here hands off to a function that already exists, with
        its own confirmation and its own timestamped backup. That is the rule
        the planner's docstring states, and this is where it would be easiest
        to break: an inline `json.dump` in one of these branches would be a
        second write path with none of those guarantees.
        """
        if step.kind in self._DEFERRED_KINDS:
            self._show_details = True
            self._render()
            return
        if step.kind == "restore_userscope":
            from dialogs.tokensave_mcp_picker import TokensaveMCPPickerDialog
            TokensaveMCPPickerDialog(self, self._cfg)
            self._render()
            return
        if step.kind in ("bind_project", "rebind_project"):
            self._bind_one(step.target)
            return
        if step.kind == "approve_project":
            self._approve_one(step.target)
            return
        if step.kind == "trust_project":
            self._open_trust_session(step.target)
            return
        if step.kind == "strict_tree":
            self._enable_strict_tree(step.target)
            return

    def _open_trust_session(self, project_root: str):
        """Open a Claude Code session in the folder so the user can trust it.

        The one step in any of these plans that no file can perform. Claude
        Code gates loading a project's `.mcp.json` on
        `hasTrustDialogAccepted`, and that flag is written when a person
        answers "Do you trust the files in this folder?" — measured across six
        projects with byte-identical `.mcp.json` files and approval granted in
        every one, trust alone separated the two that served from the four
        that did not.

        Launched through `canonical_launch_dir` rather than the raw path.
        Claude Code keys per-project state by the spelling of the directory it
        was started in, so launching with a spelling the user never uses does
        not merely record trust in the wrong place — it CREATES another
        duplicate key, and this manager's own probes are already documented as
        a source of those.
        """
        from helpers.claude_cli import spawn_claude_cli_interactive
        from helpers.mcp_projects import canonical_launch_dir

        exe = getattr(self._cfg, "claude_cli_exe", "") or ""
        if not exe:
            messagebox.showinfo(
                "Claude Code CLI not configured",
                "Set the Claude Code CLI path in Settings, or open a terminal "
                "in:\n\n%s\n\nand run `claude` once, accepting \"Do you "
                "trust the files in this folder?\"." % project_root,
                parent=self)
            return
        ok, err = spawn_claude_cli_interactive(
            exe, canonical_launch_dir(project_root))
        if ok:
            self._log_to_app(
                "MCP: opened a Claude Code session in %s — accept the trust "
                "prompt, then click Re-detect." % project_root, C["sky"])
        else:
            messagebox.showerror("Could not open a session", err, parent=self)

    def _enable_strict_tree(self, project_root: str):
        """Hardening, behind its own confirmation and its own writer."""
        from helpers.tokensave_config import set_strict_tree

        if not messagebox.askyesno(
                "Enable strict_tree",
                "Turn on tokensave's wrong-tree refusal for:\n\n%s\n\n"
                "A tokensave call that would be answered from a different "
                "working tree then errors, naming both roots, instead of "
                "returning a plausible answer about a checkout you are not "
                "in.\n\nTurn it off again if it refuses something it should "
                "not." % project_root, parent=self):
            return
        ok, detail = set_strict_tree(project_root, True)
        self._log_to_app(
            ("MCP: %s" % detail) if ok else ("MCP: strict_tree — %s" % detail),
            C["green"] if ok else C["peach"])
        self._render()

    # ── the table ────────────────────────────────────────────────────────

    def _render_project_table(self, posture):
        """One line per project, ordered worst first.

        The badge is read from `posture.service`, never from `tier`: the same
        project is fine or broken depending on whether a machine-wide fallback
        exists, and that is not a fact a per-project row can carry.
        """
        if not posture.projects:
            return
        order = {SERVICE_WRONG: 0, SERVICE_UNSERVED: 1, SERVICE_UNKNOWN: 2,
                 SERVICE_AUTOMATIC: 3, SERVICE_SELF: 4}
        rows = sorted(posture.projects,
                      key=lambda p: (order.get(posture.service(p), 9), p.name))

        tk.Label(self._body, text="  How each project is served",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["blue"], anchor=tk.W).pack(
            fill=tk.X, padx=4, pady=(12, 2))
        self._render_bulk_bind(posture, rows)
        for project in rows:
            service = posture.service(project)
            badge, colour, meaning = _SERVICE_ROWS.get(
                service, _SERVICE_ROWS[SERVICE_UNKNOWN])
            row = tk.Frame(self._body, bg=C["base"])
            row.pack(fill=tk.X, padx=12, pady=1)
            tk.Label(row, text=badge, font=("Segoe UI", 10, "bold"),
                     bg=C["base"], fg=C[colour], width=2).pack(side=tk.LEFT)
            tk.Label(row, text=project.name, font=("Segoe UI", 9),
                     bg=C["base"], fg=C["text"], width=26,
                     anchor=tk.W).pack(side=tk.LEFT)
            tk.Label(row, text=meaning, font=("Segoe UI", 9),
                     bg=C["base"], fg=C[colour], anchor=tk.W).pack(
                side=tk.LEFT)
            self._render_row_toggle(row, project, service)

    def _render_row_toggle(self, row, project, service: str):
        """The per-project half of the same switch, and it goes both ways.

        Binding had a button and unbinding had none, so a project could be
        bound and never released except by deleting a file by hand.
        That asymmetry is the same shape as the Desktop migration's: an action
        offered as a one-way door because the undo was never built.

        Nothing is offered for a MISBOUND or UNKNOWN row: the first needs a
        rebind rather than a toggle (the plan above names it), and the second
        is a state we could not read, where any button would be a guess.
        """
        if service == SERVICE_AUTOMATIC:
            ttk.Button(row, text="Bind\u2026",
                       command=lambda r=project.display_root:
                           self._bind_one(r)).pack(side=tk.RIGHT)
        elif service in (SERVICE_SELF, SERVICE_UNSERVED):
            ttk.Button(row, text="Unbind\u2026",
                       command=lambda r=project.display_root, n=project.name:
                           self._unbind_project(r, n)).pack(side=tk.RIGHT)

    def _render_bulk_bind(self, posture, rows):
        """One click for every automatically-served project.

        Offered only when there is more than one, because a "do all" button
        over a single item is just a second name for the button beside it.
        """
        automatic = [p for p in rows
                     if posture.service(p) == SERVICE_AUTOMATIC]
        if len(automatic) < 2:
            return
        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=12, pady=(2, 4))
        tk.Label(strip,
                 text="%d served automatically \u2014 nothing to fix, but you "
                      "can bind them for determinism:" % len(automatic),
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
            side=tk.LEFT)
        ttk.Button(strip, text="Bind all %d\u2026" % len(automatic),
                   command=lambda ps=list(automatic):
                       self._bind_all(ps)).pack(side=tk.RIGHT)

    def _bind_all(self, projects):
        """Bind every automatically-served project, behind one confirmation.

        The same batch-with-one-review shape `_approve_all` already uses: each
        write still goes through `_apply_mcp_fix` with its own backup, and the
        list of what will be touched is shown before anything is.
        """
        listing = "\n".join("  \u2022  %s" % p.name for p in projects[:12])
        if len(projects) > 12:
            listing += "\n  \u2026 and %d more" % (len(projects) - 12)
        if not messagebox.askyesno(
                "Bind %d projects" % len(projects),
                "Write a .mcp.json binding into each of these:\n\n%s\n\n"
                "They are already served correctly \u2014 this makes it "
                "explicit rather than resolved from each session's folder, "
                "which matters for a git worktree, a nested repo, or a "
                "session started in a subdirectory.\n\nEach file is written "
                "with a timestamped backup, and each project will ask for "
                "approval the first time you open it." % listing,
                parent=self):
            return
        done = 0
        for project in projects:
            path = _project_mcp_path(project.display_root)
            info = _classify_mcp_entry(path, self._cfg.raw)
            ok, _msg = _apply_mcp_fix(path, info["proposed"])
            done += 1 if ok else 0
        self._log_to_app(
            "MCP: bound %d of %d project%s." % (
                done, len(projects), "" if len(projects) == 1 else "s"),
            C["green"] if done == len(projects) else C["peach"])
        self._render()

    def _unbind_project(self, project_root: str, name: str):
        """Remove a project's own binding and let it be served automatically.

        The consequence depends on one machine-wide fact, so the confirmation
        states which one applies rather than describing both. With a fallback
        present this is a no-op in practice — the project keeps working and
        stops carrying a file. Without one it takes tokensave away from the
        project entirely, and that has to be said before the click, not
        discovered after it.
        """
        posture = getattr(self, "_posture", None)
        fallback = bool(getattr(posture, "automatic_fallback", False))
        if fallback:
            consequence = (
                "It keeps working: with no binding of its own it falls back "
                "to the machine-wide `tokensave` entry, which each session "
                "starts in that session's own folder \u2014 so it resolves to "
                "this project.")
        else:
            consequence = (
                "\u26a0  There is NO machine-wide fallback on this machine, so "
                "removing this binding leaves %s with no tokensave at all "
                "until you bind it again." % name)
        if not messagebox.askyesno(
                "Unbind %s" % name,
                "Remove the `tokensave` entry from:\n\n%s\n\n%s\n\n"
                "A timestamped backup is written first, and any other servers "
                "in the file are left alone." % (
                    _project_mcp_path(project_root), consequence),
                parent=self):
            return
        changed, detail = remove_mcp_entry(_project_mcp_path(project_root))
        self._log_to_app(
            ("MCP: unbound %s. %s" % (name, detail.splitlines()[0]))
            if changed else ("MCP: %s" % detail),
            C["green"] if changed else C["peach"])
        if not changed:
            messagebox.showinfo("Nothing removed", detail, parent=self)
        self._render()


    # ── the toggle ───────────────────────────────────────────────────────

    def _render_details_toggle(self):
        """Everything the five original panels do, one click away.

        Collapsed by default because their combined output is a diagnosis, not
        a dashboard: per-file diffs, duplicate `~/.claude.json` keys, the
        Cursor section and two migrations. Opening the dialog on that is what
        made a healthy machine look like a problem.
        """
        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(14, 2))
        shown = getattr(self, "_show_details", False)
        ttk.Button(strip,
                   text=("Hide details ▴" if shown
                         else "Show details ▾  (per-file wiring, migrations, "
                              "duplicates, Cursor)"),
                   command=self._toggle_details).pack(side=tk.LEFT)
        if not shown:
            return
        ttk.Separator(self._body, orient="horizontal").pack(
            fill=tk.X, padx=8, pady=(8, 2))

    def _toggle_details(self):
        self._show_details = not getattr(self, "_show_details", False)
        self._render()
