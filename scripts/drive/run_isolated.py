"""Run a drive script in a SECOND Manager that cannot touch the real one.

    python scripts/drive/run_isolated.py scripts/drive/geometry-sweep.json

The Manager holds a single-instance mutex, so `TOKENSAVE_MANAGER_DRIVE` alone
cannot start while the real window is open (hidden to the tray counts). This
starts the app with a scratch copy of the config and tells it to run isolated:
no lock, no extension-request poller, no tray, no update poller, its own logs.
The real `manager-config.json` is only READ. See `src/helpers/isolated_run.py`
for why isolation has to be proven by the config rather than requested.

The scratch folder is removed afterwards, whatever the run did. The drive
report (`<script>.report.json`) is written beside the script as usual and is not
removed: it is the evidence.

What this does NOT isolate: a drive step that writes a PROJECT (wire, sync,
commit) writes the real repository. Report and screenshot steps are safe.

The window is shown without taking focus (`SW_SHOWNOACTIVATE`) so a driven run
does not pull keyboard input away from whatever is being typed into.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", ".."))
APP = os.path.join(REPO, "src", "app.py")
DEFAULT_CONFIG = os.path.join(REPO, "manager-config.json")

SW_SHOWNOACTIVATE = 4


def prepare(script: str, real_config: str, scratch_root: "str | None" = None):
    """Make the scratch folder and the environment. Returns (folder, env).

    Reads `real_config` and writes nothing to it. A missing config is refused
    rather than papered over with an empty one: an empty config opens the
    first-run Settings dialog, which is not the window being checked.
    """
    if not os.path.isfile(real_config):
        raise FileNotFoundError("no config to copy: %s" % real_config)
    if not os.path.isfile(script):
        raise FileNotFoundError("no drive script: %s" % script)
    folder = tempfile.mkdtemp(prefix="tsm-drive-", dir=scratch_root)
    scratch = os.path.join(folder, "manager-config.json")
    shutil.copyfile(real_config, scratch)
    env = dict(os.environ)
    env["TOKENSAVE_MANAGER_DRIVE"] = os.path.abspath(script)
    env["TOKENSAVE_MANAGER_CONFIG"] = scratch
    return folder, env


def _startup_info():
    """Show the first window without activating it (Windows only)."""
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = SW_SHOWNOACTIVATE
    return info


def run(script: str, real_config: str = DEFAULT_CONFIG,
        command: "list[str] | None" = None) -> int:
    """Run the app (or `command`, for tests) isolated; always clean up."""
    folder, env = prepare(script, real_config)
    try:
        argv = command or [sys.executable, APP]
        return subprocess.run(argv, env=env, cwd=REPO,
                              startupinfo=_startup_info()).returncode
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("script", help="drive script (JSON)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="config to copy (default: this install's)")
    args = parser.parse_args(argv)
    try:
        return run(args.script, args.config)
    except FileNotFoundError as exc:
        print("run_isolated: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
