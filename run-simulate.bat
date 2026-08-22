@echo off
REM Runs the bridge on synthetic signals - no ES-9, no rack power needed.
REM The OSC output is identical to the real thing, so you can build and test
REM your TouchDesigner patch against this first.
REM
REM A fake wraparound stall is injected after 30 seconds so you can watch the
REM watchdog catch it and reopen. Look at the recovery log on the status page.

cd /d "%~dp0"
title ES-9 Bridge (SIMULATE)
py es9_bridge.py --config es9.toml --simulate --simulate-fault 30
echo.
pause
