<#
.SYNOPSIS
    Run a process on a private Windows desktop, and return its exit code.

.DESCRIPTION
    A window on a desktop that is not the active one cannot take the
    foreground, cannot raise itself, and is not composited onto your screen —
    so this removes the test-run focus theft completely rather than reacting to
    it. `keep-out-of-the-way.ps1` minimises windows after they appear and
    leaves a flash as long as its detection latency; this leaves nothing,
    because the window never exists anywhere you can see.

    Measured before it was built: VS Code starts and stays up on a private
    desktop (alive after 25 s, five windows drawn on that desktop, **zero** on
    the active one). Electron does not require the interactive desktop.

.NOTES
    THREE THINGS THAT ARE EASY TO GET WRONG, all found by getting them wrong.

    **A `string` P/Invoke parameter given $null arrives as an EMPTY STRING,
    not NULL.** `CreateDesktopW`'s lpszDevice must be NULL, and an empty string
    there fails with ERROR_INVALID_PARAMETER (87) — which reads as "your access
    mask is wrong" and sends you off testing access masks. Every
    must-be-NULL pointer here is declared `IntPtr` and passed `IntPtr::Zero`.

    **The child's stdout must be inherited, not given a new console.**
    `CREATE_NEW_CONSOLE` puts the test output in a console window on a desktop
    nobody can see, which is the same as losing it. `STARTF_USESTDHANDLES`
    plus this process's own handles and `bInheritHandles = TRUE` sends the
    child's output up the pipe to the launcher. Verified end to end, including
    a non-zero exit code surviving the trip.

    **The desktop cannot be closed while anything is still on it.** Electron
    spawns helper processes; killing only the parent strands them, and the
    desktop then leaks for the life of the session. The whole tree goes.

.PARAMETER SpecPath
    A JSON file: { "exe": "...", "args": ["..."], "timeoutSeconds": 600 }.
    Arguments arrive as an array and are quoted here, so nothing has to
    survive a shell — this repository's own path has spaces in it, and the
    argument list routinely contains more.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $SpecPath,
    [string] $DesktopName = ''
)

$ErrorActionPreference = 'Stop'

Add-Type -Namespace Desk -Name Api -MemberDefinition @'
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb;
        public string lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize;
        public int dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public System.IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public System.IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    // lpszDevice / pDevmode / lpsa must be NULL -- see .NOTES.
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern System.IntPtr CreateDesktopW(string name,
        System.IntPtr device, System.IntPtr devmode, int flags, uint access,
        System.IntPtr sa);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool CloseDesktop(System.IntPtr hDesktop);

    // lpApplicationName / lpCurrentDirectory likewise. The command line is a
    // StringBuilder because CreateProcessW may write to it in place.
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CreateProcessW(System.IntPtr app,
        System.Text.StringBuilder cmdline, System.IntPtr pa, System.IntPtr ta,
        bool inherit, uint flags, System.IntPtr env, System.IntPtr cwd,
        ref STARTUPINFO si, out PROCESS_INFORMATION pi);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern System.IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(System.IntPtr h, uint ms);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetExitCodeProcess(System.IntPtr h, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(System.IntPtr h);
'@

function Say([string] $Message) {
    [Console]::Out.WriteLine($Message)
    [Console]::Out.Flush()
}

function Fail([string] $What) {
    $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    throw ("{0} failed: {1} ({2})" -f $What,
           (New-Object ComponentModel.Win32Exception $code).Message, $code)
}

<#
    Quote one argument the way CommandLineToArgvW will read it back.

    Written out rather than approximated with "wrap it if it has a space",
    because a trailing backslash before the closing quote escapes that quote
    and swallows the next argument -- and every path in the argument list is a
    Windows directory, so trailing backslashes are the normal case, not an
    edge one.
#>
function Quote([string] $Argument) {
    if ($Argument -ne '' -and $Argument -notmatch '[\s"]') { return $Argument }
    $out = New-Object Text.StringBuilder '"'
    $slashes = 0
    foreach ($ch in $Argument.ToCharArray()) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') {
            [void]$out.Append('\' * ($slashes * 2 + 1)).Append('"')
        } else {
            [void]$out.Append('\' * $slashes).Append($ch)
        }
        $slashes = 0
    }
    [void]$out.Append('\' * ($slashes * 2)).Append('"')
    return $out.ToString()
}

$spec = Get-Content -Raw -Path $SpecPath | ConvertFrom-Json
if (-not $DesktopName) {
    # Unique per run: two suites running at once must not share a desktop, and
    # a leftover name from a crashed run must not be reused while its windows
    # are still on it.
    $DesktopName = 'tokensave-test-{0}-{1}' -f $PID, (Get-Random -Maximum 99999)
}
$timeoutMs = [uint32](1000 * [int]($spec.timeoutSeconds | ForEach-Object { if ($_) { $_ } else { 900 } }))

$GENERIC_ALL = [uint32]0x10000000
$hDesk = [Desk.Api]::CreateDesktopW($DesktopName, [IntPtr]::Zero, [IntPtr]::Zero,
                                    0, $GENERIC_ALL, [IntPtr]::Zero)
if ($hDesk -eq [IntPtr]::Zero) { Fail 'CreateDesktop' }
Say "[desktop] created '$DesktopName'"

$si = New-Object Desk.Api+STARTUPINFO
$si.cb = [Runtime.InteropServices.Marshal]::SizeOf([type][Desk.Api+STARTUPINFO])
$si.lpDesktop = "WinSta0\$DesktopName"
$si.dwFlags = 0x00000100                       # STARTF_USESTDHANDLES
$si.hStdInput = [Desk.Api]::GetStdHandle(-10)
$si.hStdOutput = [Desk.Api]::GetStdHandle(-11)
$si.hStdError = [Desk.Api]::GetStdHandle(-12)

$parts = @(Quote $spec.exe) + @($spec.args | ForEach-Object { Quote $_ })
$cmdline = New-Object Text.StringBuilder ($parts -join ' ')

$pi = New-Object Desk.Api+PROCESS_INFORMATION
# No creation flags: a new console would put the test output somewhere
# invisible, which is the same as discarding it.
$ok = [Desk.Api]::CreateProcessW([IntPtr]::Zero, $cmdline, [IntPtr]::Zero,
                                 [IntPtr]::Zero, $true, 0, [IntPtr]::Zero,
                                 [IntPtr]::Zero, [ref]$si, [ref]$pi)
if (-not $ok) {
    [void][Desk.Api]::CloseDesktop($hDesk)
    Fail 'CreateProcess'
}
Say "[desktop] pid $($pi.dwProcessId) running off-screen"

$wait = [Desk.Api]::WaitForSingleObject($pi.hProcess, $timeoutMs)
$exit = 1
if ($wait -eq 0) {
    $code = [uint32]0
    if ([Desk.Api]::GetExitCodeProcess($pi.hProcess, [ref]$code)) { $exit = [int]$code }
} else {
    Say "[desktop] TIMED OUT after $($timeoutMs / 1000)s; killing the tree"
    $exit = 124
}

# Electron leaves helpers behind, and a desktop with anything still on it
# cannot be closed -- it would leak for the life of the session.
#
# Two traps in three lines. `taskkill` writes to stderr when the process has
# already gone, which is the NORMAL case after a clean run; with
# $ErrorActionPreference = 'Stop' a native command's stderr becomes a
# TERMINATING error, so the launcher reported a passing suite as a failure.
# And `2>&1 | Out-Null` is what causes that, by merging stderr into the
# pipeline -- redirecting to $null keeps it out of PowerShell's hands.
if (Get-Process -Id $pi.dwProcessId -ErrorAction SilentlyContinue) {
    $ErrorActionPreference = 'Continue'
    & taskkill.exe /PID $pi.dwProcessId /T /F 2>$null | Out-Null
    $ErrorActionPreference = 'Stop'
}
Start-Sleep -Milliseconds 700

[void][Desk.Api]::CloseHandle($pi.hProcess)
[void][Desk.Api]::CloseHandle($pi.hThread)
[void][Desk.Api]::CloseDesktop($hDesk)
Say "[desktop] closed '$DesktopName' (exit $exit)"

exit $exit
