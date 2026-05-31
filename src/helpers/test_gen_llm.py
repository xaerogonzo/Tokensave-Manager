"""AI-powered test content generation.

Dispatches to Claude CLI (``claude --print``) or the configured LLM provider
(Ollama / Anthropic API / OpenAI-compat) to generate a real pytest file for a
given source file — replacing the template-stub ``assert True`` placeholders
that ``test_scaffold.generate_test_file`` writes.

No Tkinter imports.  The caller is responsible for threading.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback example used when the project has no small test file yet
# ---------------------------------------------------------------------------

_FALLBACK_EXAMPLE = '''\
"""Tests for helpers/example.py — minimal structure reference."""
import pytest
from helpers import example


def test_basic():
    """Smoke test: module imports cleanly."""
    assert example is not None


def test_returns_expected_value():
    result = example.main_function("input")
    assert result == "expected"
'''

# Template-matched fallbacks — used when the repo has no on-disk example of the
# right shape (so a fresh repo's first Tk/subprocess test still learns the right
# pattern instead of copying a pure example and hanging).
_FALLBACK_EXAMPLE_SUBPROCESS = '''\
"""Tests for helpers/example.py — subprocess helper (mock at the import site)."""
from types import SimpleNamespace
from helpers import example


def test_runs_and_parses(monkeypatch):
    monkeypatch.setattr(
        "helpers.example.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    assert example.run_thing(".") == "ok"


def test_failure_returns_none(monkeypatch):
    monkeypatch.setattr(
        "helpers.example.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert example.run_thing(".") is None
'''

_FALLBACK_EXAMPLE_TK = '''\
"""Tests for dialogs/example.py — Tk dialog (tk_root fixture, never mainloop)."""
import pytest

pytestmark = pytest.mark.tk
tk = pytest.importorskip("tkinter")

from dialogs.example import ExampleDialog


def test_builds_without_error(tk_root, mock_config):
    dlg = ExampleDialog(tk_root, mock_config)
    assert dlg.winfo_exists()
    dlg.destroy()
'''

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert Python test engineer writing pytest tests for a project that \
uses these conventions:

- Test files live under tests/ and are named test_<module>.py \
  (dialogs use test_dialog_<name>.py).
- Available conftest fixtures:
    * tk_root          — function-scoped withdrawn Tk root; asserts no daemon \
threads leaked at teardown (G-D). Use for all dialog tests.
    * _tk_root_session — session-scoped backing root; never reference directly \
in tests.
    * wait_for(pred, timeout_s)  — drives root.update() each iteration so \
after(0,...) callbacks fire; use instead of time.sleep() when waiting for \
worker threads (G-G/G-M).
    * patch_after(dialog) → AfterHarness — captures self.after() into a queue; \
call harness.advance(ms) / harness.drain() for tick-based after() loops (G-A/G-H).
    * fake_home        — redirects HOME/USERPROFILE/APPDATA to tmp_path; use \
for helpers that read ~/.claude or %APPDATA% paths (G-F).
    * mock_config      — minimal ManagerConfig stub; check cfg._saved is True \
after any operation that should persist config.
- Mark ALL Tkinter tests at module level: pytestmark = pytest.mark.tk
- Guard the tk import: tk = pytest.importorskip("tkinter")
- Mock at the import site (G-E): patch("module.under.test.subprocess.run"), \
never patch("subprocess.run") globally — the latter breaks pytest's own \
subprocess infrastructure.
- Windows subprocess safety (G-WIN): when the code under test passes \
creationflags=CREATE_NO_WINDOW to Popen/run/check_output, assert it in the \
test: _, kwargs = mock.call_args; assert kwargs.get("creationflags") == \
CREATE_NO_WINDOW
- Keep tests focused and independent — no shared mutable state between tests.
- IMPORTS (critical): the file MUST import every name it references. ALWAYS \
`import pytest`. Import the module/class under test \
(e.g. `from controllers.projects_tab import ProjectsTabController`). Import every \
stdlib module you use (`subprocess`, `os`, …) and every typing name (`from typing \
import Optional`). A reference to an unimported name makes the file invalid.
- Import each name explicitly, one symbol per `from x import y` line (a clean, \
explicit import list — list every symbol you need).
- Every fixture that uses `monkeypatch` MUST be declared with the default function \
scope: write plain `@pytest.fixture` with no `scope=` argument (monkeypatch is \
function-scoped, so a broader-scoped fixture using it fails at setup).
- Assert what the API actually returns, taken from the source above — do not guess:
    * Tk normalises `widget.cget("font")` to a STRING like "Arial 11", not a tuple. \
Compare with `tkfont.Font(font=w.cget("font")).actual()` or the "family size" string, \
never `== ("Arial", 11)`.
    * Build a real widget/controller/dialog with the `tk_root` fixture (and a real \
parent), NEVER a `Mock()` master — `tk.Frame(Mock())` raises "Mock has no attribute \
'tk'". Tk construction needs a live Tk parent.
    * Call functions with the EXACT parameter names from the source (e.g. \
`timeout_s=`, not `timeout=`); read the signature above before calling.
- Guard platform-specific code: only patch/assert POSIX-only calls \
(`os.getpgid`, `os.killpg`, `os.setsid`) under `sys.platform != "win32"` (or \
`@pytest.mark.skipif`); on Windows assert the `taskkill`/`CREATE_NO_WINDOW` path.
- NEVER run a real `git`/network/subprocess in a fixture or test — mock at the import \
site (`monkeypatch.setattr("module.under.test.subprocess.run", …)`); a real \
`subprocess.run` in a fixture fails under the manager's windowed process.
- MUST NOT (or the test is killed at a 20s timeout and discarded): call \
`.mainloop()`; call `input()`; make a real network request; run a real \
subprocess (git / pytest / ollama / claude); or call the real LLM. MOCK every \
such call at the import site — e.g. \
`monkeypatch.setattr("module.under.test.subprocess.run", ...)`, patch \
`helpers.llm._call_llm`. For Tk, use the `tk_root` fixture — NEVER create a bare \
`tk.Tk()` or call `mainloop()`. Each test must finish in well under 20 seconds.

OUTPUT CONTRACT (critical):
- Respond with ONLY the Python source of the test file, as your message text.
- Do NOT create or write any files. Do NOT use any tools. Do NOT ask for
  permission. Do NOT include any explanation, preamble, markdown fences, or
  commentary — only the .py file contents.
- Begin your reply with the module docstring (triple quotes).

Example of a well-structured test file from this project:
{example_test}
"""

_USER_PROMPT_TEMPLATE = """\
Generate a complete pytest test file for the following source file.

Source file path: {rel_path}

Source code:
```python
{source_code}
```

Output the full Python source for tests/{test_filename} as your reply text —
ONLY the code, no prose, no markdown fences, no file-writing, no permission
requests. Start with the module docstring.
"""

# Appended to the user prompt on a single repair retry when the first reply
# didn't parse as Python (e.g. the model emitted prose or a permission request).
_REPAIR_PROMPT_TEMPLATE = """\
Your previous reply was NOT valid Python and could not be parsed:
  {error}

Reply again with ONLY the corrected Python source of the test file — no prose,
no markdown fences, no commentary, no tool use. Start with the module docstring.
"""

# Appended to the repair prompt when the parse error looks like a truncation
# (unterminated string / unexpected EOF) — i.e. the prior reply ran past the
# token budget. Tell the model to produce a shorter, COMPLETE file.
_TRUNCATION_HINT = (
    "Your previous output was CUT OFF mid-file (it ended in the middle of the "
    "code). Produce a SHORTER but COMPLETE test file — use fewer test functions "
    "if needed, but the file MUST end cleanly with all strings and blocks closed."
)

# Pasted into an AGENTIC Claude Code session ("Copy Claude Code prompt"): unlike the
# one-shot --print path, Claude Code writes the file, runs pytest, and iterates to
# green itself — so we instruct it to do exactly that. Only {file_list} is filled.
_CLAUDE_CODE_HANDOFF_TEMPLATE = """\
Write pytest tests for the following source files in this project. For EACH file,
create its `tests/test_*.py`, then RUN it and FIX any failures until it is green
before moving to the next.

Files needing tests:
{file_list}

Workflow for each file:
1. Read the source file and a few existing `tests/test_*.py` + `tests/conftest.py`
   to match the project's style and fixtures.
2. Write the test file (focused, independent tests).
3. Run it: `pytest -m "not tk" tests/<the_test_file> -q`. For a Tk/dialog source,
   mark the module `pytestmark = pytest.mark.tk` and run `pytest -m tk tests/<file> -q`.
4. If it fails, fix the test (or flag a genuine source bug) and re-run until all pass.

Project conventions (follow exactly):
- Tests live in `tests/`, named `test_<module>.py`; `src/` is on `sys.path` so import
  as `from helpers.x import Y` / `from controllers.x import Y`.
- Mock at the import site (`monkeypatch.setattr("module.under.test.subprocess.run", ...)`),
  never globally; NEVER run real git/network/subprocess in a test or fixture.
- Tk: use the `tk_root` fixture (never a bare `tk.Tk()` or a `Mock()` master);
  mark Tk tests `pytestmark = pytest.mark.tk`; never call `.mainloop()`.
- `widget.cget("font")` returns a normalized string ("Arial 11"), not a tuple.
- Guard POSIX-only calls (`os.getpgid`/`killpg`) behind `sys.platform`/`skipif`.
- Only CREATE files under `tests/`. Do NOT modify any source file.

When done, list the test files you created and confirm they pass.
"""


def build_claude_code_handoff_prompt(suggestions, project_root: str) -> str:
    """Build a paste-into-Claude-Code instruction to write + verify tests for
    *suggestions* (SuggestedTest items). Pure — no LLM call, no I/O beyond deriving
    the destination test filenames."""
    from helpers.test_scaffold import _test_filename_for
    rows = []
    for sg in suggestions:
        try:
            tname = _test_filename_for(sg.source_path, project_root)
        except Exception:
            base = os.path.splitext(os.path.basename(sg.source_path))[0]
            tname = f"test_{base}.py"
        rel = getattr(sg, "rel_path", "") or os.path.basename(sg.source_path)
        rows.append(f"- `{rel}` → `tests/{tname}`")
    return _CLAUDE_CODE_HANDOFF_TEMPLATE.format(file_list="\n".join(rows))

# Appended to the user prompt on a runtime-failure repair pass: the generated
# test parsed fine but FAILED when actually run under pytest.
_RUNTIME_REPAIR_TEMPLATE = """\
The test file you generated parsed fine but FAILED when run under pytest:

--- pytest output ---
{report}
--- end output ---

Here is the file you wrote:
```python
{prior}
```

Reply with ONLY the corrected, full Python source of the test file that makes
the test pass — fix the failure (imports, fixtures, assertions, mocking at the
import site). No prose, no markdown fences, no commentary. Start with the
module docstring.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_ai_test_content(
    source_path: str,
    project_root: str,
    backend: str,           # "claude_cli" | "llm" | "auto"
    cfg,                    # ManagerConfig — for llm + claude_cli settings
    cancel_event: "threading.Event | None" = None,
    max_source_chars: int = 8_000,
    template: "str | None" = None,
    existing_test: "str | None" = None,
    on_token=None,
) -> "tuple[str | None, str | None]":
    """Generate test file content using an AI backend.

    Args:
        source_path:    Absolute path to the source file to test.
        project_root:   Absolute path to the project root.
        backend:        One of ``"claude_cli"``, ``"llm"``, or ``"auto"``.
        cfg:            Live ``ManagerConfig`` instance.
        cancel_event:   Optional ``threading.Event``; checked before dispatch.
        max_source_chars: Cap on source code sent to the model.
        template:       Scaffold template of the source (pure_helper /
                        subprocess_helper / dialog_tk); steers the example test
                        the model is shown so it copies the right mocking pattern.
        existing_test:  For regeneration — the current test file's content. When
                        given, the prompt instructs the model to RETAIN all
                        existing tests and ADD coverage (no coverage loss).

    Returns:
        ``(content, None)`` on success or ``(None, error_message)`` on failure.
    """
    if cancel_event and cancel_event.is_set():
        return None, "Cancelled."

    # Resolve backend
    effective_backend = _resolve_backend(backend, cfg)
    if effective_backend is None:
        return None, (
            "No AI backend is configured.\n"
            "Set a Claude Code CLI path or an LLM provider in Settings."
        )

    # Build prompts
    try:
        system_prompt, user_prompt = _build_prompts(
            source_path, project_root, max_source_chars,
            template=template, existing_test=existing_test,
        )
    except Exception as exc:
        return None, f"Could not read source file: {exc}"

    if cancel_event and cancel_event.is_set():
        return None, "Cancelled."

    def _run(up: str) -> "tuple[str | None, str | None]":
        return _dispatch(effective_backend, cfg, system_prompt, up, project_root,
                         on_token=on_token)

    # First attempt.
    raw, derr = _run(user_prompt)
    if raw is None:
        return None, (f"AI backend returned no output: {derr}" if derr
                      else "AI backend returned no output — check your settings.")
    code = _extract_code(raw)
    if not code.strip():
        return None, "AI returned an empty response."
    err = _looks_like_python(code)

    # One repair pass when the reply isn't valid Python — covers the case where
    # the model emitted prose or a permission request ("May I write this file?")
    # instead of code. NEVER return un-parseable text (the caller writes it to disk).
    if err is not None:
        if cancel_event and cancel_event.is_set():
            return None, "Cancelled."
        repair_up = user_prompt + "\n\n" + _REPAIR_PROMPT_TEMPLATE.format(error=err)
        # If the previous reply was cut off (output exceeded max_tokens), the
        # parse error is an unterminated string / unexpected EOF — tell the model
        # to produce a SHORTER complete file instead of re-truncating.
        if any(s in err.lower() for s in ("unterminated", "eof", "was never closed")):
            repair_up += "\n" + _TRUNCATION_HINT
        raw2, _ = _run(repair_up)
        if raw2:
            code2 = _extract_code(raw2)
            err2 = _looks_like_python(code2)
            if err2 is None and code2.strip():
                return _autofix_imports(code2, source_path, project_root), None
            err, code = (err2 or err), (code2 or code)
        first_line = (code.strip().splitlines() or [""])[0][:80]
        return None, (
            f"AI did not return valid Python ({err}). "
            f"First line was: {first_line!r}"
        )

    return _autofix_imports(code, source_path, project_root), None

    return code, None


@dataclass
class VerifiedResult:
    """Outcome of generate-then-verify for one source file.

    status ∈ {"pass", "fail", "error", "cancelled"}:
      * pass      — generated, ran, all assertions passed; file was WRITTEN.
      * fail      — generated valid Python but it failed when run / dropped prior
                    coverage / target locked; NOTHING was written (discarded).
      * error     — could not generate valid Python at all (backend/syntax).
      * cancelled — cancel_event fired mid-flow.
    report holds the last pytest output (for a "View failures" surface).
    preserved_existing — True when this was a REGENERATE that failed: the
    original test file was left untouched (so the UI says "kept original",
    not a bare ✗ that looks like data loss).
    kept/total — for a PARTIAL pass (per-test pruning kept kept-of-total tests and
    dropped the failing ones). Both 0 on a clean full pass (no pruning happened).
    """
    content: "str | None"
    status: str
    report: str = ""
    preserved_existing: bool = False
    kept: int = 0
    total: int = 0
    written_path: str = ""      # repo-relative path of the written test (pass/partial)
    is_tk: bool = False         # written file is @pytest.mark.tk (skips the not-tk gate)


def generate_verified_test(
    source_path: str,
    project_root: str,
    backend: str,
    cfg,
    cancel_event: "threading.Event | None" = None,
    run: bool = True,
    max_runtime_repairs: int = 1,
    template: "str | None" = None,
    allow_overwrite: bool = False,
    target_path: "str | None" = None,
    on_token=None,
) -> VerifiedResult:
    """Generate a test, RUN it, repair once on failure, keep only if it passes.

    New file: write a candidate to throwaway ``tests/test__aigen_<base>_<uuid>.py``,
    run it; on pass ``os.replace`` onto the derived ``test_<base>.py``; on fail
    discard. REGENERATE (``allow_overwrite`` + ``target_path``): read the existing
    test, instruct the model to RETAIN all of it, and after a pass enforce a
    coverage-retention gate (no prior ``test_*`` dropped) before overwriting the
    real file lock-safely — a failed regenerate leaves the original untouched.

    The temp file is always cleaned up in ``finally``. Caller writes nothing.
    """
    if cancel_event and cancel_event.is_set():
        return VerifiedResult(None, "cancelled", "")

    from helpers.test_scaffold import _test_filename_for
    tests_dir = os.path.join(project_root, "tests")

    # Resolve the real destination: explicit (regenerate) or derived (new file).
    if target_path:
        final_path = (target_path if os.path.isabs(target_path)
                      else os.path.join(project_root, target_path))
    else:
        final_path = os.path.join(tests_dir, _test_filename_for(source_path, project_root))
    final_rel = os.path.relpath(final_path, project_root).replace(os.sep, "/")

    # Regenerate: read the existing test so the model retains it, and capture its
    # test ids for the retention gate.
    is_update = bool(allow_overwrite and os.path.exists(final_path))
    existing_test = None
    old_ids: set = set()
    if is_update:
        try:
            with open(final_path, encoding="utf-8", errors="replace") as fh:
                existing_test = fh.read()
            old_ids = _test_function_ids(existing_test)
        except OSError:
            existing_test = None

    content, err = generate_ai_test_content(
        source_path, project_root, backend, cfg, cancel_event,
        template=template, existing_test=existing_test, on_token=on_token)
    if err or not content:
        return VerifiedResult(None, "error", err or "AI returned no content.")
    if not run:
        return VerifiedResult(content, "pass", "")
    if cancel_event and cancel_event.is_set():
        return VerifiedResult(None, "cancelled", "")

    from helpers import smoke_runner          # lazy — avoids import cycle

    base = os.path.splitext(os.path.basename(source_path))[0]
    tmp_name = f"test__aigen_{base}_{uuid.uuid4().hex[:8]}.py"
    tmp_path = os.path.join(tests_dir, tmp_name)
    tmp_rel = os.path.join("tests", tmp_name)

    effective_backend = _resolve_backend(backend, cfg)
    try:
        system_prompt, user_prompt = _build_prompts(
            source_path, project_root, 8_000,
            template=template, existing_test=existing_test)
    except Exception:
        system_prompt = user_prompt = ""

    last_report = ""
    try:
        os.makedirs(tests_dir, exist_ok=True)
        attempts = max(1, max_runtime_repairs + 1)
        for attempt in range(attempts):
            if cancel_event and cancel_event.is_set():
                return VerifiedResult(None, "cancelled", last_report)
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            passed, output = smoke_runner.run_single_test_file(project_root, tmp_rel)
            last_report = output
            if passed:
                # Retention gate — a regenerate must not drop any prior test.
                if is_update and old_ids:
                    missing = old_ids - _test_function_ids(content)
                    if missing:
                        return VerifiedResult(
                            content, "fail",
                            "Regenerated test DROPPED existing tests "
                            f"({', '.join(sorted(missing))}). Kept the original "
                            "to avoid losing coverage.",
                            preserved_existing=True)
                # New-file collision (not a sanctioned overwrite) → keep existing.
                if os.path.exists(final_path) and not allow_overwrite:
                    return VerifiedResult(
                        content, "fail",
                        f"{os.path.basename(final_path)} already exists; not overwriting.")
                if not _safe_replace(tmp_path, final_path):
                    return VerifiedResult(
                        content, "fail",
                        f"{os.path.basename(final_path)} is locked by another "
                        "process — close it and retry.",
                        preserved_existing=is_update)
                return VerifiedResult(content, "pass", output,
                                      written_path=final_rel,
                                      is_tk=("pytest.mark.tk" in content))

            # Failed. Stop if out of repair budget or can't re-dispatch.
            if attempt >= attempts - 1 or effective_backend is None or not system_prompt:
                break
            if cancel_event and cancel_event.is_set():
                return VerifiedResult(None, "cancelled", last_report)
            repair_up = user_prompt + "\n\n" + _RUNTIME_REPAIR_TEMPLATE.format(
                report=(output or "")[-4000:], prior=content)
            raw, _ = _dispatch(effective_backend, cfg, system_prompt, repair_up,
                               project_root, on_token=on_token)
            if not raw:
                break
            fixed = _extract_code(raw)
            if not fixed.strip() or _looks_like_python(fixed) is not None:
                break  # repair produced non-Python → don't run garbage
            # A repair that re-dropped an import gets re-fixed deterministically.
            content = _autofix_imports(fixed, source_path, project_root)

        # Per-test pruning (new-file path only — regenerate's retention gate forbids
        # dropping prior tests). Salvage the passing tests by removing the failing
        # ones, then RE-VERIFY the remainder green before writing.
        if not is_update:
            pruned = _prune_after_failure(
                content, last_report, project_root, tmp_path, tmp_rel,
                final_path, allow_overwrite, smoke_runner, cancel_event)
            if pruned is not None:
                return pruned

        return VerifiedResult(content, "fail", last_report,
                              preserved_existing=is_update)   # discarded
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _safe_replace(src: str, dst: str, retries: int = 3, delay: float = 0.15) -> bool:
    """`os.replace` with retry on a locked destination (Windows AV/IDE/test-disco
    holds the handle → sharing violation = OSError, not always PermissionError).
    Returns True on success, False if still locked after the retries."""
    for i in range(retries):
        try:
            os.replace(src, dst)
            return True
        except OSError:
            if i == retries - 1:
                return False
            time.sleep(delay)
    return False


def _test_function_ids(source: str) -> set:
    """Qualified ids of every test function in *source*.

    Walks the whole tree (NOT just module level) so tests nested in
    ``class Test…:`` are counted; methods are qualified by class
    (``ClassName.test_x``) so two classes' same-named methods can't mask a drop.
    Nested helper functions inside a test are NOT collected.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    ids: set = set()

    class _V(ast.NodeVisitor):
        def __init__(self):
            self.cls: "str | None" = None

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if node.name.startswith("test"):
                ids.add(f"{self.cls}.{node.name}" if self.cls else node.name)
            # do NOT recurse — don't collect helper funcs nested in a test

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    _V().visit(tree)
    return ids


# ---------------------------------------------------------------------------
# Per-test pruning — salvage the passing tests when a minority fail
# ---------------------------------------------------------------------------
#
# A one-shot generation often yields a mostly-passing suite with a few wrong
# assertions. Rather than discard the whole file, drop just the failing test
# functions and keep the rest (re-verified green before writing).


def _failing_test_ids(pytest_output: str) -> "tuple[set, bool]":
    """Parse the ``-rfE`` summary for failing test ids.

    Returns ``(ids, prunable)``:
      * ``ids`` — qualified ids in ``_test_function_ids`` shape (``Class.method`` or
        ``test_func``), with any ``[param]`` suffix stripped so a parametrized
        failure maps back to its def name.
      * ``prunable`` — False if any FAILED/ERROR line is a whole-file collection
        error (no ``::id``); such a file can't be salvaged by removing functions.
    """
    ids: set = set()
    prunable = True
    for raw in pytest_output.splitlines():
        line = raw.strip()
        if not (line.startswith("FAILED ") or line.startswith("ERROR ")):
            continue
        # "FAILED tests/x.py::Cls::method - reason"  /  "ERROR tests/x.py - reason"
        token = line.split(" ", 1)[1].split(" ")[0]          # tests/x.py::Cls::method
        parts = token.split("::")
        if len(parts) < 2:
            prunable = False                                  # whole-file error
            continue
        leaf = parts[-1].split("[", 1)[0]                     # strip parametrize bracket
        qualified = f"{parts[-2]}.{leaf}" if len(parts) >= 3 else leaf
        ids.add(qualified)
    return ids, prunable


def _failing_files(pytest_output: str) -> set:
    """Forward-slash file paths of every ``FAILED``/``ERROR`` summary line.

    Used to attribute full-suite gate failures to the file that owns them. Takes
    element [0] after splitting on ``::`` so it captures BOTH test-level failures
    (`FAILED tests/x.py::T::m - …`) AND collection-level aborts that have no `::id`
    (`ERROR tests/x.py - ImportError`). Separators normalized so the match works on
    Windows (pytest emits `/`, callers may hold `\\`).
    """
    files: set = set()
    for raw in pytest_output.splitlines():
        line = raw.strip()
        if not (line.startswith("FAILED ") or line.startswith("ERROR ")):
            continue
        token = line.split(" ", 1)[1].split(" ")[0]           # tests/x.py[::…]
        path = token.split("::")[0].replace("\\", "/").strip()
        if path:
            files.add(path)
    return files


def _prune_test_functions(code: str, failing_ids: set) -> "str | None":
    """Remove the named ``test_*`` funcs/methods from *code*; keep everything else.

    Uses the AST purely as a read-only line indexer (never ``ast.unparse`` — that
    would reformat survivors). Spans start at the first decorator's line (so
    ``@pytest.mark.parametrize``/``@pytest.fixture`` go with the function) and end at
    ``end_lineno``. A class whose every test method is dropped is removed whole.
    Spans are deleted high-to-low so earlier removals don't shift later indices.
    Returns the pruned source, or ``None`` if it no longer parses or nothing remains.
    """
    if not failing_ids:
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    def _span(node) -> "tuple[int, int]":
        start = node.lineno
        for dec in getattr(node, "decorator_list", []):
            start = min(start, dec.lineno)
        return start, (node.end_lineno or node.lineno)

    spans: "list[tuple[int, int]]" = []
    _FN = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, _FN) and node.name.startswith("test") \
                and node.name in failing_ids:
            spans.append(_span(node))
        elif isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, _FN)
                       and n.name.startswith("test")]
            drop = [m for m in methods if f"{node.name}.{m.name}" in failing_ids]
            if methods and len(drop) == len(methods):
                spans.append(_span(node))            # whole class — all tests fail
            else:
                spans.extend(_span(m) for m in drop)

    if not spans:
        return code
    lines = code.splitlines(keepends=True)
    for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
        del lines[start - 1:end]                     # 1-based inclusive → 0-based slice
    pruned = "".join(lines)
    try:
        ast.parse(pruned)
    except SyntaxError:
        return None
    return pruned if _test_function_ids(pruned) else None


def _prune_after_failure(content, last_report, project_root, tmp_path, tmp_rel,
                         final_path, allow_overwrite, smoke_runner,
                         cancel_event) -> "VerifiedResult | None":
    """Try to salvage a failing new-file generation by dropping the failing tests.

    Returns a ``"pass"`` ``VerifiedResult`` (with kept/total + a ``[prune-verify]``
    report) when the pruned remainder runs green and clears the survivor floor; else
    ``None`` to let the caller fall through to a normal discard.
    """
    failing_ids, prunable = _failing_test_ids(last_report)
    if not prunable or not failing_ids:
        return None                                   # whole-file error / nothing to cut
    total = len(_test_function_ids(content))
    if not total:
        return None
    pruned = _prune_test_functions(content, failing_ids)
    if not pruned:
        return None
    survivors = len(_test_function_ids(pruned))
    if not survivors or survivors / total < _MIN_SURVIVORS_FRAC:
        log.info("Pruning declined: only %d/%d tests would survive (floor %.0f%%).",
                 survivors, total, _MIN_SURVIVORS_FRAC * 100)
        return None
    if cancel_event and cancel_event.is_set():
        return None
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(pruned)
    passed, out = smoke_runner.run_single_test_file(project_root, tmp_rel)
    if not passed:
        # Isolate this from the original generation failure for debuggability.
        log.warning("Prune-verify re-run did not pass; discarding.\n[prune-verify] %s",
                    (out or "")[-1500:])
        return None
    if os.path.exists(final_path) and not allow_overwrite:
        return None                                   # don't clobber an existing test
    if not _safe_replace(tmp_path, final_path):
        return None                                   # target locked
    dropped = ", ".join(sorted(failing_ids))
    log.info("Pruned %s: kept %d/%d, dropped: %s",
             os.path.basename(final_path), survivors, total, dropped)
    final_rel = os.path.relpath(final_path, project_root).replace(os.sep, "/")
    return VerifiedResult(
        pruned, "pass",
        f"[prune-verify] kept {survivors}/{total} tests; dropped: {dropped}",
        kept=survivors, total=total,
        written_path=final_rel, is_tk=("pytest.mark.tk" in pruned))


def reverify_against_suite(project_root: str, test_relpaths: list) -> dict:
    """Re-verify just-written test files against the FULL ``-m "not tk"`` gate and
    roll back any that fail it. Closes the isolation gap (a test can pass alone yet
    fail in the real suite). Returns ``{test_relpath: "ok" | "rolled_back"}`` and
    guarantees the gate is green afterwards (assuming committed HEAD was green).

    Strategy: run gate → if green, done. If the output has no parseable failures
    (collection collapse), roll back the whole batch. Else roll back the written
    files that OWN failures and re-run; if still red, roll back the rest (safety),
    logging a dirty-workspace diagnostic when the residual failures are all outside
    the batch. All rollbacks are logged to the persistent manager log.
    """
    from helpers import smoke_runner
    verdict = {p: "ok" for p in test_relpaths}
    if not test_relpaths:
        return verdict
    norm_to_orig = {p.replace("\\", "/"): p for p in test_relpaths}
    batch_norm = set(norm_to_orig)

    def _rollback(orig: str) -> None:
        try:
            os.remove(os.path.join(project_root, orig))
        except OSError:
            pass
        verdict[orig] = "rolled_back"

    ok, output = smoke_runner.run_gate(project_root)
    if ok:
        return verdict

    failing = _failing_files(output)
    if not failing:                                     # collection collapse → nuclear
        log.warning("Full-suite gate failed with no parseable failures (collection "
                    "collapse?); rolling back all %d generated file(s).",
                    len(test_relpaths))
        for p in test_relpaths:
            _rollback(p)
        return verdict

    owners = [norm_to_orig[n] for n in failing if n in norm_to_orig]
    for p in owners:
        log.warning("Generated test failed the full -m 'not tk' gate — rolling back %s.", p)
        _rollback(p)

    if owners:
        ok, output = smoke_runner.run_gate(project_root)
        if ok:
            return verdict
        failing = _failing_files(output)

    # Still red: roll back the rest (never leave the gate red). Diagnose whether the
    # residual failures are even ours.
    if failing and not (failing & batch_norm):
        log.warning("Gate still red after rolling back failing generated files, but the "
                    "residual failures are OUTSIDE the generated batch: %s — likely a "
                    "pre-existing/dirty workspace, not the generator.", sorted(failing))
    for p in [p for p in test_relpaths if verdict[p] == "ok"]:
        log.warning("Full-batch rollback (gate still red) — rolling back %s.", p)
        _rollback(p)
    return verdict


# ---------------------------------------------------------------------------
# Deterministic import auto-repair
# ---------------------------------------------------------------------------
#
# A local 14B model reliably DROPS imports it clearly needs — it emits
# `pytestmark = pytest.mark.tk` without `import pytest`, references the class
# under test without importing it, uses `subprocess`/`Dict` unimported. Those
# are NameErrors that only surface at the 20s pytest gate (and a slow LLM repair
# pass). We catch them statically (pyflakes, in-process) and inject the missing
# imports deterministically BEFORE running — faster, and backend-agnostic.

# Names we can resolve to a known import line. Grouped by emission style.
_TYPING_NAMES = {
    "List", "Dict", "Set", "Tuple", "Optional", "Union", "Any", "Callable",
    "Type", "cast", "Iterable", "Iterator", "Sequence", "Mapping",
}
_MOCK_NAMES = {"MagicMock", "patch", "mock", "Mock", "call", "ANY", "PropertyMock"}
_PLAIN_MODULES = {
    "pytest", "subprocess", "os", "sys", "re", "json", "threading", "time",
    "tempfile", "shutil", "uuid", "pathlib", "io", "textwrap", "itertools",
}
# name -> (module, symbol) for `from module import symbol`.
_FROM_IMPORTS = {
    "Path": ("pathlib", "Path"),
    "SimpleNamespace": ("types", "SimpleNamespace"),
    "dataclass": ("dataclasses", "dataclass"),
    "CREATE_NO_WINDOW": ("constants", "CREATE_NO_WINDOW"),  # G-WIN rule
}


def _undefined_names(code: str) -> "list[str]":
    """Names used but never bound, per pyflakes (deduped, source order).

    Returns ``[]`` if the code doesn't parse (the syntax gate owns that) or if
    pyflakes can't be imported (packaged build) — autofix then no-ops and the
    pytest gate catches anything real.
    """
    try:
        from pyflakes.checker import Checker
    except ImportError:
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: "list[str]" = []
    for m in Checker(tree, filename="t.py").messages:
        if type(m).__name__ == "UndefinedName":
            name = m.message_args[0]
            if name not in out:
                out.append(name)
    return out


def _module_dotted(source_path: str, project_root: str) -> str:
    """Dotted import path of the source module, as tests import it.

    `src/` is the import root (conftest puts it on sys.path), so
    ``…/src/controllers/projects_tab.py`` → ``controllers.projects_tab``.
    """
    src_dir = os.path.join(project_root, "src")
    base = src_dir if os.path.isdir(src_dir) else project_root
    try:
        rel = os.path.relpath(source_path, base)
    except ValueError:
        rel = os.path.basename(source_path)
    return os.path.splitext(rel)[0].replace(os.sep, ".").replace("/", ".")


def _module_symbols(source_path: str) -> "set[str]":
    """Top-level class/function names defined in the source file."""
    try:
        with open(source_path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return set()
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _bound_names(tree: ast.Module) -> "set[str]":
    """Names already imported/bound at module level (so we never duplicate)."""
    bound: "set[str]" = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
    return bound


def _emit_from(module: str, names: "list[str]", nl: str) -> str:
    """One ``from module import …`` line; parenthesised + trailing comma if >3."""
    names = sorted(set(names))
    if len(names) > 3:
        inner = "".join(f"    {n},{nl}" for n in names)
        return f"from {module} import ({nl}{inner})"
    return f"from {module} import " + ", ".join(names)


def _autofix_imports(code: str, source_path: str, project_root: str) -> str:
    """Inject imports for names pyflakes flags as undefined.

    Local-first (the source file's own symbols win over the stdlib map, so a
    source that defines its own ``Path``/``Any`` isn't hijacked), then a
    module-reference fallback for dotted access, then the generic known-imports
    map. Uses the AST purely as a read-only indexer to find the line AFTER the
    docstring/__future__ block, then does a string splice (never ``ast.unparse``,
    which would strip the model's formatting). Returns the original code on any
    parse trouble — never hands back worse code.
    """
    undefined = _undefined_names(code)
    if not undefined:
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    already = _bound_names(tree)
    undefined = [n for n in undefined if n not in already]
    if not undefined:
        return code

    dotted = _module_dotted(source_path, project_root)
    top = dotted.split(".")[0]
    stem = dotted.split(".")[-1]
    parent = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    local_syms = _module_symbols(source_path)

    local: "list[str]" = []          # symbols of the module under test
    extra: "list[str]" = []          # module-reference fallback lines
    typing_n: "list[str]" = []
    mock_n: "list[str]" = []
    plain: "list[str]" = []
    froms: "dict[str, list[str]]" = {}

    for name in undefined:
        if name in local_syms:                          # 1. local-first
            local.append(name)
        elif name == top:                               # 2. module-ref fallback
            line = f"import {dotted}"
            if line not in extra:
                extra.append(line)
        elif name == stem and parent:
            line = f"from {parent} import {stem}"
            if line not in extra:
                extra.append(line)
        elif name == stem:                              # top-level module, no parent
            line = f"import {stem}"
            if line not in extra:
                extra.append(line)
        elif name in _TYPING_NAMES:                     # 3. known-imports map
            typing_n.append(name)
        elif name in _MOCK_NAMES:
            mock_n.append(name)
        elif name in _PLAIN_MODULES:
            plain.append(f"import {name}")
        elif name in _FROM_IMPORTS:
            mod, sym = _FROM_IMPORTS[name]
            froms.setdefault(mod, []).append(sym)
        # else: leave for the pytest run → runtime-repair loop

    nl = "\r\n" if "\r\n" in code else "\n"
    blocks: "list[str]" = []
    if local:
        blocks.append(_emit_from(dotted, local, nl))
    blocks.extend(extra)
    if typing_n:
        blocks.append(_emit_from("typing", typing_n, nl))
    if mock_n:
        blocks.append(_emit_from("unittest.mock", mock_n, nl))
    blocks.extend(sorted(set(plain)))
    for mod in sorted(froms):
        blocks.append(_emit_from(mod, froms[mod], nl))
    if not blocks:
        return code

    # Read-only AST indexing: insert AFTER the module docstring and any leading
    # __future__ import. end_lineno is 1-based; splitlines() is 0-based, so the
    # list insert index equals the 1-based line number (old line N+1 is index N).
    idx = 0
    if tree.body:
        first = tree.body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(getattr(first, "value", None), ast.Constant)
                and isinstance(first.value.value, str)):
            idx = first.end_lineno or 0
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                idx = max(idx, node.end_lineno or 0)

    block_text = nl.join(blocks) + nl
    lines = code.splitlines(keepends=True)
    candidate = "".join(lines[:idx]) + block_text + "".join(lines[idx:])

    try:
        ast.parse(candidate)            # safety: never return un-parseable code
    except SyntaxError:
        return code
    return candidate


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


# Generation knobs (named so the num_ctx guard and the clamp can't drift apart).
_MAX_GEN_TOKENS = 4000       # max_tokens handed to the model for one test file
                             # (a ~200-line test exceeds 2500 → mid-file cutoff)
_LOCAL_CTX_CEILING = 16384   # largest num_ctx we'll request from a local model
_CTX_SLACK = 512             # headroom above prompt+output before the ceiling
_CHARS_PER_TOKEN = 2.5       # conservative est (code is token-denser than prose)
_LOCAL_TIMEOUT = 300         # per-read socket timeout for slow local models (s)
_CLOUD_TIMEOUT = 120         # cloud APIs are fast — keep the short timeout (s)
_CLI_GEN_TIMEOUT = 240       # `claude --print` one-shot gen for a whole test file (s)
_MIN_SURVIVORS_FRAC = 0.5    # keep a pruned-partial file only if ≥this frac survives


def _dispatch(effective_backend: str, cfg, system_prompt: str,
              user_prompt: str, project_root: str,
              on_token=None) -> "tuple[str | None, str | None]":
    """Route one prompt to the resolved backend. Shared by generate + repair.

    Returns ``(content, error)``: ``(text, None)`` on success or
    ``(None, why)`` on failure, so callers can surface the REAL cause (timeout,
    too-large prompt, CLI auth) instead of a generic "check your settings".
    ``on_token`` only reaches the streaming LLM path (the CLI is not streamed).
    """
    if effective_backend == "claude_cli":
        return _dispatch_claude_cli(cfg, system_prompt, user_prompt, project_root)
    return _dispatch_llm(cfg, system_prompt, user_prompt, on_token=on_token)

def _resolve_backend(backend: str, cfg) -> "str | None":
    """Return the concrete backend to use, or None if nothing is configured."""
    if backend == "auto":
        if getattr(cfg, "claude_cli_exe", ""):
            return "claude_cli"
        # Fall back to LLM if any provider is configured
        llm_cfg = cfg.raw.get("commit_message_llm", {})
        if llm_cfg.get("provider") or llm_cfg.get("api_key"):
            return "llm"
        return None
    if backend == "claude_cli":
        return "claude_cli" if getattr(cfg, "claude_cli_exe", "") else None
    if backend == "llm":
        llm_cfg = cfg.raw.get("commit_message_llm", {})
        if llm_cfg.get("provider") or llm_cfg.get("api_key"):
            return "llm"
        return None
    return None


def _dispatch_claude_cli(cfg, system_prompt: str, user_prompt: str,
                          project_root: str) -> "tuple[str | None, str | None]":
    """Call Claude CLI in --print mode with the combined prompt.

    cwd is a NEUTRAL dir (~), NOT project_root: running `claude --print` inside
    the repo loads CLAUDE.md/AGENTS.md and pushes Claude into agentic mode, where
    a "generate a test file" task makes it try to USE its Write tool and ask for
    permission (which it returns as prose) instead of printing the code. The
    source is already inlined in the prompt, so project context isn't needed.
    (Same rationale as commit-message generation — see call_claude_cli_print.)

    Returns ``(content, error)``. On failure the error is the SPECIFIC cause from
    ``get_last_cli_error`` (missing binary vs expired auth vs timeout), not a guess.

    The system prompt is folded into the STDIN prompt (not passed as
    ``--append-system-prompt``): on Windows the CLI is a ``claude.cmd`` shim run via
    cmd.exe, whose ~8191-char command-line limit a large system prompt (conventions
    + example) blows ("The command line is too long"). stdin has no such limit, and
    for a one-shot generate the leading-preamble placement is equivalent.
    """
    from helpers.claude_cli import call_claude_cli_print, get_last_cli_error
    combined = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
    content = call_claude_cli_print(
        claude_exe=cfg.claude_cli_exe,
        prompt=combined,
        system_prompt="",                  # via stdin — keeps the command line tiny
        timeout=_CLI_GEN_TIMEOUT,          # large controllers need >90s one-shot
        model=getattr(cfg, "claude_cli_model", "") or "",
        cwd=os.path.expanduser("~"),
    )
    if content is None:
        cause = get_last_cli_error()
        if cause:
            return None, f"Claude CLI failed: {cause}"
        return None, ("Claude CLI returned no output — verify the CLI path and "
                      "that you're logged in")
    return content, None


def _dispatch_llm(cfg, system_prompt: str, user_prompt: str,
                  on_token=None) -> "tuple[str | None, str | None]":
    """Call the configured LLM provider with the combined prompt.

    Returns ``(content, error)``. LOCAL providers (Ollama / openai_compatible)
    get the long ``_LOCAL_TIMEOUT`` per-read timeout, STREAM (so the socket stays
    alive per-token), and a prompt-sized ``num_ctx`` injected so the model isn't
    capped at Ollama's 4096 default. Cloud providers (anthropic / openai) keep the
    short timeout, no ``num_ctx``, and no size ceiling. The streamed text is still
    returned whole, so the contract is unchanged.
    """
    from helpers.llm import _call_llm, get_last_llm_error

    # Shallow copy — NEVER mutate the live shared config dict: a leaked num_ctx
    # would pollute commit-message generation and may be persisted on save.
    llm_cfg = dict(cfg.raw.get("commit_message_llm", {}))
    provider = (llm_cfg.get("provider") or "anthropic").lower()
    is_local = provider in ("ollama", "openai_compatible")
    timeout = _LOCAL_TIMEOUT if is_local else _CLOUD_TIMEOUT

    if is_local and llm_cfg.get("num_ctx") is None:
        # num_ctx is the COMBINED input+output window, so reserve the full
        # _MAX_GEN_TOKENS response or generation truncates mid-test.
        est = int((len(system_prompt) + len(user_prompt)) / _CHARS_PER_TOKEN)
        needed = est + _MAX_GEN_TOKENS + _CTX_SLACK
        if needed > _LOCAL_CTX_CEILING:
            # Clamping here would silently starve the output → a guaranteed
            # truncated test after a wasted multi-minute prefill. Bail BEFORE
            # the call so no prefill is burned.
            return None, (
                f"source too large for local generation (needs ~{needed} ctx "
                f"> {_LOCAL_CTX_CEILING}); use the Claude CLI backend for this file"
            )
        # Round UP to the nearest 1024 for a stable llama.cpp KV-cache allocation
        # across sequential files (avoids VRAM re-fragmentation).
        num_ctx = min(_LOCAL_CTX_CEILING, max(4096, -(-needed // 1024) * 1024))
        llm_cfg["num_ctx"] = num_ctx

    content = _call_llm(
        cfg=llm_cfg,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=_MAX_GEN_TOKENS,
        timeout=timeout,
        on_token=on_token,
    )
    if content is None:
        # get_last_llm_error() is set by _call_llm on THIS thread — already a
        # dynamic message (e.g. "Timed out after 300s …"); no hardcoded literal.
        return None, get_last_llm_error()
    return content, None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_UPDATE_DIRECTIVE_TEMPLATE = """\
EXISTING TEST FILE DETECTED — this file already has tests. Output the ENTIRE
updated test file: RETAIN every existing test verbatim (same function/method
names) and ADD coverage for the changed code. NEVER delete or rename a prior
test. Current contents:
```python
{existing}
```
"""

_SUBPROCESS_MOCK_RE = re.compile(
    r"(monkeypatch\.setattr|mock\.patch|mocker\.patch|patch\().*subprocess")


def _build_prompts(
    source_path: str,
    project_root: str,
    max_source_chars: int,
    template: "str | None" = None,
    existing_test: "str | None" = None,
) -> "tuple[str, str]":
    """Return ``(system_prompt, user_prompt)``."""
    with open(source_path, encoding="utf-8", errors="replace") as fh:
        source_code = fh.read(max_source_chars)
    if len(source_code) == max_source_chars:
        source_code += "\n# ... [truncated]"

    # Show the model a template-matched example so it copies the right pattern.
    example_test = _find_example_test(project_root, template)

    try:
        rel_path = os.path.relpath(source_path, project_root).replace("\\", "/")
    except ValueError:
        rel_path = os.path.basename(source_path)

    base = os.path.splitext(os.path.basename(source_path))[0]
    test_filename = f"test_{base}.py"

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(example_test=example_test)
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        rel_path=rel_path,
        source_code=source_code,
        test_filename=test_filename,
    )
    if existing_test:
        user_prompt += "\n\n" + _UPDATE_DIRECTIVE_TEMPLATE.format(existing=existing_test)
    return system_prompt, user_prompt


def _example_matches(body: str, template: str) -> bool:
    """Does *body* demonstrate the pattern the *template* needs?"""
    if template == "dialog_tk":
        return "pytest.mark.tk" in body
    if template == "subprocess_helper":
        return bool(_SUBPROCESS_MOCK_RE.search(body))
    if template == "pure_helper":
        return "pytest.mark.tk" not in body and not _SUBPROCESS_MOCK_RE.search(body)
    return True


def _find_example_test(project_root: str, template: "str | None" = None) -> str:
    """Return a small example test that MATCHES *template* (so the model copies the
    right mocking pattern), else a template-specific fallback.

    Picks the smallest on-disk ``tests/test_*.py`` (≤ 120 lines) matching the
    template's shape. If none matches a SPECIFIC template, returns the matching
    hardcoded fallback (never a pure example for a Tk/subprocess target — that's
    what dooms a fresh repo's first such test to hang). pure_helper/blank fall
    back to the smallest overall, then the generic example.
    """
    want = template if template in ("subprocess_helper", "dialog_tk", "pure_helper") else None
    best_match: "str | None" = None
    best_match_lines = 10 ** 9
    smallest: "str | None" = None
    smallest_lines = 10 ** 9

    tests_dir = os.path.join(project_root, "tests")
    if os.path.isdir(tests_dir):
        real_root = os.path.realpath(project_root)
        try:
            names = os.listdir(tests_dir)
        except OSError:
            names = []
        for fname in names:
            if not (fname.startswith("test_") and fname.endswith(".py")):
                continue
            fpath = os.path.join(tests_dir, fname)
            try:
                if not os.path.realpath(fpath).startswith(real_root):
                    continue
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            n = body.count("\n") + 1
            if n > 120:
                continue
            if n < smallest_lines:
                smallest_lines, smallest = n, body
            if want and n < best_match_lines and _example_matches(body, want):
                best_match_lines, best_match = n, body

    if best_match is not None:
        return best_match
    if want == "subprocess_helper":
        return _FALLBACK_EXAMPLE_SUBPROCESS
    if want == "dialog_tk":
        return _FALLBACK_EXAMPLE_TK
    return smallest if smallest is not None else _FALLBACK_EXAMPLE


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

_CODE_START_PREFIXES = ('"""', "'''", "import ", "from ", "#", "@", "def ", "class ")


def _extract_code(text: str) -> str:
    """Pull the Python source out of an LLM reply.

    Handles three shapes:
      1. A fenced ``` ```python … ``` ``` (or bare ``` ``` ```) block anywhere →
         return the first block's body.
      2. A prose preamble followed by code (the failure mode that wrote 8 junk
         files) → drop leading lines until the first code-ish line.
      3. Already-clean code → returned unchanged.

    Returns the best-effort code; :func:`_looks_like_python` is the real gate.
    """
    if not text:
        return ""
    fence = re.search(r"```[A-Za-z0-9_]*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip("\n")
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(_CODE_START_PREFIXES):
            return "\n".join(lines[i:]).strip("\n")
    return text.strip()


def _looks_like_python(text: str) -> "str | None":
    """Return None if *text* parses as a Python module, else the error message."""
    try:
        ast.parse(text)
        return None
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
