@echo off
for /f "delims=" %%P in ('powershell -NoProfile -Command "(Get-Content '%~dp0manager-config.json' | ConvertFrom-Json).python_exe"') do set PYEXE=%%P
start "" "%PYEXE%" "%~dp0src\tokensave-manager.py"
