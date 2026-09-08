@echo off
REM ---------------------------------------------------------------------------
REM  Build the browser app's release bundle: the zips you attach to a GitHub
REM  release, plus the release notes that describe them.
REM
REM    build_web_release.bat                    app only (a few MB)
REM    build_web_release.bat -all               app + every chart in the library
REM    build_web_release.bat -charts noatak     app + the charts you name
REM    build_web_release.bat -version 1.1.0
REM
REM  Everything lands in dist\release\. Charts ship as one zip per survey, so
REM  nobody downloads water they will never sail, and no single asset comes
REM  near the 2 GB limit a release asset has.
REM
REM  NOTE: keep this file plain ASCII. cmd reads .bat in the OEM codepage, and
REM  UTF-8 punctuation corrupts the lines it appears on.
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "ARGS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="-all" (
    set "ARGS=!ARGS! --charts all"
    shift
    goto parse
)
if /i "%~1"=="-charts" (
    REM cmd splits unquoted arguments on commas, so "-charts a,b" arrives as
    REM two arguments; take the first and absorb what follows.
    set "PICKED=%~2"
    shift
    shift
    goto moreCharts
)
if /i "%~1"=="-version" (
    set "ARGS=!ARGS! --version %~2"
    shift
    shift
    goto parse
)
if /i "%~1"=="-out" (
    set "ARGS=!ARGS! --out "%~2""
    shift
    shift
    goto parse
)
echo Unknown option: %~1
echo Run with no arguments for the app on its own, or read the header.
exit /b 1

:moreCharts
if "%~1"=="" goto chartsDone
set "TOKEN=%~1"
if "!TOKEN:~0,1!"=="-" goto chartsDone
set "PICKED=!PICKED!,!TOKEN!"
shift
goto moreCharts
:chartsDone
set "ARGS=!ARGS! --charts !PICKED!"
goto parse

:parsed

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

python "%~dp0pipeline\release_web.py" !ARGS!
set "RESULT=%ERRORLEVEL%"

echo.
pause
exit /b %RESULT%
