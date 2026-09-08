@echo off
REM Serve AnchorHold Web Viewer on this machine.
REM Charts come from this PC's web chart library, web_charts\. Add a survey
REM in the app: Settings, then Charts on this computer.

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

start "" http://localhost:8000/
python pipeline\web_server.py --port 8000
pause
