#Requires -Version 5.1
# Off-screen, isolated regression: only the fixture's own windows and worker are touched.
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HiddenRegression {
 public delegate IntPtr Proc(IntPtr h,uint m,IntPtr w,IntPtr l);
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] public struct WNDCLASS { public uint style; public Proc proc; public int a,b; public IntPtr instance,icon,cursor,brush; public string menu,name; }
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern ushort RegisterClass(ref WNDCLASS c);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr CreateWindowEx(uint ex,string cls,string title,uint style,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr param);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr DefWindowProc(IntPtr h,uint m,IntPtr w,IntPtr l);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int cmd);
 [DllImport("user32.dll")] public static extern bool DestroyWindow(IntPtr h);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr GetModuleHandle(string n);
 [StructLayout(LayoutKind.Sequential)] public struct MSG { public IntPtr h; public uint m; public IntPtr w,l; public uint t; public int x,y; }
 [DllImport("user32.dll")] public static extern bool PeekMessage(out MSG m,IntPtr h,uint min,uint max,uint remove);
 [DllImport("user32.dll")] public static extern bool TranslateMessage(ref MSG m);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr DispatchMessage(ref MSG m);
 public static void Pump() { MSG m;while(PeekMessage(out m,IntPtr.Zero,0,0,1)){TranslateMessage(ref m);DispatchMessage(ref m);} }
 static Proc callback=DefWindowProc;
 static IntPtr module=GetModuleHandle(null);
 public static void Register(string name) { WNDCLASS c=new WNDCLASS();c.proc=callback;c.instance=module;c.name=name;if(RegisterClass(ref c)==0)throw new Exception("RegisterClass failed"); }
 public static IntPtr Create(string name,string title) { IntPtr h=CreateWindowEx(0x80,name,title,0x00CF0000,-32000,-32000,100,80,IntPtr.Zero,IntPtr.Zero,module,IntPtr.Zero); if(h==IntPtr.Zero)throw new Exception("CreateWindowEx failed");return h; }
}
'@
$testDirectory = Join-Path $env:TEMP ('AutoMinimize-HiddenRegression-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDirectory | Out-Null
$identity = [guid]::NewGuid().ToString('N')
$class = 'AutoMinimizeHiddenRegression_' + $identity
$source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'AutoMinimizeWorker.cs')).Replace('Local\\AutoMinimizePopupConsoles', ('Local\\AutoMinimizeHiddenRegression_' + $identity))
$sourcePath = Join-Path $testDirectory 'Worker.cs'
[IO.File]::WriteAllText($sourcePath, $source, [Text.Encoding]::UTF8)
$exe = Join-Path $testDirectory 'Worker.exe'
$csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:winexe /optimize+ /codepage:65001 ('/out:' + $exe) $sourcePath
if ($LASTEXITCODE -ne 0) { throw 'Regression worker compilation failed' }
$config = @'
{
"IntervalMs": 1500,
"IgnoreExistingWindows": true,
"RestoreFocus": false,
"LogPopupSource": false,
"WhitelistTitlePattern": "whitelist",
"IncludeClasses": [
"CLASS"
],
"TitlePatterns": [
"^match"
]
}
'@
[IO.File]::WriteAllText((Join-Path $testDirectory 'autominimize.json'), $config.Replace('CLASS',$class), [Text.Encoding]::UTF8)
[HiddenRegression]::Register($class)
$windows = @()
$worker = $null
try {
    $existing = [HiddenRegression]::Create($class,'match existing')
    $windows += $existing
    [void][HiddenRegression]::ShowWindow($existing,4)
    [void][HiddenRegression]::ShowWindow($existing,4)
    $worker = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru
    $ready = [Diagnostics.Stopwatch]::StartNew()
    do {
        [HiddenRegression]::Pump()
        Start-Sleep -Milliseconds 40
        $log = Join-Path $testDirectory 'autominimize.log'
        $started = (Test-Path -LiteralPath $log) -and ((Get-Content -LiteralPath $log -Raw -Encoding UTF8) -match '事件钩子已挂载')
    } while (-not $started -and $ready.ElapsedMilliseconds -lt 3000)
    if (-not $started) { throw 'Regression worker failed to start' }
    $hiddenMatch = [HiddenRegression]::Create($class,'match hidden')
    $hiddenOther = [HiddenRegression]::Create($class,'other hidden')
    $visibleMatch = [HiddenRegression]::Create($class,'match visible')
    $whitelist = [HiddenRegression]::Create($class,'match whitelist')
    $other = [HiddenRegression]::Create($class,'other visible')
    $windows += @($hiddenMatch,$hiddenOther,$visibleMatch,$whitelist,$other)
    foreach ($h in @($visibleMatch,$whitelist,$other)) { [void][HiddenRegression]::ShowWindow($h,4); [void][HiddenRegression]::ShowWindow($h,4) }
    $hiddenChanged = $false
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt 1800) {
        [HiddenRegression]::Pump()
        foreach ($h in @($hiddenMatch,$hiddenOther)) {
            if ([HiddenRegression]::IsWindowVisible($h) -or [HiddenRegression]::IsIconic($h)) { $hiddenChanged = $true }
        }
        Start-Sleep -Milliseconds 10
    }
    $result = [ordered]@{
        HiddenWindowsStayedHidden = (-not $hiddenChanged)
        NewVisibleMatchMinimized = [HiddenRegression]::IsIconic($visibleMatch)
        ExistingWindowUnchanged = ([HiddenRegression]::IsWindowVisible($existing) -and -not [HiddenRegression]::IsIconic($existing))
        WhitelistUnchanged = ([HiddenRegression]::IsWindowVisible($whitelist) -and -not [HiddenRegression]::IsIconic($whitelist))
        UnmatchedWindowUnchanged = ([HiddenRegression]::IsWindowVisible($other) -and -not [HiddenRegression]::IsIconic($other))
    }
    [void][HiddenRegression]::ShowWindow($visibleMatch,4)
    $watch.Restart()
    while ($watch.ElapsedMilliseconds -lt 1700) { [HiddenRegression]::Pump(); Start-Sleep -Milliseconds 10 }
    $result.ManualRestoreRemainsVisible = ([HiddenRegression]::IsWindowVisible($visibleMatch) -and -not [HiddenRegression]::IsIconic($visibleMatch))
    $result | ConvertTo-Json
    if (@($result.Values | Where-Object { -not $_ }).Count -gt 0) { throw 'Hidden-window regression failed' }
} finally {
    if ($worker -and -not $worker.HasExited) { Stop-Process -Id $worker.Id -Force }
    foreach ($window in $windows) { [void][HiddenRegression]::DestroyWindow($window) }
}
