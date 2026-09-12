"""The Settings section contract, and the one helper every section shares.

A *section* is a self-contained block of the Settings surface. Seven of them
exist (`settings_paths`, `settings_projects`, `settings_git`, `settings_ai`,
`settings_codegraph`, `settings_pyscope`, `settings_mcp`), and
`controllers/settings_tab.py` composes them onto pages.

THE CONTRACT, in full:

    __init__(host, body, cfg, **injected)
        `host`   the APPLICATION WINDOW, for Tk plumbing only -- `after()`,
                 `parent=` on a messagebox or filedialog, `transient()`, and
                 parenting a child Toplevel. It is passed in precisely so
                 that no section ever has to walk up from its own widgets to
                 find a real window. See below.
        `body`   the frame this section packs its widgets onto.
        `cfg`    read-only ManagerConfig. Values are read at execution time,
                 never snapshotted (the ManagerConfig contract).

    save_into(raw) -> bool
        Write this section's fields into `raw`. Returning False aborts the
        whole save with the page left open; the section shows its own reason.
        `raw` is a STAGING COPY, not `cfg.raw` -- see the controller.

    bind_dirty(callback) -> None
        Call `callback` whenever the user changes anything in this section.

    snapshot() -> hashable        [OPTIONAL]
        Comparable state the section holds OUTSIDE Tk variables. Only
        `settings_projects` needs it, for its Treeview of roots: the
        controller decides "is this page dirty" by comparing variable values
        against a baseline, and a Treeview is invisible to that, so adding a
        search root would leave the Save bar hidden.

WHY `host` MAY NOT BE WALKED UPWARDS. `settings_paths` used to reach the
application as `self._dlg.master`, then read `_tokensave_current_version` off
it with `getattr(host, ..., None)`. Inside a Notebook page `.master` is the
Notebook, so that returns None *silently and permanently* -- the upgrade row
reads "version unknown" forever -- while `host.cmd_upgrade_tokensave` raises.
The section's own test would still have passed, because it stubs the attribute
onto the root it happens to construct under.

So sections RECEIVE what they need (`on_upgrade_tokensave`,
`get_tokensave_versions`, ...) and never discover it.
`tests/test_settings_no_parent_walk.py` enforces that: no `.master`,
`.winfo_toplevel()` or `.nametowidget()` in any `dialogs/settings_*` module.
This is the project's callback-injection rule, and the defect class recorded
in `memory/moved_code_passes_its_own_tests.md`.
"""

from __future__ import annotations

import tkinter as tk


def bind_vars(section, callback) -> None:
    """Fire `callback` when any Tk variable the section owns is written.

    Introspective rather than a hand-kept list per section, for the reason
    every hand-kept list in this repository has eventually earned a rule:
    `settings_ai` alone owns eighteen of them, several created inside row
    helpers, and a list would silently stop covering a new one.

    Every variable a section can SAVE is reachable here, because `save_into`
    reads them off `self` too -- a variable not on the instance cannot be
    persisted either.

    It does NOT cover non-variable state such as a Treeview's rows. A section
    holding any is responsible for calling the callback itself; that is why
    `bind_dirty` is a method on the section rather than something the
    controller does to it.
    """
    for value in vars(section).values():
        if isinstance(value, tk.Variable):
            value.trace_add("write", lambda *_unused: callback())
