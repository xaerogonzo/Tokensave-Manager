"""DoctorController — tokensave doctor command group for the Projects tab.

Extracted from ProjectsTabController (Round 5). Roadmap-2 added the monolith
audit pass (file / method / class / complexity caps) — caps and semantics are
canonical in BASIC_INSTRUCTIONS.md (Rule A).

Dependency contract:
  • tab          — the Projects tk.Frame (after() scheduling + winfo_toplevel())
  • cfg          — read-only ManagerConfig (.tokensave_exe + .raw)
  • on_log       — thread-safe log callback  (msg: str, colour: str = "")
  • on_set_running  — (running: bool, label: str) -> None
  • on_set_proc  — (proc_or_none) -> None  updates parent's current_proc so
                   App._auto_refresh can detect when the controller is busy
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import threading
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from constants import C, CREATE_NO_WINDOW, _ANSI
from helpers.runtime import log

import time

if TYPE_CHECKING:
    from state import ManagerConfig


# ── Monolith-audit constants (canonical thresholds — see BASIC_INSTRUCTIONS Rule A) ──

_CAP_FILE_LINES         = 800
_CAP_METHOD_LINES       = 100
_CAP_CLASS_METHODS      = 40
_CAP_COMPLEXITY         = 10
_CAP_LAYOUT_COMPLEXITY  = 3

# Layout-method name patterns — qualify for the 100-line carve-out IF cyclomatic
# complexity is also ≤ _CAP_LAYOUT_COMPLEXITY. Naming alone never grants immunity.
_LAYOUT_NAME_RE = re.compile(r"^_?(build|populate|render|layout)(_.*)?$")

# Top-of-file exemption: `# anti-monolith: exempt — <non-empty reason>` appearing
# in the file's comment block BEFORE the first class/def (robust to long docstrings,
# grouped imports, formatter reordering, __future__ blocks).
_EXEMPT_RE = re.compile(r"#\s*anti-monolith:\s*exempt\s*[—-]\s*(\S.*\S)")


class DoctorController:
    """Runs `tokensave doctor`, parses stale entries, and offers to purge them."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_set_running: Callable[[bool, str], None],
        on_set_proc: Callable[[object], None],
    ) -> None:
        self._tab           = tab
        self._cfg           = cfg
        self._on_log        = on_log
        self._on_set_running = on_set_running
        self._on_set_proc   = on_set_proc

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Public entry point ────────────────────────────────────────────────────

    def cmd_doctor(self, path: str) -> None:
        """Run doctor + offer purge. Call after require_tokensave guard passes."""
        self._run_with_purge_offer(path)

    # ── Worker helpers ────────────────────────────────────────────────────────

    def _run_with_purge_offer(self, path: str) -> None:
        label = os.path.basename(path)

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            log.info("RUN  tokensave doctor")
            output_lines: list[str] = []
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [self._cfg.tokensave_exe, "doctor"],
                    cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._on_set_proc(proc)
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._on_log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                else:
                    self._on_log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                stale = self._extract_stale_paths(output_lines)
                if stale and proc.returncode == 0:
                    self._tab.after(0, self._offer_purge, path, stale)
                # Roadmap-2: monolith audit always runs after doctor finishes
                self._run_monolith_audit(path)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_doctor")
            finally:
                self._on_set_proc(None)
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-worker").start()

    def _run_purge(self, path: str) -> None:
        """Re-run `tokensave doctor` with `y` piped to confirm the purge prompt."""
        label = "doctor (purge)"

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            captured: list[str] = []
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [self._cfg.tokensave_exe, "doctor"],
                    cwd=path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._on_set_proc(proc)
                try:
                    proc.stdin.write("y\ny\ny\ny\ny\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                except (OSError, BrokenPipeError):
                    pass
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    captured.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                self._on_log(
                    "Done." if proc.returncode == 0
                    else f"Exited with code {proc.returncode}",
                    C["green"] if proc.returncode == 0 else C["red"])
                still_stale = self._extract_stale_paths(captured)
                if still_stale:
                    self._on_log(
                        f"  ⚠ Purge didn't take — tokensave still "
                        f"reports {len(still_stale)} stale entr"
                        f"{'y' if len(still_stale) == 1 else 'ies'}. "
                        "tokensave doctor needs a real terminal "
                        "(piped stdin doesn't trigger the prompt).",
                        C["peach"])
                    self._tab.after(0, self._offer_in_cmd, path, len(still_stale))
                else:
                    self._on_log("  ✓ Stale entries purged.", C["green"])
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in doctor purge")
            finally:
                self._on_set_proc(None)
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-purge").start()

    # ── Main-thread dialogs ───────────────────────────────────────────────────

    def _offer_purge(self, path: str, stale_paths: list[str]) -> None:
        n = len(stale_paths)
        bullets = "\n".join(f"  • {p}" for p in stale_paths)
        msg = (
            f"tokensave doctor found {n} stale project entr"
            f"{'y' if n == 1 else 'ies'} in the global DB.\n\n"
            f"{bullets}\n\n"
            "These projects were registered but their `.tokensave/` "
            "folders are gone — most likely deleted folders.\n\n"
            "Purge them now?  The manager will re-run `tokensave "
            "doctor` with `y` piped to confirm the interactive "
            "purge prompt."
        )
        if not messagebox.askyesno("Purge stale tokensave projects?", msg,
                                   parent=self._root):
            self._on_log("  (purge skipped — stale entries left in place)", C["overlay0"])
            return
        self._run_purge(path)

    def _offer_in_cmd(self, path: str, n_stale: int) -> None:
        plural = "entry" if n_stale == 1 else "entries"
        if not messagebox.askyesno(
                "Open Doctor in a new terminal?",
                f"The piped-stdin purge didn't work — tokensave needs "
                f"a real terminal for its interactive 'y/n' prompt.\n\n"
                f"Open a new cmd.exe window with `tokensave doctor` "
                f"running there?  You'll see the {n_stale} stale "
                f"{plural} listed and tokensave will ask you to "
                f"confirm — type 'y' and press Enter to purge.\n\n"
                f"The window stays open after, so you can close it "
                f"yourself when done.",
                parent=self._root):
            self._on_log(
                "  (terminal-purge skipped — stale entries still in DB)",
                C["overlay0"])
            return
        try:
            cmd_line = f'cmd.exe /k ""{self._cfg.tokensave_exe}" doctor"'
            subprocess.Popen(
                cmd_line,
                cwd=path,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self._on_log(
                "  Opened cmd.exe — type 'y' at the prompt to purge, "
                "then close the window.",
                C["sky"])
        except OSError as e:
            self._on_log(f"  ✗ Could not launch cmd.exe: {e}", C["red"])

    # ── Monolith audit ────────────────────────────────────────────────────────

    def _run_monolith_audit(self, project_path: str) -> None:
        """Walk *.py under project_path and log cap violations.

        Triggered at the end of cmd_doctor. Caps + semantics are canonical
        in BASIC_INSTRUCTIONS.md Rule A.
        """
        self._on_log("═══ Code-health caps ═══", C["mauve"])

        skip = {p.replace("\\", "/")
                for p in (self._cfg.raw.get("doctor_skip_monolith_paths") or [])}

        violations, exempt_notes, files_scanned = _audit_project_tree(
            project_path, skip)

        self._log_audit_results(violations, exempt_notes, files_scanned)

    def _log_audit_results(
        self,
        violations: list[str],
        exempt_notes: list[str],
        files_scanned: int,
    ) -> None:
        if not violations:
            plural = "s" if files_scanned != 1 else ""
            self._on_log(
                f"  OK — {files_scanned} file{plural} checked, no cap violations.",
                C["green"])
        else:
            plural = "s" if len(violations) != 1 else ""
            self._on_log(
                f"  Found {len(violations)} violation{plural} "
                f"across {files_scanned} files:",
                C["peach"])
            for line in violations:
                self._on_log(line, C["peach"])

        for note in exempt_notes:
            self._on_log(note, C["overlay0"])

    # ── Output parser ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_stale_paths(output_lines: list[str]) -> list[str]:
        """Parse tokensave doctor's stdout for the stale-entries section."""
        bullet_re = re.compile(r"^\s*[•*\-]\s+(.+?)\s*$")
        in_block = False
        paths: list[str] = []
        for line in output_lines:
            if "stale project" in line and "global DB" in line:
                in_block = True
                continue
            if not in_block:
                continue
            if "Re-run" in line and "tokensave doctor" in line:
                break
            m = bullet_re.match(line)
            if m:
                paths.append(m.group(1).strip())
            elif paths and not line.startswith((" ", "\t")):
                break
        return paths


# ── Module-level audit helpers (AST-based; canonical semantics — Rule A) ──

def _parse_exempt_header(source: str) -> str | None:
    """Return the exemption rationale if present in the file's pre-class/def
    comment block. Returns None if no exemption (or if rationale is empty)."""
    for raw_line in source.splitlines():
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            m = _EXEMPT_RE.search(stripped)
            if m:
                reason = m.group(1).strip()
                return reason if reason else None
            continue
        # Hit a non-comment, non-blank line — exemption can only appear before
        # the first class/def. Anything else (imports, docstrings, decorators,
        # module-level statements) ends the search window.
        return None
    return None


def _method_span_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Method body span — decorators do NOT count toward length.

    `node.lineno` points at the `def` line (after any decorators); `body[-1]`
    has the final statement's end_lineno. This matches the Rule A semantics.
    """
    if not node.body:
        return 1
    end = getattr(node.body[-1], "end_lineno", None) or node.lineno
    return end - node.lineno + 1


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Cyclomatic complexity per Rule A.

    Each if/elif/for/while/except adds 1; each and/or short-circuit adds 1;
    each match arm adds 1; comprehension `if` clauses add 1 each.
    Nested function/class definitions are NOT recursed into — they have
    their own scores.
    """
    score = 1  # base path

    def _visit(n: ast.AST, is_root: bool) -> None:
        nonlocal score
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not is_root:
            return  # nested scope — not our complexity
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            score += 1
        elif isinstance(n, ast.BoolOp):
            score += max(0, len(n.values) - 1)
        elif isinstance(n, ast.comprehension):
            score += len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            score += 1
        for child in ast.iter_child_nodes(n):
            _visit(child, is_root=False)

    _visit(node, is_root=True)
    return score


def _is_layout_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Layout-method carve-out: name pattern + complexity ≤ 3.

    Naming alone never grants immunity (Rule A).
    """
    if not _LAYOUT_NAME_RE.match(node.name):
        return False
    return _cyclomatic_complexity(node) <= _CAP_LAYOUT_COMPLEXITY


_AUDIT_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".tokensave", ".codegraph",
    ".venv", "venv", "node_modules", "dist", "build",
    ".build", ".onefile-build",
})


def _audit_project_tree(
    project_path: str,
    skip_rel_paths: set[str],
) -> tuple[list[str], list[str], int]:
    """Walk *.py files under project_path; return (violations, exempts, count)."""
    violations: list[str] = []
    exempt_notes: list[str] = []
    files_scanned = 0

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _AUDIT_SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, project_path).replace("\\", "/")
            if rel in skip_rel_paths:
                continue
            files_scanned += 1
            result = _audit_python_file(full)
            if result is None:
                continue
            if result["exempt"]:
                exempt_notes.append(f"  (exempt: {rel} — {result['exempt_reason']})")
            else:
                violations.extend(f"  {rel}: {v}" for v in result["violations"])

    return violations, exempt_notes, files_scanned


def _audit_method_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Return violation strings for a single method/function node."""
    out: list[str] = []
    span = _method_span_lines(node)
    if span > _CAP_METHOD_LINES and not _is_layout_method(node):
        out.append(f"{node.name}() is {span} lines (cap {_CAP_METHOD_LINES})")
    cc = _cyclomatic_complexity(node)
    if cc > _CAP_COMPLEXITY:
        out.append(f"{node.name}() complexity {cc} (cap {_CAP_COMPLEXITY})")
    return out


def _audit_class_node(node: ast.ClassDef) -> list[str]:
    """Return violation strings for a class node."""
    method_count = sum(
        1 for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    if method_count > _CAP_CLASS_METHODS:
        return [f"class {node.name} has {method_count} direct methods "
                f"(cap {_CAP_CLASS_METHODS})"]
    return []


def _audit_python_file(path: str) -> dict | None:
    """Audit a single .py file. Returns dict or None on parse failure.

    Returned dict:
        {"exempt": bool, "exempt_reason": str | None, "violations": list[str]}
    """
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return None

    reason = _parse_exempt_header(source)
    if reason:
        return {"exempt": True, "exempt_reason": reason, "violations": []}

    violations: list[str] = []
    line_count = source.count("\n") + 1
    if line_count > _CAP_FILE_LINES:
        violations.append(f"file is {line_count} lines (cap {_CAP_FILE_LINES})")

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"exempt": False, "exempt_reason": None, "violations": violations}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_audit_method_node(node))
        elif isinstance(node, ast.ClassDef):
            violations.extend(_audit_class_node(node))

    return {"exempt": False, "exempt_reason": None, "violations": violations}


def count_methods_in_class(file_path: str, class_name: str) -> int:
    """Return the AST-direct-children method count for a class.

    Public helper exported for verification scripts (e.g. god-class regression
    checks after extractions). Matches the same semantics the Doctor audit
    uses — never use `dir()` for this; it includes inherited attributes,
    descriptor magic, and runtime additions.
    """
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sum(
                1 for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    raise LookupError(f"class {class_name!r} not found in {file_path}")
