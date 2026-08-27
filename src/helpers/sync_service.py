"""helpers/sync_service.py — the one implementation of "sync this project".

Extracted from :mod:`controllers.sync_ctrl` when the headless CLI arrived. The
controller and the CLI need the same *decisions* and genuinely different
*transports*: a GUI wants the indexer's output streamed into a log as it
arrives, a CLI wants a blocking call that returns a result it can serialise.

The split follows that seam exactly. Everything a caller could get wrong lives
here — which argv to run, the environment tokensave needs, whether shadow links
must be refreshed first, how to read the outcome. Only the act of spawning
differs, and the controller keeps its streaming spawn.

The alternative was letting `cli.py` re-derive any of it, which is how a
Manager-sync and a CLI-sync quietly stop agreeing about what "sync" means.

No Tk, no third-party imports at module scope — this module is imported by the
CLI, which must run without the GUI's dependencies present.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess

from constants import CREATE_NO_WINDOW
from helpers.shadow_links import load_shadow_config, refresh_shadows


def sync_argv(*, force: bool = False) -> list:
    """Arguments for a sync run, without the executable.

    One source of truth so the Git tab, the batch runner and the CLI cannot
    drift into subtly different invocations.
    """
    return ["sync", "--force"] if force else ["sync"]


def tokensave_env(base: "dict | None" = None) -> dict:
    """Environment for a tokensave subprocess whose output we intend to read.

    ``NO_COLOR`` and ``TERM=dumb`` are not cosmetic. tokensave emits ANSI
    colour regardless of whether stdout is a terminal (see
    docs/MCP_INTEGRATION_GOTCHAS.md), so without these the escape codes end up
    in the Manager's log widget and, worse, inside the CLI's JSON payload.
    """
    env = dict(os.environ if base is None else base)
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


@dataclasses.dataclass(frozen=True)
class ShadowPrep:
    """Outcome of the pre-sync shadow-link refresh (SL2).

    ``ran`` is False when the project has not opted in, which is the common
    case and is not worth reporting. ``failed`` on a volume that supports
    hardlinks is the interesting signal — it presents to the user as
    "auto-shadow appears to do nothing at all".
    """
    ran: bool = False
    created: int = 0
    failed: int = 0

    @property
    def worth_reporting(self) -> bool:
        return self.ran and bool(self.created or self.failed)


def prepare_shadows(project_root: str) -> ShadowPrep:
    """Regenerate shadow links when the project opted in, before indexing.

    Runs before the sync rather than after, so the new links are present for
    the indexer that is about to read them. Cost when disabled is one small
    file read: the walk and the hardlink probe sit behind the flag.

    Returns data rather than logging, so the GUI can colour it and the CLI can
    serialise it from the same call.
    """
    config = load_shadow_config(project_root)
    if not (config and config.auto_shadow):
        return ShadowPrep()
    result = refresh_shadows(project_root, config.ext_map)
    return ShadowPrep(
        ran=bool(result.get("ran")),
        created=int(result.get("created") or 0),
        failed=int(result.get("failed") or 0),
    )


@dataclasses.dataclass(frozen=True)
class SyncResult:
    """What a completed sync produced, for a caller that cannot stream."""
    ok: bool
    returncode: int
    output: str
    argv: list
    shadows: ShadowPrep = dataclasses.field(default_factory=ShadowPrep)
    error: str = ""


def run_sync(project_root: str, tokensave_exe: str, *,
             force: bool = False, timeout: "float | None" = None) -> SyncResult:
    """Refresh shadows, then run tokensave sync to completion.

    Synchronous by design: this is the CLI's path. The GUI keeps its streaming
    runner so output still appears line by line, and calls
    :func:`prepare_shadows` and :func:`sync_argv` directly.

    A missing executable is reported as a result rather than raised, because
    both callers have to render it as a message either way, and exit code 3
    ("unavailable prerequisite") is exactly this case.
    """
    argv = sync_argv(force=force)
    shadows = prepare_shadows(project_root)
    try:
        proc = subprocess.run(
            [tokensave_exe] + argv,
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=tokensave_env(),
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except FileNotFoundError:
        return SyncResult(ok=False, returncode=127, output="", argv=argv,
                          shadows=shadows,
                          error=f"tokensave executable not found: {tokensave_exe}")
    except subprocess.TimeoutExpired:
        return SyncResult(ok=False, returncode=124, output="", argv=argv,
                          shadows=shadows,
                          error=f"tokensave sync timed out after {timeout}s")
    except OSError as exc:
        return SyncResult(ok=False, returncode=1, output="", argv=argv,
                          shadows=shadows, error=str(exc))
    return SyncResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        output=proc.stdout or "",
        argv=argv,
        shadows=shadows,
    )
