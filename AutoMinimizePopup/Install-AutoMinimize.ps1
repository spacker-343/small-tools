#Requires -Version 5.1
<#
.SYNOPSIS
    一键安装 / 卸载"黑框自动最小化"：复制到本地目录 + 注册登录自启动计划任务 + 立即启动。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-AutoMinimize.ps1
    安装并立即开始生效。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-AutoMinimize.ps1 -Uninstall
    停掉后台进程并删除计划任务（保留文件）。

.PARAMETER InstallDir
    安装目录，默认 %LOCALAPPDATA%\AutoMinimizePopup。

.PARAMETER TaskName
    计划任务名，默认 AutoMinimizePopupConsoles。

.PARAMETER NoTask
    只复制文件并立刻跑一次，不注册计划任务。

.PARAMETER RemoveFiles
    配合 -Uninstall 使用，连安装目录一起删（只删自己复制过去的那几个文件）。

.PARAMETER LimitedWorker
 让计划任务以普通（受限）权限跑 worker。默认当前用户是管理员时用 Highest（登录时不会弹 UAC）。
 注意 Windows 的低完整性进程不能操作高完整性窗口（静默失效），受限跑就收不动管理员脚本弹的黑框。
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'AutoMinimizePopup'),
    [string]$TaskName = 'AutoMinimizePopupConsoles',
    [switch]$NoTask,
    [switch]$Uninstall,
    [switch]$RemoveFiles,
    [switch]$LimitedWorker
)

$ErrorActionPreference = 'Stop'
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$files = @('AutoMinimizeConsole.ps1', 'AutoMinimizeWorker.cs', 'autominimize.json', 'Start-AutoMinimizeHidden.vbs', 'Test-Popup.ps1')

function Get-WorkerPid {
    # 优先读 worker 自己写的 pid 文件；读不到再退回命令行匹配（必须是 -File ...\AutoMinimizeConsole.ps1 形式）。
    $found = @()
    $pf = Join-Path $InstallDir 'autominimize.pid'
    if (-not (Test-Path -LiteralPath $pf)) { $pf = Join-Path $scriptDir 'autominimize.pid' }
    if (Test-Path -LiteralPath $pf) {
        foreach ($line in @(Get-Content -LiteralPath $pf -ErrorAction SilentlyContinue)) {
            $id = 0
             if (-not [int]::TryParse("$line".Trim(), [ref]$id)) { continue }
             if ($id -eq $PID) { continue }
             $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
             if (-not $proc) { continue }
             # pid 文件可能指向已被回收复用的 PID，只认这几个进程名，避免误杀。
             if ($proc.ProcessName -notin @('powershell', 'pwsh', 'AutoMinimizeWorker')) { continue }
             $found += $id
         }
    }
     # pid 文件之外再按进程名扫一遍：多开的 exe 常驻件也要能被停掉，
     # 否则旧实例锁着 AutoMinimizeWorker.exe，重装时 csc 会报 CS0016。
     foreach ($p in @(Get-Process -Name 'AutoMinimizeWorker' -ErrorAction SilentlyContinue)) {
         if ($p.Id -ne $PID) { $found += $p.Id }
     }
    if ($found.Count) { return @($found | Select-Object -Unique) }
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction Stop |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File "?[^"]*AutoMinimizeConsole\.ps1"?\s*$' }
        foreach ($p in $procs) { $found += $p.ProcessId }
    } catch {
        Write-Warning "查找后台进程失败: $($_.Exception.Message)"
    }
    return @($found | Select-Object -Unique)
}

function Stop-Worker {
    $ids = @(Get-WorkerPid)
    foreach ($id in $ids) {
        # 双保险：命令行里确实带着本脚本才杀。
        $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue).CommandLine
         if ($cl -and $cl -notmatch 'AutoMinimizeConsole\.ps1|AutoMinimizeWorker\.exe') { continue }
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 300
    $left = @(Get-WorkerPid)
    if (-not $ids.Count) { Write-Host '没有正在运行的后台实例。' }
    elseif ($left.Count) { Write-Host ("后台实例仍未退出: {0}" -f ($left -join ',')) }
    else { Write-Host ("已停止后台实例 {0}。" -f ($ids -join ',')) }
}

if ($Uninstall) {
    Write-Host "== 卸载 $TaskName =="
     Stop-Worker
     # 卸载前把藏起来的黑框放回任务栏：exe 一旦被 -RemoveFiles 删掉，就再也没法 /reveal 了。
     $prevExe = Join-Path $InstallDir 'AutoMinimizeWorker.exe'
     if (Test-Path -LiteralPath $prevExe) {
         Start-Process -FilePath $prevExe -ArgumentList '/reveal' -Wait -NoNewWindow
         Write-Host '已把之前藏起来的黑框放回任务栏。'
     }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务 $TaskName"
    } else {
        Write-Host "计划任务 $TaskName 不存在，跳过"
    }
    if ($RemoveFiles -and (Test-Path -LiteralPath $InstallDir)) {
        foreach ($f in $files) {
            $src = Join-Path $scriptDir $f
            $dst = Join-Path $InstallDir $f
            # 只删安装目录里的同名文件，避免误删源目录。
            if ((Test-Path -LiteralPath $dst) -and ($dst -ne $src)) { Remove-Item -LiteralPath $dst -Force }
        }
         # 编译产物和运行期产物也清掉，否则空目录删不掉（AutoMinimizeWorker.exe 不在 $files 里，
         # 是安装时 csc 生成到安装目录的，漏掉它就会在卸载后残留一个没人管的 exe）。
         foreach ($rt in @('AutoMinimizeWorker.exe', 'autominimize.log', 'autominimize.pid', 'console-windows.txt')) {
             $rp = Join-Path $InstallDir $rt
             if ((Test-Path -LiteralPath $rp) -and ($rp -ne (Join-Path $scriptDir $rt))) { Remove-Item -LiteralPath $rp -Force -ErrorAction SilentlyContinue }
         }
        if (-not (Get-ChildItem -LiteralPath $InstallDir -File -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "已清理 $InstallDir"
    }
    Write-Host '完成。当前会话里已经不再自动最小化黑框。'
    return
}

Write-Host "== 安装黑框自动最小化 =="
Write-Host "源目录  : $scriptDir"
Write-Host "安装目录: $InstallDir"

# ---------------------------------------------------------------- 复制文件
if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
}
$installed = @()
foreach ($f in $files) {
    $src = Join-Path $scriptDir $f
    $dst = Join-Path $InstallDir $f
    if (-not (Test-Path -LiteralPath $src)) {
        if ($f -eq 'Test-Popup.ps1') { continue }
        throw "缺少必要文件: $src"
    }
    if ($src -eq $dst) { $installed += $dst; Write-Host "  $f （已在安装目录）"; continue }
    if ($f -eq 'autominimize.json' -and (Test-Path -LiteralPath $dst)) {
        Write-Host "  autominimize.json 已存在，保留你改过的配置（如需重置先删它）"
        $installed += $dst
        continue
    }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $installed += $dst
    Write-Host "  $f"
}
$worker = Join-Path $InstallDir 'AutoMinimizeConsole.ps1'
$vbs    = Join-Path $InstallDir 'Start-AutoMinimizeHidden.vbs'
$exe    = Join-Path $InstallDir 'AutoMinimizeWorker.exe'

# ---------------------------------------------------------------- 语法自检
foreach ($ps in @($installed | Where-Object { $_ -like '*.ps1' })) {
    $errs = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($ps, [ref]$null, [ref]$errs)
    if ($errs -and $errs.Count) {
        throw "脚本语法有误: $ps -> $($errs[0].Message)"
    }
}
Write-Host '语法自检通过。'

# ---------------------------------------------------------------- 编译 C# 常驻件
# 常驻件优先用编译的 exe：事件驱动、无控制台窗口，实测 1% 单核 / 21MB 内存。
 # PowerShell 版保留作兜底（极端情况下 csc.exe 不可用时仍能跑）。
 # 必须先停旧实例：正在跑的 exe 会锁住输出文件，csc 报 CS0016 写不进去。
 Stop-Worker
 $useExe = $false
$csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$csSrc = Join-Path $InstallDir 'AutoMinimizeWorker.cs'
if (Test-Path -LiteralPath $csc) {
    & $csc /nologo /target:winexe /optimize+ /codepage:65001 /out:"$exe" "$csSrc" 2>&1 |
        ForEach-Object { Write-Host "  csc: $_" }
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $exe)) { $useExe = $true; Write-Host '已编译 AutoMinimizeWorker.exe' }
     else {
         Write-Warning '编译失败，尝试用随包附带的预编译 exe'
         $prebuilt = Join-Path $scriptDir 'AutoMinimizeWorker.exe'
         if (Test-Path -LiteralPath $prebuilt) {
             Copy-Item -LiteralPath $prebuilt -Destination $exe -Force
         }
         if (Test-Path -LiteralPath $exe) { $useExe = $true }
         else { Write-Warning '没有可用 exe，改用 PowerShell 版常驻件' }
     }
} elseif (Test-Path -LiteralPath $exe) {
    $useExe = $true
    Write-Host '没找到 csc.exe，直接用随包附带的 AutoMinimizeWorker.exe'
} else {
    Write-Warning '既没有 csc.exe 也没有可用 exe，改用 PowerShell 版常驻件'
}
$launchExe = $useExe

# ---------------------------------------------------------------- 注册计划任务
 # 旧实例在编译前已经停掉了，这里只需注册任务。

if (-not $NoTask) {
    if ($launchExe) {
        $action = New-ScheduledTaskAction -Execute $exe
    } else {
         $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wscript.exe" -Argument "`"$vbs`""
    }
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartInterval (New-TimeSpan -Minutes 1) `
        -RestartCount 3 -MultipleInstances IgnoreNew
    $sid = (New-Object System.Security.Principal.NTAccount($env:USERDOMAIN, $env:USERNAME)).Translate(
        [System.Security.Principal.SecurityIdentifier]).Value

    # 完整性级别决定 worker 能动哪些黑框：Windows 的 UIPI 规则是"低的不能操作高的窗口"，
    # 而且是静默失败（标题读得到，ShowWindow 也返回成功，就是没作用）。
    # 管理员账户下默认用 Highest：计划任务的 Highest 登录时不会再弹 UAC，
    # 这样普通脚本弹的（中等完整性）和管理员脚本弹的（高完整性）黑框都能收。
    $runLevel = 'Limited'
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        if (-not $LimitedWorker -and
            (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)) { $runLevel = 'Highest' }
    } catch { }
    $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel $runLevel

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description '新弹出的控制台黑框（cmd/bat/脚本）自动最小化。脚本位于 AutoMinimizeConsole.ps1，配置在 autominimize.json。' `
        -Force | Out-Null
    Write-Host "已注册计划任务 $TaskName （登录时自动启动，隐藏窗口，权限 $runLevel）"
}

# ---------------------------------------------------------------- 立刻启动
if ($NoTask) {
    if ($launchExe) { Start-Process -FilePath $exe -WindowStyle Hidden }
    else { Start-Process -FilePath "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$vbs`"" -WindowStyle Hidden }
} else {
    Start-ScheduledTask -TaskName $TaskName
}
Start-Sleep -Seconds 2

$alive = @(Get-WorkerPid)
if ($alive.Count) {
    Write-Host ("后台实例已运行 pid={0}" -f ($alive -join ','))
} else {
    Write-Warning '后台实例没起来，去安装目录里手动跑一次 AutoMinimizeConsole.ps1 看报错。'
}

Write-Host ''
Write-Host '下一步：'
Write-Host "  验证效果 : powershell -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\Test-Popup.ps1`""
Write-Host "  改规则   : 编辑 $InstallDir\autominimize.json 后重新跑一次 Install-AutoMinimize.ps1"
Write-Host "  看日志   : $InstallDir\autominimize.log"
Write-Host "  卸载     : powershell -NoProfile -File `"$scriptDir\Install-AutoMinimize.ps1`" -Uninstall"
