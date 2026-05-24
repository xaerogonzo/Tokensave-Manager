"""Tk dialog classes (one per file).

Each dialog is a leaf `tk.Toplevel` subclass — it takes a parent widget
plus a small set of callbacks / data parameters in `__init__`, and never
reads module globals. Dialogs that need live config values
(e.g. SettingsDialog → tokensave_exe / git_exe) take a `cfg: ManagerConfig`
parameter; pure UI widgets that just gather user input via callbacks
(NewBranchDialog, SwitchBranchDialog, etc.) take no cfg.

The split follows the boundaries that survived three rounds of intra-file
refactoring before Phase B carved them out — each class was already a
self-contained widget in the legacy `src/tokensave-manager.py` monolith;
this just moves the class verbatim into its own file. (Monolith was
deleted in Phase E — `src/app.py` is the current entry point.)
"""
