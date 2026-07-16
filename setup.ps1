# One-time setup for Windows (PowerShell).
#
#   Right-click -> Run with PowerShell, or from a terminal:
#       powershell -ExecutionPolicy Bypass -File setup.ps1
#
# If you get "running scripts is disabled on this system", that
# ExecutionPolicy flag above is the fix - it applies to this one invocation
# only, not a permanent policy change.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "ProviderMap - Windows setup" -ForegroundColor Cyan
Write-Host ("-" * 44)

# --- Python check ---
$py = $null
foreach ($cmd in @("py -3", "python", "python3")) {
    try {
        $exe, $rest = $cmd.Split(" ", 2)
        $v = & $exe $rest --version 2>&1
        if ($LASTEXITCODE -eq 0) { $py = $cmd; Write-Host "Found: $v"; break }
    } catch { }
}
if (-not $py) {
    Write-Host "Python not found." -ForegroundColor Red
    Write-Host "Install Python 3.10+ from https://www.python.org/downloads/"
    Write-Host 'IMPORTANT: tick "Add python.exe to PATH" in the installer.'
    exit 1
}

$exe, $rest = $py.Split(" ", 2)
$verOut = (& $exe $rest -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
$parts = $verOut.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
    Write-Host "Python $verOut found, but 3.10+ is required." -ForegroundColor Red
    exit 1
}

# --- venv ---
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $exe $rest -m venv .venv
} else {
    Write-Host "Virtual environment already exists."
}

Write-Host "Installing dependencies (including dev tools)..."
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed." -ForegroundColor Red; exit 1 }

# --- config.yaml from the example, if not already present ---
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml from config.example.yaml."
}

# --- offline self-test: proves the install works without touching the network ---
Write-Host ""
Write-Host "Running the offline self-test (100 fixture records, no network)..." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" run.py test
if ($LASTEXITCODE -ne 0) {
    Write-Host "Self-test FAILED. Do not run against a live site yet." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open config.yaml and replace REPLACE_WITH_YOUR_EMAIL"
Write-Host "     (or set the PROVIDERMAP_CONTACT_EMAIL environment variable instead)"
Write-Host "  2. run.bat investigate         (analyse the live site)"
Write-Host "  3. run.bat scrape --dry-run --limit 50"
Write-Host ""
