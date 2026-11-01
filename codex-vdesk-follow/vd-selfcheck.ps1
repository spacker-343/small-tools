[CmdletBinding()]
param([switch]$Detailed)
$ErrorActionPreference = 'Stop'
try {
  if ($PSVersionTable.PSEdition -eq 'Core') { throw 'Use Windows PowerShell 5.1 for this COM interface.' }
  if (-not ('VDeskInterop' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'VDeskInterop.cs') }
  $os = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
  Write-Output ("OS: {0} {1} build {2}.{3}" -f $os.ProductName,$os.DisplayVersion,$os.CurrentBuild,$os.UBR)
  Write-Output ([VDeskInterop]::SelfCheck())
  Write-Output 'Public IVirtualDesktopManager cross-process moves are not a reliable follow method.'
} catch { Write-Error $_; exit 1 }
