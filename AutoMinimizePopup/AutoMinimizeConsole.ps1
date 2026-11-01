#Requires -Version 5.1
<#
.SYNOPSIS
    监视"新弹出来的控制台小黑框"，出现后立刻自动最小化（不激活、不抢焦点）。

.DESCRIPTION
    轮询所有顶层窗口，命中规则的窗口用 ShowWindow(SW_SHOWMINNOACTIVE) 最小化，
    并把焦点还给黑框出现前的那个窗口。间隔、匹配规则、白名单都在同目录的
    autominimize.json 里改，改完重启本脚本即可生效。

.PARAMETER ConfigPath
    指定配置文件。默认先找脚本同目录 autominimize.json，再找 %LOCALAPPDATA%\AutoMinimizePopup\autominimize.json。

.PARAMETER Once
    只扫描一轮就退出（调试用）。

.PARAMETER DryRun
    只记录"本来会最小化哪个窗口"，不真的最小化。

.PARAMETER NoLog
    不写日志文件。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\AutoMinimizeConsole.ps1

.EXAMPLE
    powershell -NoProfile -File .\AutoMinimizeConsole.ps1 -DryRun -Once
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$Once,
    [switch]$DryRun,
    [switch]$NoLog
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 内置默认配置
$defaults = @{
    # 兜底全量扫描间隔（毫秒）。正常靠事件回调即时响应，这个只是保险；500 就够，CPU 几乎为零。
    IntervalMs            = 500
    # true = 日志里记录弹框进程的命令行与父进程，方便你查出是谁在弹。
    LogPopupSource        = $true
    # 只处理这些窗口类。ConsoleWindowClass = 经典 conhost 黑框（cmd / bat / 各种脚本弹框）。
    # Windows Terminal、VS Code 内嵌终端是别的窗口类，天然不受影响。
    IncludeClasses        = @('ConsoleWindowClass')
    # 标题正则（不区分大小写），命中任意一条才算"临时弹框"。
    TitlePatterns         = @(
        '^C\:\\WINDOWS\\system32\\cmd\.exe$',
        '^C\:\\WINDOWS\\SysWow64\\cmd\.exe$',
        '^(Administrator: )?(命令提示符|Command Prompt)$',
        '^[A-Za-z]:\\.*\\(cmd|conhost|cscript|wscript|mshta)\.exe( .*)?$',
        '^[A-Za-z]:\\.*\.(bat|cmd)$'
    )
    # 命中 TitlePatterns 但同时命中这条的，放过（例如你自己的常驻终端标题）。
    WhitelistTitlePattern = ''
    # 进程名（不含 .exe）黑名单：这些进程拥有的窗口一律不动。
    ExcludeProcessNames   = @('WindowsTerminal', 'Code', 'Cursor', 'wt')
    # 脚本启动时已经存在的窗口一律不动（避免动你正开着的终端）。
    IgnoreExistingWindows = $true
    # 每个窗口只最小化一次：你手动点回来后，脚本不会再抢着最小化它。
    MinimizeOnceOnly      = $true
    # 最小化后把焦点还给黑框出现前的窗口。
    RestoreFocus          = $true
    # 日志文件。'' = 脚本同目录 autominimize.log ；'none' = 不写日志。
    LogFile               = ''
}

# ---------------------------------------------------------------- P/Invoke
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
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);

    public const int SW_SHOWMINNOACTIVE = 7;

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

    public static string ClassNameOf(IntPtr h) {
        var cls = new StringBuilder(256);
        GetClassNameW(h, cls, cls.Capacity);
        return cls.ToString();
    }

    public delegate void WinEventCallback(IntPtr hWinEventHook, uint eventType, IntPtr hwnd,
        int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);

    [StructLayout(LayoutKind.Sequential)]
    public struct NativeMsg {
        public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam;
        public uint time; public int ptX; public int ptY;
    }

    [DllImport("user32.dll")] private static extern IntPtr SetWinEventHook(uint eventMin, uint eventMax, IntPtr hmod, WinEventCallback cb, uint idProcess, uint idThread, uint dwFlags);
    [DllImport("user32.dll")] public static extern bool UnhookWinEvent(IntPtr hWinEventHook);
    [DllImport("user32.dll")] private static extern bool PeekMessage(out NativeMsg m, IntPtr hWnd, uint min, uint max, uint removeMsg);
    [DllImport("user32.dll")] private static extern bool TranslateMessage(ref NativeMsg m);
    [DllImport("user32.dll")] private static extern IntPtr DispatchMessageW(ref NativeMsg m);
    [DllImport("user32.dll")] private static extern uint MsgWaitForMultipleObjects(uint nCount, IntPtr[] pHandles, bool fWaitAll, uint dwMilliseconds, uint dwWakeMask);

    public const uint EVENT_OBJECT_CREATE = 0x8000;
    public const uint EVENT_OBJECT_SHOW = 0x8002;
    public const uint PM_REMOVE = 0x0001;
    public const uint QS_ALLINPUT = 0x04FF;
    public const uint WINEVENT_OUTOFCONTEXT = 0x0000;
    public const uint WINEVENT_SKIPOWNPROCESS = 0x0002;

    // 事件驱动：窗口被创建/显示时操作系统直接回调，不用轮询去猜。
    public static IntPtr HookShowEvents(WinEventCallback cb) {
        return SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, IntPtr.Zero, cb, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
    }

    public static uint WaitMessages(uint ms) {
        return MsgWaitForMultipleObjects(0, null, false, ms, QS_ALLINPUT);
    }

    public static bool PumpOne() {
        NativeMsg m;
        if (!PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { return false; }
        TranslateMessage(ref m);
        DispatchMessageW(ref m);
        return true;
    }
}
'@
    Add-Type -TypeDefinition $src -Language CSharp -ErrorAction Stop
}

# ---------------------------------------------------------------- 路径与配置
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }

function Resolve-ConfigFile {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += (Join-Path $scriptDir 'autominimize.json')
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'AutoMinimizePopup\autominimize.json') }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return $null
}

function ConvertTo-StringArray {
    param($Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [System.Array]) { return @($Value | ForEach-Object { [string]$_ }) }
    $s = [string]$Value
    if ($s.Trim() -eq '') { return @() }
    return @($s)
}

function Get-Config {
    param([string]$Explicit)
    $cfg = @{}
    foreach ($e in $defaults.GetEnumerator()) { $cfg[$e.Key] = $e.Value }

    $file = Resolve-ConfigFile -Explicit $Explicit
    if ($file) {
        try {
            # 容忍整行的 // 注释，方便把配置文件当带注释的说明文档用。
            $raw = Get-Content -LiteralPath $file -Raw
            $clean = ($raw -split "`r?`n" | Where-Object { $_ -notmatch '^\s*//' }) -join "`n"
            $obj = $clean | ConvertFrom-Json
            foreach ($p in $obj.PSObject.Properties) {
                if ($defaults.ContainsKey($p.Name)) { $cfg[$p.Name] = $p.Value }
            }
        } catch {
            Write-Warning "配置文件解析失败，改用内置默认值: $($_.Exception.Message)"
        }
    }

    # 归一化
    $iv = 0
    if ([int]::TryParse([string]$cfg.IntervalMs, [ref]$iv) -and $iv -ge 100) { $cfg.IntervalMs = $iv } else { $cfg.IntervalMs = 500 }

    $cfg.IncludeClasses = @(ConvertTo-StringArray $cfg.IncludeClasses)
    $cfg.TitlePatterns  = @(ConvertTo-StringArray $cfg.TitlePatterns)
    if ($cfg.TitlePatterns.Count -eq 0) { $cfg.TitlePatterns = @($defaults.TitlePatterns) }
    $cfg.ExcludeProcessNames = @(ConvertTo-StringArray $cfg.ExcludeProcessNames | ForEach-Object { $_.Trim() } | Where-Object { $_ })

    # New-Object + 集合初始值在 PowerShell 里会被当成构造参数展开，所以空构造再逐个 Add。
    $cfg.ClassSet = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($c in $cfg.IncludeClasses) { [void]$cfg.ClassSet.Add($c.ToLowerInvariant()) }
    $cfg.ProcSet = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($c in $cfg.ExcludeProcessNames) { [void]$cfg.ProcSet.Add($c.ToLowerInvariant()) }

    $regexOpts = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor `
                 [System.Text.RegularExpressions.RegexOptions]::Compiled
    $rxList = New-Object System.Collections.Generic.List[object]
    foreach ($p in $cfg.TitlePatterns) {
        try { $rxList.Add([regex]::new($p, $regexOpts)) } catch { Write-Warning "标题正则无效，已忽略: $p" }
    }
    $cfg.TitleRegexes = $rxList

    $wl = [string]$cfg.WhitelistTitlePattern
    $cfg.WhitelistRegex = $null
    if ($wl) {
        try { $cfg.WhitelistRegex = [regex]::new($wl, $regexOpts) } catch { Write-Warning "白名单正则无效，已忽略: $wl" }
    }

    foreach ($b in 'IgnoreExistingWindows', 'MinimizeOnceOnly', 'RestoreFocus', 'LogPopupSource') {
        $val = $cfg[$b]
        if ($val -is [bool]) { continue }
        $s = ([string]$val).Trim().ToLowerInvariant()
        $cfg[$b] = ($s -eq 'true' -or $s -eq '1' -or $s -eq 'yes')
    }

    $lf = [string]$cfg.LogFile
    if ($lf -eq 'none' -or $lf -eq 'off' -or $lf -eq '-') { $cfg.LogFilePath = $null }
    elseif ($lf) {
        $cfg.LogFilePath = if ([System.IO.Path]::IsPathRooted($lf)) { $lf } else { Join-Path $scriptDir $lf }
    } else { $cfg.LogFilePath = (Join-Path $scriptDir 'autominimize.log') }
    if ($NoLog) { $cfg.LogFilePath = $null }

    $cfg.ConfigFile = $file
    return $cfg
}

$cfg = Get-Config -Explicit $ConfigPath

# ---------------------------------------------------------------- 日志
$script:logPath = $cfg.LogFilePath
function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if (-not $script:logPath) { return }
    try {
        $dir = Split-Path -Parent $script:logPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -LiteralPath $script:logPath -Value $line -Encoding UTF8
        $fi = New-Object System.IO.FileInfo $script:logPath
        if ($fi.Exists -and $fi.Length -gt 200KB) {
            Set-Content -LiteralPath $script:logPath -Value (Get-Content -LiteralPath $script:logPath -Tail 400) -Encoding UTF8
        }
    } catch { }
}

# ---------------------------------------------------------------- 单实例
$mutex = New-Object System.Threading.Mutex($false, 'Local\AutoMinimizePopupConsoles')
if (-not $mutex.WaitOne(0, $false)) {
    Write-Host '已有实例在运行，本进程退出。'
    $mutex.Dispose()
    return
}
# PID 文件：安装/测试脚本靠它精确锁定后台实例，不按命令行字符串猜，避免误伤别的进程。
$pidFile = Join-Path $scriptDir 'autominimize.pid'
try { Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII } catch { }

# ---------------------------------------------------------------- 运行期状态
$ownPids = New-Object 'System.Collections.Generic.HashSet[uint32]'
[void]$ownPids.Add([uint32]$PID)
try {
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop |
        Select-Object -First 1).ParentProcessId
    if ($parent) { [void]$ownPids.Add([uint32]$parent) }
} catch { }

$pidNames = @{}
function Get-ProcName {
    param([uint32]$ProcId)
    if (-not $pidNames.ContainsKey($ProcId)) {
        $n = ''
        try { $n = (Get-Process -Id $ProcId -ErrorAction Stop).ProcessName } catch { }
        $pidNames[$ProcId] = $n
    }
    return $pidNames[$ProcId]
}

$handled = New-Object 'System.Collections.Generic.HashSet[intptr]'
$ignored = New-Object 'System.Collections.Generic.HashSet[intptr]'
$lastRealFg = [IntPtr]::Zero
$lastPrune = Get-Date

function Test-Target {
    param($Window, [ref]$ProcNameOut)
    if (-not $Window.Visible -or $Window.Iconic) { return $false }
    if (-not $cfg.ClassSet.Contains($Window.ClassName.ToLowerInvariant())) { return $false }
    if ([string]::IsNullOrWhiteSpace($Window.Title)) { return $false }
    $matched = $false
    foreach ($rx in $cfg.TitleRegexes) { if ($rx.IsMatch($Window.Title)) { $matched = $true; break } }
    if (-not $matched) { return $false }
    if ($cfg.WhitelistRegex -and $cfg.WhitelistRegex.IsMatch($Window.Title)) { return $false }
    if ($ownPids.Contains($Window.Pid)) { return $false }
    $pn = Get-ProcName -ProcId $Window.Pid
    if ($ProcNameOut) { $ProcNameOut.Value = $pn }
    if ($pn -and $cfg.ProcSet.Contains($pn.ToLowerInvariant())) { return $false }
    return $true
}

# 启动时就存在的窗口永久放过（包括你自己开着的终端），只处理之后新出现的。
if ($cfg.IgnoreExistingWindows) {
    foreach ($w in [NativeWin]::TopWindows()) { [void]$ignored.Add($w.Hwnd) }
}

$modeList = @()
if ($DryRun) { $modeList += 'DryRun' }
if ($Once) { $modeList += 'Once' }
$mode = if ($modeList.Count) { '(' + ($modeList -join ',') + ')' } else { 'live' }
Write-Log ("启动 pid={0} 间隔={1}ms 模式={2} 配置={3} 日志={4}" -f `
    $PID, $cfg.IntervalMs, $mode,
    $(if ($cfg.ConfigFile) { $cfg.ConfigFile } else { '内置默认' }),
    $(if ($script:logPath) { $script:logPath } else { 'off' }))
Write-Log ("窗口类={0} | 标题规则数={1} | 进程排除={2} | 忽略已有={3} | 只处理一次={4}" -f `
    ($cfg.IncludeClasses -join ','), $cfg.TitleRegexes.Count,
    $(if ($cfg.ExcludeProcessNames.Count) { $cfg.ExcludeProcessNames -join ',' } else { '无' }),
    $cfg.IgnoreExistingWindows, $cfg.MinimizeOnceOnly)

# ---------------------------------------------------------------- 事件钩子
# 窗口被创建 / 显示时操作系统直接回调，比轮询快一个量级。回调本身什么都不做，
# 只是让下面的 WaitMessages 立刻返回，再由主循环判定并最小化。
$script:hookDelegate = $null
$hookHandle = [IntPtr]::Zero
if (-not $Once) {
    try {
        $script:hookDelegate = [NativeWin+WinEventCallback] {
            param([IntPtr]$hHook, [uint32]$eventType, [IntPtr]$hwnd, [int]$idObject, [int]$idChild,
                  [uint32]$dwEventThread, [uint32]$dwmsEventTime)
        }
        $hookHandle = [NativeWin]::HookShowEvents($script:hookDelegate)
    } catch { $hookHandle = [IntPtr]::Zero }
    if ($hookHandle -eq [IntPtr]::Zero) {
        $script:hookDelegate = $null
        Write-Log '事件钩子装不上，退回纯轮询：延迟 = 扫描间隔'
    } else {
        Write-Log '事件钩子已挂载：新窗口出现即时响应'
    }
}

# 后台进程会被 Windows 的后台节流降到 BelowNormal，那样延迟会从毫秒级涨到秒级，这里钉回 Normal。
function Reset-Priority {
    try {
        $me = Get-Process -Id $PID
        if ($me.PriorityClass -ne [System.Diagnostics.ProcessPriorityClass]::Normal) {
            $me.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::Normal
        }
    } catch { }
}
Reset-Priority

# ---------------------------------------------------------------- 主循环
$lastScan = [DateTime]::MinValue
try {
    do {
        # 有窗口事件就立刻醒；没有就最多等 IntervalMs 毫秒，兜底扫一次。
        if ($hookHandle -ne [IntPtr]::Zero) {
            [void][NativeWin]::WaitMessages([uint32]$cfg.IntervalMs)
            while ([NativeWin]::PumpOne()) { }
            # 事件刷屏时给扫描限速（最快 15ms 一次），避免拖满 CPU。
            if (((Get-Date) - $lastScan).TotalMilliseconds -lt 15) { continue }
        }
        $lastScan = Get-Date

        $wins = [NativeWin]::TopWindows()

        # 记住"上一次真正的焦点窗口"，好把焦点从黑框手里还回去。
        $fg = [NativeWin]::GetForegroundWindow()
        if ($fg -ne [IntPtr]::Zero) {
            $fgCls = [NativeWin]::ClassNameOf($fg)
            if (-not $cfg.ClassSet.Contains($fgCls.ToLowerInvariant())) { $lastRealFg = $fg }
        }

        foreach ($w in $wins) {
            if ($ignored.Contains($w.Hwnd)) { continue }

            $alreadyHandled = $handled.Contains($w.Hwnd)
            if ($cfg.MinimizeOnceOnly -and $alreadyHandled) { continue }

            $pn = ''
            if (-not (Test-Target -Window $w -ProcNameOut ([ref]$pn))) {
                # 被我们自己按下去的窗口现在是最小化态，必须继续记账：
                # 否则用户点回来（不再 iconic）就被当成新窗口又按一次。
                if ($alreadyHandled -and -not $w.Iconic) { [void]$handled.Remove($w.Hwnd) }
                continue
            }

            [void]$handled.Add($w.Hwnd)
            $tag = if ($DryRun) { 'DRYRUN 将最小化' } else { '最小化' }
            Write-Log ("{0} hwnd=0x{1} pid={2} 进程={3} 类={4} 标题='{5}'" -f `
                $tag, $w.Hwnd.ToInt64().ToString('X'), $w.Pid, $(if ($pn) { $pn } else { '?' }), $w.ClassName, $w.Title)

            if ($DryRun) { continue }
            [void][NativeWin]::ShowWindow($w.Hwnd, [NativeWin]::SW_SHOWMINNOACTIVE)

            if ($cfg.RestoreFocus -and $fg -eq $w.Hwnd -and
                $lastRealFg -ne [IntPtr]::Zero -and $lastRealFg -ne $w.Hwnd) {
                if ([NativeWin]::IsWindow($lastRealFg) -and [NativeWin]::IsWindowVisible($lastRealFg) -and
                    -not [NativeWin]::IsIconic($lastRealFg)) {
                    [void][NativeWin]::SetForegroundWindow($lastRealFg)
                    Write-Log ("  焦点还给 hwnd=0x{0}" -f $lastRealFg.ToInt64().ToString('X'))
                }
            }

            # 已经按完了才去查来源，别拖慢最小化本身。
            if ($cfg.LogPopupSource) {
                try {
                    $pi = Get-CimInstance Win32_Process -Filter "ProcessId=$($w.Pid)" -ErrorAction Stop | Select-Object -First 1
                    $cl = [string]$pi.CommandLine
                    if ($cl.Length -gt 180) { $cl = $cl.Substring(0, 180) + '...' }
                    $ppn = ''
                    try { $ppn = (Get-Process -Id ([int]$pi.ParentProcessId) -ErrorAction Stop).ProcessName } catch { }
                    Write-Log ("  来源: 父进程={0} 命令行='{1}'" -f $(if ($ppn) { $ppn } else { '?' }), $cl)
                } catch { }
            }
        }

        # 清掉已销毁窗口的记账：既防 HashSet 增长，也让系统复用同一句柄时能及时脱离"启动时已存在"名单。
        if ((Get-Date) - $lastPrune -gt [TimeSpan]::FromSeconds(5)) {
            foreach ($h in @($handled | Where-Object { -not [NativeWin]::IsWindow($_) })) { [void]$handled.Remove($h) }
            foreach ($h in @($ignored | Where-Object { -not [NativeWin]::IsWindow($_) })) { [void]$ignored.Remove($h) }
            foreach ($k in @(@($pidNames.GetEnumerator()) | Where-Object { $_.Value -eq '' } | ForEach-Object { $_.Key })) {
                $pidNames.Remove($k)
            }
            $lastPrune = Get-Date
            Reset-Priority
        }

        if ($Once) { break }
        if ($hookHandle -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds ([int]$cfg.IntervalMs) }
    } while ($true)
} finally {
    Write-Log ("退出 pid={0} 已处理={1} 已忽略={2}" -f $PID, $handled.Count, $ignored.Count)
    if ($hookHandle -ne [IntPtr]::Zero) { [void][NativeWin]::UnhookWinEvent($hookHandle) }
    $script:hookDelegate = $null
    try { if ((Test-Path -LiteralPath $pidFile) -and (@(Get-Content -LiteralPath $pidFile)[0] -eq "$PID")) { Remove-Item -LiteralPath $pidFile -Force } } catch { }
    [void]$mutex.ReleaseMutex()
    $mutex.Dispose()
}
