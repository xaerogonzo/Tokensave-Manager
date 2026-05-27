"""codegraph_freshness — Proactive freshness management for the codegraph index.

v4.3 additions:
  * ensure_fresh(project_path, codegraph_exe)
      Blocking helper called right before build_codegraph_block.
      If the index is 'stale', runs ``codegraph sync`` synchronously
      (~2-5 s) and re-checks; if still unhealthy, returns the
      updated status so the caller can skip/warn.

  * maybe_prompt_reindex(parent_widget, project_path, codegraph_exe,
                         on_complete_callback)
      Once-per-session messagebox for the 'broken' state.  Shows an
      actionable explanation, lets the user decline (no-op for the
      rest of the session) or trigger a background full reindex.

  * kick_autosync(project_path, codegraph_exe, on_complete)
      Thin wrapper for the projects-tab on-select autosync path.
      Returns immediately; sync happens in the shared background
      executor.  Two-layer debounce: per-project (30 s) + a single
      max_workers=1 executor so rapid project switching never spawns
      more than one concurrent codegraph process.

Design principles:
  * All functions are fail-open: any exception is logged to stderr and
    the caller proceeds without codegraph grounding.
  * ``maybe_prompt_reindex`` is stateful (per-session set) — once the
    user dismisses the dialog for a project, it will not reappear until
    the next application start.
  * No circular dependency on CodeGraphController; these helpers use
    subprocess directly and accept plain strings.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

# ── Module-level state (per-process, reset on restart) ───────────────────────

# Per-project timestamp of the last autosync attempt (prevents rapid re-fires).
_last_autosync_ts: dict[str, float] = {}
# Set of projects currently being autosynced.
_autosync_in_flight: set[str] = set()
# Lock protecting both of the above.
_autosync_lock = threading.Lock()
# Single-worker executor so codegraph subprocesses are serialised globally.
_autosync_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cg_autosync")
# Per-session set of projects for which the reindex prompt was already shown.
_reindex_prompted_for: set[str] = set()

_AUTOSYNC_DEBOUNCE_S = 30   # minimum seconds between per-project autosyncs

# CREATE_NO_WINDOW flag (Windows only; 0 elsewhere)
try:
    from subprocess import CREATE_NO_WINDOW as _CNW
except ImportError:
    _CNW = 0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_codegraph_sync(project_path: str, codegraph_exe: str) -> bool:
    """Run ``codegraph sync <project_path>`` and return True on rc == 0."""
    try:
        proc = subprocess.run(
            [codegraph_exe, "sync", project_path],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            creationflags=_CNW,
        )
        return proc.returncode == 0
    except Exception as exc:
        print(f"[codegraph_freshness] sync failed: {exc}", file=sys.stderr)
        return False


def _run_codegraph_reindex(project_path: str, codegraph_exe: str) -> bool:
    """Run ``codegraph index --force <project_path>`` and return True on success."""
    try:
        proc = subprocess.run(
            [codegraph_exe, "index", "--force", project_path],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
            creationflags=_CNW,
        )
        return proc.returncode == 0
    except Exception as exc:
        print(f"[codegraph_freshness] reindex failed: {exc}", file=sys.stderr)
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def ensure_fresh(project_path: str, codegraph_exe: str) -> tuple[str, str]:
    """Ensure the codegraph index is up-to-date before a grounding call.

    Checks the current health; if 'stale', runs ``codegraph sync``
    synchronously (~2-5 s) and re-checks.  Returns the FINAL ``(status,
    detail)`` tuple from ``_codegraph_index_health``.

    Callers should proceed with codegraph grounding only when the
    returned status is ``'healthy'``.  Any exception is swallowed and
    the original health tuple is returned unchanged.

    Typical usage::

        ensure_fresh(project, cfg.codegraph_exe)
        cg_block = build_codegraph_block(project, recipe, ...)
    """
    if not codegraph_exe:
        return "missing", "no codegraph_exe configured"
    try:
        from helpers.doc_grounding import _codegraph_index_health
        status, detail = _codegraph_index_health(project_path, codegraph_exe)
        if status == "stale":
            print(f"[codegraph_freshness] index stale ({detail}); running sync…",
                  file=sys.stderr)
            _run_codegraph_sync(project_path, codegraph_exe)
            # Re-check after sync.
            status, detail = _codegraph_index_health(project_path, codegraph_exe)
        return status, detail
    except Exception as exc:
        print(f"[codegraph_freshness] ensure_fresh error: {exc}", file=sys.stderr)
        return "broken", str(exc)


def maybe_prompt_reindex(
    parent_widget,
    project_path: str,
    codegraph_exe: str,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Show a once-per-session reindex prompt when the index is 'broken'.

    Silently returns if:
      - the project was already prompted this session
      - codegraph_exe is absent
      - the messagebox would be shown from a non-main thread (unsafe)

    If the user confirms, a background thread runs ``codegraph index
    --force`` and then calls ``on_complete()`` (typically a tree refresh).
    If the user declines, the project is added to the session-level
    "already prompted" set — no further nags until restart.

    ``parent_widget`` is passed as ``parent=`` to the messagebox so it
    renders correctly above modal dialogs (grab_set() windows).
    """
    if not codegraph_exe:
        return
    if project_path in _reindex_prompted_for:
        return
    _reindex_prompted_for.add(project_path)

    # Must run on main thread — caller is responsible for calling via after(0, …)
    # when dispatching from a background thread.
    import tkinter.messagebox as mb
    try:
        project_name = os.path.basename(project_path)
        confirm = mb.askyesno(
            "CodeGraph index appears under-indexed",
            f"Tokensave has indexed significantly more files than codegraph "
            f"for '{project_name}'.\n\n"
            "This usually means the project layout changed since codegraph "
            "was first initialised — Sync alone can't catch up because it "
            "only re-scans files that already have entries.\n\n"
            "Run a full re-index now?\n"
            "(Takes 30 s – 2 min; grounding will proceed with tokensave "
            "only while it runs.)",
            parent=parent_widget,
            default="no",
        )
        if not confirm:
            return
    except Exception as exc:
        print(f"[codegraph_freshness] prompt error: {exc}", file=sys.stderr)
        return

    def _do_reindex():
        success = _run_codegraph_reindex(project_path, codegraph_exe)
        msg = "completed" if success else "FAILED (see console)"
        print(f"[codegraph_freshness] reindex {msg}: {project_name}",
              file=sys.stderr)
        if on_complete:
            try:
                on_complete()
            except Exception:
                pass

    threading.Thread(target=_do_reindex, daemon=True).start()


def kick_autosync(
    project_path: str,
    codegraph_exe: str,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Non-blocking autosync on project select.

    Checks the two-layer debounce (per-project 30 s + single-worker
    executor serialisation) before submitting a sync job.

    * If a sync for this project is already in-flight → drop silently.
    * If synced within the last 30 s → drop silently.
    * Otherwise → submit to the shared executor.

    After sync completes, calls ``on_complete()`` if provided (typically
    a UI tree-refresh callback; fired on the worker thread — callers that
    need main-thread execution must wrap with ``parent.after(0, ...)``.
    """
    if not codegraph_exe:
        return
    with _autosync_lock:
        now = time.monotonic()
        last = _last_autosync_ts.get(project_path, 0)
        if project_path in _autosync_in_flight:
            return
        if now - last < _AUTOSYNC_DEBOUNCE_S:
            return
        _last_autosync_ts[project_path] = now
        _autosync_in_flight.add(project_path)

    def _worker():
        try:
            from helpers.doc_grounding import _codegraph_index_health
            status, detail = _codegraph_index_health(project_path, codegraph_exe)
            if status == "stale":
                print(f"[codegraph_freshness] autosync: {os.path.basename(project_path)} "
                      f"stale ({detail})", file=sys.stderr)
                _run_codegraph_sync(project_path, codegraph_exe)
            # 'missing' / 'broken' → no autosync; caller handles via
            # maybe_prompt_reindex if needed.
        except Exception as exc:
            print(f"[codegraph_freshness] autosync error: {exc}", file=sys.stderr)
        finally:
            with _autosync_lock:
                _autosync_in_flight.discard(project_path)
            if on_complete:
                try:
                    on_complete()
                except Exception:
                    pass

    try:
        _autosync_executor.submit(_worker)
    except RuntimeError:
        # Executor shut down (app exit race) — clean up state.
        with _autosync_lock:
            _autosync_in_flight.discard(project_path)
