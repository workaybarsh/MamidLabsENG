Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type @'
using System; using System.Runtime.InteropServices; using System.Text; using System.Collections.Generic;
public static class W32 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr h, int m, IntPtr w, string l, int f, int t, out IntPtr r);
  [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, int m, IntPtr w, IntPtr l, int f, int t, out IntPtr r);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, int m, IntPtr w, IntPtr l);
  public struct RECT { public int L, T, R, B; }
  public static string Text(IntPtr h) { var s = new StringBuilder(512); GetWindowText(h, s, 512); return s.ToString(); }
  public static string Cls(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, 256); return s.ToString(); }
  public static List<IntPtr> Tops() { var l = new List<IntPtr>(); EnumWindows((h, x) => { if (IsWindowVisible(h)) l.Add(h); return true; }, IntPtr.Zero); return l; }
  public static List<IntPtr> Kids(IntPtr p) { var l = new List<IntPtr>(); EnumChildWindows(p, (h, x) => { l.Add(h); return true; }, IntPtr.Zero); return l; }
}
'@

# Win32 helpers for the Vista+ common file dialog (Open/Save).
# PITFALL: writing a FULL PATH into the file-name box (GetDlgItem 1148) makes Windows 11
# show "A file name cannot contain any of the following characters: \ / : * ? \" < > |".
# So: first navigate to the target FOLDER (address-bar edit, or the folder path in the
# file-name box + Enter), then write ONLY the file name into the file-name box
# (see ui.ps1 Set-FileDialogTarget).
Add-Type @'
using System; using System.Runtime.InteropServices; using System.Text; using System.Collections.Generic;
public static class FileDlg {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h, int id);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr after, string cls, string title);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr SendMessageText(IntPtr h, int m, IntPtr w, string l);
  [DllImport("user32.dll", EntryPoint="SendMessageW")] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] public static extern uint GetLongPathNameW(string s, StringBuilder l, uint n);
  public const int WM_SETTEXT = 0x000C, WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101, VK_RETURN = 0x0D, BM_CLICK = 0x00F5;
  public static string Cls(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, 256); return s.ToString(); }
  public static List<IntPtr> Tops() { var l = new List<IntPtr>(); EnumWindows((h, x) => { if (IsWindowVisible(h)) l.Add(h); return true; }, IntPtr.Zero); return l; }
  public static IntPtr Deep(IntPtr parent, string cls) {
    var e = FindWindowEx(parent, IntPtr.Zero, cls, null);
    if (e != IntPtr.Zero) return e;
    for (IntPtr c = FindWindowEx(parent, IntPtr.Zero, null, null); c != IntPtr.Zero; c = FindWindowEx(parent, c, null, null)) {
      var r = Deep(c, cls); if (r != IntPtr.Zero) return r;
    }
    return IntPtr.Zero;
  }
  public static IntPtr FindById(IntPtr parent, int id, string cls) {
    IntPtr res = IntPtr.Zero;
    EnumChildWindows(parent, (h, x) => { if (res != IntPtr.Zero) return false; if (GetDlgCtrlID(h) == id && (cls == null || Cls(h) == cls)) { res = h; return false; } return true; }, IntPtr.Zero);
    return res;
  }
  // File-name box: GetDlgItem 1148 (ComboBoxEx32 -> ComboBox -> Edit)
  public static IntPtr FileNameEdit(IntPtr dlg) { var c = GetDlgItem(dlg, 1148); return c == IntPtr.Zero ? IntPtr.Zero : Deep(c, "Edit"); }
  // Editable address-bar Edit (Vista+: id 41477; 1001 on some builds)
  public static IntPtr AddressEdit(IntPtr dlg) {
    var c = FindById(dlg, 41477, "Edit"); if (c != IntPtr.Zero) return c;
    c = FindById(dlg, 1001, "Edit"); if (c != IntPtr.Zero) return c;
    var c2 = FindById(dlg, 41477, null); return c2 == IntPtr.Zero ? IntPtr.Zero : Deep(c2, "Edit");
  }
  // Give focus to a control; join thread inputs so SetFocus works across processes.
  public static void FocusIn(IntPtr dlg, IntPtr ctl) {
    uint fg = GetWindowThreadProcessId(dlg, IntPtr.Zero); uint me = GetCurrentThreadId();
    AttachThreadInput(me, fg, true); SetForegroundWindow(dlg); SetFocus(ctl); AttachThreadInput(me, fg, false);
  }
  public static void SetTextValue(IntPtr h, string t) { SendMessageText(h, WM_SETTEXT, IntPtr.Zero, t); }
  public static void PressEnter(IntPtr h) { SendMessage(h, WM_KEYDOWN, (IntPtr)VK_RETURN, IntPtr.Zero); SendMessage(h, WM_KEYUP, (IntPtr)VK_RETURN, IntPtr.Zero); }
  public static void ClickButton(IntPtr dlg, int id) { var b = GetDlgItem(dlg, id); if (b != IntPtr.Zero) SendMessage(b, BM_CLICK, IntPtr.Zero, IntPtr.Zero); }
  // Convert an 8.3 short path to its long form (the dialog may not navigate a short path).
  public static string LongPath(string p) {
    try { var sb = new StringBuilder(1024); uint n = GetLongPathNameW(p, sb, 1024); if (n > 0) return sb.ToString(); } catch {}
    return p;
  }
}
'@
function Find-Top([string]$like) { [W32]::Tops() | Where-Object { [W32]::Text($_) -like $like } | Select-Object -First 1 }
function Shot-Hwnd($h, [string]$name) {
    [void][W32]::SetForegroundWindow($h); Start-Sleep -Milliseconds 500
    $r = New-Object W32+RECT; [void][W32]::GetWindowRect($h, [ref]$r)
    $bmp = New-Object Drawing.Bitmap ($r.R - $r.L), ($r.B - $r.T); $g = [Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size); $g.Dispose()
    $p = Join-Path $env:TEMP "mamidlabs\ui\shots\$name.png"; $bmp.Save($p); $bmp.Dispose(); $p
}
function Kids-Text($h) { foreach ($k in [W32]::Kids($h)) { if ([W32]::IsWindowVisible($k)) { '{0} [{1}] {2}' -f $k, [W32]::Cls($k), [W32]::Text($k) } } }
function Click-Btn($h) { $o = [IntPtr]::Zero; [void][W32]::PostMessage($h, 0xF5, [IntPtr]::Zero, [IntPtr]::Zero); Start-Sleep -Milliseconds 800 }
