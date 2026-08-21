"""GitHubSetupDialog — the GitHub-flavoured first-push wizard.

The wizard itself now lives in ``dialogs/remote_setup.py``, driven by a
provider object from ``helpers/remote_providers.py``. What used to be
GitHub-specific — the branding, the sign-in and new-repo URLs, the advice
text, and the ``gh release create`` command — is data on ``GITHUB`` rather
than code in the dialog.

This class stays so that every existing caller keeps working unchanged; the
constructor signature is identical. ``tests/test_remote_providers.py`` pins
the release argv against what this file used to build, because a
generalisation is only worth anything if the path it generalised still runs
the same commands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dialogs.remote_setup import RemoteSetupDialog
from helpers.remote_providers import GITHUB

if TYPE_CHECKING:
    from state import ManagerConfig


class GitHubSetupDialog(RemoteSetupDialog):
    """Step-by-step GitHub setup wizard."""

    def __init__(self, parent, path: str, cfg: "ManagerConfig"):
        super().__init__(parent, path, cfg, provider=GITHUB)
