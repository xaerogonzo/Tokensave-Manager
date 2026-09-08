# build-extension.ps1 - TokenSave Manager VS Code extension build
#
# Produces a verified .vsix in vscode-extension\ and, with -Install, puts it in
# your editor.
#
#   .\build-extension.ps1                 clean build + verify
#   .\build-extension.ps1 -Install        ... and install it
#   .\build-extension.ps1 -SkipTests      ... without running the suite
#
# WHY THIS EXISTS
#
# The same pipeline already ran in .github\workflows\release-extension.yml, and
# only there. On a developer machine the only route to a .vsix was hand-running
# vsce and trusting yourself, so the installed extension drifted three minor
# versions behind APP_VERSION without anything being able to say so. This is
# that workflow, runnable locally, step for step - deliberately the same
# commands so local and CI cannot diverge.
#
# WHAT IS DELIBERATE
#
#   * npm ci, never npm install. A lockfile that disagrees with package.json
#     must fail loudly; that disagreement IS the bug, not an inconvenience.
#   * out\ is deleted before compiling. tsc does not remove the .js of a .ts
#     you deleted, and .vscodeignore keeps out\**\*.js, so a stale module would
#     ship forever.
#   * Any existing .vsix is deleted first. verify_vsix.py refuses to run with
#     two present (uploading the wrong one is the mistake it exists to stop),
#     so removing them here makes that condition unreachable rather than fatal.
#   * Verification failure is a build failure. The verifier's exit code is this
#     script's exit code; there is no path where a package is produced, judged
#     unfit, and still reported as a success.
#
# NETWORK: npm ci reaches the registry. Compile, tests, packaging and
# verification are local, so a warm node_modules makes this essentially offline.

[CmdletBinding()]
param(
    # The editor used by -Install. The Manager stores this as editor_cmd and it
    # is not necessarily VS Code, so the GUI passes its configured value through
    # rather than this script assuming "code".
    [string]$EditorCmd = "code",
    [switch]$Install,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# StrictMode treats a never-yet-set automatic variable as an error, and
# $LASTEXITCODE does not exist until the first native command has run.
$global:LASTEXITCODE = 0

$ROOT     = $PSScriptRoot
$EXT      = Join-Path $ROOT "vscode-extension"
$VERIFIER = Join-Path $ROOT ".github\scripts\verify_vsix.py"

# ---------- Pre-flight --------------------------------------------------------
# Each probe throws with the fix in the message. A build that dies six steps in
# because node is missing has already buried the interesting part of its output.

function Get-ToolPath($name, $fix) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "$name is not on PATH. $fix" }
    return $cmd.Source
}

# npm.cmd rather than plain npm: on this platform npm resolves to npm.ps1, a
# PowerShell script, whose exit status reaches the caller less predictably than
# a native command's. The .cmd is a process, so $LASTEXITCODE is its real code.
$NODE = Get-ToolPath "node"    "Install Node 20 or newer from https://nodejs.org."
$NPM  = Get-ToolPath "npm.cmd" "It ships with Node - reinstall Node if it is absent."
$PY   = Get-ToolPath "python"  "The artefact verifier is a Python script."

$nodeVersion = (& $NODE --version).TrimStart("v")
$nodeMajor   = [int](($nodeVersion -split '\.')[0])
if ($nodeMajor -lt 20) {
    throw "Node $nodeVersion is too old; the extension declares node >=20. Upgrade Node."
}

if (-not (Test-Path $EXT))      { throw "no vscode-extension\ directory beside this script" }
if (-not (Test-Path $VERIFIER)) { throw "missing artefact verifier at $VERIFIER" }

# ---------- Step runner -------------------------------------------------------

function Invoke-Step {
    <#
      Runs one native command, streaming its output, and throws on a non-zero
      exit. $Arguments is an array on purpose: this repository lives under
      "D:\Claude Co worker\Token Save Manager Source", and a concatenated
      command string would split that path on its spaces.
    #>
    param(
        [Parameter(Mandatory)][string]   $Name,
        [Parameter(Mandatory)][string]   $Exe,
        [Parameter(Mandatory)][string[]] $Arguments,
        [string] $WorkDir = $ROOT
    )

    Write-Host ""
    Write-Host "  -> $Name" -ForegroundColor Cyan

    # npm and tsc write progress to stderr. Under $ErrorActionPreference =
    # "Stop" every stderr line from a native command becomes a
    # NativeCommandError and aborts the script, so Stop is suspended for the
    # duration of the call and restored immediately afterwards.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Push-Location $WorkDir
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "     $_" }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEAP
    }

    if ($code -ne 0) { throw "$Name failed (exit $code)" }
}

# ---------- Build -------------------------------------------------------------

Write-Host ""
Write-Host "=== TokenSave Manager - VS Code extension build ===" -ForegroundColor Cyan
Write-Host "    node $nodeVersion" -ForegroundColor DarkGray

Invoke-Step "Install locked dependencies (npm ci)" $NPM @("ci") $EXT

$out = Join-Path $EXT "out"
if (Test-Path $out) {
    Write-Host ""
    Write-Host "  -> Clean out\" -ForegroundColor Cyan
    Remove-Item $out -Recurse -Force
    Write-Host "     removed $out" -ForegroundColor DarkGray
}

Invoke-Step "Compile" $NPM @("run", "compile") $EXT

$testsRan = $false
if ($SkipTests) {
    Write-Host ""
    Write-Host "  [WARN] -SkipTests: the suite did NOT run. The artefact is" -ForegroundColor Yellow
    Write-Host "         verified for its contents, not for its behaviour."  -ForegroundColor Yellow
} else {
    Invoke-Step "Test" $NPM @("test") $EXT
    $testsRan = $true
}

# Exactly one artefact must exist when the verifier looks.
$stale = @(Get-ChildItem -Path $EXT -Filter "*.vsix" -File -ErrorAction SilentlyContinue)
if ($stale.Count -gt 0) {
    Write-Host ""
    Write-Host "  -> Clear previous artefacts" -ForegroundColor Cyan
    foreach ($old in $stale) {
        Remove-Item $old.FullName -Force
        Write-Host "     removed $($old.Name)" -ForegroundColor DarkGray
    }
}

Invoke-Step "Package" $NPM @("run", "package") $EXT

# ---------- Verify ------------------------------------------------------------
# Run from the repository root: verify_vsix.py resolves vscode-extension\ from
# its own location but imports constants by putting src\ on sys.path, so the
# working directory must not be somewhere that shadows it.

Invoke-Step "Verify the artefact" $PY @($VERIFIER) $ROOT

$built = @(Get-ChildItem -Path $EXT -Filter "*.vsix" -File)[0]

# ---------- Install (opt-in) --------------------------------------------------

$installedVersion = $null
if ($Install) {
    Invoke-Step "Install into $EditorCmd" $EditorCmd @(
        "--install-extension", $built.FullName, "--force") $EXT

    # Read the version back rather than reporting the one we meant to install.
    # The last line of this script should be evidence, not a claim.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $listing = & $EditorCmd --list-extensions --show-versions 2>&1
    $ErrorActionPreference = $prevEAP

    foreach ($line in $listing) {
        if ("$line" -match '^tokensave\.tokensave-manager@(.+)$') {
            $installedVersion = $Matches[1]
        }
    }
}

# ---------- Summary -----------------------------------------------------------
# Tests and artefact are reported on separate lines so that -SkipTests cannot
# read as a clean bill of health.

Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
if ($testsRan) {
    Write-Host "    Tests:     PASSED"  -ForegroundColor Green
} else {
    Write-Host "    Tests:     SKIPPED" -ForegroundColor Yellow
}
Write-Host "    Artifact:  VERIFIED" -ForegroundColor Green
Write-Host "    Path:      $($built.FullName)"
Write-Host "    Size:      $([math]::Round($built.Length / 1KB, 1)) KB"
if ($Install) {
    if ($installedVersion) {
        Write-Host "    Installed: tokensave.tokensave-manager@$installedVersion" -ForegroundColor Green
    } else {
        Write-Host "    Installed: could not read the version back from $EditorCmd" -ForegroundColor Yellow
    }
} else {
    Write-Host "    Installed: not requested (pass -Install)" -ForegroundColor DarkGray
}
Write-Host ""

exit 0
