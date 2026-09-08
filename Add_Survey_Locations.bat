@echo off
REM Opens Add Survey Locations (pipeline/add_survey_locations.py).
REM Uses the same interpreter the pipeline runs on - it needs the packages in
REM pipeline/requirements.txt for the build step.

setlocal
set "SCRIPT_DIR=%~dp0"
python "%SCRIPT_DIR%pipeline\add_survey_locations.py" %*
