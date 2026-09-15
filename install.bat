@echo off
rem Flowing Curve Generator -- Windows one-click installer.
rem
rem Meant to be downloaded and double-clicked on its own (e.g. straight from
rem the project's docs page) -- it fetches the whole project into a folder
rem next to itself, then hands off to that copy's own run.bat to finish
rem setting up a virtual environment, install dependencies, and start the
rem app. Safe to run again later: it updates the existing copy instead of
rem re-downloading everything.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set REPO_URL=https://github.com/Shane-Stevens-Git/Curvy_Line.git
set ZIP_URL=https://github.com/Shane-Stevens-Git/Curvy_Line/archive/refs/heads/main.zip
set TARGET=FlowingCurveGenerator

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

where git >nul 2>nul
if %errorlevel%==0 (
    if exist "%TARGET%\.git" (
        echo Updating the existing copy in %TARGET% ...
        git -C "%TARGET%" pull --ff-only
    ) else (
        echo Downloading Flowing Curve Generator into %TARGET% ...
        git clone --depth 1 %REPO_URL% "%TARGET%"
    )
    if errorlevel 1 (
        echo.
        echo Could not download/update the project -- see the message above.
        pause
        exit /b 1
    )
) else (
    echo git was not found, so downloading a zip of the project instead ...
    if exist "%TARGET%" (
        echo A "%TARGET%" folder already exists here -- using it as-is.
        echo ^(Delete that folder first if you want a completely fresh copy.^)
    ) else (
        curl -L -o curvyline.zip "%ZIP_URL%"
        if errorlevel 1 (
            echo.
            echo Download failed -- check your internet connection and try again.
            pause
            exit /b 1
        )
        tar -xf curvyline.zip
        if errorlevel 1 (
            echo.
            echo Could not extract the downloaded zip.
            pause
            exit /b 1
        )
        move "Curvy_Line-main" "%TARGET%" >nul
        del curvyline.zip
    )
)

if not exist "%TARGET%\run.bat" (
    echo.
    echo Something went wrong -- %TARGET%\run.bat was not found after downloading.
    pause
    exit /b 1
)

echo.
echo Handing off to %TARGET%\run.bat to finish setup and start the app ...
echo.
cd "%TARGET%"
call run.bat
