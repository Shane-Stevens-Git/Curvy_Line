@echo off
rem Flowing Curve Generator -- installs the built screensaver.scr as your
rem active Windows screen saver.
rem
rem Run build_screensaver.bat first if screensaver.scr doesn't exist yet.
rem Must be run on a real Windows machine (not this sandbox).
rem
rem This writes straight to HKEY_CURRENT_USER\Control Panel\Desktop --
rem same key install.bat/install.sh's own pattern points at, and no
rem administrator rights are needed for HKCU. That's a different (simpler,
rem no-elevation) path than the standard "right-click screensaver.scr ->
rem Install", which instead copies the .scr into Windows\System32 so it
rem shows up in the Settings app's screen saver dropdown -- if you'd
rem rather have that, just do that instead of running this script; both
rem end up with the same screensaver active. This script's way activates
rem it immediately without needing to open Settings at all, but the
rem Settings dropdown may still show your previous choice (or None) since
rem it doesn't know about a .scr living outside System32 -- the screensaver
rem set here still runs either way.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "screensaver.scr" (
    echo screensaver.scr was not found in this folder.
    echo Run build_screensaver.bat first to build it.
    pause
    exit /b 1
)

set SCR_PATH=%cd%\screensaver.scr

echo Setting %SCR_PATH% as your screen saver ...
reg add "HKCU\Control Panel\Desktop" /v SCRNSAVE.EXE /t REG_SZ /d "%SCR_PATH%" /f >nul
if errorlevel 1 (
    echo.
    echo Failed to write SCRNSAVE.EXE to the registry -- see any message above.
    pause
    exit /b 1
)

reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 1 /f >nul
if errorlevel 1 (
    echo.
    echo Failed to write ScreenSaveActive to the registry -- see any message above.
    pause
    exit /b 1
)

rem Only set a default wait time if none is configured yet -- don't clobber
rem a timeout you already picked in Settings.
reg query "HKCU\Control Panel\Desktop" /v ScreenSaveTimeOut >nul 2>nul
if errorlevel 1 (
    echo No screen saver wait time was set yet -- defaulting to 10 minutes.
    reg add "HKCU\Control Panel\Desktop" /v ScreenSaveTimeOut /t REG_SZ /d 600 /f >nul
)

echo.
echo Installed. Your screen saver is now Flowing Curve Generator.
echo.
echo To test it right now without waiting for the idle timeout, double-click
echo screensaver.scr, or run: screensaver.scr /s
echo.
echo To change the wait time, or switch back to a different screen saver
echo later, use Settings -^> Personalization -^> Lock screen -^> Screen saver
echo settings as usual.
pause
