"""Tab controllers for the TokenSave Manager main notebook.

Each controller owns one notebook tab and the per-tab commands that run
from it. Each takes `notebook: ttk.Notebook` and `cfg: ManagerConfig`
in `__init__`, plus a small set of callbacks for cross-tab actions
(opening the commit dialog, kicking off App.refresh(), etc.).

Per the Round 4 plan:
  - Reads happen through `self._cfg.git_exe` / `self._cfg.raw.get(...)`
    at execution time, NEVER snapshot in __init__ (Rule 3).
  - Mutations go through `self._cfg.raw[k] = v` + `self._cfg.save()`.
  - Controller modules sit above helpers/ + dialogs/ in the import
    graph, below app.py.
"""
