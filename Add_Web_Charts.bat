@echo off
REM ---------------------------------------------------------------------------
REM  Add Web Charts - SUPERSEDED. The browser app does all of this itself now:
REM
REM    run_web_app.bat  ->  Settings  ->  Charts on this computer
REM
REM  There you can add any survey built under output\, add one from a bundle
REM  .zip, refresh a chart whose build has moved on, choose which chart the app
REM  opens on, and remove one - without a command line, and without stopping
REM  the server first, since it lets go of the files itself.
REM
REM  This still works, for scripting and for when the server is not running:
REM    Add_Web_Charts.bat list
REM    Add_Web_Charts.bat add output\creve-coeur-lake2
REM    Add_Web_Charts.bat remove creve-coeur-lake2
REM    Add_Web_Charts.bat default noatak
REM
REM  Charts are hard-linked, so adding one costs no extra disk. They land in
REM  web_charts\, which is the only place the browser app reads.
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

echo.
echo   The browser app can do all of this itself now:
echo   run_web_app.bat, then Settings, Charts on this computer.
echo.

python "%~dp0pipeline\web_charts.py" %*
set "RESULT=%ERRORLEVEL%"

echo.
pause
exit /b %RESULT%
