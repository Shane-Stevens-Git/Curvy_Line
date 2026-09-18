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

set FIRST_RUN=0
if not exist ".venv\Scripts\python.exe" (
    set FIRST_RUN=1
    echo Setting up a virtual environment in .venv the first time this runs ...
    %PYTHON% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo Failed to create the virtual environment -- see the message above.
        pause
        exit /b 1
    )
)

rem First run only (see FIRST_RUN above) -- ask about a Desktop shortcut
rem right here rather than every launch. The app itself has the same
rem "Create Desktop Shortcut..." button (bottom of the sidebar) for anyone
rem who says no now and changes their mind later, or already cloned the
rem repo and launched via run.bat directly without ever seeing this prompt.
if "%FIRST_RUN%"=="1" (
    if exist "assets\icon.ico" (
        echo.
        choice /c YN /n /m "Create a Desktop shortcut for Flowing Curve Generator? [Y,N] "
        if errorlevel 2 (
            echo Skipping -- you can add one later from the app itself.
        ) else (
            call :make_shortcut
        )
        echo.
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
exit /b 0

rem Adds a "Flowing Curve Generator.lnk" Desktop shortcut targeting this
rem same run.bat, using assets\icon.ico -- same approach (and same result)
rem as gui.py's own "Create Desktop Shortcut..." button (_create_desktop_
rem shortcut): a small generated VBScript run through cscript.exe, since
rem WScript.Shell's CreateShortcut is the standard way to make a real
rem Windows .lnk without any extra tools or dependencies.
:make_shortcut
set SHORTCUT_VBS=%TEMP%\fcg_make_shortcut.vbs
> "%SHORTCUT_VBS%" (
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = oWS.SpecialFolders^("Desktop"^) ^& "\Flowing Curve Generator.lnk"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%~dp0run.bat"
    echo oLink.WorkingDirectory = "%~dp0"
    echo oLink.IconLocation = "%~dp0assets\icon.ico"
    echo oLink.Description = "Flowing Curve Generator"
    echo oLink.Save
)
cscript //nologo "%SHORTCUT_VBS%" >nul 2>nul
if errorlevel 1 (
    echo Could not create the Desktop shortcut -- you can still add one later
    echo from the app itself ^("Create Desktop Shortcut..." at the bottom of
    echo the sidebar^).
) else (
    echo Added "Flowing Curve Generator" to your Desktop.
)
del "%SHORTCUT_VBS%" >nul 2>nul
goto :eof
