# Bundled Manager CLI

`tokensave-manager-cli.exe` goes here before packaging. It is the third build
target from the Manager's `build.ps1` — a **console-subsystem** binary, built
with `--windows-console-mode=force`.

That flag is the whole reason the target exists: the Manager's other two exes
are built `--windows-console-mode=disable`, and such a binary has **no stdout
at all**, so the CLI's entire contract (one JSON envelope on stdout) would be
unreachable through them. See `docs/ARCHITECTURE.md` in the Manager repo.

The binary is not committed — it is a build artifact. `npm run package` expects
it to be present.
