@echo off
REM ---------------------------------------------------------------------------
REM  Make Chart Bundle - packs a built survey into one file both apps can take.
REM
REM  A bundle is a plain .zip holding everything one survey needs: tiles, the
REM  depth grid you can tap for a sounding, contours, shallow bands, the outline
REM  and the survey record. It needs no Play console and no app rebuild.
REM
REM    Make_Chart_Bundle.bat build output\indian-hills-lake
REM    Make_Chart_Bundle.bat build --all
REM    Make_Chart_Bundle.bat list
REM    Make_Chart_Bundle.bat inspect dist\charts\noatak-chart.zip
REM    Make_Chart_Bundle.bat install-web dist\charts\noatak-chart.zip
REM    Make_Chart_Bundle.bat send dist\charts\noatak-chart.zip
REM
REM  Bundles are written to dist\charts\. To use one:
REM    Phone    copy it across, then Settings > Import chart bundle
REM    Browser  Settings > Charts on this computer > Add chart from a bundle
REM  Neither route needs the other.
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

if "%~1"=="" (
    python "%~dp0pipeline\chart_bundle.py" build --all
) else (
    python "%~dp0pipeline\chart_bundle.py" %*
)
set "RESULT=%ERRORLEVEL%"

echo.
pause
exit /b %RESULT%
