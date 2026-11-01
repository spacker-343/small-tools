[CmdletBinding()]
param([string]$AppId, [switch]$Unpin, [switch]$Status, [switch]$Json)
& (Join-Path $PSScriptRoot 'Pin-AllDesktops.ps1') -Target ticktick @PSBoundParameters
exit $LASTEXITCODE
