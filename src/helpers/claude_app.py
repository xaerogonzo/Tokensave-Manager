"""Open a new Claude Code session in the Claude DESKTOP app, from a `claude://` link.

The terminal agent (`agent_cli.spawn`) starts by itself; the desktop app's does not, so the
Manager can only do what its link allows. Anthropic documents one Claude Code link
(support.claude.com, "Open Claude desktop with a link")::

    claude://code/new?q=<prompt>&folder=<absolute path>

Every value URL-encoded; `q` prefills the composer and is cut near 14,000 characters; `folder`
is an absolute path, only the first is used, and a folder passed by link is treated as
UNTRUSTED, so the app asks to trust it every time. If the app is not running the OS starts it.
A community report says that on Windows, with the app already running, the link may not open a
new window, and Code deep links have been reported gated off in some builds. So a link is never
the only route: the caller also puts the message on the clipboard first, shows it, and says that
the OS accepting the link is not proof the composer was filled.

What is NOT claimed: that the app opened, that it opened the right folder, that the composer
was filled, or that the new session can read the brief. Those are what the live check answers.

No UI imports.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

NEW_SESSION = "claude://code/new"

#: The documented truncation of `q` is about 14,000 characters. The handoff message is a few
#: hundred, so this is a guard against a bug that put a brief in the link, not a working limit.
MAX_MESSAGE_CHARS = 2000


def new_session_url(folder: str, message: str) -> str:
    """The link for a new session in `folder` with `message` in the composer.

    Each value is encoded on its own with nothing left safe (`%20`, never `+`; `:` `\\` `/` `&`
    `%` `#` and non-ASCII all escaped), so a Windows path with spaces or an ampersand in the
    message cannot change what the link means. Raises ValueError rather than build a link that
    would be cut or that has nothing to open.
    """
    if not folder or not os.path.isabs(folder):
        raise ValueError(f"the folder must be an absolute path, not {folder!r}")
    if not message.strip():
        raise ValueError("there is no message to put in the composer")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"the message is {len(message)} characters; the link carries only the "
                         f"short instruction (at most {MAX_MESSAGE_CHARS}), never a brief")
    return f"{NEW_SESSION}?folder={quote(folder, safe='')}&q={quote(message, safe='')}"


def registered() -> bool:
    """True when this user has a handler registered for `claude://` (the desktop app's installer
    writes it). That the app is installed is not the same as the link working, but a link with
    no handler certainly does not."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\claude\shell\open\command"):
            return True
    except OSError:
        return False


def open_url(url: str) -> "tuple[bool, str]":
    """Hand the link to the OS. `(True, "")` means the OS ACCEPTED it, nothing more."""
    if sys.platform != "win32":
        return False, "Opening a claude:// link is only supported on Windows here."
    try:
        os.startfile(url)                      # ShellExecute: the registered protocol handler
    except OSError as exc:
        return False, f"Windows could not open the link: {exc}"
    return True, ""
