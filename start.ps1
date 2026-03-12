param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("agent", "dashboard")]
    [string]$Target,

    [string]$Host = "0.0.0.0",

    [int]$Port = 0,

    [switch]$WithDevDeps,

    [switch]$Debug
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$appDir = Join-Path $root $Target

if (-not (Test-Path $appDir)) {
    throw "App directory not found: $appDir"
}

if ($Port -le 0) {
    if ($Target -eq "dashboard") {
        $Port = 8000
    }
    else {
        $Port = 8001
    }
}

$venvDir = Join-Path $appDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment in $venvDir"
    python -m venv $venvDir
}

if (-not (Test-Path $venvPython)) {
    throw "Could not create virtual environment. Ensure Python is on PATH."
}

Write-Host "Installing runtime dependencies for $Target"
& $venvPython -m pip install -r (Join-Path $appDir "requirements.txt")

$devReq = Join-Path $appDir "requirements-dev.txt"
if (($WithDevDeps -or $Debug) -and (Test-Path $devReq)) {
    Write-Host "Installing dev dependencies for $Target"
    & $venvPython -m pip install -r $devReq
}

$envExample = Join-Path $root "deploy\$Target.env.example"
$envLocal = Join-Path $appDir ".env"
if ((Test-Path $envExample) -and (-not (Test-Path $envLocal))) {
    Write-Host "Creating $envLocal from example file"
    Copy-Item $envExample $envLocal
}

Set-Location $appDir

if ($Debug) {
    Write-Host "Running $Target test suite before startup"
    & $venvPython -m pytest -q
}

$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", $Host, "--port", "$Port")
if ($Target -eq "dashboard") {
    $uvicornArgs += "--reload"
}

Write-Host "Starting $Target on http://$Host`:$Port"
& $venvPython @uvicornArgs
