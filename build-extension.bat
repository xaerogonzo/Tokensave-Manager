@echo off
REM Launcher for build-extension.ps1 - builds and verifies the VS Code extension.
REM
REM %* forwards switches (-Install, -SkipTests) through to PowerShell.
REM
REM The ERRORLEVEL is captured BEFORE pause. pause is a command and it
REM always succeeds, so ending on it would make this batch exit 0 even
REM when the build failed - invisible to a human watching the console,
REM and wrong for every caller that reads the exit code.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0build-extension.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
