"""smoke_runner.py — Run the logic-layer smoke test suite from the manager UI.

Provides:
  run_smoke_tests(project_root)              → (passed, total, output_text)
  run_pytest_in_background(...)              → PytestRun handle (V-E v4.13)
  install_pre_commit_hook(repo_root)         → (ok, message)
  uninstall_pre_commit_hook(repo_root)       → (ok, message)
  is_hook_installed(repo_root)               → bool

As of v4.12 this runs the whole pytest suite under ``tests/`` rather than a
single file, and discovery is pytest's own: it collects both pytest-native
test functions and the ``unittest.TestCase`` classes that several test
modules still use. Roadmap-9 split the original ``tests/smoke_test.py`` into
per-subsystem modules; nothing here names a test file, so that split needed
no change on this side.

v4.13 (V-E) extracts the worker-thread+subprocess wrapping into
``run_pytest_in_background`` so multiple dialogs can share it. Adds a
``PytestRun`` handle whose ``.cancel()`` terminates the subprocess —
required by the Test Manager's Tab 1 Stop button (V-G).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from typing import Callable, Optional

from helpers.proc_kill import kill_popen_tree

# Marker embedded in every hook the manager writes so we can identify it.
_HOOK_MARKER = "# TokenSaveManager-smoke-hook"

# Content written to .git/hooks/pre-commit.
# v4.12: switched from `python tests/smoke_test.py` to the full pytest
# suite excluding Tk-marked tests (so the hook doesn't need a display).
_HOOK_SCRIPT = """\
#!/bin/sh
{marker} — managed by Token Save Manager
# To disable: Settings → Behaviour → uncheck "Run smoke tests before commits"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
python -m pytest tests/ -m "not tk" -q
exit $?
""".format(marker=_HOOK_MARKER)

# --- subprocess creation flag (Windows: hide the console window) ----------
# Uses the platform-guarded constant rather than a local try/except, so there
# is exactly one way to reach these flags and the structural guard in
# tests/test_no_windows_only_subprocess_flags.py can stay strict.
from constants import CREATE_NO_WINDOW as _CREATE_NO_WINDOW  # noqa: E402


# ── Public helpers ─────────────────────────────────────────────────────────

def run_smoke_tests(project_root: str) -> tuple[int, int, str]:
    """Run the full ``tests/`` pytest suite in *project_root* and return
    ``(passed, total, combined_output)``.

    Returns ``(0, 0, err_text)`` when the tests directory can't be found
    or pytest fails to launch.

    v4.12: now invokes ``python -m pytest tests/`` instead of the single
    ``smoke_test.py`` file. Tk-marked tests are excluded by default since
    most users running this from the manager are on Windows where Tk
    works, BUT the pre-commit hook variant also excludes them — pytest's
    own discovery + the existing fixtures take it from there.
    """
    tests_dir = os.path.join(project_root, "tests")
    if not os.path.isdir(tests_dir):
        return 0, 0, f"tests/ directory not found: {tests_dir}"

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-v"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=300,             # bumped from 120 — full suite incl. Tk
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 0, 0, "pytest timed out after 300 s."
    except OSError as exc:
        return 0, 0, f"Failed to launch pytest: {exc}"

    combined = (proc.stdout or "") + (proc.stderr or "")
    passed, total = _parse_pytest_summary(combined)
    return passed, total, combined


def _kill_tree(proc) -> None:
    """Kill *proc* and its entire child process tree (best-effort).

    A misbehaving AI test can spawn children (e.g. one that calls the real
    ``run_smoke_tests`` launches a child pytest, or ``spawn_claude_cli`` opens
    a console). Killing only the parent on timeout would orphan them, leaving
    them burning CPU until reboot.

    Delegates to ``proc_kill.kill_popen_tree``, which keeps this shape exactly:
    ``taskkill /F /T`` on Windows, ``killpg`` with SIGKILL on POSIX, and
    ``proc.kill()`` as the last resort. Tree kill stays TREE kill -- the
    shared helper takes ``tree`` as a parameter precisely so folding these
    two callers together could not quietly turn this into a single-process
    kill and start orphaning children again.
    """
    kill_popen_tree(proc)


def run_single_test_file(project_root: str, test_relpath: str,
                         timeout: int = 20) -> "tuple[bool, str]":
    """Run ONE pytest file and return ``(passed, combined_output)``.

    Used by the generate-then-verify loop: a freshly AI-generated test is run in
    isolation; only tests that pass are kept.

    ``passed`` is True ONLY when pytest exits 0 (ran AND all passed). pytest exits
    non-zero on failures (1), no-tests-collected (5), and usage/marker errors (4) —
    all correctly count as not-passing.

    ``timeout`` is deliberately short (20s, decoupled from the LLM timeouts): a
    unit test that infinite-loops, blocks, spawns a child, or starts a Tk
    mainloop is caught fast. On timeout the WHOLE process tree is killed (see
    ``_kill_tree``) so a child pytest/console isn't orphaned. ``--tb=short
    --no-header`` keeps the traceback compact for the repair prompt.

    Source-mode only: ``sys.executable -m pytest`` assumes a real Python
    interpreter (same assumption as ``run_smoke_tests``). Under a frozen build it
    returns ``(False, <error>)``, which the caller treats as a normal failure.
    """
    popen_kw = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=project_root, creationflags=_CREATE_NO_WINDOW,
    )
    if sys.platform != "win32":
        popen_kw["start_new_session"] = True       # own group → killpg on timeout
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", test_relpath,
             "--tb=short", "--no-header", "-rfE"],
            **popen_kw,
        )
    except OSError as exc:
        return False, f"Failed to launch pytest: {exc}"

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)            # reap so no zombie remains
        except Exception:
            pass
        return False, (f"TIMEOUT after {timeout}s — the test likely has an "
                       "infinite loop, a blocking call, spawns a child process, "
                       "or starts a Tk mainloop.")
    return proc.returncode == 0, (out or "")


def run_pytest_selection(project_root: str, nodeids: "tuple | list" = (),
                         markers: str = "",
                         timeout: int = 300) -> "tuple[str, str]":
    """Run part or all of the suite; return ``(combined_output, junit_xml)``.

    The per-test counterpart of :func:`run_smoke_tests`, and the one the
    headless CLI drives. Three deliberate differences:

    * **``-v`` is passed explicitly.** This project's own ``addopts`` forces
      it, but the CLI runs against *other* people's repositories, and the
      verbose progress lines are the only place pytest prints exact nodeids.
      Passing it twice is harmless; relying on someone else's config is not.

    * **``--junitxml`` goes to a temp file that is always cleaned up.** It
      supplies per-test duration and failure messages, which the progress
      lines do not carry. It is *not* an identity source — see
      ``helpers/pytest_report.py`` for the measurement behind that.

    * **``-p no:cacheprovider``**, so running one test from an editor does not
      write ``.pytest_cache`` into the user's tree as a side effect of looking.

    ``nodeids`` and ``markers`` are alternatives, not a pair: the caller
    (``cli.py``) refuses them together rather than inventing a composition
    rule. With neither, the whole ``tests/`` tree runs.

    On timeout the entire process tree is killed (see :func:`_kill_tree`), and
    whatever pytest managed to print is returned — a partial run is still
    worth attributing, and the caller decides what an unreadable summary means.
    """
    import tempfile

    tests_path = os.path.join(project_root, "tests")
    if not os.path.isdir(tests_path):
        return f"tests/ directory not found: {tests_path}", ""

    tmpdir = tempfile.mkdtemp(prefix="tsm-pytest-")
    report = os.path.join(tmpdir, "report.xml")

    argv = [sys.executable, "-m", "pytest"]
    argv += list(nodeids) if nodeids else ["tests/"]
    if markers:
        # The Manager's `--markers` is its own option name; pytest's selector
        # is `-m`. `--markers` means "list the registered markers" to pytest,
        # so passing it through would print a catalogue and run nothing.
        argv += ["-m", markers]
    argv += ["-v", "-p", "no:cacheprovider", f"--junitxml={report}"]

    popen_kw = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=project_root, creationflags=_CREATE_NO_WINDOW,
    )
    if sys.platform != "win32":
        popen_kw["start_new_session"] = True       # own group → killpg
    try:
        proc = subprocess.Popen(argv, **popen_kw)
    except OSError as exc:
        _rmtree(tmpdir)
        return f"Failed to launch pytest: {exc}", ""

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)   # reap; keep partial output
        except Exception:
            out = ""
        out = (out or "") + f"\npytest timed out after {timeout} s."

    junit = ""
    try:
        with open(report, encoding="utf-8", errors="replace") as fh:
            junit = fh.read()
    except OSError:
        # A run killed before pytest wrote its report has no XML. The progress
        # lines are still there, so results degrade to "no duration" rather
        # than disappearing.
        pass
    _rmtree(tmpdir)
    return (out or ""), junit


def _rmtree(path: str) -> None:
    """Best-effort cleanup of the temp report directory."""
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:                              # noqa: BLE001 - cleanup
        pass


def run_gate(project_root: str, timeout: int = 180) -> "tuple[bool, str]":
    """Run the project's `-m "not tk"` gate once: ``(all_passed, combined_output)``.

    Used to RE-VERIFY freshly generated tests against the FULL suite, not just in
    isolation — a test can pass alone yet fail amid the real suite (shared state,
    test-ordering, or a test that itself invokes the test runner). `-m "not tk"`
    (not a markerless run) so a hanging generated Tk test can't consume the whole
    timeout and nuke the batch, and to match the CI gate. ``-rfE`` guarantees the
    parseable ``FAILED/ERROR …::id`` summary block for owner attribution.

    ``all_passed`` is True only on exit 0. On timeout the whole process tree is
    killed. Frozen build / launch failure → ``(False, <error>)``.
    """
    popen_kw = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=project_root, creationflags=_CREATE_NO_WINDOW,
    )
    if sys.platform != "win32":
        popen_kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "tests/", "-m", "not tk",
             "-rfE", "--tb=line", "-q"],
            **popen_kw,
        )
    except OSError as exc:
        return False, f"Failed to launch pytest: {exc}"
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return False, f"TIMEOUT after {timeout}s running the full -m 'not tk' gate."
    return proc.returncode == 0, (out or "")


# ── V-E: cancellable background pytest runner ──────────────────────────────

class PytestRun:
    """Handle returned by :func:`run_pytest_in_background`.

    Exposes the worker thread and (once spawned) the ``subprocess.Popen``
    so a caller can ``.cancel()`` mid-run. Cancellation terminates the
    pytest subprocess with a 5 s grace before SIGKILL.

    Tk dialogs typically store this handle while the run is in flight,
    surface a ``🛑 Stop`` button bound to ``handle.cancel()``, and clear
    the handle in the on_complete callback.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancelled: bool = False
        self._lock = threading.Lock()

    # NOTE: properties read shared state — main thread reads them while
    # worker thread mutates them. GIL makes attribute reads atomic for
    # these simple values; the lock guards the cancel-vs-spawn race only.

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self, grace_s: float = 5.0) -> None:
        """Terminate the underlying pytest subprocess (if any).

        Idempotent — safe to call repeatedly. After ``grace_s`` seconds
        a hard kill is sent if the process hasn't exited.
        """
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            pass


def parse_pytest_summary(output: str) -> tuple[int, int]:
    """Extract (passed, total) from pytest's footer line.

    Recognises ``===== N passed [, M skipped] [, K failed] in Xs =====``
    and minor variants. Skipped tests are NOT counted in either ``passed``
    or ``total`` (pytest already reports them separately).
    """
    passed = failed = errored = 0
    for line in output.splitlines():
        if "passed" not in line and "failed" not in line and "error" not in line:
            continue
        if "=" not in line:
            continue
        m_pass  = re.search(r"(\d+)\s+passed",  line)
        m_fail  = re.search(r"(\d+)\s+failed",  line)
        m_error = re.search(r"(\d+)\s+errors?", line)
        if m_pass:   passed  = int(m_pass.group(1))
        if m_fail:   failed  = int(m_fail.group(1))
        if m_error:  errored = int(m_error.group(1))
    return passed, passed + failed + errored


#: The name this was known by before `cli.py` needed it too. An alias rather
#: than a second implementation: two parsers of one footer is how two callers
#: start reporting different totals for the same run.
_parse_pytest_summary = parse_pytest_summary


def run_pytest_in_background(
    project_root: str,
    on_complete: Callable[[int, int, str, bool], None],
    target: str = "tests/",
    extra_args: Optional[list[str]] = None,
    timeout_s: int = 300,
) -> PytestRun:
    """Spawn ``python -m pytest <target>`` in a daemon thread.

    Arguments:
      project_root   — directory to ``cd`` into (contains ``tests/``)
      on_complete    — called once when pytest exits or is cancelled.
                       Signature: ``(passed, total, combined_output,
                       cancelled)``. **Fires from the worker thread** —
                       Tk callers MUST wrap their callback via
                       ``widget.after(0, lambda: cb(...))``.
      target         — what to run. ``"tests/"`` (default) for whole
                       suite; ``"tests/test_foo.py"`` for one file.
      extra_args     — appended to the pytest argv; e.g.
                       ``["-m", "not tk"]`` for marker filtering.
      timeout_s      — hard wall-clock cap. Cancellation does NOT bypass
                       this — even a cancelled subprocess gets the grace.

    Returns a :class:`PytestRun` handle; ``.cancel()`` to interrupt.
    """
    handle = PytestRun()
    args: list[str] = [sys.executable, "-m", "pytest", target, "-v"]
    if extra_args:
        args.extend(extra_args)

    def _worker() -> None:
        if not os.path.isdir(os.path.join(project_root, "tests")):
            on_complete(0, 0,
                f"tests/ directory not found under {project_root!r}",
                handle._cancelled)
            return
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project_root,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            on_complete(0, 0, f"Failed to launch pytest: {exc}",
                          handle._cancelled)
            return

        with handle._lock:
            if handle._cancelled:
                # Race: caller cancelled before Popen returned. Terminate
                # the just-spawned process immediately.
                try: proc.terminate()
                except Exception: pass
            handle._proc = proc

        try:
            stdout_text, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except Exception: pass
            on_complete(0, 0,
                f"pytest timed out after {timeout_s} s.",
                handle._cancelled)
            return
        except Exception as exc:
            on_complete(0, 0,
                f"pytest worker error: {type(exc).__name__}: {exc}",
                handle._cancelled)
            return

        output = stdout_text or ""
        if handle._cancelled:
            on_complete(0, 0,
                output + "\n[cancelled by user]\n",
                True)
            return

        passed, total = _parse_pytest_summary(output)
        on_complete(passed, total, output, False)

    handle._thread = threading.Thread(
        target=_worker, daemon=True, name="pytest-worker")
    handle._thread.start()
    return handle


# ── Hook install/remove (unchanged from v4.12) ────────────────────────────

def is_hook_installed(repo_root: str) -> bool:
    """Return True iff the manager's pre-commit hook is installed."""
    hook_path = _hook_path(repo_root)
    if not os.path.isfile(hook_path):
        return False
    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            return _HOOK_MARKER in fh.read()
    except OSError:
        return False


def install_pre_commit_hook(repo_root: str) -> tuple[bool, str]:
    """Write (or overwrite) the manager's pre-commit hook.

    If a pre-existing hook WITHOUT the manager's marker is found, it is NOT
    overwritten — we return ``(False, reason)`` so the caller can warn the
    user that a custom hook already exists.
    """
    hook_path = _hook_path(repo_root)

    # Guard against overwriting a user's own hook.
    if os.path.isfile(hook_path):
        try:
            with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            if _HOOK_MARKER not in content:
                return (
                    False,
                    f"A pre-commit hook already exists at:\n  {hook_path}\n\n"
                    "It was NOT written by the manager — overwriting it could "
                    "break your workflow.\n\nDelete or rename it manually, then "
                    "re-enable the smoke-test hook here.",
                )
        except OSError as exc:
            return False, f"Could not read existing hook: {exc}"

    hooks_dir = os.path.dirname(hook_path)
    try:
        os.makedirs(hooks_dir, exist_ok=True)
        with open(hook_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_HOOK_SCRIPT)
        # Make the hook executable on POSIX.
        if os.name != "nt":
            os.chmod(hook_path, 0o755)
    except OSError as exc:
        return False, f"Could not write hook: {exc}"

    return True, f"Hook installed at:\n  {hook_path}"


def uninstall_pre_commit_hook(repo_root: str) -> tuple[bool, str]:
    """Remove the manager's pre-commit hook.

    Only removes the file if it bears the manager's marker — never deletes a
    user's custom hook.
    """
    hook_path = _hook_path(repo_root)
    if not os.path.isfile(hook_path):
        return True, "No hook file found — nothing to remove."

    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"Could not read hook file: {exc}"

    if _HOOK_MARKER not in content:
        return (
            False,
            "The pre-commit hook at:\n"
            f"  {hook_path}\n"
            "was NOT written by the manager.  It has NOT been removed.",
        )

    try:
        os.remove(hook_path)
    except OSError as exc:
        return False, f"Could not remove hook: {exc}"

    return True, "Hook removed."


# ── Internal ───────────────────────────────────────────────────────────────

def _hook_path(repo_root: str) -> str:
    return os.path.join(repo_root, ".git", "hooks", "pre-commit")
