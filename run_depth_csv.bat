@echo off
REM ---------------------------------------------------------------------------
REM Runs pipeline/depth_csv_from_sonar.py in the conda env PINGMapper lives in
REM (the same "ping" env PINGWizard.bat activates - the plain Windows python has
REM no GDAL, which PINGMapper needs).
REM
REM   run_depth_csv.bat <recording.DAT> [--out-dir DIR] [--project NAME] ...
REM ---------------------------------------------------------------------------

setlocal

set "SCRIPT_DIR=%~dp0"
REM Where conda lives. CONDA_BASE set beforehand wins; otherwise the three
REM distributions are tried under the profile, in the order the Python side
REM searches them.
if not defined CONDA_BASE (
  for %%D in (miniforge3 anaconda3 miniconda3) do (
    if exist "%USERPROFILE%\%%D\Scripts\activate.bat" (
      if not defined CONDA_BASE set "CONDA_BASE=%USERPROFILE%\%%D"
    )
  )
)
if not defined CONDA_BASE set "CONDA_BASE=%USERPROFILE%\miniforge3"
set "CONDA_ENV=ping"

if not exist "%CONDA_BASE%\Scripts\activate.bat" goto no_conda
call "%CONDA_BASE%\Scripts\activate.bat" %CONDA_ENV%

python "%SCRIPT_DIR%pipeline\depth_csv_from_sonar.py" %*
goto :eof

:no_conda
echo Could not find conda at %CONDA_BASE%.
echo Activate the environment PINGMapper is installed in, then run:
echo   python "%SCRIPT_DIR%pipeline\depth_csv_from_sonar.py" ^<recording^>
