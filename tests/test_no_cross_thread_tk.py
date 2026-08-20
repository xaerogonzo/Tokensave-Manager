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

The rule: a worker hands a callable to a queue and a main-thread pump runs it.
`theme.UiPumpMixin` is the shared implementation — mix it in, call
`_start_ui_pump()` in `__init__`, and have workers call `_post()`.

## Why this file resolves worker scope instead of assuming it

The first version of this guard trusted a naming convention: a function was
worker code if it was called `_worker` or `worker`. That was measured, and it
was wrong three separate ways, each of which hid real bugs:

  1. **31 of 111 thread targets are named something else** — `_bg_load`,
     `_generate_worker`, `_publish_worker`, `_fetch`, `_probe`, `_run`. That
     alone hid 17 sites and three whole files.
  2. **Helpers called *from* worker code**, which look main-thread at their
     definition. `app.py::_run_capture` says "must be called from a
     background thread" in its own docstring and the guard still missed it,
     along with `_log`, `_set_status` and `_log_threadsafe`.
  3. **`concurrent.futures`**, which never goes near `threading.Thread` at
     all. `ChecksDialog` submits to a `ThreadPoolExecutor` and updates rows
     from `add_done_callback` handlers, which run on the completing worker.
     That file was invisible to every earlier version of this guard.

So worker scope is now *discovered* — from actual `Thread(target=...)`,
`submit(...)` and `add_done_callback(...)` arguments — and the convention
names are only a backstop.

`test_no_mixin_class_calls_after_zero` is the stronger, simpler rule, and the
one that makes a conversion complete rather than partial: in a class that has
a pump, `self.after(0, ...)` is never the right call. It means exactly what
`_post()` means on the Tk thread, and only `_post()` is also correct off it —
so which thread a given helper runs on stops being something anyone has to
work out. A timed `after(N, ...)` for N > 0 is a real timer and stays legal.

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

# Backstop only — see the module docstring. Real scope is resolved below.
_WORKER_NAMES = {"_worker", "worker"}
_WORKER_SUFFIXES = ("_worker", "_threadsafe")
_WORKER_PREFIXES = ("_bg_",)

# Calls that hand a callable to another thread.
_SPAWNERS = {"submit", "add_done_callback"}


# ── Ratchet ───────────────────────────────────────────────────────────────
#
# Every window is converted, so this is empty. It stays a ratchet rather than
# a bare `== {}` so a genuinely hard case in future can be recorded here with
# a reason, instead of the guard being disabled outright.
_KNOWN_OFFENDERS: dict = {}


def _worker_scope(tree) -> set:
    """Function names that run on a background thread in this module."""
    names = set()

    def _add(node):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Lambda):
            # add_done_callback(lambda f, k=key: self._on_done(k, f))
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "self"):
                    names.add(sub.func.attr)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_thread = ((isinstance(fn, ast.Attribute) and fn.attr == "Thread")
                     or (isinstance(fn, ast.Name) and fn.id == "Thread"))
        if is_thread:
            for kw in node.keywords:
                if kw.arg == "target":
                    _add(kw.value)
        elif isinstance(fn, ast.Attribute) and fn.attr in _SPAWNERS:
            for arg in node.args:
                _add(arg)

    return names | _WORKER_NAMES


def _is_worker_name(name: str, scope: set) -> bool:
    return (name in scope
            or name.endswith(_WORKER_SUFFIXES)
            or name.startswith(_WORKER_PREFIXES))


class _Scanner(ast.NodeVisitor):
    """Flag `self.<tk method>(...)` lexically inside worker-thread code."""

    def __init__(self, rel: str, scope: set):
        self.rel = rel
        self.scope = scope
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
                and any(_is_worker_name(n, self.scope) for n in self.stack)):
            chain = " > ".join(self.stack)
            self.hits.append(
                f"{self.rel}:{node.lineno}: self.{fn.attr}(...) inside {chain}")
        self.generic_visit(node)


def _iter_src():
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        yield path, tree


def _offenders() -> list:
    out = []
    for path, tree in _iter_src():
        scanner = _Scanner(path.relative_to(_ROOT).as_posix(),
                           _worker_scope(tree))
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
        "let a main-thread pump run it (theme.UiPumpMixin): " + repr(new))


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


# ── the stronger rule: a class with a pump never needs after(0, ...) ──────

def _mixin_classes(tree) -> dict:
    """Classes naming UiPumpMixin as a base -> the ClassDef node."""
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and any(isinstance(b, ast.Name) and b.id == "UiPumpMixin"
                    for b in n.bases)}


def _self_calls(node) -> set:
    return {c.func.attr for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "self"}


def _after_zero_calls(node) -> list:
    """Line numbers of `self.after(0, ...)` / `self.after_idle(...)`."""
    out = []
    for c in ast.walk(node):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "self"):
            continue
        if c.func.attr == "after_idle":
            out.append(c.lineno)
        elif (c.func.attr == "after" and c.args
                and isinstance(c.args[0], ast.Constant)
                and c.args[0].value == 0):
            out.append(c.lineno)
    return out


def test_no_mixin_class_calls_after_zero():
    """In a class with a pump, `after(0, ...)` is never the right call.

    It means what `_post()` means on the Tk thread and is wrong off it, so
    banning it outright removes the need to work out which thread any given
    helper runs on — which is precisely the reasoning that let three whole
    classes of cross-thread call hide from the earlier guard.
    """
    bad = []
    for path, tree in _iter_src():
        rel = path.relative_to(_ROOT).as_posix()
        for cls, node in _mixin_classes(tree).items():
            for lineno in _after_zero_calls(node):
                bad.append(f"{rel}:{lineno} ({cls})")
    assert bad == [], (
        "these are in a class that has a UI pump — use self._post(fn, *args) "
        "instead, which is correct from either thread: " + repr(bad))


def test_every_mixin_user_starts_its_pump():
    """The failure this catches is invisible by construction.

    `_post` needs the queue that `_start_ui_pump` creates. A class that mixes
    the mixin in but never starts it raises AttributeError on its FIRST post
    — on a worker thread, where nothing is watching — and the window then
    simply never updates. Same silent shape as the bug being fixed.
    """
    missing = []
    for path, tree in _iter_src():
        for cls, node in _mixin_classes(tree).items():
            if "_start_ui_pump" not in _self_calls(node):
                missing.append(f"{path.relative_to(_ROOT).as_posix()}:{cls}")
    assert missing == [], (
        "these mix in UiPumpMixin but never call self._start_ui_pump(), so "
        "the first _post() raises AttributeError on a worker thread where "
        "nothing reports it: " + repr(missing))


# ── the conversion cannot be quietly undone ───────────────────────────────

# Every Tk window that runs background work. Named individually because the
# ratchet alone would stay green if someone dropped the mixin from one.
_CONVERTED = (
    "app.py",
    "dialogs/ai_code_review.py",
    "dialogs/checks_dialog.py",
    "dialogs/codegraph_daemon_manager.py",
    "dialogs/codegraph_mcp_picker.py",
    "dialogs/cost_viewer.py",
    "dialogs/doc_drafter.py",
    "dialogs/git_commit.py",
    "dialogs/gitignore.py",
    "dialogs/ollama_model_mgr.py",
    "dialogs/private_repo_mgr.py",
    "dialogs/release_wizard.py",
    "dialogs/roadmap_mgr.py",
    "dialogs/scrub_history.py",
    "dialogs/test_manager.py",
    "dialogs/tokensave_mcp_picker.py",
    "dialogs/tool_manager.py",
)


def test_the_converted_windows_use_the_shared_mixin():
    for rel in _CONVERTED:
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        assert _mixin_classes(tree), (
            f"{rel} no longer mixes in UiPumpMixin, so its workers have "
            "nowhere safe to post")


def test_the_mixin_itself_is_intact():
    src = (_SRC / "theme.py").read_text(encoding="utf-8")
    for token in ("class UiPumpMixin", "def _start_ui_pump", "def _post",
                  "def _post_after", "def _ui_pump", "def _stop_ui_pump"):
        assert token in src, f"theme.py lost {token}"


# ── the guard can actually fail ───────────────────────────────────────────

def _scan(source: str) -> list:
    tree = ast.parse(source)
    scanner = _Scanner("synthetic.py", _worker_scope(tree))
    scanner.visit(tree)
    return scanner.hits


def test_the_guard_would_catch_a_regression():
    """A guard that cannot fail is not a guard."""
    assert len(_scan(
        "class D:\n"
        "    def go(self):\n"
        "        def _worker():\n"
        "            self.after(0, lambda: None)\n")) == 1


def test_the_guard_catches_an_unconventionally_named_worker():
    """The blind spot that hid 17 real sites: a thread target named anything
    other than `_worker`."""
    assert len(_scan(
        "class D:\n"
        "    def go(self):\n"
        "        def _refresh_the_list():\n"
        "            self.after(0, lambda: None)\n"
        "        threading.Thread(target=_refresh_the_list).start()\n")) == 1


def test_the_guard_catches_an_executor_done_callback():
    """The blind spot that hid ChecksDialog entirely: concurrent.futures
    never goes near threading.Thread."""
    assert len(_scan(
        "class D:\n"
        "    def _on_done(self, f):\n"
        "        self.after(0, lambda: None)\n"
        "    def go(self):\n"
        "        fut.add_done_callback(lambda f: self._on_done(f))\n")) == 1


def test_main_thread_tk_calls_are_not_flagged():
    """Only worker-scoped calls count — the pump itself must stay legal."""
    assert _scan("class D:\n"
                 "    def _pump(self):\n"
                 "        self.after(50, self._pump)\n") == []


def test_a_real_timer_stays_legal():
    """`after(N, ...)` for N > 0 is a timer, not a thread hand-off."""
    tree = ast.parse("class D(UiPumpMixin, tk.Toplevel):\n"
                     "    def hint(self):\n"
                     "        self.after(2000, self._clear)\n")
    node = next(iter(_mixin_classes(tree).values()))
    assert _after_zero_calls(node) == []


if __name__ == "__main__":
    counts = _counts_by_file()
    new = {f: n for f, n in counts.items() if f not in _KNOWN_OFFENDERS}
    worse = {f: (counts.get(f, 0), b) for f, b in _KNOWN_OFFENDERS.items()
             if counts.get(f, 0) > b}
    after_zero = []
    for _path, _tree in _iter_src():
        _rel = _path.relative_to(_ROOT).as_posix()
        for _cls, _node in _mixin_classes(_tree).items():
            after_zero += [f"{_rel}:{n} ({_cls})"
                           for n in _after_zero_calls(_node)]
    if new or worse or after_zero:
        print("FAIL: cross-thread Tk calls present.")
        for f, n in sorted(new.items()):
            print(f"  NEW        {f}: {n}")
        for f, (now, was) in sorted(worse.items()):
            print(f"  WORSE      {f}: {now} (was {was})")
        for line in after_zero:
            print(f"  AFTER(0)   {line}")
        raise SystemExit(1)
    print(f"OK: no cross-thread Tk calls, and no after(0, ...) in the "
          f"{len(_CONVERTED)} windows that have a UI pump.")
