# UIMRS Windows development launcher
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot"

$python = Join-Path $PSScriptRoot "backend\venv\Scripts\python.exe"
if (!(Test-Path $python)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    py -3 -m venv backend\venv
}

& $python -m pip install --upgrade pip setuptools wheel | Out-Host
& $python -m pip install -r backend\requirements.txt | Out-Host

$env:PYTHONPATH = (Join-Path $PSScriptRoot "backend")

if (-not $env:SECRET_KEY) {
    $env:SECRET_KEY = "uimrs-local-development-secret-only-not-for-production"
    Write-Host "Using the documented development-only SECRET_KEY." -ForegroundColor Yellow
}
if (-not $env:UIMRS_FRONTEND_MODE) {
    $env:UIMRS_FRONTEND_MODE = "india"
}
if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = "sqlite:///$((Join-Path $PSScriptRoot 'uimrs_india_dev.db').Replace('\','/'))"
}

Write-Host "Validating application configuration and database..." -ForegroundColor Cyan
& $python -c "import sys; sys.path.insert(0, 'backend'); from app.core.config import get_settings; from app.database import engine; from app.db_guard import guard_startup; from app.db_migrate import current_revision, HEAD_REVISION; s=get_settings(); state=guard_startup(engine, s.DATABASE_URL); rev=current_revision(s.DATABASE_URL); print('frontend mode:', __import__('os').getenv('UIMRS_FRONTEND_MODE')); print('database state:', state); print('database revision:', rev); print('expected revision:', HEAD_REVISION); assert state == 'validated' and rev == HEAD_REVISION" 
if ($LASTEXITCODE -ne 0) {
    Write-Host "Configuration or database validation failed. Review the error above." -ForegroundColor Red
    exit 1
}

$hostName = if ($env:UIMRS_HOST) { $env:UIMRS_HOST } else { "127.0.0.1" }
$port = if ($env:UIMRS_PORT) { $env:UIMRS_PORT } else { "8000" }

Write-Host "Starting UIMRS development server on http://$hostName`:$port" -ForegroundColor Green
& $python -m uvicorn app.main:app --app-dir backend --host $hostName --port $port --reload
