using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class VDeskInterop {
  [ComImport, Guid("6D5140C1-7436-11CE-8034-00AA006009FA"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IServiceProvider { [PreserveSig] int QueryService(ref Guid service, ref Guid iid, out IntPtr result); }
  [ComImport, Guid("4CE81583-1E4C-4632-A621-07A53543148F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPinned {
    [PreserveSig] int IsAppIdPinned([MarshalAs(UnmanagedType.LPWStr)] string id, out int pinned);
    [PreserveSig] int PinAppID([MarshalAs(UnmanagedType.LPWStr)] string id);
    [PreserveSig] int UnpinAppID([MarshalAs(UnmanagedType.LPWStr)] string id);
    [PreserveSig] int IsViewPinned(IView view, out int pinned);
    [PreserveSig] int PinView(IView view);
    [PreserveSig] int UnpinView(IView view);
  }
  [ComImport, Guid("1841C6D7-4F9D-42C0-AF41-8747538F10E5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IViews {
    [PreserveSig] int GetViews(out IntPtr p);
    [PreserveSig] int GetViewsByZOrder(out IntPtr p);
    [PreserveSig] int GetViewsByAppUserModelId([MarshalAs(UnmanagedType.LPWStr)] string id, out IntPtr p);
    [PreserveSig] int GetViewForHwnd(IntPtr hwnd, out IView view);
  }
  [ComImport, Guid("372E1D3B-38D3-42E4-A15B-8AB2B178F513"), InterfaceType(ComInterfaceType.InterfaceIsIInspectable)]
  interface IView {
    [PreserveSig] int SetFocus(); [PreserveSig] int SwitchTo(); [PreserveSig] int TryInvokeBack(IntPtr p);
    [PreserveSig] int GetThumbnailWindow(out IntPtr p); [PreserveSig] int GetMonitor(out IntPtr p);
    [PreserveSig] int GetVisibility(out int p); [PreserveSig] int SetCloak(int a, int b);
    [PreserveSig] int GetPosition(ref Guid g, out IntPtr p); [PreserveSig] int SetPosition(ref IntPtr p);
    [PreserveSig] int InsertAfterWindow(IntPtr p); [PreserveSig] int GetExtendedFramePosition(out Rect r);
    [PreserveSig] int GetAppUserModelId([MarshalAs(UnmanagedType.LPWStr)] out string id);
  }
  [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
  [ComImport, Guid("A5CD92FF-29BE-454C-8D04-D82879FB3F1B"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IVDM { [PreserveSig] int IsWindowOnCurrentVirtualDesktop(IntPtr h, out int on); [PreserveSig] int GetWindowDesktopId(IntPtr h, out Guid id); [PreserveSig] int MoveWindowToDesktop(IntPtr h, ref Guid id); }
  [DllImport("ole32.dll")] static extern int CoCreateInstance(ref Guid c, IntPtr outer, uint context, ref Guid iid, out IntPtr p);
  delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  public class Window { public long Hwnd; public uint ProcessId; public string Title; }
  public class Result {
    public long Hwnd; public string AppId; public string Error;
    public string GetViewHR, GetAppIdHR, AppQueryHR, ViewQueryHR, AppActionHR, ViewActionHR, AppAfterHR, ViewAfterHR;
    public bool? AppPinned, ViewPinned, AppPinnedAfter, ViewPinnedAfter;
    public bool Success;
  }
  static string H(int hr) { return "0x" + unchecked((uint)hr).ToString("X8"); }
  static object Service(Guid sid, Guid iid) {
    Guid cls = new Guid("C2F03A33-21F5-47FA-B4BB-156362A2F239");
    Guid isp = new Guid("6D5140C1-7436-11CE-8034-00AA006009FA"); IntPtr p;
    int hr = CoCreateInstance(ref cls, IntPtr.Zero, 5, ref isp, out p);
    if (hr < 0 || p == IntPtr.Zero) throw new COMException("CoCreate ImmersiveShell " + H(hr), hr);
    object o = Marshal.GetObjectForIUnknown(p); Marshal.Release(p);
    try { IntPtr q; hr = ((IServiceProvider)o).QueryService(ref sid, ref iid, out q);
      if (hr < 0 || q == IntPtr.Zero) throw new COMException("QueryService " + sid + " " + H(hr), hr);
      object v = Marshal.GetObjectForIUnknown(q); Marshal.Release(q); return v;
    } finally { Marshal.ReleaseComObject(o); }
  }
  static IPinned Pinned() { return (IPinned)Service(new Guid("B5A399E7-1C87-46B8-88E9-FC5747B171BD"), typeof(IPinned).GUID); }
  static IViews Views() { return (IViews)Service(typeof(IViews).GUID, typeof(IViews).GUID); }
  public static Window[] Windows() {
    var list = new List<Window>(); EnumWindows(delegate(IntPtr h, IntPtr l) {
      if (!IsWindowVisible(h)) return true; var s = new StringBuilder(1024); GetWindowText(h,s,s.Capacity);
      if (s.Length == 0) return true; uint pid; GetWindowThreadProcessId(h,out pid);
      list.Add(new Window { Hwnd=h.ToInt64(), ProcessId=pid, Title=s.ToString() }); return true;
    },IntPtr.Zero); return list.ToArray();
  }
  public static Result Run(long hwnd, string explicitId, string mode) {
    var r = new Result { Hwnd=hwnd, AppId=explicitId }; IPinned pin = null; IViews views = null; IView view = null;
    try {
      pin = Pinned();
      if (hwnd != 0) {
        views = Views(); int hr = views.GetViewForHwnd(new IntPtr(hwnd), out view); r.GetViewHR=H(hr);
        if (hr < 0 || view == null) throw new COMException("GetViewForHwnd " + H(hr), hr);
        string id; hr = view.GetAppUserModelId(out id); r.GetAppIdHR=H(hr);
        if (hr < 0) throw new COMException("GetAppUserModelId " + H(hr), hr);
        r.AppId=id;
      }
      int state, h;
      if (!String.IsNullOrWhiteSpace(r.AppId)) { h=pin.IsAppIdPinned(r.AppId,out state); r.AppQueryHR=H(h); if(h<0) throw new COMException("IsAppIdPinned " + H(h),h); r.AppPinned=state!=0; }
      if (view != null) { h=pin.IsViewPinned(view,out state); r.ViewQueryHR=H(h); if(h<0) throw new COMException("IsViewPinned " + H(h),h); r.ViewPinned=state!=0; }
      if (r.AppPinned == null && r.ViewPinned == null) throw new InvalidOperationException("没有 AppID 或 Shell view");
      if (mode == "pin") {
        if (r.AppPinned == false) { h=pin.PinAppID(r.AppId); r.AppActionHR=H(h); if(h<0) throw new COMException("PinAppID " + H(h),h); }
        if (r.AppPinned == null && r.ViewPinned == false) { h=pin.PinView(view); r.ViewActionHR=H(h); if(h<0) throw new COMException("PinView " + H(h),h); }
      } else if (mode == "unpin") {
        if (r.AppPinned == true) { h=pin.UnpinAppID(r.AppId); r.AppActionHR=H(h); if(h<0) throw new COMException("UnpinAppID " + H(h),h); }
        if (r.ViewPinned == true) { h=pin.UnpinView(view); r.ViewActionHR=H(h); if(h<0) throw new COMException("UnpinView " + H(h),h); }
      }
      if (r.AppPinned != null) { h=pin.IsAppIdPinned(r.AppId,out state); r.AppAfterHR=H(h); if(h<0) throw new COMException("IsAppIdPinned after " + H(h),h); r.AppPinnedAfter=state!=0; }
      if (r.ViewPinned != null) { h=pin.IsViewPinned(view,out state); r.ViewAfterHR=H(h); if(h<0) throw new COMException("IsViewPinned after " + H(h),h); r.ViewPinnedAfter=state!=0; }
      r.Success = mode == "status" || (mode == "pin" ? (r.AppPinnedAfter == true || r.ViewPinnedAfter == true) : (r.AppPinnedAfter != true && r.ViewPinnedAfter != true));
      if (!r.Success) r.Error="操作后状态不符合预期";
    } catch(Exception ex) { r.Error=ex.Message + " (HRESULT " + H(Marshal.GetHRForException(ex)) + ")"; }
    finally { if(view!=null) Marshal.ReleaseComObject(view); if(views!=null) Marshal.ReleaseComObject(views); if(pin!=null) Marshal.ReleaseComObject(pin); }
    return r;
  }
  public static string SelfCheck() {
    var b = new StringBuilder(); Guid c=new Guid("AA509086-5CA9-4C25-8F95-589D3C07B48A"), i=typeof(IVDM).GUID; IntPtr p;
    int hr=CoCreateInstance(ref c,IntPtr.Zero,5,ref i,out p); b.AppendLine("CoCreate IVirtualDesktopManager: " + H(hr));
    if(hr>=0 && p!=IntPtr.Zero) { object o=Marshal.GetObjectForIUnknown(p); Marshal.Release(p); try { var m=(IVDM)o; IntPtr h=GetForegroundWindow(); int on; Guid d; int a=m.IsWindowOnCurrentVirtualDesktop(h,out on), z=m.GetWindowDesktopId(h,out d); b.AppendLine("Foreground IsWindowOnCurrentVirtualDesktop: " + H(a) + " value=" + on); b.AppendLine("Foreground GetWindowDesktopId: " + H(z) + " value=" + d); } finally { Marshal.ReleaseComObject(o); } }
    try { var x=Pinned(); b.AppendLine("QueryService PinnedApps: 0x00000000"); Marshal.ReleaseComObject(x); } catch(Exception ex) { b.AppendLine("PinnedApps: " + ex.Message + " (HRESULT " + H(Marshal.GetHRForException(ex)) + ")"); }
    try { var x=Views(); b.AppendLine("QueryService ApplicationViewCollection: 0x00000000"); Marshal.ReleaseComObject(x); } catch(Exception ex) { b.AppendLine("ApplicationViewCollection: " + ex.Message + " (HRESULT " + H(Marshal.GetHRForException(ex)) + ")"); }
    return b.ToString();
  }
}
