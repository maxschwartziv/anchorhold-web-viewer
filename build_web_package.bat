@echo off
REM Build a standalone copy of the web app (charts included) in dist\anchoring-web,
REM ready to copy onto another PC. Add --zip for an archive as well.

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

python pipeline\package_web.py %*
pause
