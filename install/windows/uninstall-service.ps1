<#
.SYNOPSIS
    Stop and remove the Snap7 Industrial Gateway Windows service.
.DESCRIPTION
    The data directory (%ProgramData%\Snap7Gateway) is kept unless -Purge is
    given, because it holds the configuration, credentials and audit trail.
#>
param(
    [string]$Prefix = "$env:ProgramFiles\Snap7Gateway",
    [string]$DataDir = "$env:ProgramData\Snap7Gateway",
    [switch]$Purge
)

$ErrorActionPreference = "Continue"

& sc.exe stop Snap7Gateway | Out-Null
if (Test-Path "$Prefix\venv\Scripts\python.exe") {
    & "$Prefix\venv\Scripts\python.exe" -m snap7_gateway.service.windows_service remove
} else {
    & sc.exe delete Snap7Gateway | Out-Null
}
Remove-Item -Recurse -Force $Prefix -ErrorAction SilentlyContinue

if ($Purge) {
    Write-Host "Removing $DataDir (configuration, logs, audit trail)"
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("SNAP7_GATEWAY_DATA_DIR", $null, "Machine")
} else {
    Write-Host "Kept $DataDir. Pass -Purge to delete it as well."
}
