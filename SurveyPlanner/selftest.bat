@echo off
REM ---------------------------------------------------------------
REM  Survey Planner - self test, no window
REM  Runs fetch, plan, verify and export against a real lake.
REM  Optional: selftest.bat 44.6 -93.2   to test somewhere else.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on the PATH.
  pause
  exit /b 1
)

python selftest.py %1 %2
set RESULT=%errorlevel%
echo.
if "%RESULT%"=="0" (
  echo Self test passed.
) else (
  echo Self test FAILED - see the output above.
)
echo.
pause
exit /b %RESULT%
