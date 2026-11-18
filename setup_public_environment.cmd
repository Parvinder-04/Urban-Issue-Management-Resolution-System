@echo off
rem First-time UIMRS public setup.
rem The execution-policy bypass applies to this PowerShell process only; it does
rem not change the machine or user execution policy.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_public_environment.ps1" %*
set "UIMRS_EXIT=%ERRORLEVEL%"
endlocal & exit /b %UIMRS_EXIT%
