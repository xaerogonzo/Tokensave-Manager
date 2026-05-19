# build.ps1 - TokenSave Manager Nuitka build pipeline
#
# Produces two standalone .exe files in dist\ with no Python install required.
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
#     manager-config.json          - clean config (user configures on first run)
#     manager-config.example.json  - annotated template showing all keys
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

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT = $PSScriptRoot
$DIST = "$ROOT\dist"
$ICON = "$ROOT\icon.ico"

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
$cliArgs = @(
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

Write-Host ""
Write-Host "=== TokenSave Manager - Nuitka build ===" -ForegroundColor Cyan
Write-Host ""

New-Item -ItemType Directory -Force -Path $DIST | Out-Null

Build-Exe "$ROOT\src\tokensave-manager.py" "tokensave-manager.exe" $guiArgs
Build-Exe "$ROOT\src\tokensave-wrapper.py" "tokensave-wrapper.exe" $cliArgs

# Stage data files next to the exes
Write-Host ""
Write-Host "=== Staging distribution files ===" -ForegroundColor Cyan

# -- Clean config (blank paths - user fills in via Settings on first run) --
$cleanConfig = [ordered]@{
    tokensave_exe          = ""
    template_dir           = ""
    python_exe             = ""
    editor_cmd             = "code"
    git_exe                = ""
    search_roots           = @()
    project_categories     = [ordered]@{}
    user_snippets          = @()
    auto_commit_after_sync = $false
}
[System.IO.File]::WriteAllText(
    "$DIST\manager-config.json",
    ($cleanConfig | ConvertTo-Json -Depth 3))
Write-Host "  [file] manager-config.json" -ForegroundColor Green

# -- Example config (annotated template for reference) --
if (Test-Path "$ROOT\manager-config.example.json") {
    Copy-Item "$ROOT\manager-config.example.json" "$DIST\manager-config.example.json" -Force
    Write-Host "  [file] manager-config.example.json" -ForegroundColor Green
}

# -- Root-level markdown --
foreach ($md in @("TOKENSAVE_GUIDE.md", "CHANGELOG.md")) {
    $src = "$ROOT\$md"
    if (Test-Path $src) {
        Copy-Item $src "$DIST\$md" -Force
        Write-Host "  [file] $md" -ForegroundColor Green
    }
}

# -- templates\ --
$tmplDest = "$DIST\templates"
New-Item -ItemType Directory -Force -Path $tmplDest | Out-Null
Copy-Item "$ROOT\templates\*" $tmplDest -Force
$count = (Get-ChildItem $tmplDest -File).Count
Write-Host "  [dir]  templates\ ($count files)" -ForegroundColor Green

# -- docs\ - user-facing guides (exclude dev-only files) --
$docsSrc  = "$ROOT\docs"
$docsDest = "$DIST\docs"
New-Item -ItemType Directory -Force -Path $docsDest | Out-Null
$docsInclude = @(
    "GITHUB_GUIDE.md",
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

Write-Host ""
Write-Host "Build complete -> $DIST" -ForegroundColor Green
Write-Host ""
Write-Host "Before distributing:" -ForegroundColor Yellow
Write-Host "  1. Zip dist\ and ship (or create a GitHub Release - see docs\GITHUB_GUIDE.md)." -ForegroundColor Yellow
Write-Host "  2. Users run tokensave-manager.exe and configure paths via Settings on first run." -ForegroundColor Yellow
Write-Host ""

exit 0
