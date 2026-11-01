[CmdletBinding()]
param(
  [ValidateSet('codex','ticktick','both','custom')][string]$Target = 'codex',
  [string]$AppId,
  [string]$ProcessMatch,
  [switch]$Unpin,
  [switch]$Status,
  [switch]$Json
)
$ErrorActionPreference = 'Stop'
$mode = if ($Status) { 'status' } elseif ($Unpin) { 'unpin' } else { 'pin' }
$results = @()
$errors = @()
try { if ($PSVersionTable.PSEdition -eq 'Core') { throw 'Use Windows PowerShell 5.1; PowerShell Core does not support this COM interface.' }
  if ($Status -and $Unpin) { throw 'Status and Unpin cannot be combined.' }
  if ($AppId -and $Target -eq 'both') { throw 'AppId requires a single target.' }
  if (-not ('VDeskInterop' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'VDeskInterop.cs') }
  if ($AppId) {
    $r = [VDeskInterop]::Run(0, $AppId, $mode)
    $results += [pscustomobject]@{ Target=$Target; ProcessName=$null; ProcessId=$null; Hwnd=$null; Title=$null; Result=$r }
  } else {
    if ($Target -eq 'custom' -and -not $ProcessMatch) { throw 'Custom requires ProcessMatch or AppId.' }
    $patterns = switch ($Target) {
      'codex' { @{ codex='^(ChatGPT|Codex)$' } }
      'ticktick' { @{ ticktick='^(TickTick|dida)$' } }
      'both' { @{ codex='^(ChatGPT|Codex)$'; ticktick='^(TickTick|dida)$' } }
      'custom' { @{ custom=$ProcessMatch } }
    }
    if ($ProcessMatch -and $Target -ne 'custom') { throw 'ProcessMatch requires target custom.' }
    $windows = @([VDeskInterop]::Windows())
    $processes = @{}; Get-Process -ErrorAction SilentlyContinue | ForEach-Object { $processes[[uint32]$_.Id] = $_.ProcessName }
    foreach ($name in @($patterns.Keys | Sort-Object)) {
      $found = @($windows | Where-Object { $processes.ContainsKey($_.ProcessId) -and $processes[$_.ProcessId] -match $patterns[$name] })
      if ($found.Count -eq 0) { $errors += ('No visible titled window found for ' + $name + '. Open the window or specify AppId for one target.'); continue }
      foreach ($w in $found) {
        $r = [VDeskInterop]::Run($w.Hwnd, $null, 'status')
        if ($r.Success -and $name -ne 'custom') {
          $targetProcess = Get-Process -Id $w.ProcessId -ErrorAction Stop
          $identityOk = if ($name -eq 'codex') {
            $r.AppId -eq 'OpenAI.Codex_2p2nqsd0c76g0!App' -and $targetProcess.Path -match '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\ChatGPT\.exe$'
          } else {
            $expectedPath = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) '滴答清单\TickTick.exe'
            $r.AppId -eq '{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\滴答清单\TickTick.exe' -and $targetProcess.Path -eq $expectedPath
          }
          if (-not $identityOk) { $r.Success = $false; $r.Error = 'Target executable or Shell AppID does not match the installed application. No change was made; use custom for a different installation.' }
        }
        if ($r.Success -and $mode -ne 'status') { $r = [VDeskInterop]::Run($w.Hwnd, $null, $mode) }
        $results += [pscustomobject]@{ Target=$name; ProcessName=$processes[$w.ProcessId]; ProcessId=$w.ProcessId; Hwnd=('0x{0:X}' -f $w.Hwnd); Title=$w.Title; Result=$r }
      }
    }
  }
} catch { $errors += $_.Exception.Message }
$ok = $errors.Count -eq 0 -and $results.Count -gt 0 -and @($results | Where-Object { -not $_.Result.Success }).Count -eq 0
$output = [pscustomobject]@{ Mode=$mode; Success=$ok; Errors=@($errors); Windows=@($results) }
if ($Json) { $output | ConvertTo-Json -Depth 6 } else { $output | Format-List | Out-String | Write-Host; foreach ($item in $results) { $item | Format-List Target,ProcessName,ProcessId,Hwnd,Title | Out-String | Write-Host; $item.Result | Format-List | Out-String | Write-Host } }
if (-not $ok) { exit 1 }
