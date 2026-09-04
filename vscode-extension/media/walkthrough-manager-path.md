## Source mode, and why it is the recommendation

The extension can run the Manager two ways.

**Source mode** runs `<managerPath>/src/cli.py` under your Python. It executes
the *current* code, finds `manager-config.json` beside the checkout on its own,
and can run `checks` — which needs a real interpreter.

**A compiled `tokensave-manager-cli.exe`** is a snapshot. It goes stale the
moment the Manager changes, needs a rebuild-repackage-reinstall cycle to catch
up, and cannot run `checks` at all: under a Nuitka onefile build
`sys.executable` is the extracted binary rather than an interpreter.

So pick your checkout unless you do not have one.
