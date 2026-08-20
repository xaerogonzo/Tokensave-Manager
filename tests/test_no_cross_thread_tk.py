"""tests/test_no_cross_thread_tk.py — no worker thread may touch Tk.

Calling `self.after(...)` from a background thread is the most expensive bug
shape this project has produced, because of HOW it fails rather than how
often:

  * on Windows it usually works, so it passes review and passes locally;
  * when it does fail it raises "main thread is not in main loop", which is
    routinely swallowed by a broad `except` around the call;
  * on the Linux CI runner it does not raise at all — it BLOCKS. A CI
    diagnostic caught a ScrubHistoryDialog worker alive after 10 seconds
    having scheduled nothing and raised nothing. No error, no log line, no
    way to tell what it was waiting for.

That last shape is why this is a guard and not a review note — and it is the
same reason two issues went upstream to tokensave this round. A failure you
cannot see is worse than one that shouts.

The rule: a worker hands a callable to a queue, and a main-thread pump runs
it. See `ScrubHistoryDialog._post` / `_pump`,
`GitTabController._poll_log_queue`, and `TestManagerDialog._drain_ci_queue`.

Dual-mode: runs under pytest, and standalone for the import-free CI job.
"""
from __future__ import annotations

import ast
import collections
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"

# Tk methods that must only ever be called from the main thread.
_TK_METHODS = {"after", "after_idle", "update", "update_idletasks"}

# Functions whose bodies run on a worker thread. Named by convention here —
# every `threading.Thread(target=...)` in this codebase targets one of these.
_WORKER_NAMES = {"_worker", "worker"}


# ── Ratchet ───────────────────────────────────────────────────────────────
#
# The pattern predates this guard and lived in 12 files. Converting them all
# at once would be a large, risky change touching nearly every dialog, so
# this is a RATCHET rather than a clean assertion: files already fixed stay
# fixed, no file may get worse, and nothing new may join the list.
#
# Measured 2026-08-20. Lower a number as its dialog is converted; delete the
# entry when it reaches zero. `test_the_baseline_is_not_stale` enforces that
# — app.py was converted the same day and its entry is gone accordingly.
_KNOWN_OFFENDERS = {
    "src/dialogs/tool_manager.py":             17,
    "src/dialogs/ollama_model_mgr.py":         11,
    "src/dialogs/private_repo_mgr.py":          4,
    "src/dialogs/codegraph_daemon_manager.py":  3,
    "src/dialogs/codegraph_mcp_picker.py":      3,
    "src/dialogs/gitignore.py":                 3,
    "src/dialogs/roadmap_mgr.py":               3,
    "src/dialogs/ai_code_review.py":            2,
    "src/dialogs/doc_drafter.py":               2,
    "src/dialogs/git_commit.py":                1,
    "src/dialogs/tokensave_mcp_picker.py":      1,
}


class _Scanner(ast.NodeVisitor):
    """Flag `self.<tk method>(...)` lexically inside a worker function."""

    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list = []
        self.hits: list = []

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        fn = node.func
        if (isinstance(fn, ast.Attribute)
                and fn.attr in _TK_METHODS
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "self"
                and any(name in _WORKER_NAMES for name in self.stack)):
            chain = " > ".join(self.stack)
            self.hits.append(
                f"{self.rel}:{node.lineno}: self.{fn.attr}(...) inside {chain}")
        self.generic_visit(node)


def _offenders() -> list:
    out = []
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        scanner = _Scanner(path.relative_to(_ROOT).as_posix())
        scanner.visit(tree)
        out.extend(scanner.hits)
    return out


def _counts_by_file():
    return collections.Counter(h.split(":")[0] for h in _offenders())


# ── the ratchet's three rules ─────────────────────────────────────────────

def test_no_new_file_starts_calling_tk_from_a_worker():
    """The half that matters most: stop the pattern spreading into new code."""
    counts = _counts_by_file()
    new = {f: n for f, n in counts.items() if f not in _KNOWN_OFFENDERS}
    assert new == {}, (
        "these call a Tk method from a worker thread. On Linux that does not "
        "raise, it BLOCKS — silently, with no log line. Post to a queue and "
        "let a main-thread pump run it: " + repr(new))


def test_no_known_offender_gets_worse():
    counts = _counts_by_file()
    worse = {f: (counts.get(f, 0), base)
             for f, base in _KNOWN_OFFENDERS.items()
             if counts.get(f, 0) > base}
    assert worse == {}, f"cross-thread Tk calls increased (now, was): {worse}"


def test_the_baseline_is_not_stale():
    """A fixed file must have its entry lowered, or the ratchet silently
    leaves room for the pattern to creep back in later."""
    counts = _counts_by_file()
    stale = {f: (counts.get(f, 0), base)
             for f, base in _KNOWN_OFFENDERS.items()
             if counts.get(f, 0) < base}
    assert stale == {}, (
        "these improved — lower their _KNOWN_OFFENDERS entry (now, listed): "
        + repr(stale))


# ── the dialog this was written for ───────────────────────────────────────

def test_the_scrub_dialog_is_clean():
    """It was converted; it stays converted."""
    assert "src/dialogs/scrub_history.py" not in _counts_by_file()
    assert "src/dialogs/scrub_history.py" not in _KNOWN_OFFENDERS


def test_the_scrub_dialog_has_the_queue_and_pump():
    src = (_SRC / "dialogs" / "scrub_history.py").read_text(encoding="utf-8")
    for token in ("self._ui_queue", "def _post", "def _pump",
                  "def _stop_ui_pump"):
        assert token in src, f"scrub_history lost {token}"


def test_the_app_window_has_the_queue_and_pump():
    """app.py was the largest offender (11 sites) and is the main window, so
    a worker blocking there wedges the whole UI rather than one dialog."""
    src = (_SRC / "app.py").read_text(encoding="utf-8")
    for token in ("self._ui_queue", "def _post", "def _ui_pump",
                  "def _start_ui_pump"):
        assert token in src, f"app.py lost {token}"
    assert "src/app.py" not in _counts_by_file()


# ── the guard can actually fail ───────────────────────────────────────────

def test_the_guard_would_catch_a_regression():
    """A guard that cannot fail is not a guard."""
    bad = ("class D:\n"
           "    def go(self):\n"
           "        def _worker():\n"
           "            self.after(0, lambda: None)\n")
    scanner = _Scanner("synthetic.py")
    scanner.visit(ast.parse(bad))
    assert len(scanner.hits) == 1, scanner.hits


def test_main_thread_tk_calls_are_not_flagged():
    """Only worker-scoped calls count — the pump itself must stay legal."""
    ok = ("class D:\n"
          "    def _pump(self):\n"
          "        self.after(50, self._pump)\n")
    scanner = _Scanner("synthetic.py")
    scanner.visit(ast.parse(ok))
    assert scanner.hits == []


if __name__ == "__main__":
    counts = _counts_by_file()
    new = {f: n for f, n in counts.items() if f not in _KNOWN_OFFENDERS}
    worse = {f: (counts.get(f, 0), b) for f, b in _KNOWN_OFFENDERS.items()
             if counts.get(f, 0) > b}
    if new or worse:
        print("FAIL: cross-thread Tk calls added.")
        for f, n in sorted(new.items()):
            print(f"  NEW   {f}: {n}")
        for f, (now, was) in sorted(worse.items()):
            print(f"  WORSE {f}: {now} (was {was})")
        raise SystemExit(1)
    total = sum(counts.values())
    print(f"OK: no new cross-thread Tk calls. "
          f"{total} remain in {len(counts)} known files.")
