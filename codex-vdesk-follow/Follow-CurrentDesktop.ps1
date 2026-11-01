[CmdletBinding()]
param([int]$IntervalMs = 400, [switch]$Restore, [string]$ProcessMatch, [switch]$Status, [switch]$Unpin, [switch]$Json)
if (-not $Json) { Write-Host 'Uses pinning on all desktops; no polling. IntervalMs and Restore are ignored.' }
if ($ProcessMatch) { & (Join-Path $PSScriptRoot 'Pin-AllDesktops.ps1') -Target custom -ProcessMatch $ProcessMatch -Status:$Status -Unpin:$Unpin -Json:$Json }
else { & (Join-Path $PSScriptRoot 'Pin-AllDesktops.ps1') -Target both -Status:$Status -Unpin:$Unpin -Json:$Json }
exit $LASTEXITCODE
