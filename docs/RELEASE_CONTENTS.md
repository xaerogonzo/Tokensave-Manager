# What a release contains

The release zip is `Tokensave-Manager-v<version>-windows.zip`, made by `build.ps1 -Release`
(`scripts/make_release_zip.py`). Everything under one versioned top folder,
`Tokensave-Manager-v<version>-windows/`, so unzipping a new version beside an old one cannot
mix two versions' files. `SHA256SUMS.txt` is written beside it, never inside it.

A test (`tests/test_release_contents.py`) fails when a tracked top-level entry has no row in the
second table, when a "No" entry is staged by `build.ps1`, or when a file the zip check requires
is missing from the first table. The page is a guard, not a description someone wrote once.

## Inside the zip

| Path | What it is | The app reads it | Safe for a user to edit |
|---|---|---|---|
| `tokensave-manager.exe` | The GUI, Nuitka onefile | runs | no |
| `tokensave-wrapper.exe` | MCP wrapper Claude Desktop launches | runs | no |
| `tokensave-manager-cli.exe` | The headless CLI (console subsystem) | runs | no |
| `manager-config.example.json` | Annotated template of every key | no | n/a |
| `README.md`, `TOKENSAVE_GUIDE.md`, `CHANGELOG.md` | Documentation; the changelog is the release's notes. `README.md` and `TOKENSAVE_GUIDE.md` are also Help documents, compiled into the GUI | `CHANGELOG.md` shown in-app; the other two no | no point |
| `LICENSE` | MIT licence text | no | no |
| `templates/` | Baseline, gotchas, drive template, release starters; copied recursively | yes: the baseline and lessons are delivered into projects from here | no: edits are overwritten by the next release |
| `docs/` | The Help documents and architecture references (a fixed list in `build.ps1`) | **no**: Help is compiled into the GUI exe; this is a human copy | no point: editing it changes nothing Help shows |

**Not in the zip, and checked not to be:** `logs/` (a run of the app writes it; v2.1.0 shipped one),
any `manager-config.json` (clean or not), and `config-migration.json`. `__pycache__` is skipped when
the archive is written.

### The settings are not in the zip

A packaged Manager keeps `manager-config.json` and its logs in `%LOCALAPPDATA%\TokenSaveManager\`
(`src/config_home.py`), so replacing or moving the program folder never touches them. The zip used to
carry a clean config beside the exe, which overwrote a user's real settings when it was unzipped over an
old folder. It carries none now, and the inventory check refuses an archive that does.

The first start of a build with no settings in the home **copies** the old ones across: beside the
executable first, then the newest sibling `Tokensave-Manager-v<version>-windows` folder. The copy is
verified, appears atomically, never overwrites, and leaves the original where it was; `config-migration.json` in the home records where it came from. A source checkout is unchanged: its
config stays in the repository root.

### Known mismatch

`templates/release/` ships, but the script that adopts it (`scripts/adopt_release.py`) is source-only,
so the starters are reachable only from a checkout. That is deliberate for now (they are for projects
that ship a Windows build, adopted by a developer), and it is listed so nobody assumes otherwise.

## The repository's top level

| Entry | Ships | Where / why not |
|---|---|---|
| `.codegraph` | No | Local index configuration |
| `.github` | No | CI and release workflows |
| `.gitignore` | No | Repository metadata |
| `BASIC_INSTRUCTIONS.md` | No | Development instructions for agent sessions |
| `CHANGELOG.md` | Yes | Staged to the zip root |
| `CLAUDE.md` | No | Development instructions entry point |
| `LICENSE` | Yes | Staged to the zip root |
| `Launch TokenSave Manager.bat` | No | Source-checkout launcher; the zip has the exe |
| `README.md` | Yes | Staged to the zip root |
| `TOKENSAVE_GUIDE.md` | Yes | Staged to the zip root |
| `build-extension.bat` | No | Builds the VS Code extension, shipped separately |
| `build-extension.ps1` | No | As above |
| `build.bat` | No | Build tooling |
| `build.ps1` | No | Build tooling |
| `changelog.d` | No | Unreleased fragments, folded into a release by the Release Wizard |
| `docs` | Partial | A fixed list of seven user-facing documents |
| `manager-config.example.json` | Yes | Staged to the zip root |
| `project-baseline.md` | No | This repository's own managed copy of the baseline; the shipped template is `templates/project-baseline.md` |
| `pyproject.toml` | No | Test configuration only |
| `requirements-dev.txt` | No | Development dependencies |
| `scripts` | No | Developer tools, including the release scripts themselves |
| `src` | Compiled | Built into the three executables, not shipped as source |
| `templates` | Yes | Copied recursively to `templates/` |
| `tests` | No | Test suite |
| `vscode-extension` | No | Packaged as a `.vsix` by `release-extension.yml`, attached to the release separately |

## How the build checks itself

`build.ps1 -Release` refuses a working tree with uncommitted changes, stamps the version resource of
all three executables from `constants.APP_VERSION`, then runs `scripts/frozen_smoke.py` on `dist\`
(version resource, CLI from a foreign directory and GUI on a drive script in a first-run state, then the CLI, the
MCP wrapper (sent a real `initialize`) and the GUI again with the real tokensave and git configured, the build's
own templates, and an indexed scratch repository, and finally the GUI as a packaged first run with an old settings
file beside it, which must be copied into the user-data home; each layer its own verdict) and
`scripts/make_release_zip.py`, which checks the archive's inventory and smoke-tests the **extracted** copy
in a folder with a space in its name before it promotes the zip and its checksums.

What this does not do: it does not exercise the MCP wrapper beyond its version resource, it does not
sign anything (Windows SmartScreen will say "unknown publisher"), and a checksum detects a damaged
download, not who published it.
