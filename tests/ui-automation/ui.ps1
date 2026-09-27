# UI Automation helpers (Windows PowerShell 5.1). For desktop tests.
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms, System.Drawing
$ErrorActionPreference = 'Stop'
$AE = [Windows.Automation.AutomationElement]
$TS = [Windows.Automation.TreeScope]
$shotDir = Join-Path $env:TEMP 'claude\ui\shots'
New-Item -ItemType Directory -Force -Path $shotDir | Out-Null

Add-Type @'
using System; using System.Runtime.InteropServices;
public static class Win {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}
'@

function Get-Window([int]$procId, [string]$titleLike = '*', [int]$timeoutSec = 60) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $cond = New-Object Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $procId)
        $wins = $AE::RootElement.FindAll($TS::Children, $cond)
        foreach ($w in $wins) { if ($w.Current.Name -like $titleLike) { return $w } }
        Start-Sleep -Milliseconds 500
    }
    throw "Window not found (pid $procId, $titleLike)"
}

function Get-WindowByTitle([string]$titleLike, [int]$timeoutSec = 60) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        foreach ($w in $AE::RootElement.FindAll($TS::Children, [Windows.Automation.Condition]::TrueCondition)) {
            if ($w.Current.Name -like $titleLike) { return $w }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Window not found: $titleLike"
}

function Show-Tree($el, [int]$max = 200) {
    $all = $el.FindAll($TS::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $i = 0
    foreach ($e in $all) {
        if ($i++ -ge $max) { break }
        $c = $e.Current
        if (!$c.IsOffscreen) { '{0,-14} [{1}] en={2} {3}' -f $c.ControlType.ProgrammaticName.Replace('ControlType.', ''), $c.Name, $c.IsEnabled, $c.AutomationId }
    }
}

function Find-El($root, [string]$name, [string]$type = $null) {
    foreach ($e in $root.FindAll($TS::Descendants, [Windows.Automation.Condition]::TrueCondition)) {
        $c = $e.Current
        if ($c.IsOffscreen) { continue }
        if ($type -and $c.ControlType.ProgrammaticName -ne "ControlType.$type") { continue }
        if ($c.Name -like $name) { return $e }
    }
    return $null
}

function Invoke-El($el) {
    $p = $null
    if ($el.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); return }
    if ($el.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); return }
    if ($el.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle(); return }
    throw "Cannot click: $($el.Current.Name)"
}

function Click-Name($root, [string]$name, [string]$type = $null, [int]$timeoutSec = 30) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $el = Find-El $root $name $type
        if ($el -and $el.Current.IsEnabled) { Invoke-El $el; Start-Sleep -Milliseconds 700; return }
        Start-Sleep -Milliseconds 500
    }
    throw "Not found or disabled: $name"
}

function Set-Text($el, [string]$text) {
    $p = $el.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
    $p.SetValue($text)
}

function Save-Shot($win, [string]$name) {
    $h = [IntPtr]$win.Current.NativeWindowHandle
    if ($h -ne [IntPtr]::Zero) { [Win]::ShowWindow($h, 9) | Out-Null; [Win]::SetForegroundWindow($h) | Out-Null; Start-Sleep -Milliseconds 400 }
    $r = $win.Current.BoundingRectangle
    $bmp = New-Object Drawing.Bitmap ([int]$r.Width), ([int]$r.Height)
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $bmp.Size)
    $g.Dispose()
    $path = Join-Path $shotDir ($name + '.png')
    $bmp.Save($path, [Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    return $path
}

Add-Type @'
using System; using System.Runtime.InteropServices; using System.Text;
public static class Msg {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, string l);
}
'@

# Inno Setup buttons show up as Pane in UIA; BM_CLICK is sent to the real HWND.
function Click-Hwnd($el) {
    $h = [IntPtr]$el.Current.NativeWindowHandle
    if ($h -eq [IntPtr]::Zero) { Invoke-El $el; return }
    [void][Msg]::SendMessage($h, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 800
}
function Click-Pane($root, [string]$name, [int]$timeoutSec = 30) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $el = Find-El $root $name
        if ($el -and $el.Current.IsEnabled) { Click-Hwnd $el; return }
        Start-Sleep -Milliseconds 500
    }
    throw "Not found or disabled: $name"
}
function Set-HwndText($el, [string]$text) {
    [void][Msg]::SendMessage([IntPtr]$el.Current.NativeWindowHandle, 0x000C, [IntPtr]::Zero, $text)
}
function Get-Wizard { Get-Window ([int](Get-Content (Join-Path $env:TEMP 'claude\ui\wizard-pid.txt'))) '*' 10 }
