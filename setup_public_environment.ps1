<#
.SYNOPSIS
    First-time UIMRS public setup.

.DESCRIPTION
    Creates a local virtual environment in backend\venv, installs the pinned
    dependencies, creates local_data\, applies the Alembic migrations to a new
    empty SQLite database, seeds the verified starter department directory,
    generates a fresh local SECRET_KEY and validates the result.

    The script never copies a historical complaint database, model artifact,
    account, password, JWT or secret from another machine. Every path is derived
    from this script's location, so the extracted folder can live anywhere.

    The script is safe to re-run: an existing virtual environment, database,
    directory rows and secret are reused instead of overwritten.

.PARAMETER SkipDependencyInstall
    Skip the pip install step (useful when the environment is already ready).

.PARAMETER RecreateDatabase
    Delete only local_data\uimrs_public.db, then migrate and seed it again.
    Requires the confirmation phrase. The local secret is kept.

.EXAMPLE
    .\setup_public_environment.cmd

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_public_environment.ps1 -SkipDependencyInstall
#>
[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall,
    [switch]$RecreateDatabase
)

$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$backendDir = Join-Path $projectRoot "backend"
$venvDir = Join-Path $backendDir "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$requirements = Join-Path $backendDir "requirements.txt"
$localData = Join-Path $projectRoot "local_data"
$dbPath = Join-Path $localData "uimrs_public.db"
$uploadsDir = Join-Path $localData "uploads"
$secretPath = Join-Path $localData ".secret_key"
$migrate = Join-Path $projectRoot "scripts\migrate_database.py"
$seed = Join-Path $projectRoot "scripts\seed_public_departments.py"
$validator = Join-Path $projectRoot "scripts\verify_public_startup.py"
$databaseUrl = "sqlite:///$($dbPath.Replace('\', '/'))"

function Stop-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

Write-Host "UIMRS public setup" -ForegroundColor Green
Write-Host "  project root: $projectRoot"
Write-Host "  database:     $dbPath"
Write-Host "  evidence:     $uploadsDir"
Write-Host ""

if (!(Test-Path -LiteralPath $requirements -PathType Leaf)) {
    Stop-WithError "backend\requirements.txt is missing. Re-extract the public release and try again."
}

# ---------------------------------------------------------------------------
# 1. Locate a suitable Python interpreter (3.11 to 3.13).
# ---------------------------------------------------------------------------
Write-Step "Checking Python"

$basePython = $null
$baseArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($candidate in @("-3.13", "-3.12", "-3.11")) {
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & py $candidate -c "import sys" 2>&1 | Out-Null
        $candidateOk = $LASTEXITCODE -eq 0
        $ErrorActionPreference = $previousErrorAction
        if ($candidateOk) {
            $basePython = "py"
            $baseArgs = @($candidate)
            break
        }
    }
    if (!$basePython) {
        $basePython = "py"
        $baseArgs = @("-3")
    }
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $basePython = "python"
} else {
    Stop-WithError "Python was not found. Install Python 3.13 from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
}

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$versionText = (& $basePython @baseArgs -c "import sys; print('.'.join(str(p) for p in sys.version_info[:3]))" 2>&1 | ForEach-Object { "$_" })
$versionExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($versionExit -ne 0) {
    Stop-WithError "Could not run the Python interpreter. Install Python 3.13 and try again."
}
Write-Host "  interpreter: $basePython $($baseArgs -join ' ')"
Write-Host "  version:     $versionText"

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $basePython @baseArgs -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" 2>&1 | Out-Null
$versionOk = $LASTEXITCODE -eq 0
$ErrorActionPreference = $previousErrorAction
if (!$versionOk) {
    Stop-WithError "Python 3.11 to 3.13 is required (the pinned dependencies are verified on 3.13). Found $versionText. Install Python 3.13 from https://www.python.org/downloads/ and run setup again."
}

# ---------------------------------------------------------------------------
# 2. Create the local virtual environment.
# ---------------------------------------------------------------------------
Write-Step "Preparing backend\venv"

if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    Write-Host "  Reusing the existing virtual environment."
} else {
    if (Test-Path -LiteralPath $venvDir) {
        Stop-WithError "backend\venv exists but is incomplete. Delete the backend\venv folder and run setup again."
    }
    & $basePython @baseArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Stop-WithError "Could not create the virtual environment in backend\venv."
    }
    Write-Host "  Created backend\venv."
}

# ---------------------------------------------------------------------------
# 3. Install the pinned dependencies.
# ---------------------------------------------------------------------------
if ($SkipDependencyInstall) {
    Write-Step "Skipping dependency installation (-SkipDependencyInstall)"
} else {
    Write-Step "Installing backend dependencies (this can take a few minutes)"
    & $venvPython -m pip install --upgrade pip setuptools wheel --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Could not upgrade pip inside backend\venv."
    }
    & $venvPython -m pip install -r $requirements --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Dependency installation failed. Check the network connection and run setup again."
    }
    Write-Host "  Dependencies installed." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 4. Prepare local data directories.
# ---------------------------------------------------------------------------
Write-Step "Preparing local_data"

if (!(Test-Path -LiteralPath $localData -PathType Container)) {
    New-Item -ItemType Directory -Path $localData -Force | Out-Null
}
if (!(Test-Path -LiteralPath $uploadsDir -PathType Container)) {
    New-Item -ItemType Directory -Path $uploadsDir -Force | Out-Null
}
if (!(Test-Path -LiteralPath (Join-Path $uploadsDir "evidence") -PathType Container)) {
    New-Item -ItemType Directory -Path (Join-Path $uploadsDir "evidence") -Force | Out-Null
}
Write-Host "  local_data and evidence directories are ready."

# ---------------------------------------------------------------------------
# 5. Create or migrate the empty database.
# ---------------------------------------------------------------------------
Write-Step "Applying Alembic migrations"

if ($RecreateDatabase) {
    Write-Host "  This deletes only local_data\uimrs_public.db." -ForegroundColor Yellow
    $answer = Read-Host "Type 'RESET PUBLIC DATABASE' to continue"
    if ($answer -ne "RESET PUBLIC DATABASE") {
        Stop-WithError "Not confirmed. Nothing was deleted."
    }
    foreach ($suffix in @("", "-journal", "-wal", "-shm")) {
        $candidate = "$dbPath$suffix"
        if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Force }
    }
}

& $venvPython -B $migrate --database-url $databaseUrl
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Database migration failed. Review the messages above."
}

Write-Step "Seeding the verified starter department directory"
& $venvPython -B $seed --database-url $databaseUrl
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Department seeding failed. Review the messages above."
}

# ---------------------------------------------------------------------------
# 6. Generate a fresh, machine-local SECRET_KEY (never printed).
# ---------------------------------------------------------------------------
Write-Step "Preparing the local SECRET_KEY"

function New-RandomSecret {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
    $secret = (Get-Content -LiteralPath $secretPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($secret) -or $secret.Length -lt 32) {
        Stop-WithError "The stored SECRET_KEY is missing or too short: $secretPath. Delete it and run setup again to generate a new one."
    }
    Write-Host "  Reusing the existing local SECRET_KEY (value is never printed)."
} else {
    $secret = New-RandomSecret
    $null = New-Item -ItemType File -Path $secretPath -Force
    Set-Content -LiteralPath $secretPath -Value $secret -NoNewline -Encoding ascii
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = Get-Acl -LiteralPath $secretPath
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")
        $acl.SetAccessRule($rule)
        Set-Acl -LiteralPath $secretPath -AclObject $acl
    } catch {
        Write-Host "  Warning: could not restrict the secret file ACL." -ForegroundColor Yellow
    }
    Write-Host "  Generated a new local SECRET_KEY in local_data\.secret_key." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 7. Validate the finished environment without starting the server.
# ---------------------------------------------------------------------------
Write-Step "Validating the public environment"

$env:DATABASE_URL = $databaseUrl
$env:UPLOAD_DIR = $uploadsDir
$env:UIMRS_FRONTEND_MODE = "india"
$env:UIMRS_HOST = "127.0.0.1"
$env:UIMRS_PORT = "8000"
$env:SECRET_KEY = $secret
$env:PYTHONPATH = $backendDir

if (!(Test-Path -LiteralPath $validator -PathType Leaf)) {
    Stop-WithError "Startup validator missing: $validator"
}

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$validationOutput = & $venvPython -B $validator 2>&1 | ForEach-Object { "$_" }
$validationExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
$validationOutput | ForEach-Object { Write-Host "  $_" }
if ($validationExit -ne 0) {
    Stop-WithError "The public environment failed validation. Review the messages above."
}

# ---------------------------------------------------------------------------
# 8. Print the exact next steps for this laptop.
# ---------------------------------------------------------------------------
$pythonForUser = "backend\venv\Scripts\python.exe"

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps (run these from $projectRoot):" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Create YOUR administrator account (you choose the password):"
Write-Host "   $pythonForUser scripts\provision_public_admin.py --name `"Your Name`" --email `"you@example.com`""
Write-Host ""
Write-Host "2. Optional: create YOUR officer account (pick a Bengaluru or Gurugram department):"
Write-Host "   $pythonForUser scripts\provision_officer.py"
Write-Host ""
Write-Host "3. Register your own citizen account in the browser at http://127.0.0.1:8000/citizen after starting UIMRS."
Write-Host ""
Write-Host "4. Start UIMRS on every later run:"
Write-Host "   .\start_public_environment.cmd"
Write-Host ""
Write-Host "The database contains 21 starter department directory rows, no complaints and no accounts."
Write-Host "The Bengaluru classifier artifact is intentionally not included."
Write-Host ""
exit 0
