@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_doc_sync.ps1" %*
if errorlevel 1 (
    echo.
    echo See logs\startup.log for startup details.
    pause
    exit /b 1
)
