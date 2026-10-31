"""The Projects tab's "agent handoff here" entry, kept out of the tab itself.

`projects_tab.py` was 1,485 lines against the 1,500 where Doctor warns when this
was added, so the behaviour lives here and the tab keeps only two thin delegates.
Stateless on purpose: everything a call needs is passed in, so there is nothing
here that could go stale when Settings rebinds the config.
"""

from __future__ import annotations

import os
from tkinter import messagebox

from constants import C

ON_LABEL = "🤝  Enable agent handoff here…"
OFF_LABEL = "🤝  Stop agent handoff here…"


def label_for(cfg, path: str) -> str:
    """Which direction this project can go. An unreadable project offers Enable,
    for the reason the strict_tree entry does: offering Stop for a state that
    could not be determined would assert a fact the Manager does not have."""
    if path:
        try:
            from helpers import handoff_setup
            if handoff_setup.takes_part(path, cfg.git_exe):
                return OFF_LABEL
        except Exception:                                  # noqa: BLE001
            pass              # a mislabelled entry must not eat the menu
    return ON_LABEL


def toggle(root, cfg, on_log, path: str) -> None:
    """Add or remove this project's handoff rule (`CLAUDE.local.md`).

    Confirmed, because it writes into the project. The file is git-ignored and
    holds this machine's paths, so what is confirmed is wording a person can
    check: what is written, that it never reaches a commit, and that the
    machine-wide switch is a separate thing.
    """
    if not path:
        return
    from helpers import handoff_setup
    git_exe = cfg.git_exe
    if handoff_setup.takes_part(path, git_exe):
        if not messagebox.askyesno(
                "Stop agent handoff here",
                "Remove the handoff rule from\n%s?\n\nAgents will no longer be "
                "told about the command, and the Cursor hook will stop recording "
                "sessions for this project. Notes already in the ledger stay; "
                "clear them with `manager-cli handoff --action clear --yes`."
                % path, parent=root):
            return
        done = handoff_setup.disable(path, git_exe)
        verb = "removed the handoff rule from"
    else:
        off = ("" if cfg.enable_agent_handoff else
               "\n\nHandoff is switched OFF in Settings > AI, so the command will "
               "refuse until you turn it on there.")
        if not messagebox.askyesno(
                "Enable agent handoff here",
                "Add a handoff rule to\n%s?\n\nThis writes CLAUDE.local.md, a file "
                "git ignores (it is added to this clone's local exclude file first) "
                "that names this machine's Manager command. Claude Code and Cursor "
                "both read it, so either can leave notes for the other. A file "
                "already committed under that name is never touched.%s"
                % (path, off), parent=root):
            return
        done = handoff_setup.enable(path, git_exe)
        verb = "added the handoff rule to"
    name = os.path.basename(path) or path
    if done.ok:
        on_log("[handoff] %s %s" % (verb, name), C["green"])
    else:
        on_log("[handoff] %s: %s" % (name, done.error), C["peach"])
        messagebox.showerror("Agent handoff", done.error, parent=root)
