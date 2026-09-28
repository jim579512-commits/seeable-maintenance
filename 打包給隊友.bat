@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 pack_for_team.py
) else (
  python pack_for_team.py
)
echo.
pause
