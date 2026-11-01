// Read-only, event-driven console-window recorder. .NET Framework 4.x, /target:winexe.
using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal static class PopupSourceMonitor {
    delegate void WinEventProc(IntPtr hook, uint evt, IntPtr hwnd, int obj, int child, uint thread, uint time);
    delegate bool EnumProc(IntPtr hwnd, IntPtr param);
    [StructLayout(LayoutKind.Sequential)] struct MSG { public IntPtr hwnd; public uint msg; public IntPtr wp, lp; public uint time; public int x, y; }
    [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, WinEventProc cb, uint pid, uint tid, uint flags);
    [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr param);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr process, out long created, out long exited, out long kernel, out long user);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("user32.dll")] static extern uint MsgWaitForMultipleObjects(uint count, IntPtr[] handles, bool all, uint timeout, uint mask);
    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG msg, IntPtr hwnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG msg);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG msg);

    sealed class Proc {
        public uint Id, ParentId;
        public string Name = "?", Path = "", Command = "", Detail = "";
        public DateTime Started;
        public long Generation;
        public Proc ParentSnapshot;
    }
    sealed class Window {
        public uint Pid;
        public string Class, Title, Key;
        public long Generation;
    }
    sealed class Record {
        public string Kind, At, Event, Hwnd, Class, Title, Key;
        public uint Pid, EventTick;
        public bool Visible, Minimized, Foreground, Existing;
        public Proc Process;
        public long Generation;
    }
    sealed class Group {
        public string Source, Category;
        public int NewWindows, ExistingWindows, ShowEvents, VisibleSamples;
        public string LastSeen;
    }

    static readonly object procGate = new object();
    static readonly Dictionary<uint, Proc> procs = new Dictionary<uint, Proc>();
    static readonly Queue<Proc> procOrder = new Queue<Proc>();
    static readonly Dictionary<IntPtr, Window> windows = new Dictionary<IntPtr, Window>();
    static readonly Queue<KeyValuePair<IntPtr,string>> windowOrder = new Queue<KeyValuePair<IntPtr,string>>();
    static readonly BlockingCollection<Record> queue = new BlockingCollection<Record>(4096);
    static readonly Dictionary<string, Group> groups = new Dictionary<string, Group>();
    static readonly Dictionary<string, string> windowGroups = new Dictionary<string, string>();
    static readonly Queue<string> groupWindowOrder = new Queue<string>();
    static readonly Stopwatch clock = Stopwatch.StartNew();
    static readonly WinEventProc callback = OnWindow;
    static string directory, traceStatus = "starting";
    static DateTime endUtc;
    static long dropped, written, newWindowCount;
    static int newProcessCount;
    static volatile Exception consumerError;
    static bool traceReady;
    static StreamWriter writer;
    static readonly JavaScriptSerializer json = new JavaScriptSerializer();

    static string Stamp() { return DateTimeOffset.UtcNow.ToOffset(TimeSpan.FromHours(8)).ToString("yyyy-MM-ddTHH:mm:ss.fffzzz", CultureInfo.InvariantCulture); }
    static string Limit(string value) { if (value == null) return ""; return value.Length > 4096 ? value.Substring(0,4096) + " [truncated]" : value; }
    static string Value(ManagementBaseObject item, string name) { try { return Convert.ToString(item[name], CultureInfo.InvariantCulture); } catch { return ""; } }
    static uint Number(ManagementBaseObject item, string name) { uint value; return uint.TryParse(Value(item,name), out value) ? value : 0; }

    static long GenerationOf(uint pid) {
        IntPtr handle=OpenProcess(0x1000,false,pid);
        if (handle == IntPtr.Zero) return 0;
        try { long created, exited, kernel, user; return GetProcessTimes(handle,out created,out exited,out kernel,out user) ? created : 0; }
        finally { CloseHandle(handle); }
    }
    static Proc ReadProcess(uint pid) {
        long generation=GenerationOf(pid);
        if (generation == 0) return null;
        try {
            using (var search = new ManagementObjectSearcher("SELECT ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate FROM Win32_Process WHERE ProcessId=" + pid))
            using (var rows = search.Get()) {
                foreach (ManagementObject row in rows) using (row) {
                    if (GenerationOf(pid) != generation) return null;
                    var p=FromRow(row); p.Generation=generation; p.Started=DateTime.FromFileTimeUtc(generation); return p;
                }
            }
        } catch { }
        return null;
    }
    static Proc FromRow(ManagementBaseObject row) {
        var p = new Proc { Id=Number(row,"ProcessId"), ParentId=Number(row,"ParentProcessId"), Name=Value(row,"Name"), Path=Value(row,"ExecutablePath"), Command=Limit(Value(row,"CommandLine")), Started=DateTime.UtcNow };
        try { p.Started = ManagementDateTimeConverter.ToDateTime(Value(row,"CreationDate")).ToUniversalTime(); } catch { }
        p.Detail = p.Command.Length == 0 ? "command_line_unavailable_or_empty" : "live_process_query";
        return p;
    }
    static void Remember(Proc p) {
        long parentGeneration=GenerationOf(p.ParentId);
        lock (procGate) {
            Proc old;
            if (procs.TryGetValue(p.Id,out old) && old.Started > p.Started) return;
            Proc parent;
            if (parentGeneration != 0 && procs.TryGetValue(p.ParentId,out parent) && parent.Generation == parentGeneration && parent.Started <= p.Started) p.ParentSnapshot=CopyAncestors(parent,7);
            procs[p.Id] = p; procOrder.Enqueue(p);
            while (procOrder.Count > 8192) {
                var first = procOrder.Dequeue();
                Proc current;
                if (procs.TryGetValue(first.Id,out current) && object.ReferenceEquals(first,current)) procs.Remove(first.Id);
            }
        }
    }
    static Proc CopyAncestors(Proc source, int remaining) {
        if (source == null || remaining <= 0) return null;
        return new Proc { Id=source.Id, ParentId=source.ParentId, Name=source.Name, Path=source.Path, Command=source.Command, Detail=source.Detail, Started=source.Started, Generation=source.Generation, ParentSnapshot=CopyAncestors(source.ParentSnapshot,remaining-1) };
    }
    static Proc Known(uint pid, DateTime before, long generation) {
        lock (procGate) { Proc p; return generation != 0 && procs.TryGetValue(pid,out p) && p.Generation == generation && p.Started <= before ? p : null; }
    }
    static void Seed() {
        using (var search = new ManagementObjectSearcher("SELECT ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate FROM Win32_Process"))
        using (var rows = search.Get()) { foreach (ManagementObject row in rows) using (row) {
            var p=FromRow(row); long generation=GenerationOf(p.Id);
            if (generation == 0 || Math.Abs(DateTime.FromFileTimeUtc(generation).Ticks-p.Started.Ticks) >= TimeSpan.TicksPerMillisecond) continue;
            p.Generation=generation; p.Started=DateTime.FromFileTimeUtc(generation); Remember(p);
        } }
    }
    static bool Interesting(string name) {
        name = name.ToLowerInvariant();
        return name == "conhost.exe" || name == "openconsole.exe" || name == "windowsterminal.exe" || name == "cmd.exe" || name == "powershell.exe" || name == "pwsh.exe" || name == "wscript.exe" || name == "cscript.exe" || name == "python.exe" || name == "pythonw.exe" || name == "node.exe";
    }
    static void OnProcess(object sender, EventArrivedEventArgs args) {
        try {
            uint id = Number(args.NewEvent,"ProcessID"), parent = Number(args.NewEvent,"ParentProcessID");
            DateTime created = DateTime.UtcNow;
            try { created = DateTime.FromFileTimeUtc(Convert.ToInt64(args.NewEvent["TIME_CREATED"],CultureInfo.InvariantCulture)); } catch { }
            var p = ReadProcess(id);
            if (p == null || p.Started > created) p = new Proc { Id=id, ParentId=parent, Name=Value(args.NewEvent,"ProcessName"), Started=created, Detail="exited_before_metadata_query" };
            Remember(p); Interlocked.Increment(ref newProcessCount);
            if (Interesting(p.Name)) Offer(new Record { Kind="process_start", At=Stamp(), Pid=id, Process=p });
        } catch (Exception e) { Offer(new Record { Kind="error", At=Stamp(), Title="process event: " + e.Message }); }
    }
    static void Offer(Record record) {
        try { if (!queue.TryAdd(record)) Interlocked.Increment(ref dropped); }
        catch (InvalidOperationException) { Interlocked.Increment(ref dropped); }
    }

    static void OnWindow(IntPtr hook, uint evt, IntPtr hwnd, int obj, int child, uint thread, uint time) {
        if (hwnd == IntPtr.Zero || obj != 0 || child != 0) return;
        if (evt != 0 && evt != 3 && evt != 0x8000 && evt != 0x8001 && evt != 0x8002 && evt != 0x8003 && evt != 0x800C) return;
        try {
            Window w;
            if (!windows.TryGetValue(hwnd,out w)) {
                var cls = new StringBuilder(256); GetClassName(hwnd,cls,cls.Capacity);
                string name = cls.ToString();
                bool console=name == "ConsoleWindowClass" || name.IndexOf("CASCADIA",StringComparison.OrdinalIgnoreCase) >= 0;
                if (!console && (evt != 0x8002 || !IsWindowVisible(hwnd) || GetAncestor(hwnd,2) != hwnd)) return;
                uint pid; GetWindowThreadProcessId(hwnd,out pid);
                w = new Window { Pid=pid, Class=name, Title="", Key=pid + ":" + hwnd.ToInt64().ToString("X") + ":" + Stamp(), Generation=GenerationOf(pid) };
                windows[hwnd] = w;
                windowOrder.Enqueue(new KeyValuePair<IntPtr,string>(hwnd,w.Key));
                while (windowOrder.Count > 8192) { var first=windowOrder.Dequeue(); Window old; if(windows.TryGetValue(first.Key,out old) && old.Key == first.Value) windows.Remove(first.Key); }
            }
            var title = new StringBuilder(1024); GetWindowText(hwnd,title,title.Capacity);
            if (title.Length > 0) w.Title = title.ToString();
            Offer(new Record { Kind="window", At=Stamp(), Event=evt == 0 ? "EXISTING" : evt == 3 ? "FOREGROUND" : evt == 0x8000 ? "CREATE" : evt == 0x8001 ? "DESTROY" : evt == 0x8002 ? "SHOW" : evt == 0x8003 ? "HIDE" : "NAMECHANGE", Hwnd="0x" + hwnd.ToInt64().ToString("X"), Class=w.Class, Title=w.Title, Key=w.Key, Pid=w.Pid, Generation=w.Generation, EventTick=time, Visible=IsWindowVisible(hwnd), Minimized=IsIconic(hwnd), Foreground=GetForegroundWindow() == hwnd, Existing=evt == 0 } );
            if (evt == 0x8001) windows.Remove(hwnd);
        } catch { Interlocked.Increment(ref dropped); }
    }

    static List<Proc> Chain(Proc start) {
        var chain = new List<Proc>(); var seen = new HashSet<uint>();
        for (var p = start; p != null && chain.Count < 8 && seen.Add(p.Id); ) {
            chain.Add(p);
            if (p.ParentId == 0) break;
            var parent = p.ParentSnapshot ?? Known(p.ParentId,p.Started,GenerationOf(p.ParentId));
            if (parent == null) { parent = ReadProcess(p.ParentId); if (parent != null && parent.Started <= p.Started) Remember(parent); else parent = null; }
            p = parent;
        }
        return chain;
    }
    static void Rotate() {
        writer.Flush();
        if (writer.BaseStream.Length < 8 * 1024 * 1024) return;
        writer.Dispose();
        for (int i=3;i>=0;i--) {
            string from = Path.Combine(directory,i == 0 ? "events.jsonl" : "events." + i + ".jsonl");
            string to = Path.Combine(directory,"events." + (i+1) + ".jsonl");
            if (File.Exists(to)) File.Delete(to);
            if (File.Exists(from)) File.Move(from,to);
        }
        writer = new StreamWriter(Path.Combine(directory,"events.jsonl"),true,new UTF8Encoding(false));
    }
    static void Output(Record r) {
        DateTime observed = DateTimeOffset.Parse(r.At,CultureInfo.InvariantCulture).UtcDateTime;
        Proc p = r.Process ?? Known(r.Pid,observed,r.Generation);
        if (p == null && r.Pid != 0 && (r.Kind != "window" || r.Generation != 0)) { p = ReadProcess(r.Pid); if (p != null && p.Started <= observed && (r.Kind != "window" || p.Generation == r.Generation)) Remember(p); else p=null; }
        var chain = Chain(p);
        var list = new List<object>(); var labels = new List<string>();
        string category = "background_or_unknown";
        foreach (var item in chain) {
            list.Add(new { pid=item.Id, parent_pid=item.ParentId, name=item.Name, path=item.Path, command_line=item.Command, started_utc=item.Started.ToString("o"), metadata=item.Detail });
            labels.Add(item.Name + "(" + item.Id + ")");
            if (item.Command.IndexOf("PopupMonitorSelfTest",StringComparison.OrdinalIgnoreCase)>=0) category="self_test";
            else if (category != "self_test" && (item.Name.Equals("codex.exe",StringComparison.OrdinalIgnoreCase) || item.Name.Equals("ChatGPT.exe",StringComparison.OrdinalIgnoreCase))) category="codex_or_diagnostic_activity";
        }
        string label = string.Join(" <- ",labels.ToArray());
        writer.WriteLine(json.Serialize(new { kind=r.Kind, at=r.At, window_event=r.Event, hwnd=r.Hwnd, window_key=r.Key, window_class=r.Class, title=r.Title, pid=r.Pid, process_generation=r.Generation, event_tick=r.EventTick, visible_at_callback=r.Visible, minimized_at_callback=r.Minimized, foreground_at_callback=r.Foreground, existing_at_start=r.Existing, category=category, process_chain=list }));
        Interlocked.Increment(ref written);
        if (r.Kind != "window") return;
        string key;
        if (!windowGroups.TryGetValue(r.Key,out key)) {
            var names = new List<string>(); foreach (var item in chain) names.Add(item.Name);
            key = category + ":" + r.Class + ":" + string.Join(" <- ",names.ToArray());
            if (chain.Count > 1) key += ":" + chain[1].Path;
            if (!groups.ContainsKey(key) && groups.Count >= 511) key="overflow";
            windowGroups[r.Key] = key;
            groupWindowOrder.Enqueue(r.Key);
            while(groupWindowOrder.Count > 8192) windowGroups.Remove(groupWindowOrder.Dequeue());
            Group group;
            if (!groups.TryGetValue(key,out group)) { group=new Group { Source=key == "overflow" ? "Additional sources; see events.jsonl" : r.Class + ": " + label, Category=key == "overflow" ? "overflow" : category }; groups[key]=group; }
            if (r.Existing) group.ExistingWindows++; else { group.NewWindows++; Interlocked.Increment(ref newWindowCount); }
        }
        Group g = groups[key]; g.LastSeen=r.At;
        if (r.Event == "SHOW") g.ShowEvents++;
        if (r.Visible) g.VisibleSamples++;
        if (r.Event == "DESTROY") windowGroups.Remove(r.Key);
    }
    static void WriteSummary() {
        var lines = new List<string>();
        lines.Add("黑窗来源监控（北京时间）"); lines.Add("更新时间：" + Stamp());
        lines.Add("计划结束：" + new DateTimeOffset(endUtc).ToOffset(TimeSpan.FromHours(8)).ToString("o"));
        lines.Add("模式：只记录；不显示、隐藏、最小化或结束其他程序。");
        lines.Add("进程启动事件：" + traceStatus);
        lines.Add("新观测窗口数：" + newWindowCount + "；写入事件：" + written + "；丢弃事件：" + dropped);
        lines.Add("记录包含控制台及其他新出现的顶层窗口；普通应用浮窗也会记录，不代表每一条都是黑窗。");
        lines.Add("SHOW 是异步事件通知；visible_at_callback 是回调时状态，不能据此精确重建屏幕显示时长。");
        lines.Add("codex_or_diagnostic_activity 包含本次排查工具活动，不等于用户空闲时的后台弹窗。");
        var sorted = new List<Group>(groups.Values); sorted.Sort(delegate(Group a, Group b) { return b.NewWindows.CompareTo(a.NewWindows); });
        foreach (var g in sorted) { lines.Add(""); lines.Add("分类：" + g.Category + "；新窗口=" + g.NewWindows + "；启动前已有=" + g.ExistingWindows + "；SHOW通知=" + g.ShowEvents + "；可见状态采样=" + g.VisibleSamples); lines.Add("来源链：" + g.Source); lines.Add("最后记录：" + g.LastSeen); }
        if (sorted.Count == 0) lines.Add("尚未记录到控制台窗口。");
        File.WriteAllLines(Path.Combine(directory,"summary.txt"),lines.ToArray(),new UTF8Encoding(true));
    }
    static void Consume() {
        try {
            long nextSummary=0;
            while (!queue.IsCompleted) {
                Record r;
                if (queue.TryTake(out r,1000)) { Output(r); Rotate(); }
                if (clock.ElapsedMilliseconds >= nextSummary) { WriteSummary(); nextSummary=clock.ElapsedMilliseconds + 30000; writer.Flush(); }
            }
            WriteSummary(); writer.Flush();
        } catch (Exception e) { consumerError=e; }
    }

    [STAThread] static int Main(string[] args) {
        directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"AutoMinimizePopup","monitor",DateTime.Now.ToString("yyyyMMdd-HHmmss"));
        double hours=12;
        for (int i=0;i<args.Length;i++) {
            if (args[i] == "--output" && i+1<args.Length) directory=Path.GetFullPath(args[++i]);
            else if (args[i] == "--hours" && i+1<args.Length) { double value; if (double.TryParse(args[++i],NumberStyles.Float,CultureInfo.InvariantCulture,out value) && value>0 && value<=12) hours=value; }
        }
        Directory.CreateDirectory(directory);
        bool fresh;
        using (var mutex = new Mutex(true,"Local\\AutoMinimizePopupSourceMonitor",out fresh)) {
            if (!fresh) return 2;
            ManagementEventWatcher watcher=null; IntPtr hook=IntPtr.Zero, foreground=IntPtr.Zero; Thread consumer=null;
            try {
                endUtc=DateTime.UtcNow.AddHours(hours);
                File.WriteAllText(Path.Combine(directory,"monitor.pid"),Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture));
                writer=new StreamWriter(Path.Combine(directory,"events.jsonl"),true,new UTF8Encoding(false));
                try { Seed(); } catch (Exception e) { traceStatus="initial snapshot error: " + e.Message; }
                try {
                    watcher=new ManagementEventWatcher("SELECT * FROM Win32_ProcessStartTrace"); watcher.EventArrived+=OnProcess; watcher.Start(); traceReady=true; traceStatus="active";
                } catch (Exception e) { traceStatus="unavailable: " + e.GetBaseException().Message; if (watcher != null) { watcher.Dispose(); watcher=null; } }
                hook=SetWinEventHook(0x8000,0x800C,IntPtr.Zero,callback,0,0,2);
                foreground=SetWinEventHook(3,3,IntPtr.Zero,callback,0,0,2);
                if (hook == IntPtr.Zero || foreground == IntPtr.Zero) throw new InvalidOperationException("Window event hook failed");
                EnumWindows(delegate(IntPtr h, IntPtr ignored) { OnWindow(IntPtr.Zero,0,h,0,0,0,0); return true; },IntPtr.Zero);
                Offer(new Record { Kind="monitor_start", At=Stamp(), Title="read-only; process trace=" + traceStatus });
                consumer=new Thread(Consume); consumer.IsBackground=true; consumer.Start();
                long nextStatus=0;
                while (DateTime.UtcNow<endUtc && !File.Exists(Path.Combine(directory,"monitor.stop"))) {
                    if (consumerError != null) throw new IOException("Log consumer failed",consumerError);
                    MsgWaitForMultipleObjects(0,null,false,1000,0x04FF);
                    MSG m; int pumped=0;
                    while (pumped++ < 256 && PeekMessage(out m,IntPtr.Zero,0,0,1)) { TranslateMessage(ref m); DispatchMessage(ref m); }
                    if (clock.ElapsedMilliseconds >= nextStatus) {
                        File.WriteAllText(Path.Combine(directory,"status.json"),new JavaScriptSerializer().Serialize(new { pid=Process.GetCurrentProcess().Id, status="running", at=Stamp(), end_utc=endUtc.ToString("o"), process_trace=traceStatus, window_hook=true, records=Interlocked.Read(ref written), dropped=Interlocked.Read(ref dropped), process_starts=newProcessCount }));
                        nextStatus=clock.ElapsedMilliseconds + 30000;
                    }
                }
                return 0;
            } catch (Exception e) { File.WriteAllText(Path.Combine(directory,"error.txt"),e.ToString()); return 1; }
            finally {
                if (hook != IntPtr.Zero) UnhookWinEvent(hook);
                if (foreground != IntPtr.Zero) UnhookWinEvent(foreground);
                if (watcher != null) { try { if(traceReady) watcher.Stop(); watcher.Dispose(); } catch { } }
                queue.CompleteAdding();
                if (consumer != null) consumer.Join(5000);
                if (writer != null && (consumer == null || !consumer.IsAlive)) writer.Dispose();
                File.WriteAllText(Path.Combine(directory,"stopped.txt"),Stamp());
                File.WriteAllText(Path.Combine(directory,"status.json"),new JavaScriptSerializer().Serialize(new { pid=Process.GetCurrentProcess().Id, status="stopped", at=Stamp(), process_trace=traceStatus, records=Interlocked.Read(ref written), dropped=Interlocked.Read(ref dropped) }));
                mutex.ReleaseMutex();
            }
        }
    }
}
