"""helpers/install_analyzers.py — obtaining ruff, pyright and markdownlint.

`helpers/headless_analyzers.py` answers *"is it there, and what did it say"*.
This answers *"how do I get it"*, and they are deliberately separate modules:
one is read-only analysis, the other is lifecycle. Same split as
`helpers/pyscope.py` versus `helpers/install_codegraph.py`.

**Two package managers, one table.** The three analyzers are not obtained the
same way and cannot be:

    pyright, markdownlint-cli2   npm      global install
    ruff                         uv       `uv tool install`

**ruff is not on npm**, and this was checked rather than assumed — the `ruff`
package on the registry is an unrelated ES6-generator coroutine library at
version 1.5.4, and installing it would put something entirely wrong on the
machine while every log line said success. Only WebAssembly bindings are
published under `@astral-sh/`. ruff ships on PyPI and as a GitHub release
binary, and `uv tool install` is the route this codebase already understands:
PyScope arrives the same way, and `~/.local/bin` is already a probed location
for exactly that reason.

**Why not a GitHub-release download.** `helpers/install_tokensave.py` does that
and does it well, but its repository and asset suffix are baked in. Reusing it
would mean parameterising a module on the release path, and writing a second
one would duplicate 358 lines of zip-safety handling to obtain a tool that a
one-line command already installs.

**A package manager that is absent is a state, not an error.** `uv` missing
means ruff cannot be installed *from here* — the row says so and shows the
command, rather than offering a button that fails. That is the same posture
`settings_pyscope` takes, and the reason `_build_tool_row(actions=False)`
exists.

Every call takes the package manager's **absolute path**, resolved by the
caller. A bare `npm` or `uv` raises `WinError 2` from Python on Windows even
though it works in a shell — `CreateProcess` appends only `.exe` and ignores
PATHEXT. See `gotchas/windows-subprocess.md`.

No Tk. Safe to call from a worker thread.
"""

from __future__ import annotations

import dataclasses
import subprocess
from typing import Callable, Optional, Tuple

from constants import CREATE_NO_WINDOW

#: Manager-not-found, kept distinct from "the command failed". One means
#: "install uv first", the other means "the install went wrong", and they want
#: different things from the reader.
MANAGER_MISSING = "manager_missing"


@dataclasses.dataclass(frozen=True)
class ManagerSpec:
    """One package manager, described by the verbs it uses.

    `{pkg}` is substituted with the package name. Spelling the argv out per
    manager rather than branching on a name is the same rule the analyzer table
    follows: a field named `is_npm` re-creates the cascade.
    """

    key: str
    label: str
    #: How to find it. Returns an absolute path or "" — never a bare name.
    detect: Callable
    install: tuple
    update: tuple
    uninstall: tuple
    #: Shown when the manager itself is absent. The user runs this, not us.
    install_manager_hint: str
    #: Longer for a first npm global install on a cold cache.
    timeout: int = 300


def _detect_npm_exe() -> str:
    from helpers.detection import _detect_npm
    return _detect_npm()


def _detect_uv_exe() -> str:
    from helpers.detection import _detect_uv
    return _detect_uv()


MANAGERS: dict = {
    "npm": ManagerSpec(
        key="npm", label="npm",
        detect=_detect_npm_exe,
        install=("install", "-g", "{pkg}"),
        # `install …@latest` rather than `npm update -g`, which sometimes
        # silently no-ops on Windows. Same decision `install_codegraph` records.
        update=("install", "-g", "{pkg}@latest"),
        uninstall=("uninstall", "-g", "{pkg}"),
        install_manager_hint="Install Node.js, which provides npm: "
                             "https://nodejs.org",
    ),
    "uv": ManagerSpec(
        key="uv", label="uv",
        detect=_detect_uv_exe,
        # Verbs confirmed against `uv tool --help` on 0.11.5 rather than
        # remembered: install / upgrade / uninstall.
        install=("tool", "install", "{pkg}"),
        update=("tool", "upgrade", "{pkg}"),
        uninstall=("tool", "uninstall", "{pkg}"),
        install_manager_hint="Install uv: https://docs.astral.sh/uv/ "
                             "(or: winget install astral-sh.uv)",
    ),
}


def _run(manager: ManagerSpec, exe: str, args: list,
         on_log: Optional[Callable[[str], None]] = None
         ) -> Tuple[bool, str]:
    """Run one package-manager command. Never raises.

    `exe` must be an absolute path; see the module docstring on bare names.
    """
    import os

    if not exe or not os.path.isfile(exe):
        return False, "%s not found at: %r" % (manager.label, exe)

    line = "$ %s %s" % (manager.label, " ".join(args))
    if on_log is not None:
        try:
            on_log(line)
        except Exception:                                # noqa: BLE001
            pass
    try:
        proc = subprocess.run(
            [exe] + args, capture_output=True, timeout=manager.timeout,
            creationflags=CREATE_NO_WINDOW,
            # Decode explicitly: a legacy console codepage otherwise decides,
            # and a mangled byte raising out of here reads like our own crash.
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "%s\n%s timed out after %ds" % (line, manager.label,
                                                      manager.timeout)
    except OSError as exc:
        return False, "%s\ncould not launch %s: %s" % (line, manager.label, exc)

    log = line + "\n" + (proc.stdout or "") + (proc.stderr or "")
    if on_log is not None:
        for out in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
            try:
                on_log(out)
            except Exception:                            # noqa: BLE001
                pass
    return proc.returncode == 0, log


def manager_for(spec) -> "ManagerSpec | None":
    """The package manager an analyzer row is obtained through, or None.

    None means the row declares no install route at all, which is a legitimate
    state — it is how a future analyzer says "locate it yourself".
    """
    return MANAGERS.get(getattr(spec, "installer", ""))


def availability(spec) -> Tuple[str, str]:
    """`(state, detail)` for whether this analyzer CAN be installed from here.

    Returns `("ok", <manager exe>)`, or `(MANAGER_MISSING, <hint>)`.

    Deliberately three-valued in effect: a row with no installer at all is not
    the same as one whose manager is missing, and neither is an error.
    """
    manager = manager_for(spec)
    if manager is None:
        return MANAGER_MISSING, "no install route is declared for this tool"
    exe = manager.detect()
    if not exe:
        return MANAGER_MISSING, manager.install_manager_hint
    return "ok", exe


def command_hint(spec) -> str:
    """The exact command a person would run. Shown, never auto-executed.

    Same posture `settings_pyscope.INSTALL_HINT` takes: when the Manager cannot
    do it, it says precisely what to type rather than pretending.
    """
    manager = manager_for(spec)
    if manager is None:
        return ""
    return "%s %s" % (manager.label,
                      " ".join(a.format(pkg=spec.package)
                               for a in manager.install))


def _act(spec, verb: str,
         on_log: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    manager = manager_for(spec)
    if manager is None:
        return False, "no install route is declared for %s" % spec.key
    state, detail = availability(spec)
    if state != "ok":
        # Not attempted, and said so. Running the verb anyway would produce a
        # WinError the user cannot act on.
        return False, "%s is not installed. %s" % (manager.label, detail)
    args = [a.format(pkg=spec.package) for a in getattr(manager, verb)]
    return _run(manager, detail, args, on_log)


def install(spec, on_log=None) -> Tuple[bool, str]:
    return _act(spec, "install", on_log)


def update(spec, on_log=None) -> Tuple[bool, str]:
    return _act(spec, "update", on_log)


def uninstall(spec, on_log=None) -> Tuple[bool, str]:
    return _act(spec, "uninstall", on_log)
