### Added
- (cli) **`manager-cli release-guide --stage preflight|plan|post`.** The Release Wizard's pre-flight, exact tag commands and post-build draft check as one JSON envelope, for agents and scripts. The exit code follows the verdict (0 ready, 1 a check failed, 4 something is a warning or could not be read, which is not a pass), and it reports the planned commands without running any of them. Classified `observe_refresh` because it runs `git fetch origin`. (`src/cli_release_command.py`, `src/cli.py`, `src/helpers/commands.py`, `vscode-extension/src/commands.ts`, `tests/test_cli_release_guide.py`)

### Fixed
- (release) **The older workflow mode now finds a project's `tools/release_info.py`.** It only looked in `scripts/`, so a project keeping it under `tools/` was silently not checked against its version before the tag was pushed. (`src/dialogs/release_wizard.py`)
- (release) **A failing project check in the new release flow is shown as its first few lines, cut per line,** instead of the last 300 characters cut mid-word. (`src/helpers/release_flow.py`)
