@echo off
REM ---------------------------------------------------------------
REM  Survey Planner - launch the application
REM  Double-click this file. It installs what is missing on the
REM  first run, then opens the window.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on the PATH.
  echo Install Python 3.10 or newer from python.org, ticking
  echo "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

python -c "import shapely, numpy, matplotlib, skimage" >nul 2>&1
if errorlevel 1 (
  echo First run - installing dependencies. This takes a minute.
  echo.
  python -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Install failed. Try running this by hand to see why:
    echo     python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
  echo Done.
  echo.
)

echo Starting Survey Planner...
python survey_planner.py
if errorlevel 1 (
  echo.
  echo The application exited with an error - the message is above.
  echo.
  pause
  exit /b 1
)
endlocal
