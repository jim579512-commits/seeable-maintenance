@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 share.py
) else (
  python share.py
)
echo.
pause
