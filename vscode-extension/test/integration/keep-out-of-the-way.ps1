<#
.SYNOPSIS
    Put Extension Development Host windows back down the instant they appear,
    so a test run does not repeatedly steal the foreground.

.DESCRIPTION
    `@vscode/test-electron`'s `runTests` spawns VS Code with a plain
    `child_process.spawn` and exposes no window, position or activation
    options, so the launcher cannot be asked to start it hidden. Electron then
    shows and activates the window itself. Nothing outside the process can
    prevent that — but anything outside can put the window straight back down,
    which is what this does.

    It matters because of volume rather than any one window: a mutation run
    launches an editor per live arm — fourteen on the current set — and each
    takes the foreground away from whatever you were doing.

.NOTES
    SPEED IS THE WHOLE DESIGN. The window is visible from the moment Electron
    shows it until this puts it down, so the only thing that shrinks the flash
    is noticing sooner. Two decisions follow:

    * `EnumWindows` + `GetWindowText` through P/Invoke, NOT `Get-Process`.
      `Get-Process` builds a rich object per process and costs tens of
      milliseconds a sweep; enumerating top-level windows and reading their
      titles costs well under one. That is the difference between a poll
      interval of 120 ms and one of 15 ms, and the flash is as long as the
      interval.
    * A restore is watched for, not just the first appearance. VS Code shows
      its window, loads the extension host, then activates itself again — so a
      one-shot minimise gets undone a second later. Matched windows are put
      back down for $GraceSeconds, after which they are left alone, so this
      never fights a user who deliberately restores one.

    THE FILTER IS THE SAFETY PROPERTY. Only windows whose title contains
    "Extension Development Host" are touched. VS Code puts that marker in the
    title of a window opened for extension testing and nowhere else, so an
    editor you are working in is never matched. The one way to fool it is to
    open a file or folder literally named "Extension Development Host". That
    is preferred to matching on process id: the test host is a `Code.exe` like
    any other, so a pid filter would have to guess which one, and guessing
    wrong minimises your editor.

    A flash of a few milliseconds still remains and cannot be removed from
    here: Electron activates the window before any external process can see it
    exists.

    It is NOT fixable with HKCU:\Control Panel\Desktop\ForegroundLockTimeout,
    which is the advice usually given and which was checked before being
    repeated here — the machine this was written on already has it at 150000,
    and the windows still take focus. That setting governs whether a
    *background* process may steal the foreground. These launches are not
    background: VS Code is spawned by the terminal running the tests, and
    Windows grants foreground rights to a child of the foreground process, so
    the activation is legitimate by the rules the setting enforces.

    The complete fix is a separate window station / desktop
    (`CreateDesktop` + `STARTUPINFO.lpDesktop`), whose windows cannot take
    focus from the active desktop at all. That is a real change with real risk
    for a GPU-accelerated Electron app, and it has not been done — so what
    remains is a brief flash per launch, roughly one poll interval long.

    Set TOKENSAVE_TEST_FOCUS=1 to disable this entirely and watch a run.
#>
[CmdletBinding()]
param(
    # How long to keep watching. The launcher kills this process when the run
    # ends; the cap is a backstop so an abandoned watcher cannot outlive the
    # session that started it.
    [int] $TimeoutSeconds = 1800,

    # Window-title marker identifying a test host.
    [string] $Marker = 'Extension Development Host',

    # Poll interval. Tight, because the flash lasts exactly as long as this
    # takes to notice. Affordable only because the enumeration is cheap.
    [int] $PollMs = 15,

    # How long to keep putting a given window back down after first seeing it.
    # Covers VS Code's own re-activation once the extension host has loaded.
    [int] $GraceSeconds = 25
)

$ErrorActionPreference = 'Stop'

Add-Type -Namespace Win -Name Native -MemberDefinition @'
    public delegate bool EnumProc(System.IntPtr hWnd, System.IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumProc cb, System.IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(System.IntPtr hWnd,
        System.Text.StringBuilder text, int count);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(System.IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    public static extern bool IsIconic(System.IntPtr hWnd);

    // Visible top-level windows whose title contains `needle`.
    public static System.Collections.Generic.List<System.IntPtr> Find(string needle) {
        var hits = new System.Collections.Generic.List<System.IntPtr>();
        var buf = new System.Text.StringBuilder(512);
        EnumWindows(delegate (System.IntPtr h, System.IntPtr l) {
            if (!IsWindowVisible(h)) { return true; }
            buf.Length = 0;
            if (GetWindowTextW(h, buf, buf.Capacity) > 0 &&
                buf.ToString().IndexOf(needle, System.StringComparison.Ordinal) >= 0) {
                hits.Add(h);
            }
            return true;
        }, System.IntPtr.Zero);
        return hits;
    }

    public static string TitleOf(System.IntPtr h) {
        var buf = new System.Text.StringBuilder(512);
        GetWindowTextW(h, buf, buf.Capacity);
        return buf.ToString();
    }
'@

$SW_MINIMIZE = 6

# `Write-Output` through a redirected pipe is buffered by .NET and arrives
# only when the process exits, which makes this script undiagnosable from the
# launcher's log at exactly the moment you want to know what it is doing.
function Say([string] $Message) {
    [Console]::Out.WriteLine($Message)
    [Console]::Out.Flush()
}

# handle -> when it was first seen. See $GraceSeconds in .NOTES.
$seen = @{}
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$count = 0

Say "watching for '$Marker' windows every ${PollMs}ms (timeout ${TimeoutSeconds}s)"

while ((Get-Date) -lt $deadline) {
    try {
        foreach ($h in [Win.Native]::Find($Marker)) {
            if (-not $seen.ContainsKey($h)) { $seen[$h] = Get-Date }
            if (((Get-Date) - $seen[$h]).TotalSeconds -gt $GraceSeconds) { continue }
            if (-not [Win.Native]::IsIconic($h)) {
                [void][Win.Native]::ShowWindow($h, $SW_MINIMIZE)
                $count++
                Say "minimised: $([Win.Native]::TitleOf($h))"
            }
        }
    } catch {
        # A window can be destroyed between the enumeration and the call. That
        # is routine here, not an error worth failing a test run over -- this
        # script must never be the reason a suite goes red.
        Say "transient: $($_.Exception.Message)"
    }
    Start-Sleep -Milliseconds $PollMs
}

Say "watcher done; minimised $count window(s)"
