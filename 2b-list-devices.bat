@echo off
REM Lists every input device, grouped by host API.
REM Useful when the preflight can't find the ES-9 under ASIO - the ASIO driver
REM is named after the DRIVER, not the Windows device, so it may not contain
REM "ES-9" at all. Whatever name you see here goes in es9.toml as device.name.
REM
REM If the ASIO section says "no input devices enumerated", close TouchDesigner
REM and run this again - an ASIO driver cannot be listed while another app holds it.

cd /d "%~dp0"
py es9_bridge.py --list-devices
echo.
pause
