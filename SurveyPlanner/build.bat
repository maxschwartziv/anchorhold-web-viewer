@echo off
REM Build Survey Planner into a single .exe. Run from this folder.
REM Needs: pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed ^
  --name SurveyPlanner ^
  --icon icon.png ^
  --collect-submodules shapely ^
  --collect-data matplotlib ^
  --hidden-import skimage.graph ^
  survey_planner.py
echo.
echo Built dist\SurveyPlanner.exe
pause
