@echo off
REM Checks every prerequisite and tells you exactly what is wrong.
REM Safe to run any time. It opens the ES-9 briefly, so if TouchDesigner
REM currently holds the device this check will say so.

cd /d "%~dp0"
py es9_bridge.py --config es9.toml --check
echo.
pause
