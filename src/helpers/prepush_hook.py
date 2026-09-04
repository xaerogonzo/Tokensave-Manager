"""Pre-push quality-check hook — install / remove / detect.

Mirrors the design of helpers/precommit_hook.py but for the git pre-push
hook instead of pre-commit. Key differences from the pre-commit hook:

* **Blocking by default.** The pre-commit hook ships warn-only because it
  runs AI review (which can be wrong). The pre-push hook runs deterministic
  checks (syntax, pyflakes, doctor) — if the user installed it they want it
  to block bad pushes. Fail-open only on *infrastructure* errors.

* **Project path is passed as argv[1].** Git pre-push hooks receive remote
  info via stdin; the runner doesn't need it. The project path is hardcoded
  at install time (same as `reviewer_script` in the pre-commit hook).

* **Claude check always skipped.** Too slow + token cost for an automatic
  push gate. Users run it manually from the Run Checks dialog.
"""

from __future__ import annotations

import os

from helpers.git_hooks_env import read_hooks_env


# Sentinel — identifies hooks WE wrote (distinct from the pre-commit marker).
_HOOK_MARKER  = "# TOKENSAVE-PREPUSH-MARKER v1"
_HOOK_PATH_REL = os.path.join(".git", "hooks", "pre-push")


# ── Install / remove / detect ─────────────────────────────────────────────


# ── Where git will actually look, and the third state that needs ──────────
#
# `INSTALLED` / `ABSENT` / `INERT`. The third is not a shade of the second:
# a hook that exists somewhere git does not read needs the *routing* fixed,
# and re-installing writes the same bytes to the same ignored place. See
# `helpers/git_hooks_env` for why the directory is asked of git rather than
# composed from the project path — `core.hooksPath` and linked worktrees each
# break the composed answer, silently and in opposite directions.

INSTALLED = "installed"
ABSENT = "absent"
INERT = "inert"


def hook_path(project_path: str) -> str:
    """Absolute path of the pre-push hook, **where git will look for it**.

    Same reasoning as `precommit_hook.hook_path` — see that docstring and
    `helpers/git_hooks_env` for why the directory is asked of git rather than
    composed from the project path.
    """
    env = read_hooks_env(project_path)
    return env.hook_file("pre-push") if env.ok else os.path.join(
        project_path, _HOOK_PATH_REL)


def pre_push_hook_state(project_path: str) -> str:
    """`INSTALLED`, `INERT` or `ABSENT` — what git will actually do.

    `INERT` means our hook is on disk in the repository's own hook directory
    but `core.hooksPath` sends git somewhere else. Reporting that as `ABSENT`
    would prompt an install that changes nothing; reporting it as `INSTALLED`
    is the bug this module was rewritten for.

    Reads the hooks environment **once**. `hook_path` reads it too, so calling
    that from here would run four git subprocesses per check on a function the
    UI calls whenever a dialog opens.
    """
    env = read_hooks_env(project_path)
    effective = (env.hook_file("pre-push") if env.ok
                 else os.path.join(project_path, _HOOK_PATH_REL))
    if _has_marker(effective):
        return INSTALLED
    if env.ok and env.redirected and _has_marker(
            os.path.join(env.default_hooks_dir, "pre-push")):
        # Ours exists where git no longer looks.
        return INERT
    return ABSENT


def _has_marker(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return _HOOK_MARKER in f.read()
    except OSError:
        return False


def is_pre_push_hook_installed(project_path: str) -> bool:
    """True if git will actually run our pre-push hook.

    **False for `INERT`**, like the pre-commit equivalent: this answers "will
    it run", and a hook stranded by `core.hooksPath` will not. Callers that
    must tell "absent" from "ignored" — they need opposite advice — use
    `pre_push_hook_state`.
    """
    return pre_push_hook_state(project_path) == INSTALLED


def prepush_runner_script_path() -> str:
    """Absolute path to ``src/prepush_runner.py`` for the current install.

    Resolved relative to this module's location so moving the manager
    picks up the new path on next re-install.
    """
    here = os.path.dirname(os.path.abspath(__file__))   # → <manager>/src/helpers
    return os.path.join(os.path.dirname(here), "prepush_runner.py")  # → src/


def install_pre_push_hook(
    project_path: str,
    python_exe: str,
    runner_script: str,
) -> tuple[bool, str]:
    """Write the pre-push hook script. Returns (ok, message).

    Refuses to overwrite a user-installed hook (one without our marker).
    Replacing our own existing hook is fine (handles re-install / upgrade).
    """
    hooks_dir = os.path.join(project_path, ".git", "hooks")
    if not os.path.isdir(os.path.join(project_path, ".git")):
        return False, f"{project_path} is not a git repo (no .git/ directory)"
    try:
        os.makedirs(hooks_dir, exist_ok=True)
    except OSError as e:
        return False, f"could not create {hooks_dir}: {e}"

    p = hook_path(project_path)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        except OSError as e:
            return False, f"could not read existing hook: {e}"
        if _HOOK_MARKER not in existing:
            return False, (
                ".git/hooks/pre-push already exists and was NOT written by "
                "TokenSave Manager. Refusing to overwrite — back it up or "
                "remove it manually first, then retry.")

    # Use the console Python interpreter (pythonw.exe silently discards
    # stderr — fatal for a hook whose whole purpose is to print findings).
    python_exe = _prefer_console_python(python_exe)
    script = _render_hook_script(python_exe, runner_script, project_path)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
    except OSError as e:
        return False, f"could not write hook: {e}"

    try:
        os.chmod(p, 0o755)
    except OSError:
        pass  # Windows ignores chmod; not fatal

    return True, "installed"


def remove_pre_push_hook(project_path: str) -> tuple[bool, str]:
    """Remove our hook. Refuses to touch a user-installed one."""
    p = hook_path(project_path)
    if not os.path.isfile(p):
        return False, "no pre-push hook installed"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    except OSError as e:
        return False, f"could not read hook: {e}"
    if _HOOK_MARKER not in existing:
        return False, (
            "pre-push hook exists but was NOT written by TokenSave Manager — "
            "refusing to delete it. Remove it manually if intended.")
    try:
        os.remove(p)
    except OSError as e:
        return False, f"could not delete hook: {e}"
    return True, "removed"


# ── Internal helpers ──────────────────────────────────────────────────────

def _prefer_console_python(python_exe: str) -> str:
    """Swap pythonw.exe → python.exe (pythonw silently drops stderr)."""
    base = os.path.basename(python_exe).lower()
    if base != "pythonw.exe":
        return python_exe
    candidate = os.path.join(os.path.dirname(python_exe), "python.exe")
    if os.path.isfile(candidate):
        return candidate
    return python_exe


def _render_hook_script(
    python_exe: str,
    runner_script: str,
    project_path: str,
) -> str:
    """Return the body of ``.git/hooks/pre-push`` as a POSIX shell string.

    Git for Windows runs hooks via its bundled MSYS sh, so plain sh syntax
    works on Windows too. Forward-slashes throughout — avoids backslash
    escaping. The runner receives the project path as argv[1] so it doesn't
    have to guess where it's running from.
    """
    py  = python_exe.replace("\\", "/")
    run = runner_script.replace("\\", "/")
    prj = project_path.replace("\\", "/")
    return (
        "#!/bin/sh\n"
        f"{_HOOK_MARKER}\n"
        "# Installed by TokenSave Manager. Runs quality checks (syntax,\n"
        "# pyflakes, doctor) before allowing a push. Remove via the manager's\n"
        "# Run Checks dialog, or delete this file manually.\n"
        "\n"
        "# Pre-push hooks receive remote info on stdin; drain it so git\n"
        "# doesn't wait for the pipe to close, but we don't use the data.\n"
        "cat > /dev/null\n"
        "\n"
        f"PY='{py}'\n"
        f"RUN='{run}'\n"
        f"PRJ='{prj}'\n"
        "\n"
        "# Fail-open if paths are missing — never block pushes because the\n"
        "# manager was moved.\n"
        "if [ ! -x \"$PY\" ] && [ ! -f \"$PY\" ]; then\n"
        "    echo \"[tokensave prepush] python not found at $PY — skipping checks\" >&2\n"
        "    exit 0\n"
        "fi\n"
        "if [ ! -f \"$RUN\" ]; then\n"
        "    echo \"[tokensave prepush] runner not found at $RUN — skipping checks\" >&2\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "exec \"$PY\" \"$RUN\" \"$PRJ\"\n"
    )
