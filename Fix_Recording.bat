@echo off
REM ---------------------------------------------------------------------------
REM  Recording Fixer - look at a sonar recording before it becomes a chart.
REM
REM  Rebuilds a .DAT header and a truncated index, flags outlier depths and
REM  fixes, and draws the trackline over satellite imagery so stretches of it
REM  can be kept or rejected by dragging a box. Writes a new recording; the
REM  files off the card are never changed.
REM
REM    Fix_Recording.bat                     pick the recording in the window
REM    Fix_Recording.bat R00021.DAT          open that one straight away
REM    Fix_Recording.bat R00021.DAT --report no window, just what is wrong
REM
REM  Needs the packages in pipeline\requirements.txt (numpy, matplotlib, Pillow).
REM
REM  NOTE: keep this file plain ASCII. cmd reads .bat in the OEM codepage, and
REM  UTF-8 punctuation corrupts the lines it appears on.
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

python "%~dp0pipeline\fix_recording.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    pause
)
exit /b %RESULT%
