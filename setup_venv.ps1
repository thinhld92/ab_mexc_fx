param(
    [string]$PythonExe = "python",
    [string]$VenvDir = ".venv",
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPath = Join-Path $projectRoot $VenvDir
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$requirementsFile = Join-Path $projectRoot "requirements-dev.txt"

if ($Recreate -and (Test-Path -LiteralPath $venvPath)) {
    Write-Host "Removing existing virtual environment: $venvPath"
    Remove-Item -LiteralPath $venvPath -Recurse -Force
}

if (-not (Test-Path -LiteralPath $venvPath)) {
    Write-Host "Creating virtual environment: $venvPath"
    & $PythonExe -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment with '$PythonExe'."
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment python not found at $venvPython"
}

Write-Host "Upgrading pip, setuptools, and wheel..."
& $venvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip tooling."
}

Write-Host "Installing project dependencies from $requirementsFile ..."
& $venvPython -m pip install -r $requirementsFile
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install dependencies."
}

Write-Host ""
Write-Host "Venv is ready."
Write-Host "Activate  : .\.venv\Scripts\Activate.ps1"
Write-Host "Run tests : .\.venv\Scripts\python.exe -m pytest"
Write-Host "Start bot : .\.venv\Scripts\python.exe -m src.launcher"
