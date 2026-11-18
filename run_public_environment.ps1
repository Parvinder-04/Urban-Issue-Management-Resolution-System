<#
.SYNOPSIS
    Start UIMRS against the local public environment.

.DESCRIPTION
    Derives every path from this script's location, selects
    local_data\uimrs_public.db and local_data\uploads, reads the local SECRET_KEY
    from local_data\.secret_key, verifies the database without applying
    migrations, and then starts Uvicorn.

    The script never falls back to a private historical database and never
    prints the SECRET_KEY.

.PARAMETER Port
    Local TCP port. Defaults to 8000.

.PARAMETER HostName
    Bind address. Defaults to 127.0.0.1.

.PARAMETER PrepareOnly
    Validate the environment and exit without starting the server.

.PARAMETER Production
    Set UIMRS_ENV=production and require the production secret rules.

.EXAMPLE
    .\start_public_environment.cmd

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\run_public_environment.ps1 -Port 8001
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1",
    [switch]$PrepareOnly,
    [switch]$Production
)

$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$backendDir = Join-Path $projectRoot "backend"
$venvPython = Join-Path $backendDir "venv\Scripts\python.exe"
$localData = Join-Path $projectRoot "local_data"
$dbPath = Join-Path $localData "uimrs_public.db"
$uploadsDir = Join-Path $localData "uploads"
$secretPath = Join-Path $localData ".secret_key"
$validator = Join-Path $projectRoot "scripts\verify_public_startup.py"
$databaseUrl = "sqlite:///$($dbPath.Replace('\', '/'))"

function Stop-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

Write-Host "UIMRS public environment" -ForegroundColor Cyan
Write-Host "  project root: $projectRoot"
Write-Host "  database:     $dbPath"
Write-Host "  evidence:     $uploadsDir"
Write-Host "  bind address: ${HostName}:${Port}"
Write-Host ""

if (!(Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Stop-WithError "The virtual environment is missing: $venvPython. Run .\setup_public_environment.cmd first."
}
if (!(Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    Stop-WithError "Public database not found: $dbPath. Run .\setup_public_environment.cmd first."
}
if (!(Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    Stop-WithError "Local SECRET_KEY not found: $secretPath. Run .\setup_public_environment.cmd first."
}

$secret = (Get-Content -LiteralPath $secretPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($secret) -or $secret.Length -lt 32) {
    Stop-WithError "The stored SECRET_KEY is missing or too short. Delete '$secretPath' only if you accept that existing sessions will be invalidated, then run setup again."
}

if (!(Test-Path -LiteralPath $uploadsDir -PathType Container)) {
    New-Item -ItemType Directory -Path $uploadsDir -Force | Out-Null
}
if (!(Test-Path -LiteralPath (Join-Path $uploadsDir "evidence") -PathType Container)) {
    New-Item -ItemType Directory -Path (Join-Path $uploadsDir "evidence") -Force | Out-Null
}

$env:DATABASE_URL = $databaseUrl
$env:UPLOAD_DIR = $uploadsDir
$env:UIMRS_FRONTEND_MODE = "india"
$env:UIMRS_HOST = $HostName
$env:UIMRS_PORT = "$Port"
$env:SECRET_KEY = $secret
$env:PYTHONPATH = $backendDir
if ($Production) {
    $env:UIMRS_ENV = "production"
} elseif (-not $env:UIMRS_ENV) {
    $env:UIMRS_ENV = "development"
}

if (!(Test-Path -LiteralPath $validator -PathType Leaf)) {
    Stop-WithError "Startup validator missing: $validator"
}

Write-Host "Validating the public environment..." -ForegroundColor Cyan
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$validationOutput = & $venvPython -B $validator 2>&1 | ForEach-Object { "$_" }
$validationExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
$validationOutput | ForEach-Object { Write-Host "  $_" }
if ($validationExit -ne 0) {
    Stop-WithError "Startup validation failed. Review the messages above."
}

if ($PrepareOnly) {
    Write-Host ""
    Write-Host "Validation complete. The server was not started (-PrepareOnly)." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "Starting UIMRS on http://${HostName}:${Port}" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

& $venvPython -m uvicorn app.main:app --app-dir $backendDir --host $HostName --port $Port
exit $LASTEXITCODE
