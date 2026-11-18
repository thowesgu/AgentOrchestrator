$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
Set-Location $projectRoot

function Invoke-Step {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$Description
    )

    Write-Host "`n$Description"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$basePython = Get-Command python -ErrorAction SilentlyContinue
$pythonPrefix = @()
if (-not $basePython) {
    $basePython = Get-Command py -ErrorAction SilentlyContinue
    if (-not $basePython) {
        throw "Python 3.11 or newer is required. Install Python and run this script again."
    }
    $pythonPrefix = @("-3")
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    throw "Node.js and npm are required. Install Node.js 22 or newer and run this script again."
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Invoke-Step $basePython.Source ($pythonPrefix + @("-m", "venv", (Join-Path $projectRoot ".venv"))) "Creating Python virtual environment"
}

Invoke-Step $venvPython @("-m", "pip", "install", "-e", ".") "Installing Python dependencies"

$frontendModules = Join-Path $projectRoot "frontend\node_modules"
if (-not (Test-Path (Join-Path $frontendModules "vite\bin\vite.js"))) {
    Invoke-Step $npm.Source @("ci", "--prefix", (Join-Path $projectRoot "frontend")) "Installing frontend dependencies"
}

Invoke-Step $venvPython @("-m", "copilot", "download-runtime", "--in-process") "Preparing Copilot runtime"
Invoke-Step $npm.Source @("run", "build", "--prefix", (Join-Path $projectRoot "frontend")) "Building frontend"

$port = $env:PORT
if (-not $port) {
    $port = "8000"
}
Write-Host "`nStarting the app at http://localhost:$port (Ctrl+C to stop)."
& $venvPython -m agent_orchestrator.cli
if ($LASTEXITCODE -ne 0) {
    throw "The local server exited with code $LASTEXITCODE."
}
