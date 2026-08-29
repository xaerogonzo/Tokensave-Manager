"""tests/test_no_import_time_path_resolution.py — G-L pre-flight test.

Walks every module under ``src/`` with sentinel ``HOME`` / ``USERPROFILE`` /
``APPDATA`` / ``LOCALAPPDATA`` environment variables set to obvious "do
not use" paths. After importing each module, scans its public attributes
for strings containing the sentinels.

If any module-level variable captured a sentinel during import, it means
that module evaluated a user-path AT IMPORT TIME, which would silently
defeat the ``fake_home`` fixture in ``tests/conftest.py`` (G-F + G-J).

This is the regression-prevention companion to the one-time refactor of
``src/helpers/claude_tasks.py`` (S-1). The refactor cleared the existing
violation; this test makes sure new violations can't slip in.

If this test fails, the failure message lists the offending module +
variable + the captured sentinel-bearing value. Fix the offending module
by moving the path resolution into a function body so it re-evaluates
each call rather than at import time.

Why a test rather than a lint rule? Because a static-analysis check
would have to interpret ``os.path.expanduser("~")`` / ``os.environ.get(
"APPDATA")`` semantically — much easier to detect dynamically by
substituting a sentinel and grepping the imported namespace.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


SENTINEL_HOME    = "/__pytest_sentinel_home__/DO_NOT_USE"
SENTINEL_APPDATA = "/__pytest_sentinel_appdata__/DO_NOT_USE"

# Machine-readable markers on the subprocess's stdout. The population line is
# what lets the parent tell "scanned 157 modules, found nothing" apart from
# "scanned nothing, therefore found nothing".
_POPULATION_PREFIX   = "SCANNED_MODULES="
_UNIMPORTABLE_PREFIX = "UNIMPORTABLE="

# Floor for the population assertion. Well below the real count (157 at the
# time of writing) so ordinary growth or deletion never trips it, but high
# enough that a walk which collapses — a bad glob, a renamed src/, an
# exception swallowed inside _iter_src_modules — fails instead of reporting a
# clean scan of almost nothing.
_MIN_EXPECTED_MODULES = 100


# Modules we skip — entry-point scripts that legitimately do startup work at
# import, or package __init__ files that re-export submodules.
#
# `app` and `agent_tools` are NO LONGER skipped: they used to be carved out for
# "may pull in optional deps" reasons, but tests/test_no_thirdparty_module_imports.py
# now guarantees src/ has zero module-level third-party imports (pystray is lazy),
# so both import cleanly here. A module that still can't import in this headless
# env is caught by the `except Exception` below and silently skipped — never a
# false failure.
_SKIP_MODULES = {
    "tokensave-wrapper",            # standalone script
    "precommit_review",             # standalone script
    "prepush_runner",               # standalone script
    "dialogs",                      # package __init__, often imports all dialogs
    "controllers",                  # ditto
    "helpers",                      # ditto
}

# Modules that genuinely cannot be imported in a headless environment, each
# with the reason. Deliberately a per-module list rather than a package-wide
# rule: a blanket skip removes modules from the population silently and for
# reasons that stop being true, whereas an entry here is asserted live by
# `test_known_unimportable_is_not_stale` below.
#
# This replaced a blanket `dialogs.*` skip whose stated reason was that those
# modules "import tkinter at module level, which crashes on Linux CI without
# DISPLAY". Measured, that was wrong twice over: importing tkinter needs no
# display (only constructing a widget does), and none of the 43 dialog modules
# constructed one at import time — the only two candidates sat inside
# `if __name__ == "__main__":`. The rule removed 43 of 160 modules, 27% of
# src/, and the modules it removed were precisely the ones whose `fake_home`
# fixture this guard exists to protect. CI installs `python3-tk`, and this
# test is not tk-marked, so it runs in the non-xvfb step with tkinter present.
_KNOWN_UNIMPORTABLE: dict[str, str] = {}


def _iter_src_modules():
    """Yield (modname, path) for every .py file under src/, recursively.

    Excludes __init__.py files, .tmp.* atomic-write artifacts, and the
    explicit _SKIP_MODULES list — nothing else. In particular there is no
    package-wide carve-out: see _KNOWN_UNIMPORTABLE for why the previous
    `dialogs.*` rule was removed rather than narrowed.
    """
    src_root = Path(__file__).parent.parent / "src"
    for py in src_root.rglob("*.py"):
        name = py.name
        if name == "__init__.py":
            continue
        # Skip atomic-write temp files (`<name>.py.tmp.<pid>.<hex>`) —
        # these are leftovers from interrupted writes, not real modules.
        if ".tmp." in str(py):
            continue
        rel = py.relative_to(src_root).with_suffix("")
        modname = str(rel).replace(os.sep, ".")
        if modname in _SKIP_MODULES:
            continue
        yield modname, py


def _scan_for_offenders():
    """Import every src module; return (offenders, import_errors, discovered).

    ``discovered`` is how many modules the walk yielded, reported so the
    caller can assert the POPULATION and not merely the absence of findings.
    A walk that silently yields nothing produces an empty offender list,
    which is indistinguishable from a clean scan unless the count is checked.

    Assumes the sentinel env vars are ALREADY set in this process — which is
    why the test runs it in a subprocess rather than calling it directly.
    """
    offenders: list[str] = []
    import_errors: list[str] = []
    discovered = 0
    for modname, _path in _iter_src_modules():
        discovered += 1
        # Force a re-import so the sentinel env takes effect for any
        # module-level resolution (modules already cached in sys.modules
        # were imported with the developer's real env).
        sys.modules.pop(modname, None)
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            # Skip modules that fail to import in this environment
            # (missing optional deps, Tk-coupled, etc.).
            import_errors.append(f"{modname}: {type(e).__name__}: {e}")
            continue

        for attr in dir(mod):
            if attr.startswith("__"):
                continue
            try:
                val = getattr(mod, attr)
            except Exception:
                continue
            # Only flag strings — collections-of-strings (lists, dicts)
            # would also be worth scanning but are rare and not worth
            # the false-positive risk from container internals.
            if isinstance(val, str) and (
                SENTINEL_HOME in val or SENTINEL_APPDATA in val
            ):
                offenders.append(f"  {modname}.{attr} = {val!r}")
    return offenders, import_errors, discovered


def _parse_population(output: str) -> "int | None":
    """Read the SCANNED_MODULES= line out of the subprocess's stdout."""
    for line in output.splitlines():
        if line.startswith(_POPULATION_PREFIX):
            try:
                return int(line[len(_POPULATION_PREFIX):].strip())
            except ValueError:
                return None
    return None


def test_no_module_level_path_resolution():
    """No module under src/ captures a user-path at import time.

    Catches the G-J regression: a developer adds e.g.
    ``_CFG = os.path.expanduser("~/.claude.json")`` at module scope, which
    invisibly breaks the ``fake_home`` fixture for all dialog tests.

    **Runs in a SUBPROCESS, deliberately.** The scan has to pop every module
    under ``src/`` out of ``sys.modules`` and re-import it so the sentinel env
    is in effect at import time. Doing that in the shared pytest process
    corrupts it for everything that runs afterwards: a test module that
    already did ``from x import Thing`` holds a class whose ``__globals__``
    belong to the pre-swap module, so a later
    ``monkeypatch.setattr("x.attr", ...)`` can silently miss. For Tk code that
    means a real modal dialog opening in a headless run and blocking forever
    — which is exactly what this did to ``tests/test_shadowlinks.py``: it
    passed 8/8 alone, failed and hung in the full suite, and took the whole
    run from ~15s to >10min.

    Restoring ``sys.modules`` afterwards was tried and is NOT sufficient
    (verified: identity comes back correct and the failures persist), so the
    only reliable answer is to not do it in this process at all. A subprocess
    also gives the sentinel env vars a clean home rather than monkeypatching
    them around a live interpreter.
    """
    env = dict(os.environ)
    env.update(HOME=SENTINEL_HOME, USERPROFILE=SENTINEL_HOME,
               APPDATA=SENTINEL_APPDATA, LOCALAPPDATA=SENTINEL_APPDATA)
    r = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")

    assert r.returncode != 2, (
        "The scan could not import part of its own population, so it cannot "
        "report a clean result — 'I could not look' is not 'there is nothing "
        "there'. Either fix the module, or add it to _KNOWN_UNIMPORTABLE with "
        "the reason (which test_known_unimportable_is_not_stale then keeps "
        "honest):\n\n" + out
    )
    assert r.returncode == 0, (
        "The following module-level variables captured a user-path at "
        "IMPORT TIME. Move the path resolution into a function body so it "
        "re-evaluates each call (this is what makes the fake_home fixture "
        "in tests/conftest.py work):\n\n"
        + out
        + "\n\nSee the refactor in src/helpers/claude_tasks.py "
          "(_claude_projects_dir) for the canonical pattern."
    )

    # Population assertion. Without this the test passes just as happily when
    # the walk yields nothing at all, because no modules means no offenders.
    scanned = _parse_population(out)
    assert scanned is not None, (
        f"The scan did not report its population — expected a "
        f"{_POPULATION_PREFIX!r} line on stdout. Output was:\n\n{out}"
    )
    assert scanned >= _MIN_EXPECTED_MODULES, (
        f"The scan only walked {scanned} modules under src/, below the "
        f"floor of {_MIN_EXPECTED_MODULES}. A clean result over a population "
        f"this small is not evidence of anything — check _iter_src_modules "
        f"and _SKIP_MODULES before trusting this suite."
    )


@pytest.mark.parametrize("modname", sorted(_KNOWN_UNIMPORTABLE))
def test_known_unimportable_is_not_stale(modname):
    """Every _KNOWN_UNIMPORTABLE entry must still be unimportable.

    An exclusion's REASON rots independently of its verdict: the module gets
    fixed, the entry stays, and the module silently never re-enters the
    population. Mirrors the `_EXEMPT` staleness check in
    tests/test_no_console_flash.py.

    Runs in a subprocess for the same reason the main scan does — importing
    src modules in the pytest process corrupts sys.modules for later tests.
    """
    probe = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r})\n"
        f"importlib.import_module({modname!r})\n"
    )
    r = subprocess.run([sys.executable, "-c", probe],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, (
        f"_KNOWN_UNIMPORTABLE lists {modname!r} with reason "
        f"{_KNOWN_UNIMPORTABLE[modname]!r}, but it imports cleanly now. "
        f"Remove the entry so the module rejoins the scanned population."
    )


@pytest.mark.skip(reason="diagnostic — run manually if you suspect import errors")
def test_diagnostic_print_import_errors(capsys):
    """Diagnostic helper — prints which modules failed to import.

    Skipped by default; drop the marker if you suspect modules are being
    silently skipped above.

    Goes through the same subprocess as the real check. It used to re-import
    every src module in-process, which meant un-skipping it would quietly
    reintroduce exactly the interpreter corruption the main test was moved out
    of process to avoid.
    """
    env = dict(os.environ)
    env.update(HOME=SENTINEL_HOME, USERPROFILE=SENTINEL_HOME,
               APPDATA=SENTINEL_APPDATA, LOCALAPPDATA=SENTINEL_APPDATA,
               TSM_REPORT_IMPORT_ERRORS="1")
    r = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=300)
    with capsys.disabled():
        print(r.stdout or "(no output)")


# ── Subprocess entry point ────────────────────────────────────────────────────
# Run by `test_no_module_level_path_resolution` with sentinel env vars already
# in place. Kept dual-mode (like tests/test_no_thirdparty_module_imports.py) so
# it can also be run by hand:  python tests/test_no_import_time_path_resolution.py
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    found, errs, discovered = _scan_for_offenders()

    # Machine-readable population line, parsed by the parent test. Printed on
    # EVERY path including failure, because the population is what tells a
    # real "nothing found" apart from a scan that never ran.
    print(f"{_POPULATION_PREFIX}{discovered}")
    unexpected = [e for e in errs
                  if e.split(":", 1)[0] not in _KNOWN_UNIMPORTABLE]
    for e in unexpected:
        print(f"{_UNIMPORTABLE_PREFIX}{e}")

    if os.environ.get("TSM_REPORT_IMPORT_ERRORS") and errs:
        print("Modules that failed to import:\n  " + "\n  ".join(errs))
    if found:
        print("\n".join(found))
        sys.exit(1)
    if unexpected:
        # Not "clean" — the scan could not look at part of its population.
        # Reported as its own exit code so the parent never reads an
        # inconclusive run as a pass.
        sys.exit(2)
    print(f"OK: no module-level path resolution in src/ "
          f"({discovered} module(s) scanned, "
          f"{len(errs)} known-unimportable).")
    sys.exit(0)
