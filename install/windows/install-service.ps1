<#
.SYNOPSIS
    Install the Snap7 Industrial Gateway as a Windows service.

.DESCRIPTION
    Creates a virtualenv under the install prefix, installs the package with the
    Windows extras (pywin32), registers the service for automatic start and
    configures restart-on-failure. Run from an elevated PowerShell prompt.

.EXAMPLE
    .\install-service.ps1 -SourceDir C:\src\Snap7_gateway
#>
param(
    [string]$SourceDir = (Resolve-Path "$PSScriptRoot\..\.."),
    [string]$Prefix = "$env:ProgramFiles\Snap7Gateway",
    [string]$DataDir = "$env:ProgramData\Snap7Gateway"
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell prompt."
}

Write-Host "==> Creating $DataDir"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "crash") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "certs") | Out-Null

Write-Host "==> Installing into $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
python -m venv "$Prefix\venv"
& "$Prefix\venv\Scripts\pip.exe" install --upgrade pip | Out-Null
& "$Prefix\venv\Scripts\pip.exe" install "$SourceDir[windows]"

Write-Host "==> Bundling snap7.dll"
# python-snap7 ships the x64 DLL; copy any local override next to the venv so the
# loader finds it without touching the system PATH.
$dll = Join-Path $SourceDir "install\windows\snap7.dll"
if (Test-Path $dll) {
    Copy-Item $dll "$Prefix\venv\Scripts\snap7.dll" -Force
    Write-Host "    copied $dll"
} else {
    Write-Host "    no local snap7.dll found; relying on the one bundled with python-snap7"
}

Write-Host "==> Registering the service"
[Environment]::SetEnvironmentVariable("SNAP7_GATEWAY_DATA_DIR", $DataDir, "Machine")
$env:SNAP7_GATEWAY_DATA_DIR = $DataDir
& "$Prefix\venv\Scripts\python.exe" -m snap7_gateway.service.windows_service --startup auto install

Write-Host "==> Configuring restart on failure"
& sc.exe failure Snap7Gateway reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null

Write-Host "==> Starting"
& sc.exe start Snap7Gateway | Out-Null

Write-Host ""
Write-Host "Installed. The first-run administrator password is written ONCE to:"
Write-Host "  $DataDir\logs\gateway.log"
Write-Host "Open https://<this-host>:8443/ and change it before doing anything else."
Write-Host ""
Write-Host "Note: the virtual S7 CPU binds TCP/102. Allow it through the firewall:"
Write-Host "  New-NetFirewallRule -DisplayName 'Snap7 Gateway S7' -Direction Inbound -Protocol TCP -LocalPort 102 -Action Allow"
