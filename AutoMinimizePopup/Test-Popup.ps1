#Requires -Version 5.1
<#
.SYNOPSIS
    验证"黑框自动最小化"是否生效：造几个真弹框，然后看它们有没有被自动最小化。

.PARAMETER List
    只列出当前所有控制台窗口（类名 / 标题 / pid / 是否已最小化），用来确认窗口类名。

.PARAMETER Keep
    弹框存活秒数，默认 12。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Test-Popup.ps1

.EXAMPLE
    powershell -NoProfile -File .\Test-Popup.ps1 -List
#>
[CmdletBinding()]
param(
    [switch]$List,
    [int]$Keep = 12
)

$ErrorActionPreference = 'Stop'

if (-not ('NativeWin' -as [type])) {
    $src = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public struct WinInfo {
    public IntPtr Hwnd;
    public string ClassName;
    public string Title;
    public uint Pid;
    public bool Visible;
    public bool Iconic;
}

public static class NativeWin {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassNameW(IntPtr hWnd, StringBuilder sb, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextW(IntPtr hWnd, StringBuilder sb, int max);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);

    public static List<WinInfo> TopWindows() {
        var list = new List<WinInfo>();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            var cls = new StringBuilder(256);
            GetClassNameW(h, cls, cls.Capacity);
            var ttl = new StringBuilder(1024);
            GetWindowTextW(h, ttl, ttl.Capacity);
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            var wi = new WinInfo();
            wi.Hwnd = h; wi.ClassName = cls.ToString(); wi.Title = ttl.ToString(); wi.Pid = pid;
            wi.Visible = IsWindowVisible(h); wi.Iconic = IsIconic(h);
            list.Add(wi);
            return true;
        }, IntPtr.Zero);
        return list;
    }
}
'@
    Add-Type -TypeDefinition $src -Language CSharp -ErrorAction Stop
}

function Get-ConsoleWindows {
    # 所有窗口类含 "onsole" 的顶层窗口（经典 conhost 黑框）。
    return @(([NativeWin]::TopWindows()) | Where-Object { $_.ClassName -match 'onsole' })
}

if ($List) {
    $rows = Get-ConsoleWindows | ForEach-Object {
        [pscustomobject]@{
            Hwnd   = '0x' + $_.Hwnd.ToInt64().ToString('X')
            Class  = $_.ClassName
            标题    = $_.Title
            Pid    = $_.Pid
            可见    = $_.Visible
            已最小化 = $_.Iconic
        }
    }
    if (-not $rows) { Write-Host '当前没有控制台窗口。' } else { $rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host }
    return
}

# 后台实例是否在跑
$alive = @()
$pidFile = Join-Path $PSScriptRoot 'autominimize.pid'
if (Test-Path -LiteralPath $pidFile) {
    foreach ($line in @(Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue)) {
        $id = 0
        if ([int]::TryParse("$line".Trim(), [ref]$id) -and $id -ne $PID -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) { $alive += $id }
    }
}
if (-not $alive.Count) {
    $alive = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File "?[^"]*AutoMinimizeConsole\.ps1"?\s*$' } |
        ForEach-Object { $_.ProcessId })
}
$alive = @($alive | Select-Object -Unique)
if ($alive.Count) {
    Write-Host ("后台实例在跑: pid={0}" -f ($alive -join ','))
} else {
    Write-Warning '没检测到 AutoMinimizeConsole.ps1 后台实例 —— 先跑 Install-AutoMinimize.ps1，或手动启动它。下面继续测试。'
}

$before = @{}
foreach ($w in Get-ConsoleWindows) { $before[$w.Hwnd] = $true }

# 造两种典型弹框：cmd 直开、bat 脚本
$bat = Join-Path $env:TEMP ("amtest_{0}.bat" -f (Get-Random))
Set-Content -LiteralPath $bat -Value "@echo off`r`nping -n $([int]($Keep / 2)) 127.0.0.1 >nul`r`ndel `"%~f0`"" -Encoding ASCII

Write-Host "弹出 2 个测试黑框（存活约 $Keep 秒）..."
Start-Process cmd.exe -ArgumentList '/c', "ping -n $Keep 127.0.0.1 >nul & exit"
Start-Sleep -Milliseconds 400
Start-Process $bat

Start-Sleep -Seconds 3
$after = Get-ConsoleWindows | Where-Object { -not $before.ContainsKey($_.Hwnd) }

if (-not $after) {
    Write-Host '没抓到新弹框（可能已经退出），把 -Keep 调大再试。'
    return
}

$targets = @()
foreach ($t in $after) {
    $stale = 0
    for ($i = 0; $i -lt 20; $i++) {
        if (-not [NativeWin]::IsWindow($t.Hwnd)) { break }
        if ([NativeWin]::IsIconic($t.Hwnd)) { break }
        Start-Sleep -Milliseconds 100
        $stale++
    }
    $targets += [pscustomobject]@{
        Hwnd   = '0x' + $t.Hwnd.ToInt64().ToString('X')
        标题    = $t.Title
        类      = $t.ClassName
        结果    = $(if (-not [NativeWin]::IsWindow($t.Hwnd)) { '窗口已消失' }
                    elseif ([NativeWin]::IsIconic($t.Hwnd)) { if ($stale -eq 0) { '已自动最小化 (检测前已完成)' } else { '已自动最小化 (' + ($stale * 100) + 'ms 内)' } }
                    else { '没被最小化 <-- 检查规则' })
    }
}
$targets | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

$bad = @($targets | Where-Object { $_.结果 -like '*检查规则*' })
if ($bad.Count -eq 0) {
    Write-Host '结论: 通过 —— 新弹框都被自动最小化了。'
} else {
    Write-Host '结论: 有漏网的。把上面标题/类名加进 autominimize.json 的 TitlePatterns 或 IncludeClasses，再重装一次。'
}
try { Remove-Item -LiteralPath $bat -Force -ErrorAction SilentlyContinue } catch { }
