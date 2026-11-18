# UIMRS Windows launcher (public release compatibility alias).
# The public release uses one launcher for development and production-style
# local runs. This file exists so deployment checks and familiar commands keep
# working. Prefer start_public_environment.cmd.
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot"

$python = Join-Path $PSScriptRoot "backend\venv\Scripts\python.exe"
if (!(Test-Path $python)) {
    Write-Host "Virtual environment not found. Run .\setup_public_environment.cmd first." -ForegroundColor Red
    exit 1
}

& (Join-Path $PSScriptRoot "run_public_environment.ps1") @args
exit $LASTEXITCODE
