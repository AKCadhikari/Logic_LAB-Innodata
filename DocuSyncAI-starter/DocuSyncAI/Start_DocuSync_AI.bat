@echo off
setlocal
set "DocuSyncLauncher=%~dp0..\..\hackathon-project\Start_DocuSync_AI.bat"
if not exist "%DocuSyncLauncher%" (
    echo The DocuSync workspace launcher is missing. Keep this folder and hackathon-project in the same repository checkout.
    pause
    exit /b 1
)
call "%DocuSyncLauncher%" %*
exit /b %ERRORLEVEL%
