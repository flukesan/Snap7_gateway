<#
.SYNOPSIS
    Create a Python virtualenv for the Snap7 Industrial Gateway and install it.

.DESCRIPTION
    Development / bench setup for Windows. For a supervised service
    installation use install\windows\install-service.ps1 instead.

    Installs requirements-windows.txt by default, which adds pywin32 on top of
    the pinned runtime set.

.PARAMETER Dev
    Also install the test tooling (pytest, httpx, ruff).

.PARAMETER Locked
    Install requirements.lock.txt, pinning every transitive package as well.

.PARAMETER VenvDir
    Where to create the virtualenv. Defaults to .venv in the project root.

.EXAMPLE
    .\scripts\setup-venv.ps1 -Dev
#>
param(
    [switch]$Dev,
    [switch]$Locked,
    [string]$VenvDir
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path "$PSScriptRoot\.."
if (-not $VenvDir) { $VenvDir = Join-Path $ProjectRoot ".venv" }

$Requirements = "requirements-windows.txt"
if ($Locked) { $Requirements = "requirements.lock.txt" }
elseif ($Dev) { $Requirements = "requirements-dev.txt" }

# --- 1. Find an interpreter new enough -----------------------------------
function Find-Python {
    # The py launcher is the reliable way to ask for a specific version.
    foreach ($candidate in @(@("py", "-3.13"), @("py", "-3.14"), @("python"), @("python3"))) {
        $exe = $candidate[0]
        $prefix = @($candidate[1..($candidate.Length - 1)])
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $check = & $exe @prefix -c "import sys; print(1 if sys.version_info >= (3,13) else 0)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $check -eq "1") {
            return ,@($exe) + $prefix
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Error @"
Python 3.13 or newer is required but was not found.
Install it from https://www.python.org/downloads/windows/ (tick
"Add python.exe to PATH"), or with: winget install Python.Python.3.13
"@
}

$pyExe = $python[0]
$pyArgs = @($python[1..($python.Length - 1)])
$version = & $pyExe @pyArgs --version
Write-Host "==> Using $version"

# --- 2. Create the virtualenv --------------------------------------------
if (Test-Path $VenvDir) {
    Write-Host "==> Reusing existing virtualenv at $VenvDir"
} else {
    Write-Host "==> Creating virtualenv at $VenvDir"
    & $pyExe @pyArgs -m venv $VenvDir
}

$pip = Join-Path $VenvDir "Scripts\pip.exe"
$vpython = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "==> Upgrading pip"
& $pip install --quiet --upgrade pip

# --- 3. Install dependencies ---------------------------------------------
Write-Host "==> Installing $Requirements"
& $pip install --requirement (Join-Path $ProjectRoot $Requirements)
if ($Locked) {
    Write-Host "==> Adding pywin32 (not part of the Linux-generated lock file)"
    & $pip install --requirement (Join-Path $ProjectRoot "requirements-windows.txt")
}

# --- 4. Install the gateway itself ---------------------------------------
# --no-deps because the requirements file already decided every version;
# letting pip re-resolve here would silently defeat the pins.
Write-Host "==> Installing the gateway (editable, no dependency re-resolution)"
& $pip install --quiet --no-deps --editable $ProjectRoot

# --- 5. Verify -----------------------------------------------------------
Write-Host "==> Verifying the Snap7 library"
& $vpython -c @"
import sys
try:
    import snap7
    snap7.client.Client()
except Exception as exc:
    print(f'    Snap7 client library did NOT load: {exc}')
    print('    The gateway can still host the virtual S7 CPU, but it cannot poll')
    print('    a real PLC until this is fixed. See docs/build.md (section 5).')
    sys.exit(0)
print('    Snap7 client library loads')
"@

Write-Host "==> Verifying the CLI"
& (Join-Path $VenvDir "Scripts\snap7-gateway.exe") --version

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host ""
Write-Host "  Activate:   $VenvDir\Scripts\Activate.ps1"
Write-Host "  Run:        snap7-gateway run --data-dir .\gw-data --port 8443"
if ($Dev) { Write-Host "  Test:       pytest tests/ -q" }
Write-Host ""
Write-Host "The first-run administrator password is printed ONCE at startup and must"
Write-Host "be changed at first sign-in before any other page is reachable."
Write-Host ""
Write-Host "Binding the virtual S7 CPU to TCP/102 needs an elevated prompt."
