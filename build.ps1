# build.ps1 - TokenSave Manager Nuitka build pipeline
#
# Produces three standalone .exe files in dist\ with no Python install required.
#
# Prerequisites (run once):
#   pip install nuitka ordered-set zstandard pillow pystray
#
# Optional:
#   icon.ico - place a 256x256 icon file in the same folder as this script.
#   If absent, the --windows-icon-from-ico flag is skipped automatically.
#
# Output layout:
#   dist\
#     tokensave-manager.exe        - main GUI
#     tokensave-wrapper.exe        - MCP wrapper for Claude Desktop
#     tokensave-manager-cli.exe    - headless CLI (console subsystem)
#     manager-config.example.json  - annotated template showing all keys
#                                    (no manager-config.json: settings live in
#                                    %LOCALAPPDATA%\TokenSaveManager\, see src/config_home.py)
#     TOKENSAVE_GUIDE.md
#     CHANGELOG.md
#     templates\
#       claude-md-template.md
#       project-baseline.md
#       ...
#     docs\
#       GITHUB_GUIDE.md            - beginner GitHub guide
#       ARCHITECTURE.md            - manager internals reference
#
# After building, zip dist\ and ship it. Users run tokensave-manager.exe directly.
#
# -Release  also judges what was built by RUNNING it (scripts\frozen_smoke.py: version
#           resource, CLI, GUI), then makes release\Tokensave-Manager-v<ver>-windows.zip and
#           SHA256SUMS.txt (scripts\make_release_zip.py), which smoke-tests the extracted copy.
#           Refuses a working tree with uncommitted changes, with no override.
# -AllowDirty  silences the dirty-tree warning of an ordinary build. -Release ignores it.

param(
    [switch]$Release,
    [switch]$AllowDirty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT = $PSScriptRoot
$DIST = "$ROOT\dist"
$ICON = "$ROOT\icon.ico"

# One version number: constants.APP_VERSION, read by the script the release flow uses, so the
# build, the tag check and the version resource below cannot each parse it differently.
$VERSION = (& python "$ROOT\scripts\release_info.py" version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $VERSION -notmatch '^\d+\.\d+\.\d+$') {
    throw "cannot read the app version from scripts\release_info.py (got: $VERSION)"
}
$PRODUCT = "TokenSave Manager"
$COMPANY = "Alexander L Corthell"

# A release is built from a committed tree or it is not a release: the zip is named for a
# version, and uncommitted changes make that name a claim about code nobody can reproduce.
# An ordinary build only warns, so everyday local builds are unchanged.
function Assert-CleanTree {
    $dirty = @(& git -C $ROOT status --porcelain 2>$null)
    if ($dirty.Count -eq 0) { return }
    if ($Release) {
        throw "-Release refuses a working tree with uncommitted changes ($($dirty.Count) path(s), first: $($dirty[0])). Commit or stash, then build."
    }
    if (-not $AllowDirty) {
        Write-Host "  [warn] building from a working tree with $($dirty.Count) uncommitted path(s); pass -AllowDirty to silence" -ForegroundColor Yellow
    }
}

# -----------------------------------------------------------------------------

function Clear-NuitkaOrphans($dir) {
    # Removes leftover Nuitka temp dirs (onefile-build / build / dist) that
    # --remove-output couldn't delete due to AV file locks or interrupted runs.
    if (-not (Test-Path $dir)) { return }
    foreach ($pat in @("*.onefile-build", "*.build", "*.dist")) {
        Get-ChildItem -Path $dir -Directory -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host "  [clean] removing $($_.Name)" -ForegroundColor DarkGray
            try {
                Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Host "  [warn]  could not remove $($_.Name) - $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
    }
}

# Build-Exe($script, $outName, $nuArgs)
# $nuArgs must include -m nuitka, --onefile, --output-dir, --output-filename.
# Size check runs only when --enable-plugin=tk-inter is present (GUI builds).
# Nuitka compresses onefile payloads ~27%, so a healthy tkinter+PIL+pystray app
# lands at ~14 MB on disk even though the uncompressed payload is ~55 MB.
# We verify by parsing the payload size from Nuitka's own output rather than
# checking the compressed exe size (which would always be under 20 MB).

function Build-Exe($script, $outName, $nuArgs) {
    $isGuiBuild = $nuArgs -contains "--enable-plugin=tk-inter"

    if ((Test-Path $ICON) -and ($isGuiBuild)) {
        $nuArgs += "--windows-icon-from-ico=$ICON"
    }

    # The version resource (Properties > Details). Four parts, the last 0. scripts\frozen_smoke.py
    # reads it back from the BUILT file and fails on a mismatch.
    $descriptions = @{
        "tokensave-manager.exe"     = "$PRODUCT"
        "tokensave-wrapper.exe"     = "$PRODUCT MCP wrapper"
        "tokensave-manager-cli.exe" = "$PRODUCT command-line interface"
    }
    $nuArgs += @(
        "--product-name=$PRODUCT",
        "--product-version=$VERSION.0",
        "--file-version=$VERSION.0",
        "--company-name=$COMPANY",
        "--file-description=$($descriptions[$outName])",
        "--copyright=Copyright (c) 2026 $COMPANY"
    )

    Clear-NuitkaOrphans $DIST

    Write-Host "  Building $outName ..." -ForegroundColor Cyan

    # Capture output so we can parse the uncompressed payload size for the sanity check.
    # Temporarily suspend Stop mode: with $ErrorActionPreference = "Stop", PowerShell
    # treats each native command stderr line as a NativeCommandError and aborts.
    # Nuitka writes progress to stderr, so we must use Continue while capturing.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $buildOutput = & python @nuArgs $script 2>&1
    $nuitkaExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP

    $buildOutput | ForEach-Object { Write-Host $_ }
    if ($nuitkaExit -ne 0) {
        throw "Nuitka failed (exit $nuitkaExit) building $outName"
    }
    Write-Host "  OK: $DIST\$outName" -ForegroundColor Green

    Clear-NuitkaOrphans $DIST

    # Sanity check for GUI builds: parse the uncompressed payload size from
    # "Onefile payload compression ratio (...) size NNNNN to ..." log line.
    # A tkinter+PIL+pystray app should be at least 30 MB uncompressed.
    if ($isGuiBuild) {
        $payloadLine = ($buildOutput | Select-String "Onefile payload compression ratio") | Select-Object -Last 1
        if ($payloadLine -match "size (\d+) to") {
            $uncompressedMB = [math]::Round([long]$Matches[1] / 1MB, 1)
            if ($uncompressedMB -lt 30) {
                Write-Host ""
                Write-Host "  [WARN] $outName uncompressed payload is only $uncompressedMB MB - suspicious for a GUI build." -ForegroundColor Yellow
                Write-Host "         Likely a missing --enable-plugin or --include-package flag." -ForegroundColor Yellow
                Write-Host ""
            } else {
                Write-Host "  Payload OK: $uncompressedMB MB uncompressed" -ForegroundColor DarkGray
            }
        } else {
            $sizeMB = [math]::Round((Get-Item "$DIST\$outName").Length / 1MB, 1)
            Write-Host "  Compressed size: $sizeMB MB (could not parse uncompressed payload size)" -ForegroundColor DarkGray
        }
    }
}

# -----------------------------------------------------------------------------

$guiArgs = @(
    "-m", "nuitka",
    "--onefile",
    "--windows-console-mode=disable",
    "--enable-plugin=tk-inter",
    "--include-package=PIL",
    "--include-package=pystray",
    # Anaconda base-env bloat exclusions.
    # These packages are importable in an Anaconda environment but are not used
    # by TokenSave Manager. Without these flags Nuitka traces into them and
    # bundles ~450 MB of Intel MKL DLLs, scipy, pandas tzdata files, etc.
    # Excluding them drops the uncompressed payload from ~500+ MB to ~55 MB
    # and cuts build time significantly.
    "--nofollow-import-to=numpy",
    "--nofollow-import-to=scipy",
    "--nofollow-import-to=pandas",
    "--nofollow-import-to=matplotlib",
    "--nofollow-import-to=sklearn",
    "--nofollow-import-to=IPython",
    "--nofollow-import-to=notebook",
    "--remove-output",
    "--assume-yes-for-downloads",
    "--output-dir=$DIST",
    "--output-filename=tokensave-manager.exe"
)

# Wrapper is a headless launcher (no tkinter/PIL/pystray).
# --windows-console-mode=disable: no console popup when Claude Desktop spawns it.
$wrapperArgs = @(
    "-m", "nuitka",
    "--onefile",
    "--windows-console-mode=disable",
    "--nofollow-import-to=numpy",
    "--nofollow-import-to=scipy",
    "--nofollow-import-to=pandas",
    "--nofollow-import-to=matplotlib",
    "--nofollow-import-to=sklearn",
    "--nofollow-import-to=IPython",
    "--nofollow-import-to=notebook",
    "--remove-output",
    "--assume-yes-for-downloads",
    "--output-dir=$DIST",
    "--output-filename=tokensave-wrapper.exe"
)

# The headless CLI. The ONE thing that must differ from the other two targets is
# the console mode: both of those use `--windows-console-mode=disable` because
# they are GUI programs and a console would flash on every spawn. A binary built
# that way has NO STDOUT AT ALL, so exposing src/cli.py through either of them
# would produce an exe whose entire contract - one JSON envelope on stdout - is
# silently unreachable. Hence a third target rather than a flag on an existing one.
#
# `force` builds a console-subsystem binary. A parent that redirects stdio (the
# VS Code extension, a task runner) captures the envelope and shows no window;
# a user running it from a terminal gets normal output.
#
# --include-package=helpers because cli.py imports lazily inside its command
# handlers, so nothing at module scope tells Nuitka the package is needed.
$consoleCliArgs = @(
    "-m", "nuitka",
    "--onefile",
    "--windows-console-mode=force",
    "--include-package=helpers",
    "--nofollow-import-to=numpy",
    "--nofollow-import-to=scipy",
    "--nofollow-import-to=pandas",
    "--nofollow-import-to=matplotlib",
    "--nofollow-import-to=sklearn",
    "--nofollow-import-to=IPython",
    "--nofollow-import-to=notebook",
    # tkinter must not be dragged in: the CLI is expected to run where the GUI's
    # dependencies are absent.
    "--nofollow-import-to=tkinter",
    "--remove-output",
    "--assume-yes-for-downloads",
    "--output-dir=$DIST",
    "--output-filename=tokensave-manager-cli.exe"
)

Write-Host ""
Write-Host "=== TokenSave Manager - Nuitka build (v$VERSION) ===" -ForegroundColor Cyan
Write-Host ""

Assert-CleanTree

New-Item -ItemType Directory -Force -Path $DIST | Out-Null

# The in-app Help is compiled INTO the GUI exe, so a release's help is read-only and cannot drift from
# the build (docs\ in dist is a human copy the app does not read). The bundle is generated from
# helpers/help_docs.HELP_DOCUMENTS, named for the compiler, and removed afterwards even when the
# compile fails: a stale generated module in src\ would be picked up by the next source run.
# The GUI is the only target that has a Help tab; the CLI and the wrapper do not import it.
$HELP_BUNDLE = "$ROOT\src\_help_bundle.py"
& python "$ROOT\scripts\build_help_bundle.py"
if ($LASTEXITCODE -ne 0) { throw "the help bundle could not be built (see above); the GUI would ship with a smaller Help" }
try {
    Build-Exe "$ROOT\src\app.py" "tokensave-manager.exe" ($guiArgs + "--include-module=_help_bundle")
} finally {
    Remove-Item -LiteralPath $HELP_BUNDLE -Force -ErrorAction SilentlyContinue
}
Build-Exe "$ROOT\src\tokensave-wrapper.py" "tokensave-wrapper.exe" $wrapperArgs
Build-Exe "$ROOT\src\cli.py" "tokensave-manager-cli.exe" $consoleCliArgs

# Stage data files next to the exes
Write-Host ""
Write-Host "=== Staging distribution files ===" -ForegroundColor Cyan

# -- NO manager-config.json. A packaged Manager keeps its settings in
#    %LOCALAPPDATA%\TokenSaveManager\ (src/config_home.py), so a release must not ship one: a
#    clean config beside the exe is what overwrote a user's real settings when a zip was
#    unzipped over an old folder, and it would now be mistaken for an old config to import.
#    The inventory check in make_release_zip.py refuses an archive that contains one.

# -- Example config (annotated template for reference) --
if (Test-Path "$ROOT\manager-config.example.json") {
    Copy-Item "$ROOT\manager-config.example.json" "$DIST\manager-config.example.json" -Force
    Write-Host "  [file] manager-config.example.json" -ForegroundColor Green
}

# -- Root-level documents (LICENSE too: an MIT licence travels with every copy) --
foreach ($md in @("README.md", "TOKENSAVE_GUIDE.md", "CHANGELOG.md", "LICENSE")) {
    $src = "$ROOT\$md"
    if (Test-Path $src) {
        Copy-Item $src "$DIST\$md" -Force
        Write-Host "  [file] $md" -ForegroundColor Green
    }
}

# -- templates\ --
$tmplDest = "$DIST\templates"
New-Item -ItemType Directory -Force -Path $tmplDest | Out-Null
# -Recurse, because templates\gotchas\ is a subdirectory. Without it the
# shipped project-baseline.md indexes three gotcha files that are not in
# dist\ -- a broken reference in every project the manager scaffolds.
Copy-Item "$ROOT\templates\*" $tmplDest -Force -Recurse
$count = (Get-ChildItem $tmplDest -File -Recurse).Count
Write-Host "  [dir]  templates\ ($count files)" -ForegroundColor Green

# -- docs\ - user-facing guides (exclude dev-only files) --
$docsSrc  = "$ROOT\docs"
$docsDest = "$DIST\docs"
New-Item -ItemType Directory -Force -Path $docsDest | Out-Null
# Every document in helpers/help_docs.HELP_DOCUMENTS must appear here or
# in the root-markdown loop above: the Help tab no longer reads these (it is compiled
# into the GUI exe from the same list; see build_help_bundle.py), but they ship as a human copy,
# so one missing from a build is a help topic that silently disappears.
# tests/test_build_ships_help_corpus.py parses this list and fails when
# a corpus document is not covered -- the alternative was a hand-kept
# list that only grows by hand, which is exactly how this one went stale.
# ARCHITECTURE* are here for a different reason: shipped for readers of
# the build, not used by Help.
$docsInclude = @(
    "USER_GUIDE.md",
    "GITHUB_GUIDE.md",
    "RELOCATING.md",
    "UPGRADE_INTEGRATION.md",
    "PYSCOPE_INTEGRATION.md",
    "ARCHITECTURE.md",
    "ARCHITECTURE_TOKENSAVE.md"
)
$docsCopied = 0
foreach ($name in $docsInclude) {
    $src = "$docsSrc\$name"
    if (Test-Path $src) {
        Copy-Item $src "$docsDest\$name" -Force
        Write-Host "  [doc]  docs\$name" -ForegroundColor Green
        $docsCopied++
    }
}
if ($docsCopied -eq 0) {
    Write-Host "  [warn] no docs\ files found - skipping" -ForegroundColor Yellow
}

if ($Release) {
    Write-Host ""
    Write-Host "=== Running what was built (version resource, CLI, GUI) ===" -ForegroundColor Cyan
    & python "$ROOT\scripts\frozen_smoke.py" --dist $DIST --version $VERSION --product $PRODUCT
    if ($LASTEXITCODE -ne 0) { throw "the built executables failed their smoke test (see above); no release was made" }

    Write-Host ""
    Write-Host "=== Packaging the release zip ===" -ForegroundColor Cyan
    & python "$ROOT\scripts\make_release_zip.py" --dist $DIST --version $VERSION
    if ($LASTEXITCODE -ne 0) { throw "the release zip was not made (see above)" }
}

Write-Host ""
Write-Host "Build complete -> $DIST" -ForegroundColor Green
Write-Host ""
Write-Host "Before distributing:" -ForegroundColor Yellow
if ($Release) {
    Write-Host "  1. release\ holds the zip and SHA256SUMS.txt; try the zip, then publish (docs\GITHUB_GUIDE.md)." -ForegroundColor Yellow
} else {
    Write-Host "  1. Not a release: build with -Release for a versioned, smoke-tested zip and checksums." -ForegroundColor Yellow
}
Write-Host "  2. Users run tokensave-manager.exe and configure paths via Settings on first run." -ForegroundColor Yellow
Write-Host ""

exit 0
