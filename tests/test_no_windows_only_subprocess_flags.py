"""tests/test_no_windows_only_subprocess_flags.py — portable creationflags.

`subprocess.CREATE_NO_WINDOW` and `subprocess.CREATE_NEW_CONSOLE` do not exist
on Linux — referencing either raises AttributeError, and passing a non-zero
`creationflags` there raises ValueError. The manager is a Windows Tk app, but
its test suite runs on ubuntu CI, so any module reaching for those attributes
directly breaks the build the moment a test touches that code path.

This has now bitten the project three times:

  * `CREATE_NO_WINDOW` non-zero off-Windows (ValueError) — v4.15;
  * `pystray` imported at module scope, aborting collection — v4.15, which is
    what `test_no_thirdparty_module_imports.py` now guards;
  * `subprocess.CREATE_NEW_CONSOLE` in the purge hand-off — which kept
    master's test-gate red for five consecutive commits before anyone opened a
    PR and saw it.

`constants.py` defines both flags guarded by `sys.platform`, evaluating to 0
off-Windows. This test enforces that everything in `src/` goes through those
rather than reaching into `subprocess` directly.

Dual-mode, like the third-party-import guard: runs under pytest, and standalone
via `python tests/test_no_windows_only_subprocess_flags.py` so CI can run it in
an import-free job.
"""
from __future__ import annotations

import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"

# Attributes that simply do not exist on a non-Windows Python.
_WINDOWS_ONLY = ("CREATE_NO_WINDOW", "CREATE_NEW_CONSOLE", "STARTUPINFO",
                 "STARTF_USESHOWWINDOW", "DETACHED_PROCESS",
                 "CREATE_NEW_PROCESS_GROUP")

_PATTERN = re.compile(
    r"\bsubprocess\.(" + "|".join(_WINDOWS_ONLY) + r")\b")

# constants.py is where the platform guard lives, so it is allowed to name
# them — and does, in a comment explaining why.
_ALLOWED = {"constants.py"}


def _offenders() -> "list[str]":
    out = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name in _ALLOWED or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue          # comments and docstring bullets may name it
            m = _PATTERN.search(line)
            if m:
                rel = path.relative_to(_ROOT).as_posix()
                out.append(f"{rel}:{i}: subprocess.{m.group(1)}")
    return out


def test_src_uses_the_platform_guarded_constants():
    found = _offenders()
    assert found == [], (
        "these reach into subprocess for a Windows-only attribute, which "
        "raises AttributeError on the Linux CI runner — import the guarded "
        "constant from constants.py instead:\n  " + "\n  ".join(found))


def test_the_guarded_constants_are_zero_off_windows():
    """The other half of the rule: a non-zero creationflags raises on Linux."""
    src = (_SRC / "constants.py").read_text(encoding="utf-8")
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_CONSOLE"):
        assert re.search(
            rf"^{name}\s*=.*if sys\.platform == .win32. else 0", src, re.M), \
            f"{name} in constants.py is not guarded by sys.platform"


def test_the_windows_values_match_the_stdlib():
    """A guarded constant that guessed the wrong value would be worse than
    the AttributeError, because it would fail silently at runtime."""
    if sys.platform != "win32":
        return                     # nothing to compare against
    import subprocess
    sys.path.insert(0, str(_SRC))
    import constants
    assert constants.CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
    assert constants.CREATE_NEW_CONSOLE == subprocess.CREATE_NEW_CONSOLE


if __name__ == "__main__":
    bad = _offenders()
    if bad:
        print("FAIL: Windows-only subprocess attributes referenced in src/:")
        for line in bad:
            print("  " + line)
        raise SystemExit(1)
    print("OK: no direct Windows-only subprocess flags in src/.")
