@echo off
rem Start UIMRS against the local public environment.
rem Run setup_public_environment.cmd once before the first start.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_public_environment.ps1" %*
set "UIMRS_EXIT=%ERRORLEVEL%"
endlocal & exit /b %UIMRS_EXIT%
