@echo off
REM Starts the bridge. Leave this window open while you work.
REM Status page: http://localhost:8080
REM Close the window or press Ctrl-C to stop.

cd /d "%~dp0"
title ES-9 Bridge
py es9_bridge.py --config es9.toml
echo.
echo Bridge stopped.
pause
