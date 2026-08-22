@echo off
REM Installs the three packages the bridge needs. Run this once.
REM Double-click it, or run it from a terminal in this folder.

echo.
echo ============================================
echo   ES-9 Bridge - installing Python packages
echo ============================================
echo.

py --version >nul 2>&1
if errorlevel 1 goto nopython

py --version
echo.
py -m pip install --upgrade pip
py -m pip install sounddevice python-osc numpy
if errorlevel 1 goto pipfailed

echo.
echo Done. Now run 2-check.bat
echo.
pause
exit /b 0

:nopython
echo.
echo   Python was not found.
echo.
echo   TouchDesigner's built-in Python does not count - it is not on your PATH
echo   and you should not install packages into it.
echo.
echo   1. Download Python 3.13 from https://www.python.org/downloads/windows/
echo   2. Run the installer and TICK "Add python.exe to PATH" on the first screen
echo   3. Close this window, open a NEW one, and run 1-setup.bat again
echo.
echo   If you already installed Python and still see this message, Windows may be
echo   intercepting the command. Go to:
echo     Settings ^> Apps ^> Advanced app settings ^> App execution aliases
echo   and turn OFF the entries for python.exe and python3.exe
echo.
pause
exit /b 1

:pipfailed
echo.
echo   pip failed. Common causes:
echo     - no internet connection, or a corporate proxy blocking pypi.org
echo     - a very old pip: try  py -m pip install --upgrade pip
echo.
pause
exit /b 1
