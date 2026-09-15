@echo off
rem Flowing Curve Generator -- Windows launcher.
rem Double-click this file to set up (first run only) and start the app.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PYTHON=
where py >nul 2>nul
if %errorlevel%==0 set PYTHON=py -3
if "%PYTHON%"=="" (
    where python >nul 2>nul
    if %errorlevel%==0 set PYTHON=python
)
if "%PYTHON%"=="" (
    echo Could not find Python on your PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH" during install^), then double-click this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Setting up a virtual environment in .venv the first time this runs ...
    %PYTHON% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo Failed to create the virtual environment -- see the message above.
        pause
        exit /b 1
    )
)

echo Installing/updating dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Failed to install dependencies -- see the message above.
    pause
    exit /b 1
)

echo Starting Flowing Curve Generator ...
".venv\Scripts\python.exe" gui.py
if errorlevel 1 (
    echo.
    echo The app exited with an error -- see the message above.
    pause
)
