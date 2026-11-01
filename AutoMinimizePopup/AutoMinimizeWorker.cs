// AutoMinimizeWorker.cs —— 常驻件本体，编译成 AutoMinimizeWorker.exe（winexe，无控制台窗口）。
// 事件驱动：SetWinEventHook 收到"新窗口出现"就直接处理；每秒一次兜底扫描，几乎不占 CPU。
// 编译命令见 Install-AutoMinimize.ps1（.NET Framework 自带的 csc.exe）。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Diagnostics;

[assembly: System.Reflection.AssemblyInformationalVersion("2026.11.01-hidden-window-fix")]

internal static class Native {
    [StructLayout(LayoutKind.Sequential)]
    public struct WININFO { public IntPtr hwnd; public string cls; public string title; public uint pid; public bool visible; public bool iconic; }

    public delegate void EventCb(IntPtr hook, uint evt, IntPtr hwnd, int obj, int child, uint thread, uint time);
    public delegate bool EnumCb(IntPtr hwnd, IntPtr lparam);

    [DllImport("user32.dll")] public static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, EventCb cb, uint pid, uint tid, uint flags);
    [DllImport("user32.dll")] public static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumCb cb, IntPtr lparam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd, StringBuilder sb, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd, StringBuilder sb, int max);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll", SetLastError = true)] public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern uint MsgWaitForMultipleObjects(uint count, IntPtr[] handles, bool all, uint ms, uint mask);
    [DllImport("user32.dll")] public static extern bool PeekMessage(out MSG msg, IntPtr hwnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] public static extern bool TranslateMessage(ref MSG msg);
    [DllImport("user32.dll")] public static extern IntPtr DispatchMessage(ref MSG msg);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr hwnd, bool fUnknown);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] public static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongW")] public static extern int SetWindowLong(IntPtr hwnd, int index, int value);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] public static extern IntPtr GetWindowLongPtr(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] public static extern IntPtr SetWindowLongPtr(IntPtr hwnd, int index, IntPtr value);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hwnd, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint cmd);

    [DllImport("kernel32.dll")] public static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
    [DllImport("kernel32.dll")] public static extern bool Process32First(IntPtr snap, ref PROCESSENTRY32 e);
    [DllImport("kernel32.dll")] public static extern bool Process32Next(IntPtr snap, ref PROCESSENTRY32 e);

    [StructLayout(LayoutKind.Sequential)]
    public struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam; public uint time; public int x; public int y; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct PROCESSENTRY32 {
        public int dwSize; public int cntUsage; public int th32ProcessID; public IntPtr th32DefaultHeapID;
        public int th32ModuleID; public int cntThreads; public int th32ParentProcessID;
        public int pcPriClassBase; public int dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string szExeFile;
    }

    public const uint TH32CS_SNAPPROCESS = 0x2;
    public const uint EVENT_SYSTEM_FOREGROUND = 0x0003;
    public const uint EVENT_OBJECT_CREATE = 0x8000;
    public const uint EVENT_OBJECT_DESTROY = 0x8001;
    public const uint EVENT_OBJECT_SHOW = 0x8002;
    public const uint OBJID_WINDOW = 0x00000000;
    public const uint PM_REMOVE = 0x0001;
    public const uint QS_ALLINPUT = 0x04FF;
    public const int SW_SHOWMINNOACTIVE = 7;
    public const int SW_RESTORE = 9;
    public const int SW_SHOWNOACTIVATE = 4;
    public const byte VK_MENU = 0x12;
    public const uint KEYEVENTF_KEYUP = 0x0002;
    public const int GWL_EXSTYLE = -20;
    public const long WS_EX_TOOLWINDOW = 0x00000080;
    public const long WS_EX_APPWINDOW = 0x00040000;
    public const uint SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_NOZORDER = 0x0004;
    public const uint SWP_NOACTIVATE = 0x0010, SWP_FRAMECHANGED = 0x0020;

    public static IntPtr Hook(EventCb cb, uint min, uint max) {
        return SetWinEventHook(min, max, IntPtr.Zero, cb, 0, 0, 0 /*WINEVENT_OUTOFCONTEXT*/);
    }
    public static void Unhook(IntPtr h) { if (h != IntPtr.Zero) { UnhookWinEvent(h); } }

    public static bool ClassIsIncluded(IntPtr hwnd, HashSet<string> classes) {
        var c = new StringBuilder(256);
        return GetClassName(hwnd, c, c.Capacity) > 0 && classes.Contains(c.ToString());
    }

    public static List<WININFO> Windows(HashSet<string> classes = null) {
        var list = new List<WININFO>();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            var c = new StringBuilder(256); GetClassName(h, c, c.Capacity);
            if (classes != null && !classes.Contains(c.ToString())) { return true; }
            var t = new StringBuilder(1024); GetWindowText(h, t, t.Capacity);
            uint pid; GetWindowThreadProcessId(h, out pid);
            var w = new WININFO();
            w.hwnd = h; w.cls = c.ToString(); w.title = t.ToString(); w.pid = pid;
            w.visible = IsWindowVisible(h); w.iconic = IsIconic(h);
            list.Add(w);
            return true;
        }, IntPtr.Zero);
        return list;
    }

    public static bool InfoOf(IntPtr h, out WININFO w) {
        w = new WININFO();
        if (!IsWindow(h)) { return false; }
        var c = new StringBuilder(256); GetClassName(h, c, c.Capacity);
        var t = new StringBuilder(1024); GetWindowText(h, t, t.Capacity);
        uint pid; GetWindowThreadProcessId(h, out pid);
        w.hwnd = h; w.cls = c.ToString(); w.title = t.ToString(); w.pid = pid;
        w.visible = IsWindowVisible(h); w.iconic = IsIconic(h);
        return true;
    }

    public static void PumpAll() {
        MSG m;
        while (PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { TranslateMessage(ref m); DispatchMessage(ref m); }
    }

    public static void Wait(uint ms) { MsgWaitForMultipleObjects(0, null, false, ms, QS_ALLINPUT); PumpAll(); }

    // 把前台焦点还给 target。后台进程直接调 SetForegroundWindow 常被“前台锁”拒绝，
    // 所以按代价从低到高逐级尝试：普通调用 -> AttachThreadInput -> Alt 解锁 -> SwitchToThisWindow。
    public static bool RestoreForeground(IntPtr target) {
        if (target == IntPtr.Zero || !IsWindow(target) || IsIconic(target)) { return false; }
        try {
            if (GetForegroundWindow() == target) { return true; }
            SetForegroundWindow(target);
            if (GetForegroundWindow() == target) { return true; }

            uint dummy;
            uint curTid = GetCurrentThreadId();
            IntPtr fg = GetForegroundWindow();
            uint fgTid = GetWindowThreadProcessId(fg, out dummy);
            uint tgTid = GetWindowThreadProcessId(target, out dummy);
            bool attachedFg = false, attachedTg = false;
            try {
                if (fgTid != 0 && fgTid != curTid) { attachedFg = AttachThreadInput(curTid, fgTid, true); }
                if (tgTid != 0 && tgTid != curTid) { attachedTg = AttachThreadInput(curTid, tgTid, true); }
                BringWindowToTop(target);
                SetForegroundWindow(target);
                SetFocus(target);
            } finally {
                if (attachedTg) { AttachThreadInput(curTid, tgTid, false); }
                if (attachedFg) { AttachThreadInput(curTid, fgTid, false); }
            }
            if (GetForegroundWindow() == target) { return true; }

            keybd_event(VK_MENU, 0, 0, UIntPtr.Zero);
            try { SetForegroundWindow(target); } finally { keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, UIntPtr.Zero); }
            if (GetForegroundWindow() == target) { return true; }

            SwitchToThisWindow(target, true);
            return GetForegroundWindow() == target;
        } catch { return false; }
    }

    // 32/64 位两套入口，按指针宽度选用，避免在 64 位上被截断
    public static IntPtr GetExStyle(IntPtr hwnd) {
        return IntPtr.Size > 4 ? GetWindowLongPtr(hwnd, GWL_EXSTYLE) : new IntPtr(GetWindowLong(hwnd, GWL_EXSTYLE));
    }

    public static void SetExStyle(IntPtr hwnd, long value) {
        if (IntPtr.Size > 4) { SetWindowLongPtr(hwnd, GWL_EXSTYLE, new IntPtr(value)); }
        else { SetWindowLong(hwnd, GWL_EXSTYLE, unchecked((int)value)); }
    }

    public static bool IsToolWindow(IntPtr hwnd) {
        return (GetExStyle(hwnd).ToInt64() & WS_EX_TOOLWINDOW) != 0;
    }

    // 加 WS_EX_TOOLWINDOW —— 窗口从任务栏和 Alt+Tab 里消失，但进程照常运行、照常读写管道；
    // 想再看到它就跑一次 /reveal。清掉 WS_EX_APPWINDOW 是因为它俩互斥，那个位会强索任务栏按钮。
    public static bool HideFromTaskbar(IntPtr hwnd) { return SetToolWindow(hwnd, true); }
    public static bool ShowInTaskbar(IntPtr hwnd) { return SetToolWindow(hwnd, false); }

    static bool SetToolWindow(IntPtr hwnd, bool tool) {
        try {
            if (!IsWindow(hwnd)) { return false; }
            long cur = GetExStyle(hwnd).ToInt64();
            long want = (tool ? (cur | WS_EX_TOOLWINDOW) : (cur & ~WS_EX_TOOLWINDOW)) & ~WS_EX_APPWINDOW;
            if (want == cur) { return true; }
            SetExStyle(hwnd, want);
            SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED);
            return tool == IsToolWindow(hwnd);
        } catch { return false; }
    }

    // 不依赖 WMI，用 Toolhelp 快照拿"谁生成了这个进程"。
    public static int ParentOf(uint pid) {
        IntPtr snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == IntPtr.Zero || snap == (IntPtr)(-1)) { return -1; }
        var e = new PROCESSENTRY32();
        e.dwSize = Marshal.SizeOf(typeof(PROCESSENTRY32));
        int parent = -1;
        if (Process32First(snap, ref e)) {
            do { if (e.th32ProcessID == (int)pid) { parent = e.th32ParentProcessID; break; } } while (Process32Next(snap, ref e));
        }
        Marshal.Release(snap);
        return parent;
    }

    public static string NameOf(int pid) {
        try { return Process.GetProcessById(pid).ProcessName; } catch { return "?"; }
    }
}

internal sealed class Config {
    public int IntervalMs = 1000;
    public bool IgnoreExisting = true, MinimizeOnce = true, RestoreFocus = true, LogSource = true;
    public bool MinimizeOnCreate = true, HideFromTaskbar = true;
    public string Whitelist = "", LogFile = "";
    public List<string> Classes = new List<string>();
    public List<string> Patterns = new List<string>();
    public List<string> ExcludeProc = new List<string>();

    public static Config Load(string path) {
        var cfg = new Config();
        if (!File.Exists(path)) { return cfg; }
        string key = null;
        bool inArray = false;
        foreach (string raw in File.ReadAllLines(path, Encoding.UTF8)) {
            string line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("//")) { continue; }
            if (!inArray) {
                Match m = Regex.Match(line, "^\"(?<k>[A-Za-z0-9_]+)\"\\s*:\\s*(?<v>.*?)(,)?\\s*$");
                if (!m.Success) { continue; }
                key = m.Groups["k"].Value;
                string v = m.Groups["v"].Value.Trim();
                if (v == "[") { inArray = true; continue; }
                Assign(cfg, key, v);
            } else {
                if (line.StartsWith("]")) { inArray = false; continue; }
                Match it = Regex.Match(line, "^\"(?<s>.*?)\"(,)?\\s*$");
                if (!it.Success) { continue; }
                string val = it.Groups["s"].Value.Replace("\\\\", "\\").Replace("\\\"", "\"");
                if (key == "IncludeClasses") { cfg.Classes.Add(val); }
                else if (key == "TitlePatterns") { cfg.Patterns.Add(val); }
                else if (key == "ExcludeProcessNames") { cfg.ExcludeProc.Add(val); }
            }
        }
        return cfg;
    }

    private static void Assign(Config cfg, string key, string v) {
        switch (key) {
            case "IntervalMs":
                int n;
                if (int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) && n >= 200) { cfg.IntervalMs = n; }
                break;
             case "IgnoreExistingWindows": cfg.IgnoreExisting = ParseBool(v, cfg.IgnoreExisting); break;
             case "MinimizeOnceOnly": cfg.MinimizeOnce = ParseBool(v, cfg.MinimizeOnce); break;
             case "MinimizeOnCreate": cfg.MinimizeOnCreate = ParseBool(v, cfg.MinimizeOnCreate); break;
             case "HideFromTaskbar": cfg.HideFromTaskbar = ParseBool(v, cfg.HideFromTaskbar); break;
             case "RestoreFocus": cfg.RestoreFocus = ParseBool(v, cfg.RestoreFocus); break;
             case "LogPopupSource": cfg.LogSource = ParseBool(v, cfg.LogSource); break;
            case "WhitelistTitlePattern": cfg.Whitelist = Unquote(v); break;
            case "LogFile": cfg.LogFile = Unquote(v); break;
        }
    }

     // 值写错（比如 "ture"、多个空格、大括号漏了）时退回默认值，而不是静默当成 false——
     // HideFromTaskbar 的默认是 true，静默关掉的话用户只会觉得"怎么又弹任务栏了"，很难查。
     private static bool ParseBool(string v, bool fallback) {
         string s = Unquote(v).Trim().TrimEnd(',').Trim().ToLowerInvariant();
         if (s == "true" || s == "1" || s == "yes" || s == "on") { return true; }
         if (s == "false" || s == "0" || s == "no" || s == "off") { return false; }
         return fallback;
     }

    private static string Unquote(string v) {
        v = v.Trim().TrimEnd(',').Trim();
        if (v.Length >= 2 && v.StartsWith("\"") && v.EndsWith("\"")) { v = v.Substring(1, v.Length - 2); }
        return v.Replace("\\\\", "\\").Replace("\\\"", "\"");
    }
}

internal static class Worker {
    static Config cfg;
    static string baseDir;
    static HashSet<string> classSet = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    static HashSet<string> procSet = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    static List<Regex> titles = new List<Regex>();
    static Regex whitelist;
    static HashSet<IntPtr> ignored = new HashSet<IntPtr>();
    static HashSet<IntPtr> handled = new HashSet<IntPtr>();
    static HashSet<IntPtr> settled = new HashSet<IntPtr>();   // 已确认“摁住了”的窗口
    static Queue<IntPtr> pressQueue = new Queue<IntPtr>();
    static HashSet<IntPtr> pressPending = new HashSet<IntPtr>();
    static HashSet<IntPtr> wantedFocusRestore = new HashSet<IntPtr>();
     static HashSet<IntPtr> fgGivenBack = new HashSet<IntPtr>();  // 已经在按压途中把焦点还过的窗口
     // 每个黑框弹出来之前是谁在前台。按压那一刻黑框可能还没拿到前台（WM 还在切换），
     // 只看“当前前台==黑框”就会漏掉归还（实测 3 次里漏 1 次）。
     static Dictionary<IntPtr, IntPtr> prevFgOf = new Dictionary<IntPtr, IntPtr>();
     // 归还焦点之后还要盯一小会儿：系统常常在我们还完焦点之后才把黑框激活（实测有 172ms 才抢的），
     // 只还一次的话，用户看到的就是“黑框最后停在前台”。这里按 10ms 一轮倒数，盯约 450ms。
     static Dictionary<IntPtr, int> focusWatch = new Dictionary<IntPtr, int>();
     static HashSet<IntPtr> focusWatchSeen = new HashSet<IntPtr>();
    static Dictionary<IntPtr, int> pressTries = new Dictionary<IntPtr, int>();
    static Dictionary<IntPtr, int> pressStage = new Dictionary<IntPtr, int>();   // 0=只管最小化 1=已最小化，待补样式
    static Dictionary<uint, string> procNames = new Dictionary<uint, string>();
    static Dictionary<uint, int> parentPids = new Dictionary<uint, int>();
    static IntPtr lastRealFg = IntPtr.Zero;
    static StreamWriter log;
    static IntPtr hookShow = IntPtr.Zero, hookFg = IntPtr.Zero;
    static Native.EventCb cbShow, cbFg;      // 持有引用，防止回调被 GC
    static bool dryRun, once, listMode, revealMode;
    static string probeArg;
    static int minimizedCount;

    [STAThread]
    static int Main(string[] args) {
        baseDir = AppDomain.CurrentDomain.BaseDirectory;
        foreach (string a in args) {
            string s = a.TrimStart('-', '/').ToLowerInvariant();
            if (s == "dryrun") { dryRun = true; }
            else if (s == "once") { once = true; }
            else if (s == "list") { listMode = true; }
            else if (s == "reveal") { revealMode = true; }
            else if (s.StartsWith("probe")) { probeArg = a; }
        }

        cfg = Config.Load(Path.Combine(baseDir, "autominimize.json"));
        foreach (string c in cfg.Classes) { classSet.Add(c); }
        if (classSet.Count == 0) { classSet.Add("ConsoleWindowClass"); }
        foreach (string p in cfg.ExcludeProc) { if (p.Trim().Length > 0) { procSet.Add(p.Trim()); } }
        foreach (string p in cfg.Patterns) {
            try { titles.Add(new Regex(p, RegexOptions.IgnoreCase | RegexOptions.Compiled)); } catch { }
        }
        if (titles.Count == 0) {
            titles.Add(new Regex(@"^C\:\\WINDOWS\\system32\\cmd\.exe$", RegexOptions.IgnoreCase | RegexOptions.Compiled));
            titles.Add(new Regex(@"^(Administrator: )?(命令提示符|Command Prompt)$", RegexOptions.IgnoreCase | RegexOptions.Compiled));
            titles.Add(new Regex(@"^[A-Za-z]:\\.*\.(bat|cmd)$", RegexOptions.IgnoreCase | RegexOptions.Compiled));
        }
        if (cfg.Whitelist.Length > 0) {
            try { whitelist = new Regex(cfg.Whitelist, RegexOptions.IgnoreCase | RegexOptions.Compiled); } catch { }
        }

        if (listMode) { ListConsoles(); return 0; }
        if (revealMode) { RevealAll(); return 0; }
        if (probeArg != null) { Probe(probeArg); return 0; }

        Mutex guard;
        try { guard = new Mutex(true, "Local\\AutoMinimizePopupConsoles", out guardIsNew); }
        catch { guard = null; guardIsNew = true; }
        if (guard != null && !guardIsNew) { return 0; }

        OpenLog();
        WriteLog("启动 pid=" + Process.GetCurrentProcess().Id + " 兜底=" + cfg.IntervalMs + "ms 规则=" + titles.Count
                 + " 类=" + string.Join(",", new List<string>(classSet).ToArray()) + (dryRun ? " [DRYRUN]" : ""));
        try { File.WriteAllText(Path.Combine(baseDir, "autominimize.pid"), Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture)); } catch { }
        try { Process.GetCurrentProcess().PriorityClass = ProcessPriorityClass.Normal; } catch { }

        foreach (Native.WININFO w in Native.Windows(classSet)) { if (cfg.IgnoreExisting) { ignored.Add(w.hwnd); } }
        lastRealFg = Native.GetForegroundWindow();

        if (!once) {
            cbShow = OnWinEvent;
            cbFg = OnFgEvent;
            hookShow = Native.Hook(cbShow, Native.EVENT_OBJECT_CREATE, Native.EVENT_OBJECT_SHOW);
            hookFg = Native.Hook(cbFg, Native.EVENT_SYSTEM_FOREGROUND, Native.EVENT_SYSTEM_FOREGROUND);
            WriteLog(hookShow != IntPtr.Zero ? "事件钩子已挂载（即时响应）" : "事件钩子装载失败，仅靠兜底扫描");
            ScanAll();
            FlushPresses();
            var clock = Stopwatch.StartNew();
            long nextScan = cfg.IntervalMs;
            long nextTick = 0;
            while (true) {
                // 窗口消息会提前唤醒 Wait；只能按真实经过的时间扫描，不能每次唤醒都扫全桌面。
                long now = clock.ElapsedMilliseconds;
                long wait = Math.Max(0, nextScan - now);
                if (pressPending.Count > 0 || focusWatch.Count > 0) {
                    wait = Math.Min(wait, Math.Max(0, nextTick - now));
                }
                Native.Wait((uint)wait);
                now = clock.ElapsedMilliseconds;
                if (now >= nextTick && (pressPending.Count > 0 || focusWatch.Count > 0)) {
                    PressTick();
                    WatchTick();
                    nextTick = clock.ElapsedMilliseconds + 10;
                }
                if (clock.ElapsedMilliseconds >= nextScan) {
                    ScanAll();
                    nextScan = clock.ElapsedMilliseconds + cfg.IntervalMs;
                }
            }
        } else {
            ScanAll();
            FlushPresses();
        }

        CloseLog(guard);
        return 0;
    }

    static bool guardIsNew;

    static void OnWinEvent(IntPtr hook, uint evt, IntPtr hwnd, int obj, int child, uint thread, uint time) {
        if (obj != 0 || child != 0) { return; }
         try {
             if (evt == Native.EVENT_OBJECT_DESTROY) {
                 handled.Remove(hwnd); ignored.Remove(hwnd); settled.Remove(hwnd);
                 pressTries.Remove(hwnd); pressPending.Remove(hwnd);
                 wantedFocusRestore.Remove(hwnd); pressStage.Remove(hwnd); fgGivenBack.Remove(hwnd);
                 prevFgOf.Remove(hwnd); focusWatch.Remove(hwnd); focusWatchSeen.Remove(hwnd);
                 return;
             }
            if (evt == Native.EVENT_OBJECT_CREATE) { OnCreate(hwnd); }
            else if (evt == Native.EVENT_OBJECT_SHOW) { OnShow(hwnd); }
        } catch { }
    }

    // CREATE 只处理已可见且完整匹配的窗口。对隐藏窗口调用最小化会反而让它显示出来。
    // 尚未显示的窗口留给 SHOW 事件；绝不为了“提前收起”主动改变隐藏窗口的显示状态。
    static void OnCreate(IntPtr hwnd) {
        if (!cfg.MinimizeOnCreate || dryRun || ignored.Contains(hwnd) || settled.Contains(hwnd)
            || pressPending.Contains(hwnd)) { return; }
        if (!Native.ClassIsIncluded(hwnd, classSet)) { return; }
        Native.WININFO w;
        if (!Native.InfoOf(hwnd, out w)) { return; }
        Handle(w, false);
    }

    // 只收起确实可见、匹配规则且尚未收起过的窗口；不主动显示或还原其他窗口。
    static void OnShow(IntPtr hwnd) {
        if (!Native.ClassIsIncluded(hwnd, classSet)) { return; }
        Native.WININFO w;
        if (!Native.InfoOf(hwnd, out w)) { return; }
        Handle(w, false);
    }

    static void OnFgEvent(IntPtr hook, uint evt, IntPtr hwnd, int obj, int child, uint thread, uint time) {
        if (obj != 0 || child != 0) { return; }
        try {
            Native.WININFO w;
            if (!Native.InfoOf(hwnd, out w)) { return; }
            if (Matches(w)) {
                // 抢到前台的正是该收的黑框：只要还没确认摁住过就继续摁。
                // 用户自己点回来的窗口必然已在 settled 里，不会被抢。
                if (!ignored.Contains(hwnd) && !settled.Contains(hwnd)) { Handle(w, false); }
                return;
            }
            // 记成“焦点归还目标”的条件：没最小化，而且不是“标题都还没上的控制台”——
            // 后者八成就是刚弹出来的那个黑框，记下来等于把焦点还给它自己（实测踩过）
            if (Native.IsIconic(hwnd)) { return; }
            if (ClassMatches(w) && w.title.Trim().Length == 0) { return; }
            lastRealFg = hwnd;
        } catch { }
    }

    static void ScanAll() {
        try {
            foreach (Native.WININFO w in Native.Windows(classSet)) {
                Handle(w, false);
                // 已经收住的窗口如果样式被对方改回去了，趁它还最小化着补一次
                if (cfg.HideFromTaskbar && !dryRun && settled.Contains(w.hwnd) && w.iconic
                    && !Native.IsToolWindow(w.hwnd)) { Native.HideFromTaskbar(w.hwnd); }
            }
        } catch { }
    }

    // settled = 已确认“真的最小化了”的窗口，只有它算处理完，且一直保持：
    // 用户手动把它点回来时，第 452 行的守卫会直接放过它，兜底扫描和前台事件都不再抢。
    // （真正的“摁”在 PressTick 里分轮做，回调里不睡眠，见那里的注释）
    // 判定 + 记账 + 登记，真正的“摁”在 PressTick 里做
    static void Handle(Native.WININFO w, bool force) {
        if (ignored.Contains(w.hwnd)) { return; }
        if (cfg.MinimizeOnce && settled.Contains(w.hwnd) && !force) { return; }
        if (!Matches(w)) {
            // 用户自己把它点回来了，就不再抢（MinimizeOnceOnly）
            if (settled.Contains(w.hwnd) && !w.iconic && cfg.MinimizeOnce) { settled.Remove(w.hwnd); }
            return;
        }
        if (pressPending.Contains(w.hwnd)) { return; }
        minimizedCount++;
        bool stoleFocus = (w.hwnd == Native.GetForegroundWindow());
        WriteLog("收起 hwnd=0x" + w.hwnd.ToInt64().ToString("X") + " pid=" + w.pid + " 进程=" + ProcName(w.pid)
                 + " 类=" + w.cls + " 标题='" + w.title + "'" + (stoleFocus ? " [抢了焦点]" : ""));
        if (dryRun) { return; }
         if (stoleFocus) { wantedFocusRestore.Add(w.hwnd); }
         if (!prevFgOf.ContainsKey(w.hwnd)) {
             IntPtr before2 = SaneTarget(w.hwnd);
             if (before2 != IntPtr.Zero) { prevFgOf[w.hwnd] = before2; }
         }
        if (cfg.LogSource) {
            int parent = Native.ParentOf(w.pid);
            WriteLog("  来源: 父进程=" + (parent > 0 ? Native.NameOf(parent) + "(" + parent + ")" : "?")
                     + " ; 想查是谁在弹，任务管理器按这个 pid 找");
        }
        Enqueue(w.hwnd);
    }

    static void Enqueue(IntPtr hwnd) {
        if (pressPending.Contains(hwnd)) { return; }
        pressPending.Add(hwnd);
        if (!pressTries.ContainsKey(hwnd)) { pressTries[hwnd] = 0; }
        pressQueue.Enqueue(hwnd);
    }

    // 每 10ms 推进一步，分两段：先只管 ShowWindow，确认最小化之后下一轮才补任务栏样式。
    // 不能同一轮既 ShowWindow 又 SetWindowPos(FRAMECHANGED)：实测那样会把刚设好的最小化
    // 顶回可见状态，两边互相拉扯，收十几轮都收不住（probe2 的 E1/E3 对照实验）。
    static void PressTick() {
        int n = pressQueue.Count;
        for (int i = 0; i < n && pressQueue.Count > 0; i++) {
            IntPtr hwnd = pressQueue.Dequeue();
            pressPending.Remove(hwnd);
            if (!Native.IsWindow(hwnd)) { Drop(hwnd); continue; }
            if (!Native.IsWindowVisible(hwnd)) { Drop(hwnd); continue; }
            int stage = 0;
            pressStage.TryGetValue(hwnd, out stage);
            if (Native.IsIconic(hwnd)) {
                if (stage == 0) { pressStage[hwnd] = 1; Enqueue(hwnd); continue; }  // 先让最小化落地
                OnPressed(hwnd); continue;
            }
            if (dryRun) { Drop(hwnd); continue; }
             Native.ShowWindow(hwnd, Native.SW_SHOWMINNOACTIVE);
             // 焦点还在它身上、或者它正在被切到前台，就别等“确认摁住”了，先还回去，
             // 把“焦点被抢”的体感时长压到最短；还完继续盯一会儿，防止它随后才抢到前台
             if (cfg.RestoreFocus && !fgGivenBack.Contains(hwnd)) {
                 bool holds = hwnd == Native.GetForegroundWindow();
                 IntPtr back = holds ? SaneTarget(hwnd) : FgTargetFor(hwnd);
                 if (back != IntPtr.Zero && Native.RestoreForeground(back)) { fgGivenBack.Add(hwnd); }
                 Watch(hwnd);
             } else if (cfg.RestoreFocus) {
                 Watch(hwnd);
             }
            int t = 0;
            pressTries.TryGetValue(hwnd, out t);
            t++;
            if (t < 30) { pressTries[hwnd] = t; Enqueue(hwnd); }
            else {
                WriteLog("  连收 " + t + " 次仍未收住（对方在自己反复 ShowWindow），交给兜底扫描");
                Drop(hwnd);
            }
        }
    }

     static void Drop(IntPtr hwnd) {
         pressTries.Remove(hwnd); pressStage.Remove(hwnd); wantedFocusRestore.Remove(hwnd);
         fgGivenBack.Remove(hwnd); prevFgOf.Remove(hwnd);
         focusWatch.Remove(hwnd); focusWatchSeen.Remove(hwnd);
    }

    // 确认收住了。这时才改任务栏样式（上一轮已确认最小化，改样式不会再把它顶回可见），然后把焦点还回去
    static void OnPressed(IntPtr hwnd) {
        bool tookFg = wantedFocusRestore.Contains(hwnd);
        bool alreadyBack = fgGivenBack.Contains(hwnd);
        settled.Add(hwnd);
        Drop(hwnd);
        if (dryRun) { return; }
        if (cfg.HideFromTaskbar) { Native.HideFromTaskbar(hwnd); }
        WriteLog("  已收住 任务栏=" + (Native.IsToolWindow(hwnd) ? "已摘除" : "仍在"));
         if (cfg.RestoreFocus && !alreadyBack && (tookFg || hwnd == Native.GetForegroundWindow())) {
             IntPtr target = SaneTarget();
             if (target == IntPtr.Zero) {
                 WriteLog("  没有可用的焦点归还目标（上一个前台窗口本身就是黑框）");
             } else if (!Native.RestoreForeground(target)) {
                 WriteLog("  焦点归还失败 0x" + target.ToInt64().ToString("X"));
             }
         }
         Watch(hwnd);
     }

     // 盯住一个已收住的黑框：它要是随后才抢到前台，就再收一次并把焦点追回来
     static void Watch(IntPtr hwnd) {
         if (dryRun || !cfg.RestoreFocus || hwnd == IntPtr.Zero) { return; }
         if (focusWatch.ContainsKey(hwnd) || focusWatchSeen.Contains(hwnd)) { return; }
         focusWatch[hwnd] = 45;
     }

     static void WatchTick() {
         if (focusWatch.Count == 0) { return; }
         var keys = new IntPtr[focusWatch.Count];
         focusWatch.Keys.CopyTo(keys, 0);
         foreach (IntPtr h in keys) {
             int left;
             if (!focusWatch.TryGetValue(h, out left)) { continue; }
             if (!Native.IsWindow(h) || !Native.IsWindowVisible(h) || --left <= 0) { focusWatch.Remove(h); continue; }
             focusWatch[h] = left;
             if (h != Native.GetForegroundWindow()) { continue; }
             Native.ShowWindow(h, Native.SW_SHOWMINNOACTIVE);
             IntPtr back = FgTargetFor(h);
             if (back != IntPtr.Zero) { Native.RestoreForeground(back); }
             focusWatchSeen.Add(h);
             focusWatch.Remove(h);
             WriteLog("  它又抢了前台，补收一次并把焦点追回 0x" + back.ToInt64().ToString("X"));
         }
     }

    // 焦点归还目标必须自己还活着、没最小化，而且不是我们该收的黑框
     static IntPtr SaneTarget() { return SaneTarget(IntPtr.Zero); }

     // exclude：这次是替哪个黑框找归还目标，它自己不能当自己的目标
     static IntPtr SaneTarget(IntPtr exclude) {
         IntPtr t = lastRealFg;
         if (t == IntPtr.Zero || t == exclude || !Native.IsWindow(t) || Native.IsIconic(t)) { return IntPtr.Zero; }
         Native.WININFO w;
         if (Native.InfoOf(t, out w) && Matches(w)) { return IntPtr.Zero; }
         return t;
     }

     // 按压时用：优先“黑框弹出来之前那个窗口”，退回全局最近前台
     static IntPtr FgTargetFor(IntPtr hwnd) {
         IntPtr t;
         if (prevFgOf.TryGetValue(hwnd, out t) && t != hwnd && Native.IsWindow(t) && !Native.IsIconic(t)) { return t; }
         return SaneTarget(hwnd);
     }

    static void FlushPresses() {
        for (int i = 0; i < 60 && pressPending.Count > 0; i++) { Native.Wait(15); PressTick(); }
    }

    // 诊断用：/probe:<hwnd十六进制> —— 直接对一个窗口试一次“收”，把返回值/错误码/前后状态全打出来
    static void Probe(string arg) {
        int colon = arg.IndexOf(':');
        string hex = colon >= 0 ? arg.Substring(colon + 1) : "";
        hex = hex.Trim().Replace("0x", "").Replace("0X", "");
        long v;
        if (!long.TryParse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out v)) {
            Console.Error.Write("用法: AutoMinimizeWorker.exe /probe:<hwnd 十六进制，可用 /list 查>\n"); return;
        }
        IntPtr h = new IntPtr(v);
        var sb = new StringBuilder();
        sb.AppendLine("pid(本进程)=" + Process.GetCurrentProcess().Id + "  hwnd=0x" + v.ToString("X")
                      + "  IsWindow=" + Native.IsWindow(h));
        Native.WININFO w;
        if (Native.InfoOf(h, out w)) {
            sb.AppendLine("类=" + w.cls + " 标题='" + w.title + "' pid=" + w.pid + " 进程=" + ProcName(w.pid)
                          + " visible=" + w.visible + " iconic=" + w.iconic
                          + " toolwindow=" + Native.IsToolWindow(h)
                          + " 前台=" + (Native.GetForegroundWindow() == h));
        }
        for (int i = 1; i <= 5; i++) {
            bool ok = Native.ShowWindow(h, Native.SW_SHOWMINNOACTIVE);
            int err = Marshal.GetLastWin32Error();
            sb.AppendLine("第" + i + "次 ShowWindow(SW_SHOWMINNOACTIVE) -> " + ok + " err=" + err
                          + " 紧接着 iconic=" + Native.IsIconic(h));
            Thread.Sleep(20);
            sb.AppendLine("   20ms 后 iconic=" + Native.IsIconic(h) + " IsWindow=" + Native.IsWindow(h)
                          + " toolwindow=" + Native.IsToolWindow(h));
            if (Native.IsIconic(h)) { break; }
        }
        Console.Error.Write(sb.ToString());
    }

    // 宽松判定：窗口类命中 + 属主进程不在排除名单。标题这时可能还是空的，所以不看标题
    static bool ClassMatches(Native.WININFO w) {
        if (!classSet.Contains(w.cls)) { return false; }
        string pn = ProcName(w.pid);
        if (pn != "?" && procSet.Contains(pn)) { return false; }
        return true;
    }

    // 严格判定：类 + 标题命中规则 + 不命中白名单，且窗口当前可见未最小化
    static bool Matches(Native.WININFO w) {
        if (!w.visible || w.iconic) { return false; }
        if (!ClassMatches(w)) { return false; }
        if (string.IsNullOrEmpty(w.title) || w.title.Trim().Length == 0) { return false; }
        bool hit = false;
        foreach (Regex r in titles) { if (r.IsMatch(w.title)) { hit = true; break; } }
        if (!hit) { return false; }
        if (whitelist != null && whitelist.IsMatch(w.title)) { return false; }
        return true;
    }

    // /reveal：把被我们藏掉的控制台窗口放回任务栏并还原，临时想看输出或要敲键盘时用
    static void RevealAll() {
        int n = 0;
        var sb = new StringBuilder();
        try {
            foreach (Native.WININFO w in Native.Windows()) {
                if (!ClassMatches(w) || !Native.IsToolWindow(w.hwnd)) { continue; }
                Native.ShowInTaskbar(w.hwnd);
                Native.ShowWindow(w.hwnd, Native.SW_RESTORE);
                n++;
                sb.AppendLine("已放回 hwnd=0x" + w.hwnd.ToInt64().ToString("X") + " pid=" + w.pid
                              + " 进程=" + ProcName(w.pid) + " 标题='" + w.title + "'");
            }
        } catch { }
        Console.Error.Write(n == 0 ? "没有被藏起来的控制台窗口\n" : sb.ToString());
    }

    static string ProcName(uint pid) {
        string n;
        if (procNames.TryGetValue(pid, out n)) { return n; }
        try { n = Process.GetProcessById((int)pid).ProcessName; } catch { n = "?"; }
        procNames[pid] = n;
        return n;
    }

    static void ListConsoles() {
        var sb = new StringBuilder();
        foreach (Native.WININFO w in Native.Windows()) {
            if (w.cls.IndexOf("console", StringComparison.OrdinalIgnoreCase) < 0
                && w.cls.IndexOf("CASCADIA", StringComparison.OrdinalIgnoreCase) < 0) { continue; }
            sb.AppendLine("hwnd=0x" + w.hwnd.ToInt64().ToString("X") + "  类=" + w.cls
                          + "  可见=" + w.visible + "  已最小化=" + w.iconic + "  pid=" + w.pid
                          + "  进程=" + ProcName(w.pid) + "  标题='" + w.title + "'");
        }
        string outPath = Path.Combine(baseDir, "console-windows.txt");
        File.WriteAllText(outPath, sb.ToString(), Encoding.UTF8);
        Console.Error.Write(sb.Length == 0 ? "当前没有控制台窗口\n" : sb.ToString());
    }

    static void OpenLog() {
        string target = cfg.LogFile;
        if (target == "none" || target == "off" || target == "-") { return; }
        if (target.Length == 0) { target = Path.Combine(baseDir, "autominimize.log"); }
        else if (!Path.IsPathRooted(target)) { target = Path.Combine(baseDir, target); }
        try {
            var fi = new FileInfo(target);
            if (fi.Exists && fi.Length > 200 * 1024) {
                string[] all = File.ReadAllLines(target, Encoding.UTF8);
                int start = Math.Max(0, all.Length - 400);
                var tail = new string[all.Length - start];
                Array.Copy(all, start, tail, 0, tail.Length);
                File.WriteAllLines(target, tail, Encoding.UTF8);
            }
            log = new StreamWriter(target, true, new UTF8Encoding(false));
            log.AutoFlush = true;
        } catch { log = null; }
    }

    static void WriteLog(string msg) {
        string line = "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) + "] " + msg;
        if (log != null) { try { log.WriteLine(line); } catch { } }
    }

    static void CloseLog(Mutex guard) {
        WriteLog("退出，累计收起 " + minimizedCount + " 个");
        Native.Unhook(hookShow); Native.Unhook(hookFg);
        if (log != null) { try { log.Flush(); log.Dispose(); } catch { } }
        try {
            string pf = Path.Combine(baseDir, "autominimize.pid");
            if (File.Exists(pf) && File.ReadAllText(pf).Trim() == Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture)) {
                File.Delete(pf);
            }
        } catch { }
        if (guard != null) { try { if (guardIsNew) { guard.ReleaseMutex(); } guard.Dispose(); } catch { } }
    }
}
