@echo off
rem Flowing Curve Generator -- builds screensaver.py into a real Windows
rem .scr file with PyInstaller.
rem
rem Must be run on a real Windows machine (not this sandbox) -- PyInstaller
rem builds an executable for the OS it runs on, and the screensaver relies
rem on Windows-only APIs (GetSystemPowerStatus, SetParent, the registry)
rem that only make sense there. Double-click this file, or run it from a
rem command prompt in this folder.
rem
rem Reuses the same .venv that run.bat/install.bat set up, so run this app
rem normally at least once first if you haven't already.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv found yet -- run run.bat first to set up the app normally,
    echo then come back and run this again.
    pause
    exit /b 1
)

echo Installing/updating PyInstaller in .venv ...
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo Failed to install PyInstaller -- see the message above.
    pause
    exit /b 1
)

echo.
echo Building screensaver.exe (this can take a minute or two) ...
rem --onefile: a single portable .exe/.scr with nothing else to keep track
rem of. screensaver.py/wallpaper_engine.py's BASE_DIR (see the comments in
rem both files) resolves to sys.executable's own folder once frozen, so it
rem needs to be *the* .scr file the OS actually launched -- and for a
rem PyInstaller onefile build, sys.executable stays the real, stable path
rem to that one .exe (unlike sys._MEIPASS, the throwaway per-launch
rem extraction folder, which this deliberately does NOT rely on). That's
rem also why --onedir is avoided here: it would leave the real executable
rem sitting in its own _internal-dependent subfolder instead of directly
rem beside gui.py/wallpaper_engine.py/wallpaper_config.json.
rem --windowed: no console window behind the fullscreen screensaver.
rem --hidden-import gui/organic_curve: screensaver.py's run_config() does
rem "import gui" (and gui.py imports organic_curve) from inside a function
rem body rather than at module level -- PyInstaller's static analysis
rem usually catches that too, but these make it explicit rather than
rem relying on it.
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name screensaver ^
    --hidden-import gui ^
    --hidden-import organic_curve ^
    --hidden-import wallpaper_engine ^
    screensaver.py
if errorlevel 1 (
    echo.
    echo PyInstaller build failed -- see the message above.
    pause
    exit /b 1
)

if not exist "dist\screensaver.exe" (
    echo.
    echo Build finished but dist\screensaver.exe was not found -- something
    echo went wrong. Check the output above.
    pause
    exit /b 1
)

echo.
echo Copying the built exe here as screensaver.scr, next to gui.py/
echo wallpaper_engine.py -- it needs to stay in this folder so it can find
echo your wallpaper_config.json/wallpaper_cache (same as the live wallpaper).
copy /y "dist\screensaver.exe" "screensaver.scr" >nul
if not exist "screensaver.scr" (
    echo.
    echo Could not copy the built file -- see any message above.
    pause
    exit /b 1
)

echo.
echo Done. screensaver.scr is ready in this folder.
echo.
echo Next: run install_screensaver.bat to install it, or right-click
echo screensaver.scr yourself and choose "Install".
pause
