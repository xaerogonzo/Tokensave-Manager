# Release starters

Opt-in files for a project that ships a Windows build. They are **not** delivered by wiring,
sync or Repair, and they are not lessons: the lessons (`docs/gotchas/release-hardening.md`,
`changelog-fragments.md`, `installer-and-user-data.md`) say what to do and why; these are one
way of doing it, adapted from OpenChem Studio's release pipeline.

```bash
python scripts/adopt_release.py list
python scripts/adopt_release.py tokens
python scripts/adopt_release.py adopt --project D:/path/to/project --dry-run \
    --set PRODUCT_NAME="My App" --set ARTIFACT_PREFIX=MyApp --set REPO=me/my-app
```

Run it from the TokenSave Manager checkout. Nothing is overwritten (an existing file is
reported and left alone), each starter is judged on its own, and a rendering that fails its
check writes nothing. The files are yours afterwards: no header, no sync. Commit them.

| Starter | Lands at | What it is |
|---|---|---|
| `release-workflow` | `.github/workflows/release.yml` | Tag-triggered build that **drafts** a release. `permissions: {}`, actions pinned to full SHAs, no expression spliced into a script, tag/changelog checked before the long build. |
| `release-info` | `tools/release_info.py` | The tag must equal the version in one file; the notes are that version's `## [x.y.z]` changelog section. Stdlib only. |
| `release-zip` | `tools/make_release_zip.py` | One versioned top folder, `.partial` then promote, inventory check, smoke test on an extracted copy in a path with a space, `SHA256SUMS.txt`. |
| `frozen-smoke` | `tools/frozen_smoke.py` | Launch the built app on a drive script and judge its report, failing closed. |
| `installer` | `packaging/installer.iss` | Inno Setup: per-user default, upgrade wipes the runtime folder only, uninstall data boxes unticked. |

## What you still have to supply

The starters are the scaffolding around three things only your project can provide:

1. **A build command** (`BUILD_COMMAND`, default `./build.ps1 -Release -Installer`) that freezes the
   app, smoke-tests it, and leaves `dist/<ARTIFACT_PREFIX>-v<version>-<PLATFORM_TAG>.zip`,
   `...-setup.exe` and `SHA256SUMS.txt`. The workflow runs `sha256sum -c` on them.
2. **A driver in the app** that honours `<ENV_PREFIX>_DRIVE`, `_DRIVE_REPORT` and `_DATA_ROOT` and writes
   the JSON report described at the top of `frozen_smoke.py`. Name the assertions your driver
   emits in `REQUIRED_ASSERTIONS`; the judge fails if any is missing.
3. **A data folder the app and the installer agree on** (`DATA_FOLDER_NAME`), and, if you want the
   "moved folder" protection, the app writing `data_root.txt` there.

## Trim before you trust it

- The workflow installs `uv` and Inno Setup. Remove the `uv` step if you do not use it (and set
  `SETUP_COMMAND`); drop the Inno step and the `-setup.exe` lines if you ship a zip only.
- If you adopted changelog fragments, add a step before the build that runs your fragment check
  with `--no-pending` on a tag, so an unfolded fragment cannot be released.
- Pins (the action SHAs, the Inno Setup version and sha256) were right when copied. **Re-resolve
  each one when you adopt**: `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`, and the Inno
  hash from the maintainers' release page.
- The Inno `[Code]` section removes a registry key and a data folder on request. Delete
  `SettingsKey` use if your settings live in a file; keep the "only ever the application's own
  folder" guard on any removal.

## What it does not give you

A code signature. The files are reproducible, checksummed and attested, and Windows
SmartScreen will still say "unknown publisher".
