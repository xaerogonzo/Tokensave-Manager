"""isolated_run -- when a second Manager may run beside the real one.

The Manager takes a single-instance mutex, so a driven run
(`TOKENSAVE_MANAGER_DRIVE`) could not start while the user's own window was open
(hidden to the tray counts): it would raise that window and exit. Closing the
user's instance to make room is not this module's answer. It lets the driven run
be a SECOND instance, on one condition that it can check rather than assume.

**Isolation is proven, not requested.** A run is isolated only when BOTH hold:

* `TOKENSAVE_MANAGER_DRIVE` names a script, and
* `TOKENSAVE_MANAGER_CONFIG` names a config file that is not the install's own.

Either alone changes nothing: a drive with no scratch config still takes the lock
(and, with a real Manager open, still yields to it), because skipping the lock
there would let two processes `save()` one `manager-config.json`, whose writer
is an unsynchronised whole-file dump. Pointing `TOKENSAVE_MANAGER_CONFIG` at the
real file is refused for the same reason.

An isolated run also leaves out what acts on shared state: the extension-request
poller (the inbox is the user's, so both instances would answer one request), the
tray icon, and the update poller. It never creates the mutex, so
`manager_ipc.manager_running()` still describes the real Manager and nothing
else, and its window title carries `DRIVE_TITLE_SUFFIX` so a title match cannot
land on it.

What isolation does NOT cover: a drive step that writes a PROJECT (wire, sync,
commit) writes the real repository, exactly as it does today. Report and
screenshot steps are the safe ones.

No Tk import and no side effects, so it is cheap to test.
"""

from __future__ import annotations

import os

from constants import CONFIG_ENV, DEFAULT_CONFIG_PATH

DRIVE_ENV = "TOKENSAVE_MANAGER_DRIVE"
DRIVE_TITLE_SUFFIX = " [drive]"


def _same_file(a: str, b: str) -> bool:
    """Do two spellings name one path? Case and symlinks both fold."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def is_isolated(env=None, default_config: str = DEFAULT_CONFIG_PATH) -> bool:
    """True when this process may run beside a real Manager. See the module."""
    env = os.environ if env is None else env
    if not (env.get(DRIVE_ENV) or "").strip():
        return False
    override = (env.get(CONFIG_ENV) or "").strip()
    if not override:
        return False
    return not _same_file(os.path.abspath(os.path.expanduser(override)),
                          default_config)
