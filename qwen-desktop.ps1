# Chat/image/video generation depends on Start-ThreadJob; it exists only in the bundled
# PowerShell 7, not in Windows' system PowerShell 5.1. If invoked with the wrong host
# (e.g. directly via powershell.exe) it relaunches itself with the correct runtime.
if (!(Get-Command Start-ThreadJob -ErrorAction SilentlyContinue)) {
    # $root is not defined yet (see below); since this file lives in the install root,
    # $PSScriptRoot is the root folder itself.
    $pwsh7 = Join-Path $PSScriptRoot 'runtime\pwsh\pwsh.exe'
    if ((Test-Path -LiteralPath $pwsh7 -PathType Leaf) -and $PSCommandPath) {
        Start-Process -FilePath $pwsh7 -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-WindowStyle', 'Hidden', '-File', "`"$PSCommandPath`"")
    } else {
        [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null
        [System.Windows.Forms.MessageBox]::Show('MamidLabs could not find the bundled PowerShell 7 runtime and was unable to start.', 'MamidLabs') | Out-Null
    }
    return
}

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# Prevents inheriting the powershell.exe host's own Application User Model ID;
# otherwise Windows shows PowerShell's own icon on the taskbar.
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public static class MamidLabsAppId {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SetCurrentProcessExplicitAppUserModelID(string id);
}
'@
[void][MamidLabsAppId]::SetCurrentProcessExplicitAppUserModelID('MamidLabs.Desktop')

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class QwenWindow {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd, System.Text.StringBuilder name, int maxCount);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [StructLayout(LayoutKind.Sequential)] public struct ScrollPoint { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] private struct ScrollInfo {
        public uint cbSize, fMask; public int nMin, nMax; public uint nPage; public int nPos, nTrackPos;
    }
    [DllImport("user32.dll")] private static extern bool GetScrollInfo(IntPtr hwnd, int bar, ref ScrollInfo info);
    [DllImport("user32.dll", EntryPoint = "SendMessageW")] private static extern IntPtr SendPoint(IntPtr hwnd, int msg, IntPtr wParam, ref ScrollPoint point);
    [DllImport("user32.dll", EntryPoint = "SendMessageW")] private static extern IntPtr SendValue(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);
    public static bool NearBottom(IntPtr hwnd) {
        var info = new ScrollInfo { cbSize = (uint)Marshal.SizeOf<ScrollInfo>(), fMask = 7 };
        return !GetScrollInfo(hwnd, 1, ref info) || info.nMax - (int)info.nPage + 1 - info.nPos <= 8;
    }
    public static ScrollPoint GetScroll(IntPtr hwnd) {
        var point = new ScrollPoint(); SendPoint(hwnd, 0x04DD, IntPtr.Zero, ref point); return point;
    }
    public static void SetScroll(IntPtr hwnd, ScrollPoint point) { SendPoint(hwnd, 0x04DE, IntPtr.Zero, ref point); }
    public static void SetRedraw(IntPtr hwnd, bool enabled) { SendValue(hwnd, 0x000B, enabled ? new IntPtr(1) : IntPtr.Zero, IntPtr.Zero); }
}
'@

$appTitle = 'MamidLabs'
# CAUTION: when $null is bound to a string parameter PowerShell turns it into "" and
# FindWindow finds nothing; [NullString]::Value is required to really pass a null class name.
$existingWindow = [QwenWindow]::FindWindow([NullString]::Value, $appTitle)
# Only treat a real application window (WinForms class) as the single instance.
# Explorer's "TabProxyWindow"s can also carry this title, so matching by title alone
# made a leftover window after a force-kill look "already open" and prevented the
# app from EVER starting (seen on a real machine). The class check distinguishes
# them: a real form class starts with WindowsForms10.*.
if ($existingWindow -ne [IntPtr]::Zero) {
    $className = New-Object System.Text.StringBuilder 256
    [void][QwenWindow]::GetClassName($existingWindow, $className, 256)
    if ($className.ToString() -notlike 'WindowsForms10.*') { $existingWindow = [IntPtr]::Zero }
}
if ($existingWindow -ne [IntPtr]::Zero) {
    if ([QwenWindow]::IsIconic($existingWindow)) { [QwenWindow]::ShowWindow($existingWindow, 9) | Out-Null }
    else { [QwenWindow]::ShowWindow($existingWindow, 5) | Out-Null }
    [QwenWindow]::SetForegroundWindow($existingWindow) | Out-Null
    return
}

# ---------------------------------------------------------------- state
# This file lives in the install root; $root is derived from its own location, so
# the app runs from whichever folder it was installed to.
$root = $PSScriptRoot
. (Join-Path $root 'agent\api-providers.ps1')
. (Join-Path $root 'agent\api-settings.ps1')
. (Join-Path $root 'agent\model-registry.ps1')
. (Join-Path $root 'agent\profile-catalog.ps1')
. (Join-Path $root 'agent\media-paths.ps1')
. (Join-Path $root 'agent\media-settings.ps1')
. (Join-Path $root 'agent\hf-settings.ps1')
. (Join-Path $root 'agent\web-settings.ps1')
$script:apiSettings = Read-ApiSettings
$script:providerId = if ($script:apiSettings.selected) { [string]$script:apiSettings.selected } else { 'local' }
$scripts = Join-Path $root 'scripts'
$iconDir = Join-Path $root 'assets\icons'
$chatDir = Join-Path $root 'chats'
if (!(Test-Path -LiteralPath $chatDir)) { New-Item -ItemType Directory -Path $chatDir -Force | Out-Null }
$script:ctxLimit = 32768
$script:compactAt = 22000

$chatMessages = New-Object System.Collections.ArrayList
$bubbles = New-Object System.Collections.ArrayList
$script:selectedImages = New-Object System.Collections.Generic.List[string]
$script:lastUserImage = $null
# Previous user image (the scene) for the two-image mode of photo editing.
$script:previousUserImage = $null
$script:currentTurnHasImage = $false
$script:currentTurnImagePaths = @()
$script:currentTurnAudioPaths = @()
$script:micRecording = $false
# Non-speech audio events (AST): toggled by the microphone-menu "Non-speech sounds" checkbox;
# stored in settings (api.json -> audioEvents).
$script:audioEvents = $true
$script:currentTurnMultiImageOcr = $false
$script:multiImageOcrMode = $false
$script:selectedVideos = New-Object System.Collections.Generic.List[string]
$script:lastUserVideo = $null
$script:selectedDocuments = New-Object System.Collections.Generic.List[string]
# Audio files (transcribed with faster-whisper large-v3; the audio counterpart of image OCR).
$script:selectedAudio = New-Object System.Collections.Generic.List[string]
$script:attachedDocuments = New-Object System.Collections.Generic.List[string]
$script:videoJob = $null
$script:boundFolder = $null
$script:busy = $false
$script:imageBusy = $false
$script:activeBubble = $null
$script:thinkStart = $null
$script:modelReady = $false
$script:chatJob = $null
$script:imageJob = $null
$script:compactJob = $null
$script:sharedCompact = $null
$script:sharedRequest = $null
$script:requestStart = $null
$script:prefillWatch = $false
$script:lastTtftMs = 0
$script:lastPrefillMs = 0
$script:lastProcessedTokens = 0
$script:lastCachedTokens = 0
$script:pendingSend = $false
$script:lastPromptTokens = 0
$script:lastCompletionTokens = 0
$script:contextTokens = 0
$script:activeModel = $null
$script:switchProcess = $null
$script:pendingModelChange = $null
$script:modelSwitchError = $null
$script:suppressModelChoice = $false
$script:stackY = 8
$script:dirty = $false
$script:shuttingDown = $false
$script:lastSpeed = 0
# Last speed test result; preserved when the 2 s status timer rewrites the panel.
$script:benchReport = ''
$script:streamGenerating = $false
$script:lastMetricAt = [DateTime]::MinValue
$script:chatId = $null
$script:chatTitle = ''
$script:accessMode = 'ask'
$script:restoringUiPreferences = $false
$script:approvalOpen = $false

$colBg = [System.Drawing.Color]::FromArgb(22, 25, 32)
$colPanel = [System.Drawing.Color]::FromArgb(28, 31, 39)
$colChat = [System.Drawing.Color]::FromArgb(16, 18, 24)
$colUser = [System.Drawing.Color]::FromArgb(47, 72, 128)
$colModel = [System.Drawing.Color]::FromArgb(36, 40, 50)
$colText = [System.Drawing.Color]::FromArgb(230, 234, 242)
$colMuted = [System.Drawing.Color]::FromArgb(140, 148, 165)
$colAccent = [System.Drawing.Color]::FromArgb(125, 220, 165)
$fontBody = New-Object System.Drawing.Font('Segoe UI', 10.5)
$fontThink = New-Object System.Drawing.Font('Segoe UI', 9.5)
$fontMeta = New-Object System.Drawing.Font('Segoe UI', 9)
$bubbleTip = New-Object System.Windows.Forms.ToolTip

function Set-DoubleBuffered($control) {
    $prop = [System.Windows.Forms.Control].GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'Instance,NonPublic')
    if ($prop) { $prop.SetValue($control, $true, $null) }
}

function Load-UiImage([string]$name) {
    try { return [System.Drawing.Image]::FromFile((Join-Path $iconDir $name)) } catch { return $null }
}
$uiIconLarge = Load-UiImage 'mamidlabs-64.png'
$uiIconBanner = Load-UiImage 'mamidlabs-96.png'
$appIcon = try { New-Object System.Drawing.Icon((Join-Path $iconDir 'mamidlabs.ico')) } catch { $null }

function Get-ActiveModel {
    try { return (Invoke-RestMethod 'http://127.0.0.1:8080/v1/models' -TimeoutSec 2).data[0].id } catch { return $null }
}

function Convert-ImageToDataUri([string]$path) {
    $img = [System.Drawing.Image]::FromFile($path)
    try {
        # Keeping it small reduces mmproj splitting the M-RoPE position encoding into
        # overly small micro-batches (slowdown + inconsistency) in multi-image requests.
        $max = 1024
        $scale = [math]::Min(1.0, [math]::Min($max / [double]$img.Width, $max / [double]$img.Height))
        $w = [int][math]::Round($img.Width * $scale)
        $h = [int][math]::Round($img.Height * $scale)
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        try {
            $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $g.DrawImage($img, 0, 0, $w, $h)
        } finally { $g.Dispose() }
        $ms = New-Object System.IO.MemoryStream
        try {
            $enc = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object MimeType -eq 'image/jpeg'
            $ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
            $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, [long]85)
            $bmp.Save($ms, $enc, $ep)
            return 'data:image/jpeg;base64,' + [Convert]::ToBase64String($ms.ToArray())
        } finally { $ms.Dispose(); $bmp.Dispose() }
    } finally { $img.Dispose() }
}

# ---------------------------------------------------------------- window
$form = New-Object System.Windows.Forms.Form
$form.Text = $appTitle
$workArea = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Size = New-Object System.Drawing.Size(
    [math]::Min(1060, [math]::Max(820, $workArea.Width - 60)),
    [math]::Min(820, [math]::Max(520, $workArea.Height - 50)))
$form.MinimumSize = New-Object System.Drawing.Size(800, 500)
$form.StartPosition = 'CenterScreen'
$form.BackColor = $colBg
$form.ForeColor = $colText
if ($appIcon) { $form.Icon = $appIcon }

$menu = New-Object System.Windows.Forms.MenuStrip
$menu.BackColor = [System.Drawing.Color]::FromArgb(27, 33, 48)
$menu.ForeColor = [System.Drawing.Color]::FromArgb(220, 225, 235)
# Menu order: File leftmost, View right next to it, Profiles rightmost.
$profileMenu = New-Object System.Windows.Forms.ToolStripMenuItem('Profiles')
# The DropDownOpening event of an empty menu never fires; the placeholder is replaced on open.
[void]$profileMenu.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripMenuItem('Loading…')))
$fileMenu = New-Object System.Windows.Forms.ToolStripMenuItem('File')
$viewMenu = New-Object System.Windows.Forms.ToolStripMenuItem('View')
$menu.Items.Add($fileMenu) | Out-Null
$menu.Items.Add($viewMenu) | Out-Null
$menu.Items.Add($profileMenu) | Out-Null

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = 'Fill'
$tabs.Appearance = 'FlatButtons'
$tabs.SizeMode = 'Fixed'
$tabs.ItemSize = New-Object System.Drawing.Size(0, 1)

$chatTab = New-Object System.Windows.Forms.TabPage
$chatTab.BackColor = $colBg
$tabs.TabPages.Add($chatTab)
$controlTab = New-Object System.Windows.Forms.TabPage
$controlTab.BackColor = $colBg
$tabs.TabPages.Add($controlTab)

$form.Controls.Add($tabs)
$form.Controls.Add($menu)
$form.MainMenuStrip = $menu

# ---------------------------------------------------------------- chat area
$chatScroll = New-Object System.Windows.Forms.Panel
$chatScroll.Dock = 'Fill'
$chatScroll.AutoScroll = $true
$chatScroll.BackColor = $colChat
Set-DoubleBuffered $chatScroll

$chatInner = New-Object System.Windows.Forms.Panel
$chatInner.Location = New-Object System.Drawing.Point(0, 0)
$chatInner.BackColor = $colChat
$chatScroll.Controls.Add($chatInner)
Set-DoubleBuffered $chatInner

# Startup welcome panel: logo + name + which models it runs with
$welcome = New-Object System.Windows.Forms.Panel
$welcome.BackColor = $colChat
$chatScroll.Controls.Add($welcome)

$welcomeLogo = New-Object System.Windows.Forms.PictureBox
$welcomeLogo.SizeMode = 'Zoom'
$welcomeLogo.Size = New-Object System.Drawing.Size(96, 96)
$welcomeLogo.BackColor = [System.Drawing.Color]::Transparent
if ($uiIconBanner) { $welcomeLogo.Image = $uiIconBanner }
$welcome.Controls.Add($welcomeLogo)

$welcomeTitle = New-Object System.Windows.Forms.Label
$welcomeTitle.Text = $appTitle
$welcomeTitle.Font = New-Object System.Drawing.Font('Segoe UI', 20, [System.Drawing.FontStyle]::Bold)
$welcomeTitle.ForeColor = [System.Drawing.Color]::FromArgb(205, 213, 228)
$welcomeTitle.TextAlign = 'MiddleCenter'
$welcomeTitle.AutoSize = $false
$welcome.Controls.Add($welcomeTitle)

function Update-WelcomeLayout {
    $w = [math]::Max(320, $chatScroll.ClientSize.Width)
    $welcome.Size = New-Object System.Drawing.Size($w, 150)
    $welcome.Location = New-Object System.Drawing.Point(0, [int](($chatScroll.ClientSize.Height / 2) - 75))
    $welcomeLogo.Location = New-Object System.Drawing.Point([int](($w - 96) / 2), 0)
    $welcomeTitle.Location = New-Object System.Drawing.Point(0, 104)
    $welcomeTitle.Size = New-Object System.Drawing.Size($w, 38)
}
$welcome.BringToFront()

# --------------------------------------------------------- markdown -> RichTextBox
$fontMono = New-Object System.Drawing.Font('Consolas', 9.5)
$colCode = [System.Drawing.Color]::FromArgb(150, 220, 190)
$colHead = [System.Drawing.Color]::FromArgb(245, 248, 255)

function Add-RichRun($rtb, [string]$text, [System.Drawing.Font]$font, [System.Drawing.Color]$color) {
    if ([string]::IsNullOrEmpty($text)) { return }
    $rtb.SelectionStart = $rtb.TextLength
    $rtb.SelectionLength = 0
    $rtb.SelectionFont = $font
    $rtb.SelectionColor = $color
    $rtb.AppendText($text)
}

# LaTeX (math) -> readable Unicode. RichTextBox cannot render math; when the model produced a
# math answer the raw "\frac{}{}", "$$", "\int" markers looked like "broken symbols". This maps
# common LaTeX commands to Unicode equivalents, flattens fractions/roots and turns exponents/
# subscripts into Unicode (x^2 -> x², \int_0 -> ∫₀). The answer stays readable (not real
# typesetting). Case-sensitive (-creplace) and pairs-as-an-array are used because PowerShell
# hashtable keys are case-insensitive ("\Gamma"/"\gamma" would collide).

function Convert-ToSuperscript([string]$t) {
    if (!$t) { return $null }
    $m = @{ '0'='⁰';'1'='¹';'2'='²';'3'='³';'4'='⁴';'5'='⁵';'6'='⁶';'7'='⁷';'8'='⁸';'9'='⁹'
            '+'='⁺';'-'='⁻';'='='⁼';'('='⁽';')'='⁾'
            'a'='ᵃ';'b'='ᵇ';'c'='ᶜ';'d'='ᵈ';'e'='ᵉ';'f'='ᶠ';'g'='ᵍ';'h'='ʰ';'i'='ⁱ';'j'='ʲ'
            'k'='ᵏ';'l'='ˡ';'m'='ᵐ';'n'='ⁿ';'o'='ᵒ';'p'='ᵖ';'r'='ʳ';'s'='ˢ';'t'='ᵗ';'u'='ᵘ'
            'v'='ᵛ';'w'='ʷ';'x'='ˣ';'y'='ʸ';'z'='ᶻ' }
    $sb = New-Object Text.StringBuilder
    foreach ($ch in $t.ToCharArray()) {
        $k = [string]$ch
        if (!$m.ContainsKey($k)) { return $null }
        [void]$sb.Append($m[$k])
    }
    return $sb.ToString()
}

function Convert-ToSubscript([string]$t) {
    if (!$t) { return $null }
    $m = @{ '0'='₀';'1'='₁';'2'='₂';'3'='₃';'4'='₄';'5'='₅';'6'='₆';'7'='₇';'8'='₈';'9'='₉'
            '+'='₊';'-'='₋';'='='₌';'('='₍';')'='₎'
            'a'='ₐ';'e'='ₑ';'h'='ₕ';'i'='ᵢ';'j'='ⱼ';'k'='ₖ';'l'='ₗ';'m'='ₘ';'n'='ₙ';'o'='ₒ'
            'p'='ₚ';'r'='ᵣ';'s'='ₛ';'t'='ₜ';'u'='ᵤ';'v'='ᵥ';'x'='ₓ' }
    $sb = New-Object Text.StringBuilder
    foreach ($ch in $t.ToCharArray()) {
        $k = [string]$ch
        if (!$m.ContainsKey($k)) { return $null }
        [void]$sb.Append($m[$k])
    }
    return $sb.ToString()
}

function Convert-MathText([string]$text) {
    if ([string]::IsNullOrEmpty($text)) { return $text }
    $s = $text
    # Strip "$" delimiters only if there is a LaTeX marker; otherwise a currency "$" is kept.
    if ($s -match '\\[a-zA-Z]+|\$\$') { $s = $s.Replace('$', '') }
    # Display/paren delimiters
    $s = $s -replace '\\\[', ' ' -replace '\\\]', ' ' -replace '\\\(', ' ' -replace '\\\)', ' '
    # Fractions (simple/non-nested): \frac{a}{b} (\dfrac, \tfrac too) -> a/b. A single simple
    # term (d, dx, x, 2) gets no parentheses; otherwise (a)/(b).
    for ($k = 0; $k -lt 3; $k++) {
        $s = [regex]::Replace($s, '\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', {
            param($m)
            $a = $m.Groups[1].Value.Trim(); $b = $m.Groups[2].Value.Trim()
            $na = if ($a -match '^[A-Za-z0-9]+$') { $a } else { '(' + $a + ')' }
            $nb = if ($b -match '^[A-Za-z0-9]+$') { $b } else { '(' + $b + ')' }
            return $na + '/' + $nb
        })
    }
    # Root and box
    for ($k = 0; $k -lt 3; $k++) {
        $s = $s -replace '\\sqrt\s*\{([^{}]*)\}', '√($1)'
        $s = $s -replace '\\boxed\s*\{([^{}]*)\}', '[ $1 ]'
    }
    # Chemistry (mhchem) and physics units: make \ce{...} / \pu{...} readable.
    # Arrows (<->, <=>, ->, <-) and element numbers (H2O) are converted only inside \ce,
    # so text like '->' in code blocks is not corrupted.
    $s = [regex]::Replace($s, '\\(?:ce|pu)\s*\{([^{}]*)\}', {
        param($m)
        $inner = $m.Groups[1].Value
        $inner = $inner -replace '<=>', '⇌' -replace '<->', '↔' -replace '->', '→' -replace '<-', '←'
        $inner = [regex]::Replace($inner, '(?<=[A-Za-z\)])(\d+)', {
            param($d)
            $r = Convert-ToSubscript $d.Groups[1].Value
            if ($r) { $r } else { $d.Value }
        })
        return $inner
    })
    # Text wrappers: keep content
    $s = $s -replace '\\(text|mathrm|mathbf|mathit|operatorname|mbox)\s*\{([^{}]*)\}', '$2'
    # Drop sizing/paren commands
    $s = $s -replace '\\(left|right|big|Big|bigg|Bigg|displaystyle|textstyle)\b', ''
    # Spacing commands
    $s = $s -replace '\\[,;:!>]', ' '
    $s = $s -replace '\\quad', '   ' -replace '\\qquad', '     '
    $s = $s.Replace('~', ' ')
    # Glyph table kept as plain text: PowerShell flattens nested arrays (@(@('a','b')) -> @('a','b'))
    # and would corrupt the table (it was replacing letters wrongly).
    $table = @'
\int=∫
\iint=∬
\oint=∮
\sum=∑
\prod=∏
\infty=∞
\partial=∂
\nabla=∇
\surd=√
\times=×
\div=÷
\cdot=·
\ast=∗
\pm=±
\mp=∓
\leq=≤
\le=≤
\geq=≥
\ge=≥
\neq=≠
\ne=≠
\approx=≈
\equiv=≡
\cong=≅
\sim=∼
\simeq=≃
\propto=∝
\ll=≪
\gg=≫
\longrightarrow=⟶
\rightarrow=→
\to=→
\leftarrow=←
\Rightarrow=⇒
\Leftrightarrow=⇔
\rightleftharpoons=⇌
\leftrightharpoons=⇋
\rightleftarrows=⇄
\leftrightarrows=⇆
\longleftrightarrow=⟷
\Longleftrightarrow=⟺
\Longrightarrow=⟹
\implies=⟹
\hookrightarrow=↪
\hookleftarrow=↩
\upharpoonright=↾
\uparrow=↑
\downarrow=↓
\updownarrow=↕
\mapsto=↦
\notin=∉
\in=∈
\subseteq=⊆
\subset=⊂
\cup=∪
\cap=∩
\emptyset=∅
\varnothing=∅
\nexists=∄
\exists=∃
\forall=∀
\neg=¬
\land=∧
\lor=∨
\perp=⊥
\parallel=∥
\angle=∠
\triangle=△
\degree=°
\circ=∘
\prime=′
\vdots=⋮
\ddots=⋱
\cdots=⋯
\ldots=…
\dots=…
\varepsilon=ε
\epsilon=ε
\vartheta=ϑ
\varphi=ϕ
\varpi=ϖ
\varsigma=ς
\alpha=α
\beta=β
\gamma=γ
\delta=δ
\zeta=ζ
\eta=η
\theta=θ
\iota=ι
\kappa=κ
\lambda=λ
\mu=μ
\nu=ν
\xi=ξ
\omicron=ο
\pi=π
\rho=ρ
\sigma=σ
\tau=τ
\upsilon=υ
\phi=φ
\chi=χ
\psi=ψ
\omega=ω
\Gamma=Γ
\Delta=Δ
\Theta=Θ
\Lambda=Λ
\Xi=Ξ
\Pi=Π
\Sigma=Σ
\Upsilon=Υ
\Phi=Φ
\Psi=Ψ
\Omega=Ω
\ln=ln
\log=log
\exp=exp
\lim=lim
\max=max
\min=min
\sin=sin
\cos=cos
\tan=tan
\cot=cot
\sec=sec
\csc=csc
'@
    $pairs = @($table -split "`n" | Where-Object { $_.Trim() } | ForEach-Object {
        $p = $_.Trim() -split '=', 2; if ($p.Count -eq 2) { , @($p[0], $p[1]) } else { $null }
    } | Where-Object { $_ })
    $pairs = $pairs | Sort-Object { $_[0].Length } -Descending
    # Case-sensitive (-creplace): \Gamma must not match \gamma; word boundary: \in must not
    # corrupt \infty.
    foreach ($g in $pairs) {
        $s = $s -creplace ([regex]::Escape($g[0]) + '(?![a-zA-Z])'), $g[1]
    }
    # Superscripts/subscripts: ^{...}, ^x, _{...}, _x -> Unicode (when fully convertible).
    $s = [regex]::Replace($s, '\^\{([^{}]*)\}', { param($m)
        $r = Convert-ToSuperscript $m.Groups[1].Value
        if ($r) { $r } else { '^(' + $m.Groups[1].Value + ')' } })
    $s = [regex]::Replace($s, '\^([A-Za-z0-9\+\-])', { param($m)
        $r = Convert-ToSuperscript $m.Groups[1].Value
        if ($r) { $r } else { $m.Value } })
    $s = [regex]::Replace($s, '_\{([^{}]*)\}', { param($m)
        $r = Convert-ToSubscript $m.Groups[1].Value
        if ($r) { $r } else { '_(' + $m.Groups[1].Value + ')' } })
    $s = [regex]::Replace($s, '_([A-Za-z0-9\+\-])', { param($m)
        $r = Convert-ToSubscript $m.Groups[1].Value
        if ($r) { $r } else { $m.Value } })
    # Unknown remaining commands: drop the backslash, keep the name
    $s = $s -replace '\\([a-zA-Z]+)', '$1'
    $s = $s -replace '\\', ''
    # Collapse repeated spaces (without touching line ends)
    $s = [regex]::Replace($s, '(?<=\S)[ \t]{2,}(?=\S)', ' ')
    return $s
}

# Writes **bold**, *italic* and `code` segments with separate styles
function Add-RichInline($rtb, [string]$text, [System.Drawing.Font]$baseFont, [System.Drawing.Color]$color) {
    foreach ($part in [regex]::Split($text, '(\*\*.+?\*\*|(?<!\*)\*(?!\*).+?(?<!\*)\*(?!\*)|`[^`]+`)')) {
        if ([string]::IsNullOrEmpty($part)) { continue }
        if ($part.Length -gt 4 -and $part.StartsWith('**') -and $part.EndsWith('**')) {
            Add-RichRun $rtb $part.Substring(2, $part.Length - 4) (New-Object System.Drawing.Font($baseFont, [System.Drawing.FontStyle]::Bold)) $color
        } elseif ($part.Length -gt 2 -and $part.StartsWith('`') -and $part.EndsWith('`')) {
            Add-RichRun $rtb $part.Substring(1, $part.Length - 2) $fontMono $colCode
        } elseif ($part.Length -gt 2 -and $part.StartsWith('*') -and $part.EndsWith('*')) {
            Add-RichRun $rtb $part.Substring(1, $part.Length - 2) (New-Object System.Drawing.Font($baseFont, [System.Drawing.FontStyle]::Italic)) $color
        } else {
            Add-RichRun $rtb $part $baseFont $color
        }
    }
}

function Render-Markdown($rtb, [string]$text, [System.Drawing.Color]$color) {
    $rtb.Clear()
    $text = Convert-MathText $text
    $lines = ($text -replace "`r`n", "`n").Split("`n")
    $i = 0
    while ($i -lt $lines.Count) {
        $line = $lines[$i]
        $trim = $line.Trim()

        if ($trim.StartsWith('```')) {
            $i++
            $buf = New-Object System.Collections.Generic.List[string]
            while ($i -lt $lines.Count -and !$lines[$i].Trim().StartsWith('```')) { $buf.Add($lines[$i]); $i++ }
            $i++
            Add-RichRun $rtb (($buf -join "`n") + "`n") $fontMono $colCode
            continue
        }

        # Table: monospace and aligned columns
        if ($trim.StartsWith('|') -and $trim.EndsWith('|')) {
            $rows = New-Object System.Collections.Generic.List[object]
            while ($i -lt $lines.Count -and $lines[$i].Trim().StartsWith('|')) {
                $cells = @($lines[$i].Trim().Trim('|').Split('|') | ForEach-Object { $_.Trim() })
                $isSep = $true
                foreach ($c in $cells) { if ($c -and $c -notmatch '^:?-{2,}:?$') { $isSep = $false; break } }
                if (!$isSep) { $rows.Add($cells) }
                $i++
            }
            if ($rows.Count -gt 0) {
                $colCount = 0
                foreach ($r in $rows) { if ($r.Count -gt $colCount) { $colCount = $r.Count } }
                $widths = New-Object 'int[]' $colCount
                foreach ($r in $rows) {
                    for ($c = 0; $c -lt $r.Count; $c++) {
                        $plain = ($r[$c] -replace '\*\*|\*|`', '')
                        if ($plain.Length -gt $widths[$c]) { $widths[$c] = $plain.Length }
                    }
                }
                for ($r = 0; $r -lt $rows.Count; $r++) {
                    $cells = $rows[$r]
                    $parts = @()
                    for ($c = 0; $c -lt $colCount; $c++) {
                        $value = $(if ($c -lt $cells.Count) { ($cells[$c] -replace '\*\*|`', '') } else { '' })
                        $parts += $value.PadRight($widths[$c])
                    }
                    $rowText = '  ' + ($parts -join '  │  ') + "`n"
                    if ($r -eq 0) {
                        Add-RichRun $rtb $rowText (New-Object System.Drawing.Font($fontMono, [System.Drawing.FontStyle]::Bold)) $colHead
                        $line = '  ' + (($widths | ForEach-Object { '─' * $_ }) -join '──┼──') + "`n"
                        Add-RichRun $rtb $line $fontMono ([System.Drawing.Color]::FromArgb(110, 120, 140))
                    } else {
                        Add-RichRun $rtb $rowText $fontMono $color
                    }
                }
                Add-RichRun $rtb "`n" $fontBody $color
            }
            continue
        }

        $m = [regex]::Match($trim, '^(#{1,6})\s+(.*)$')
        if ($m.Success) {
            $size = [math]::Max(11.0, 16.0 - ($m.Groups[1].Value.Length * 1.2))
            $headFont = New-Object System.Drawing.Font('Segoe UI', $size, [System.Drawing.FontStyle]::Bold)
            Add-RichRun $rtb ($m.Groups[2].Value + "`n") $headFont $colHead
            $i++
            continue
        }

        if ($trim -match '^[-*+]\s+') {
            Add-RichRun $rtb '  •  ' $fontBody $color
            Add-RichInline $rtb ($trim -replace '^[-*+]\s+', '') $fontBody $color
            Add-RichRun $rtb "`n" $fontBody $color
            $i++
            continue
        }

        if ($trim -match '^\d+[.)]\s+') {
            Add-RichRun $rtb ('  ' + $trim.Substring(0, $trim.IndexOfAny(@('.', ')')) + 1) + ' ') $fontBody $colMuted
            Add-RichInline $rtb ($trim -replace '^\d+[.)]\s+', '') $fontBody $color
            Add-RichRun $rtb "`n" $fontBody $color
            $i++
            continue
        }

        Add-RichInline $rtb $line $fontBody $color
        Add-RichRun $rtb "`n" $fontBody $color
        $i++
    }
    $rtb.SelectionStart = 0
    $rtb.SelectionLength = 0
}

function Measure-Wrapped([string]$text, [System.Drawing.Font]$font, [int]$maxWidth) {
    if ([string]::IsNullOrEmpty($text)) { return New-Object System.Drawing.Size(0, 0) }
    $flags = [System.Windows.Forms.TextFormatFlags]::WordBreak -bor [System.Windows.Forms.TextFormatFlags]::TextBoxControl
    return [System.Windows.Forms.TextRenderer]::MeasureText($text, $font, (New-Object System.Drawing.Size($maxWidth, 0)), $flags)
}

function Get-MaxBubbleWidth {
    return [int][math]::Max(280, $chatInner.ClientSize.Width * 0.78)
}

function Update-Bubble($b) {
    $maxW = Get-MaxBubbleWidth
    $inner = $maxW - 26
    $pad = 13
    $y = $pad
    $used = 120

    if ($b.thinkToggle -and $b.thinkToggle.Visible) {
        $ts = Measure-Wrapped $b.thinkToggle.Text $fontThink $inner
        $h = [math]::Max(20, $ts.Height)
        $b.thinkToggle.Size = New-Object System.Drawing.Size($inner, $h)
        $b.thinkToggle.Location = New-Object System.Drawing.Point($pad, $y)
        $used = [math]::Max($used, $ts.Width)
        $y += $h + 6
    }
    if ($b.thinkBox -and $b.thinkBox.Visible) {
        $tt = Measure-Wrapped $b.thinkBox.Text $fontThink ($inner - 8)
        $h = [math]::Min(230, [math]::Max(34, $tt.Height + 12))
        $b.thinkBox.Size = New-Object System.Drawing.Size($inner, $h)
        $b.thinkBox.Location = New-Object System.Drawing.Point($pad, $y)
        $used = [math]::Max($used, $inner)
        $y += $h + 8
    }
    if ($b.pic -and $b.pic.Visible) {
        $b.pic.Location = New-Object System.Drawing.Point($pad, $y)
        $used = [math]::Max($used, $b.pic.Width)
        $y += $b.pic.Height + 8
    }
    if ($b.body.Text) {
        $bs = Measure-Wrapped $b.body.Text $fontBody $inner
        # Rich content with tables/code needs full width; short plain text stays in a narrow bubble.
        $bodyWidth = $(if ($b.hasRich) { $inner } else { [math]::Min($inner, [math]::Max(140, $bs.Width + 10)) })
        $b.body.Width = $bodyWidth
        $h = [int]$b.contentHeight
        if ($h -lt 18) { $h = [math]::Max(18, $bs.Height) }
        $b.body.Height = $h + 6
        $b.body.Location = New-Object System.Drawing.Point($pad, $y)
        $used = [math]::Max($used, $bodyWidth)
        $y += $b.body.Height
        $b.body.Visible = $true
    } else {
        $b.body.Visible = $false
    }

    if ($b.copyText) {
        $hasText = ![string]::IsNullOrWhiteSpace($b.body.Text)
        $hasFiles = $b.artifacts.Count -gt 0
        $b.copyText.Visible = $hasText
        $b.copyFile.Visible = $hasFiles
        $b.openFile.Visible = $hasFiles
        if ($hasText -or $hasFiles) {
            $y += 8
            $b.copyText.Location = New-Object System.Drawing.Point($pad, $y)
            $fileX = if ($hasText) { $pad + 34 } else { $pad }
            $b.openFile.Location = New-Object System.Drawing.Point($fileX, $y)
            $copyX = $fileX + $b.openFile.Width + 6
            $b.copyFile.Location = New-Object System.Drawing.Point($copyX, $y)
            $b.copyFile.Text = if ($b.artifacts.Count -gt 1) { "Copy files ($($b.artifacts.Count))" } else { 'Copy file' }
            if ($hasFiles) { $used = [math]::Max($used, $copyX - $pad + $b.copyFile.Width) }
            $y += 28
        }
    }

    $width = [math]::Min($maxW, $used + ($pad * 2))
    $b.panel.Size = New-Object System.Drawing.Size($width, ($y + $pad))
    if ($b.role -eq 'user') {
        $b.panel.Left = [math]::Max(12, $chatInner.ClientSize.Width - $width - 16)
    } else {
        $b.panel.Left = 12
    }
}

function Relayout-Chat {
    $chatInner.SuspendLayout()
    $chatInner.Width = [math]::Max(300, $chatScroll.ClientSize.Width)
    $y = 8
    foreach ($b in $bubbles) {
        Update-Bubble $b
        $b.panel.Top = $y
        $y += $b.panel.Height + 10
    }
    $script:stackY = $y
    $chatInner.Height = $y
    $chatInner.ResumeLayout()
}

function Scroll-ChatToEnd {
    $chatScroll.AutoScrollPosition = New-Object System.Drawing.Point(0, [math]::Max(0, $chatInner.Height - $chatScroll.ClientSize.Height))
}

function Test-ChatNearBottom {
    try {
        $maxScroll = [math]::Max(0, $chatInner.Height - $chatScroll.ClientSize.Height)
        $current = -$chatScroll.AutoScrollPosition.Y
        return ($maxScroll - $current) -le 80
    } catch { return $true }
}

function Append-ThinkingText($box, [string]$text) {
    # Even AppendText moves the selection/caret down. Preserve both the reading position and the selection.
    $handle = $box.Handle
    $follow = [QwenWindow]::NearBottom($handle) -and $box.SelectionLength -eq 0
    $position = [QwenWindow]::GetScroll($handle)
    $selectionStart = $box.SelectionStart
    $selectionLength = $box.SelectionLength
    [QwenWindow]::SetRedraw($handle, $false)
    try {
        $box.AppendText($text)
        if ($follow) {
            $box.SelectionStart = $box.TextLength
            $box.SelectionLength = 0
            $box.ScrollToCaret()
        } else {
            $box.Select($selectionStart, $selectionLength)
            [QwenWindow]::SetScroll($handle, $position)
        }
    } finally {
        [QwenWindow]::SetRedraw($handle, $true)
        $box.Invalidate()
    }
}

function Add-BubbleArtifact($bubble, [string]$path) {
    if (!$path) { return }
    if (!$bubble.artifacts.Contains($path)) { [void]$bubble.artifacts.Add($path) }
}

function Get-BubbleClipboardData($bubble, [bool]$files) {
    $data = New-Object System.Windows.Forms.DataObject
    if (!$files) {
        $text = if ($bubble.hasRich -and $bubble.rawText) { [string]$bubble.rawText } else { [string]$bubble.body.Text }
        if ([string]::IsNullOrWhiteSpace($text)) { throw 'There is no text to copy.' }
        $data.SetText($text, [System.Windows.Forms.TextDataFormat]::UnicodeText)
        return $data
    }
    $paths = New-Object System.Collections.Specialized.StringCollection
    foreach ($path in $bubble.artifacts) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "The file is no longer at this location: $path" }
        [void]$paths.Add([IO.Path]::GetFullPath($path))
    }
    if ($paths.Count -eq 0) { throw 'There are no files to copy.' }
    $data.SetFileDropList($paths)
    # A single image can be pasted both as a file into Explorer and as a picture into a document/chat.
    if ($paths.Count -eq 1 -and [IO.Path]::GetExtension($paths[0]) -match '^\.(png|jpe?g|bmp|gif)$') {
        $sourceImage = [Drawing.Image]::FromFile($paths[0])
        try { $data.SetImage([Drawing.Bitmap]::new($sourceImage)) } finally { $sourceImage.Dispose() }
    }
    return $data
}

function Copy-BubbleContent($bubble, [bool]$files) {
    $data = $null
    try {
        $data = Get-BubbleClipboardData $bubble $files
        [Windows.Forms.Clipboard]::SetDataObject($data, $true, 5, 100)
        $metric.Text = if ($files) { 'Material copied to the clipboard. You can paste it with Ctrl+V.' } else { 'Text copied to the clipboard.' }
    } catch { $metric.Text = 'Could not copy: ' + $_.Exception.Message }
    finally {
        if ($data -and $data.ContainsImage()) { $data.GetImage().Dispose() }
    }
}

function Open-BubbleArtifact($bubble) {
    # Opens the generated file (image/video/document) with the default application. If there
    # are several files, all are opened.
    if (!$bubble) { return }
    $opened = 0
    foreach ($path in $bubble.artifacts) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        try { Start-Process -FilePath $path; $opened++ } catch { }
    }
    if ($opened -eq 0) { $metric.Text = 'The file is no longer at this location.' }
}

function New-Bubble([string]$role) {
    $welcome.Visible = $false
    $panel = New-Object System.Windows.Forms.Panel
    $panel.BackColor = $(if ($role -eq 'user') { $colUser } else { $colModel })
    $chatInner.Controls.Add($panel)
    Set-DoubleBuffered $panel

    $body = New-Object System.Windows.Forms.RichTextBox
    $body.ReadOnly = $true
    $body.BorderStyle = 'None'
    $body.BackColor = $panel.BackColor
    $body.ForeColor = $colText
    $body.Font = $fontBody
    $body.ScrollBars = 'None'
    $body.WordWrap = $true
    $body.TabStop = $false
    $body.DetectUrls = $true
    $body.Add_LinkClicked({
        $link = [string]$_.LinkText
        if ($link -match '^https?://') { try { Start-Process -FilePath $link } catch { } }
    })
    $panel.Controls.Add($body)

    $b = @{ role = $role; panel = $panel; body = $body; thinkToggle = $null; thinkBox = $null; pic = $null
            thinkOpen = $false; seconds = $null; contentHeight = 18; hasRich = $false; rawText = ''; ocrText = ''; transcriptText = ''
            answerStarted = $false; answerStartLength = 0; deniedNote = ''
            artifacts = [Collections.ArrayList]::new(); copyText = $null; copyFile = $null; openFile = $null }
    $body.Tag = $b
    $body.Add_ContentsResized({ if ($this.Tag) { $this.Tag.contentHeight = $_.NewRectangle.Height } })

    $copyText = New-Object System.Windows.Forms.Button
    $copyText.Size = New-Object System.Drawing.Size(28, 28)
    $copyText.AccessibleName = 'Copy text'
    $copyText.Tag = $b
    $copyText.Add_Paint({
        $pen = New-Object Drawing.Pen($colMuted, 1.4)
        try { $_.Graphics.DrawRectangle($pen, 10, 6, 10, 11); $_.Graphics.DrawRectangle($pen, 6, 10, 10, 11) } finally { $pen.Dispose() }
    })
    $copyText.Add_Click({ Copy-BubbleContent $this.Tag $false })
    $bubbleTip.SetToolTip($copyText, 'Copy text')
    $copyFile = New-Object System.Windows.Forms.Button
    $copyFile.Size = New-Object System.Drawing.Size(172, 28)
    $copyFile.AccessibleName = 'Copy material'
    $copyFile.Font = $fontMeta
    $copyFile.Tag = $b
    $copyFile.Add_Click({ Copy-BubbleContent $this.Tag $true })
    $bubbleTip.SetToolTip($copyFile, 'Copy the image, video or document as a file')
    # Open the generated file (image/video/document) directly with the default application.
    $openFile = New-Object System.Windows.Forms.Button
    $openFile.Size = New-Object System.Drawing.Size(48, 28)
    $openFile.Text = 'Open'
    $openFile.AccessibleName = 'Open file'
    $openFile.Font = $fontMeta
    $openFile.Tag = $b
    $openFile.Add_Click({ Open-BubbleArtifact $this.Tag })
    $bubbleTip.SetToolTip($openFile, 'Open the generated file with the default application')
    foreach ($button in @($copyText, $copyFile, $openFile)) {
        $button.FlatStyle = 'Flat'
        $button.FlatAppearance.BorderSize = 0
        $button.ForeColor = $colMuted
        $button.BackColor = $panel.BackColor
        $button.Cursor = [Windows.Forms.Cursors]::Hand
        $panel.Controls.Add($button)
    }
    $b.copyText = $copyText
    $b.copyFile = $copyFile
    $b.openFile = $openFile

    if ($role -ne 'user') {
        $toggle = New-Object System.Windows.Forms.Label
        $toggle.AutoSize = $false
        $toggle.Font = $fontThink
        $toggle.ForeColor = $colMuted
        $toggle.BackColor = [System.Drawing.Color]::Transparent
        $toggle.Cursor = [System.Windows.Forms.Cursors]::Hand
        $toggle.Text = 'Thinking…'
        $toggle.Visible = $false
        $panel.Controls.Add($toggle)

        $think = New-Object System.Windows.Forms.RichTextBox
        $think.ReadOnly = $true
        $think.BorderStyle = 'None'
        $think.BackColor = [System.Drawing.Color]::FromArgb(27, 31, 40)
        $think.ForeColor = $colMuted
        $think.Font = $fontThink
        $think.Visible = $false
        $think.ScrollBars = 'Vertical'
        $think.TabStop = $false
        $panel.Controls.Add($think)

        $b.thinkToggle = $toggle
        $b.thinkBox = $think
        $toggle.Tag = $b
        $toggle.Add_Click({
            $bubble = $this.Tag
            if (!$bubble -or !$bubble.seconds) { return }
            if ([string]::IsNullOrWhiteSpace([string]$bubble.thinkBox.Text)) { return }
            $bubble.thinkOpen = !$bubble.thinkOpen
            $bubble.thinkBox.Visible = $bubble.thinkOpen
            $arrow = $(if ($bubble.thinkOpen) { '▾' } else { '▸' })
            $this.Text = (Get-ThinkingLabel $bubble) + " $arrow"
            Relayout-Chat
        })
    }

    [void]$bubbles.Add($b)
    Update-Bubble $b
    $b.panel.Top = $script:stackY
    $script:stackY += $b.panel.Height + 10
    $chatInner.Height = $script:stackY
    return $b
}

function Show-ThinkingStatus($b, [string]$label = 'Thinking…') {
    if (!$b -or !$b.thinkToggle -or !$b.thinkBox) { return }
    $b.thinkToggle.Visible = $true
    # No thinking tokens arrive yet during prefill; instead of showing an empty gray box,
    # show only the live status line. The box opens with the first thinking chunk.
    $b.thinkBox.Visible = $false
    $b.thinkOpen = $false
    $b.thinkToggle.Text = $label
    if (!$script:thinkStart) { $script:thinkStart = Get-Date }
    Relayout-Chat
    Scroll-ChatToEnd
}

function Set-BubbleImage($b, [string]$path) {
    try { $img = [System.Drawing.Image]::FromFile($path) } catch { return }
    $maxW = (Get-MaxBubbleWidth) - 26
    # If portrait images were scaled by width only the bubble would get extremely tall;
    # when the chat is scrolled to the bottom only the lower part shows and it looks "cut in half".
    $scale = [math]::Min(1.0, [math]::Min($maxW / [double]$img.Width, 420.0 / [double]$img.Height))
    $pic = New-Object System.Windows.Forms.PictureBox
    $pic.SizeMode = 'Zoom'
    $pic.Size = New-Object System.Drawing.Size([int]($img.Width * $scale), [int]($img.Height * $scale))
    $pic.Image = $img
    $pic.Cursor = [System.Windows.Forms.Cursors]::Hand
    $pic.Tag = $path
    $pic.Add_Click({ Open-LocalArtifact ([string]$this.Tag) })
    $b.panel.Controls.Add($pic)
    $b.pic = $pic
    Add-BubbleArtifact $b $path
}

function Invoke-FrameExtract([string]$videoPath, [string]$outPath, [string]$which = 'first', [int]$maxSide = 640) {
    $python = Join-Path $root 'venv\imagegen\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    try {
        $payload = @{ action = 'video_frame'; path = $videoPath; which = $which; out = $outPath; max_side = $maxSide } | ConvertTo-Json -Compress
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $python
        $psi.Arguments = "`"$(Join-Path $root 'agent\media-tools.py')`""
        $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
        $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true
        $psi.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
        $p = [System.Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write($payload); $p.StandardInput.Close()
        $p.StandardOutput.ReadToEnd() | Out-Null
        $p.WaitForExit(20000) | Out-Null
    } catch { return $false }
    return (Test-Path -LiteralPath $outPath -PathType Leaf)
}

function Get-VideoThumbnail([string]$videoPath) {
    $thumbDir = Join-Path $root 'outputs\thumbs'
    if (!(Test-Path -LiteralPath $thumbDir)) { New-Item -ItemType Directory -Path $thumbDir -Force | Out-Null }
    $out = Join-Path $thumbDir ([IO.Path]::GetFileNameWithoutExtension($videoPath) + '-thumb.png')
    if (Test-Path -LiteralPath $out -PathType Leaf) { return $out }
    if (Invoke-FrameExtract $videoPath $out 'first' 640) { return $out }
    return $null
}

# Shows the video in the bubble with its first frame; clicking opens it in the system player.
function Set-BubbleVideo($b, [string]$videoPath, [string]$thumbPath) {
    Add-BubbleArtifact $b $videoPath
    if (!$thumbPath -or !(Test-Path -LiteralPath $thumbPath -PathType Leaf)) { $thumbPath = Get-VideoThumbnail $videoPath }
    $pic = New-Object System.Windows.Forms.PictureBox
    $pic.SizeMode = 'Zoom'
    $pic.BackColor = [System.Drawing.Color]::FromArgb(12, 14, 18)
    $maxW = (Get-MaxBubbleWidth) - 26
    if ($thumbPath -and (Test-Path -LiteralPath $thumbPath -PathType Leaf)) {
        try {
            $img = [System.Drawing.Image]::FromFile($thumbPath)
            $scale = [math]::Min(1.0, [math]::Min($maxW / [double]$img.Width, 420.0 / [double]$img.Height))
            $pic.Size = New-Object System.Drawing.Size([int]($img.Width * $scale), [int]($img.Height * $scale))
            $pic.Image = $img
        } catch { $pic.Size = New-Object System.Drawing.Size($maxW, 200) }
    } else {
        $pic.Size = New-Object System.Drawing.Size($maxW, 200)
    }
    $pic.Cursor = [System.Windows.Forms.Cursors]::Hand
    $pic.Tag = $videoPath
    $pic.Add_Click({ Open-LocalArtifact ([string]$this.Tag) })
    # Play triangle in the center
    $pic.Add_Paint({
        $g = $_.Graphics
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $cx = $this.Width / 2; $cy = $this.Height / 2; $r = 26
        $circle = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(150, 0, 0, 0))
        $g.FillEllipse($circle, ($cx - $r), ($cy - $r), ($r * 2), ($r * 2))
        $circle.Dispose()
        $tri = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(235, 255, 255, 255))
        $points = @(
            (New-Object System.Drawing.PointF(($cx - 8), ($cy - 13))),
            (New-Object System.Drawing.PointF(($cx - 8), ($cy + 13))),
            (New-Object System.Drawing.PointF(($cx + 15), $cy))
        )
        $g.FillPolygon($tri, $points)
        $tri.Dispose()
    })
    $b.panel.Controls.Add($pic)
    $b.pic = $pic
}

# When several files (mixed image+video+document) are attached, two image/video previews
# must not be assigned to the single $b.pic slot and overlap - so a plain file list is shown
# that opens on click, with overflowing names automatically shortened with "...".
function Set-BubbleAttachmentList($b, [string[]]$paths) {
    if ($paths.Count -eq 0) { return }
    $maxW = (Get-MaxBubbleWidth) - 26
    $rowH = 22
    $wrap = New-Object System.Windows.Forms.Panel
    $wrap.Size = New-Object System.Drawing.Size($maxW, ($paths.Count * $rowH))
    $wrap.BackColor = [System.Drawing.Color]::Transparent
    $y = 0
    foreach ($path in $paths) {
        $icon = switch -Regex ($path) {
            '\.(png|jpe?g|webp|bmp|gif)$' { 'Image:'; break }
            '\.(mp4|mov|mkv|webm|avi|m4v)$' { 'Video:'; break }
            default { 'Document:' }
        }
        $link = New-Object System.Windows.Forms.LinkLabel
        $link.AutoSize = $false
        $link.AutoEllipsis = $true
        $link.Size = New-Object System.Drawing.Size($maxW, $rowH)
        $link.Location = New-Object System.Drawing.Point(0, $y)
        $link.Text = "$icon $([System.IO.Path]::GetFileName($path))"
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
            $link.Text += ' (file not found)'
            $link.Enabled = $false
        }
        $link.LinkColor = $colAccent
        $link.Font = $fontMeta
        $link.Tag = $path
        $link.Add_LinkClicked({ Open-LocalArtifact ([string]$this.Tag) })
        $wrap.Controls.Add($link)
        Add-BubbleArtifact $b $path
        $y += $rowH
    }
    $b.panel.Controls.Add($wrap)
    $b.pic = $wrap
}

# ---------------------------------------------------------------- composer
$composer = New-Object System.Windows.Forms.Panel
$composer.BackColor = $colPanel
$composer.Dock = 'Bottom'
$composer.Height = 220

$chatTab.Controls.Add($chatScroll)
$chatTab.Controls.Add($composer)

$drop = New-Object System.Windows.Forms.Button
$drop.Text = '＋'
$drop.FlatStyle = 'Flat'
$drop.BackColor = [System.Drawing.Color]::FromArgb(43, 48, 62)
$drop.ForeColor = [System.Drawing.Color]::FromArgb(205, 212, 228)
$drop.FlatAppearance.BorderSize = 0
$composer.Controls.Add($drop)

$folder = New-Object System.Windows.Forms.Button
$folder.Text = 'Select folder'
$folder.TextAlign = 'MiddleLeft'
$folder.FlatStyle = 'Flat'
$folder.BackColor = [System.Drawing.Color]::FromArgb(35, 39, 49)
$folder.ForeColor = [System.Drawing.Color]::FromArgb(205, 210, 225)
$folder.FlatAppearance.BorderSize = 0
$composer.Controls.Add($folder)

$clearImage = New-Object System.Windows.Forms.Button
$clearImage.Text = 'Remove image'
$clearImage.FlatStyle = 'Flat'
$clearImage.BackColor = [System.Drawing.Color]::FromArgb(70, 72, 82)
$clearImage.ForeColor = [System.Drawing.Color]::White
$clearImage.FlatAppearance.BorderSize = 0
$clearImage.Visible = $false
$composer.Controls.Add($clearImage)

$promptBox = New-Object System.Windows.Forms.TextBox
$promptBox.Multiline = $true
$promptBox.BackColor = [System.Drawing.Color]::FromArgb(35, 39, 49)
$promptBox.ForeColor = [System.Drawing.Color]::White
$promptBox.Font = New-Object System.Drawing.Font('Segoe UI', 11)
$promptBox.BorderStyle = 'FixedSingle'
$promptBox.ScrollBars = 'Vertical'
$composer.Controls.Add($promptBox)

$send = New-Object System.Windows.Forms.Button
$send.Text = 'Send'
$send.Font = New-Object System.Drawing.Font('Segoe UI', 10.5, [System.Drawing.FontStyle]::Bold)
$send.FlatStyle = 'Flat'
$send.BackColor = [System.Drawing.Color]::FromArgb(75, 105, 185)
$send.ForeColor = [System.Drawing.Color]::White
$send.FlatAppearance.BorderSize = 0
$composer.Controls.Add($send)

# Microphone button: immediately left of Send. While the microphone is RED (recording) clicking
# the button CANCELS the recording (stops it, deletes the file, nothing is sent); when idle it
# opens a small menu ("Use microphone" / "Non-speech sounds" / "Cancel"). Audio is transcribed
# automatically (faster-whisper) and the findings are given to the language model (the audio
# counterpart of OCR). Right-click ALWAYS opens the menu, so a recording can be finished+sent or
# cancelled even while it is running.
# GDI cannot draw color emoji; the Segoe MDL2 Assets microphone glyph (U+E720) is used.
$micBtn = New-Object System.Windows.Forms.Button
$micBtn.Text = [string][char]0xE720
$micBtn.Font = New-Object System.Drawing.Font('Segoe MDL2 Assets', 12)
$micBtn.FlatStyle = 'Flat'
$micBtn.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 76)
$micBtn.ForeColor = [System.Drawing.Color]::White
$micBtn.FlatAppearance.BorderSize = 0
$micBtn.AccessibleName = 'Add audio'
$composer.Controls.Add($micBtn)

$micMenu = New-Object System.Windows.Forms.ContextMenuStrip
$micUse = $micMenu.Items.Add('Use microphone')
[void]$micMenu.Items.Add('-')
# Non-speech sounds: when checked, AST audio events are captured (default on); when
# unchecked only the Whisper speech transcript is produced.
$script:audioEvents = if ($null -ne $script:apiSettings.audioEvents) { [bool]$script:apiSettings.audioEvents } else { $true }
$micEvents = New-Object System.Windows.Forms.ToolStripMenuItem('Non-speech sounds')
$micEvents.CheckOnClick = $true
$micEvents.Checked = $script:audioEvents
[void]$micMenu.Items.Add($micEvents)
[void]$micMenu.Items.Add('-')
$micCancel = $micMenu.Items.Add('Cancel')
# While recording the labels become "Finish and send" / "Cancel recording".
function Show-MicMenu {
    if ($script:micRecording) {
        $micUse.Text = 'Finish and send'
        $micCancel.Text = 'Cancel recording'
    } else {
        $micUse.Text = 'Use microphone'
        $micCancel.Text = 'Cancel'
    }
    $micMenu.Show($micBtn, 0, $micBtn.Height)
}
# Left click: CANCEL while recording (stop, delete, send nothing); otherwise open the menu.
# To finish and SEND, use the right-click menu or the Send button.
$micBtn.Add_Click({
    if ($script:micRecording) { Cancel-MicrophoneCapture }
    else { Show-MicMenu }
})
# Right click: always the menu (cancel is reachable while recording).
$micBtn.Add_MouseUp({
    param($sender, $mouseEvent)
    if ($mouseEvent.Button -eq [System.Windows.Forms.MouseButtons]::Right) { Show-MicMenu }
})
$micUse.Add_Click({
    if ($script:micRecording) { Complete-MicrophoneCapture }
    else { Start-MicrophoneCapture }
})
$micEvents.Add_Click({
    $script:audioEvents = [bool]$micEvents.Checked
    $script:apiSettings.audioEvents = $script:audioEvents
    Save-ApiSettings
    $metric.Text = if ($script:audioEvents) { 'Non-speech sounds: on' } else { 'Non-speech sounds: off' }
})
$micCancel.Add_Click({
    if ($script:micRecording) { Cancel-MicrophoneCapture }
    else { $micMenu.Close() }
})

function New-Chip([string]$text, [bool]$isChecked) {
    $c = New-Object System.Windows.Forms.CheckBox
    $c.Appearance = 'Button'
    $c.Text = $text
    $c.TextAlign = 'MiddleCenter'
    $c.FlatStyle = 'Flat'
    $c.Font = $fontMeta
    $c.Checked = $isChecked
    $c.FlatAppearance.BorderSize = 0
    $c.FlatAppearance.CheckedBackColor = [System.Drawing.Color]::FromArgb(52, 84, 150)
    $c.BackColor = [System.Drawing.Color]::FromArgb(38, 42, 54)
    $c.ForeColor = [System.Drawing.Color]::FromArgb(215, 222, 236)
    $composer.Controls.Add($c)
    return $c
}

function Save-UiPreferences {
    if ($script:restoringUiPreferences) { return }
    $script:apiSettings.uiPreferences = @{
        internet = [bool]$netChip.Checked
        agent = [bool]$agentChip.Checked
        accessMode = [string]$script:accessMode
    }
    try { Save-ApiSettings } catch {
        [void][System.Windows.Forms.MessageBox]::Show("Preferences could not be saved: $($_.Exception.Message)", 'Settings save error')
    }
}

function Restore-UiPreferences {
    $prefs = $script:apiSettings.uiPreferences
    $script:restoringUiPreferences = $true
    try {
        if ($prefs) {
            if ($null -ne $prefs.internet) { $netChip.Checked = [bool]$prefs.internet }
            if ($null -ne $prefs.agent) { $agentChip.Checked = [bool]$prefs.agent }
        }
        $fullAccessChip.Checked = ([string]$prefs.accessMode -eq 'full')
        $script:accessMode = if ($fullAccessChip.Checked) { 'full' } else { 'ask' }
    } finally {
        $script:restoringUiPreferences = $false
    }
}

$netChip = New-Chip 'Internet' $true
$agentChip = New-Chip 'Agent' $true
$fullAccessChip = New-Chip 'Full access' $false
$fullAccessChip.Add_Click({
    if ($fullAccessChip.Checked) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "With full access the agent writes files, creates documents and runs the commands you ask for without asking you. This choice is saved and stays in effect when the app is reopened. Use the Full access button to turn it off.`n`nTurn it on?",
            'Full access', [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { $fullAccessChip.Checked = $false; return }
        $script:accessMode = 'full'
        if ($script:sharedRequest) { $script:sharedRequest.AccessMode = 'full' }
        Save-UiPreferences
    } else {
        $script:accessMode = 'ask'
        if ($script:sharedRequest) { $script:sharedRequest.AccessMode = 'ask' }
        Save-UiPreferences
    }
})
$netChip.Add_CheckedChanged({ Save-UiPreferences })
$agentChip.Add_CheckedChanged({ Save-UiPreferences })
Restore-UiPreferences

$modelChoice = New-Object System.Windows.Forms.ComboBox
$modelChoice.DropDownStyle = 'DropDownList'
$modelChoice.Items.Add('Qwen3.8 MAX') | Out-Null
$modelChoice.Items.Add('Qwen3.8 FLASH') | Out-Null
$modelChoice.SelectedIndex = 0
$modelChoice.BackColor = [System.Drawing.Color]::FromArgb(35, 39, 49)
$modelChoice.ForeColor = [System.Drawing.Color]::FromArgb(220, 225, 235)
$modelChoice.FlatStyle = 'Flat'
$composer.Controls.Add($modelChoice)

$metric = New-Object System.Windows.Forms.Label
$metric.Text = 'Checking model…'
$metric.Font = $fontMeta
$metric.ForeColor = $colAccent
$metric.AutoEllipsis = $true
$composer.Controls.Add($metric)

# Status messages do not overwrite the speed/token field; it stays visible during VRAM swaps too.
$liveMetric = New-Object System.Windows.Forms.Label
$liveMetric.Font = $fontMeta
$liveMetric.ForeColor = $colAccent
$liveMetric.TextAlign = 'MiddleRight'
$composer.Controls.Add($liveMetric)
$metricTip = New-Object System.Windows.Forms.ToolTip

function Update-LiveMetrics {
    $speed = 0.0
    if ($script:streamGenerating -and !$script:imageBusy -and
        ([DateTime]::UtcNow - $script:lastMetricAt).TotalSeconds -lt 3) { $speed = $script:lastSpeed }
    $contextLimitValue = [int]$script:ctxLimit
    if ($contextLimitValue -le 0) { $contextLimitValue = 32768 }
    $liveMetric.Text = '{0:0.0} tok/s  |  {1:0.0}K / {2:0.0}K' -f $speed, ($script:contextTokens / 1000.0), ($contextLimitValue / 1000.0)
    if($script:providerId -and $script:providerId -ne 'local'){$liveMetric.Text='API  |  {0:0.0}K / {1:0.0}K' -f ($script:contextTokens / 1000.0),($contextLimitValue / 1000.0)}
    $detail = "Context: $script:contextTokens / $contextLimitValue tokens | Input: $script:lastPromptTokens | Output: $script:lastCompletionTokens"
    if ($script:lastTtftMs) {
        $detail += ' | First token: {0:0.0} s' -f ($script:lastTtftMs / 1000.0)
    }
    if ($script:lastPrefillMs -or $script:lastProcessedTokens -or $script:lastCachedTokens) {
        $detail += ' | Prefill: {0:0.0} s, {1} tokens reprocessed, {2} tokens from cache' -f `
            ($script:lastPrefillMs / 1000.0), $script:lastProcessedTokens, $script:lastCachedTokens
    }
    if ($script:contextTokens -gt [int]$script:compactAt) { $detail += ' | Compaction queued' }
    if ($script:imageBusy) { $detail += ' | The language model produces no tokens during image/video generation' }
    if ($metricTip.GetToolTip($liveMetric) -ne $detail) { $metricTip.SetToolTip($liveMetric, $detail) }
    # The time until the first token is the server processing the prefix; instead of waiting
    # silently we show a counter so there is no "is it stuck?" uncertainty.
    if ($script:prefillWatch -and $script:requestStart) {
        $wait = ((Get-Date) - $script:requestStart).TotalSeconds
        if ($wait -ge 2) { $metric.Text = 'Thinking… prefill {0:0} s' -f $wait }
    }
}
Update-LiveMetrics
Update-ProviderSelection

function Update-ComposerLayout {
    $cw = [math]::Max(600, $composer.ClientSize.Width)
    $drop.Location = New-Object System.Drawing.Point(14, 10)
    $drop.Size = New-Object System.Drawing.Size(40, 32)
    $folder.Location = New-Object System.Drawing.Point(60, 10)
    $folder.Size = New-Object System.Drawing.Size(($cw - 220), 32)
    $clearImage.Location = New-Object System.Drawing.Point(($cw - 150), 10)
    $clearImage.Size = New-Object System.Drawing.Size(136, 32)
    $promptBox.Location = New-Object System.Drawing.Point(14, 52)
    $promptBox.Size = New-Object System.Drawing.Size(($cw - 28), 80)
    $netChip.Location = New-Object System.Drawing.Point(14, 142)
    $netChip.Size = New-Object System.Drawing.Size(76, 30)
    $agentChip.Location = New-Object System.Drawing.Point(94, 142)
    $agentChip.Size = New-Object System.Drawing.Size(62, 30)
    $fullAccessChip.Location = New-Object System.Drawing.Point(160, 142)
    $fullAccessChip.Size = New-Object System.Drawing.Size(110, 30)
    $send.Location = New-Object System.Drawing.Point(($cw - 118), 178)
    $send.Size = New-Object System.Drawing.Size(104, 30)
    $micBtn.Location = New-Object System.Drawing.Point(($cw - 152), 178)
    $micBtn.Size = New-Object System.Drawing.Size(30, 30)
    $metric.Location = New-Object System.Drawing.Point(278, 148)
    $metric.Size = New-Object System.Drawing.Size(140, 22)
    $modelChoice.Location = New-Object System.Drawing.Point(426, 142)
    $modelChoice.Size = New-Object System.Drawing.Size([math]::Max(160, ($cw - 440)), 30)
    $modelChoice.DropDownWidth = $modelChoice.Width
    $liveMetric.Location = New-Object System.Drawing.Point(($cw - 514), 178)
    $liveMetric.Size = New-Object System.Drawing.Size(230, 28)
}
$composer.Add_Resize({ Update-ComposerLayout })
Update-ComposerLayout

$chatScroll.Add_Resize({
    Update-WelcomeLayout
    Relayout-Chat
})

# ---------------------------------------------------------------- attaching images
# Images, videos and documents can be attached together. For images the default rule is a
# single file; multi-select is accepted only after the explicit multi-image OCR mode is enabled.
# The badge label summarizes everything attached.
function Reset-CurrentTurnImageState {
    $script:currentTurnHasImage = $false
    $script:currentTurnImagePaths = @()
    $script:currentTurnAudioPaths = @()
    $script:currentTurnMultiImageOcr = $false
}

function Update-AttachmentBadge {
    $count = $script:selectedImages.Count + $script:selectedVideos.Count + $script:selectedDocuments.Count + $script:selectedAudio.Count
    if ($count -eq 0) { $clearImage.Visible = $false; return }
    $clearImage.Visible = $true
    if ($count -eq 1) {
        $only = @($script:selectedImages) + @($script:selectedVideos) + @($script:selectedDocuments) + @($script:selectedAudio)
        $icon = if ($script:selectedImages.Count) { 'Image:' } elseif ($script:selectedVideos.Count) { 'Video:' } elseif ($script:selectedDocuments.Count) { 'Document:' } else { 'Audio:' }
        # The badge is 136 px and fits ~17 characters per line; a long file name overflowed to
        # two lines and got cut off at the bottom. Only the end of the name (with extension) is shown, the full name is in the tooltip.
        $name = [System.IO.Path]::GetFileName($only[0])
        $short = if ($name.Length -gt 9) { '…' + $name.Substring($name.Length - 8) } else { $name }
        $clearImage.Text = "$icon $short"
        $bubbleTip.SetToolTip($clearImage, "$name`r`nClick to remove")
    } else {
        if ($script:multiImageOcrMode -and $script:selectedImages.Count -gt 1) {
            $clearImage.Text = "Multi-image reading on · $($script:selectedImages.Count) images ready"
            return
        }
        # GDI cannot draw color emoji (an empty box appeared); kinds are written as text.
        $kinds = @()
        if ($script:selectedImages.Count) { $kinds += 'image' }
        if ($script:selectedVideos.Count) { $kinds += 'video' }
        if ($script:selectedDocuments.Count) { $kinds += 'document' }
        if ($script:selectedAudio.Count) { $kinds += 'audio' }
        $clearImage.Text = "$count files attached (" + ($kinds -join ', ') + ")"
    }
}
function Add-SelectedImage([string]$path) {
    if (!$path) { return }
    if (@($script:selectedImages) -contains $path) { return }
    if (!$script:multiImageOcrMode -and $script:selectedImages.Count -ge 1) {
        $metric.Text = 'First send the message "turn on multi-image reading mode"; the default rule is a single image.'
        return
    }
    # The multi-select dialog runs before the prompt is written; if the explicit mode is on,
    # keep the earlier selections and add the new image.
    [void]$script:selectedImages.Add($path)
    Update-AttachmentBadge
}
function Add-SelectedVideo([string]$path) {
    if (!$path) { return }
    [void]$script:selectedVideos.Add($path)
    Update-AttachmentBadge
}
function Add-SelectedDocument([string]$path) {
    if (!$path) { return }
    [void]$script:selectedDocuments.Add($path)
    Update-AttachmentBadge
}
function Add-SelectedAudio([string]$path) {
    if (!$path) { return }
    if (@($script:selectedAudio) -contains $path) { return }
    [void]$script:selectedAudio.Add($path)
    Update-AttachmentBadge
}
# File picker for the microphone button / "Upload audio file".
function Pick-AudioFile {
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    try {
        $dlg.Title = 'Choose an audio file'
        $dlg.Filter = 'Audio files|*.wav;*.mp3;*.m4a;*.ogg;*.flac;*.opus;*.aac;*.wma|All files|*.*'
        $dlg.Multiselect = $true
        if ($dlg.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) {
            foreach ($f in $dlg.FileNames) { [void](Add-SelectedFile $f) }
        }
    } catch { $metric.Text = 'Could not attach the audio file: ' + $_.Exception.Message }
    finally { $dlg.Dispose() }
}
# Microphone capture (dependency-free): records via winmm mciSendString. `open new type
# waveaudio` uses the Windows DEFAULT recording device (WAVE_MAPPER) — no device selection needed.
# The recording format is the device default; whisper-tools.py converts it to 16 kHz mono and
# transcribes with faster-whisper large-v3. When finished the WAV is attached as an audio
# attachment and sent automatically (transcript + language-model answer).
function Add-MamidMciType {
    if (-not ([System.Management.Automation.PSTypeName]'MamidMci').Type) {
        Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices;
public static class MamidMci {
  [DllImport("winmm.dll", CharSet=CharSet.Unicode)] public static extern int mciSendString(string c, StringBuilder r, int n, IntPtr h);
}
'@
    }
}
function Start-MicrophoneCapture {
    if ($script:micRecording) { return }
    Add-MamidMciType
    $alias = 'mamidmic'
    try {
        [void][MamidMci]::mciSendString("close $alias", $null, 0, [IntPtr]::Zero)
        $openCode = [MamidMci]::mciSendString("open new type waveaudio alias $alias", $null, 0, [IntPtr]::Zero)
        if ($openCode -ne 0) { throw ('mci open: ' + $openCode) }
        $recCode = [MamidMci]::mciSendString("record $alias", $null, 0, [IntPtr]::Zero)
        if ($recCode -ne 0) { throw ('mci record: ' + $recCode) }
        $script:micRecording = $true
        $micBtn.BackColor = [System.Drawing.Color]::FromArgb(180, 60, 60)
        $metric.Text = 'Recording started — press the microphone button to cancel; use the right-click menu or Send to finish and send.'
    } catch {
        $script:micRecording = $false
        $metric.Text = 'Could not open the microphone: ' + $_.Exception.Message
    }
}
# Stops the recording. With $discard the file is NOT saved (cancel): the device is closed and the
# state reset. Returns $null when not recording; otherwise the WAV path to be created (may not exist).
function Stop-MicrophoneCapture([switch]$discard) {
    if (!$script:micRecording) { return $null }
    Add-MamidMciType
    $alias = 'mamidmic'
    $wav = $null
    try {
        [void][MamidMci]::mciSendString("stop $alias", $null, 0, [IntPtr]::Zero)
        $dir = Join-Path $root 'outputs\input'
        if (!(Test-Path -LiteralPath $dir -PathType Container)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $wav = Join-Path $dir ('microphone-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.wav')
        if (!$discard) { [void][MamidMci]::mciSendString('save ' + $alias + ' "' + $wav + '"', $null, 0, [IntPtr]::Zero) }
        [void][MamidMci]::mciSendString("close $alias", $null, 0, [IntPtr]::Zero)
    } finally {
        $script:micRecording = $false
        $micBtn.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 76)
    }
    return $wav
}
# Finishes the recording, attaches it as an audio attachment and (when idle) sends it.
function Complete-MicrophoneCapture {
    $wav = Stop-MicrophoneCapture
    if ($wav -and (Test-Path -LiteralPath $wav -PathType Leaf)) {
        Add-SelectedAudio $wav
        $metric.Text = 'Microphone recording attached; transcribing…'
        if (!$script:busy -and !$script:pendingSend -and !$script:imageBusy) { $send.PerformClick() }
    } else {
        $metric.Text = 'Microphone recording was not created (no device or access denied).'
    }
}
# Cancels the recording: stops it, deletes the file; nothing is sent.
function Cancel-MicrophoneCapture {
    $wav = Stop-MicrophoneCapture -discard
    if ($wav -and (Test-Path -LiteralPath $wav -PathType Leaf)) { Remove-Item -LiteralPath $wav -Force -ErrorAction SilentlyContinue }
    $metric.Text = 'Microphone recording cancelled.'
}
function Add-SelectedFile([string]$path) {
    if (!$path -or !(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    $extension = [IO.Path]::GetExtension($path).ToLowerInvariant()
    $limit = switch -Regex ($extension) {
        '^\.(png|jpe?g|webp|bmp|gif)$' { 100MB; break }
        '^\.(mp4|mov|mkv|webm|avi|m4v)$' { 512MB; break }
        '^\.(pdf|docx|xlsx|xlsm|pptx|zip|rar)$' { 256MB; break }
        '^\.(wav|mp3|m4a|ogg|flac|opus|aac|wma)$' { 512MB; break }
        default { 0 }
    }
    if (!$limit) { return $false }
    if ((Get-Item -LiteralPath $path).Length -gt $limit) {
        $metric.Text = "File size limit exceeded (at most $([math]::Round($limit / 1MB)) MB)."
        return $false
    }
    if ($extension -match '^\.(png|jpe?g|webp|bmp|gif)$') { Add-SelectedImage $path; return $true }
    if ($extension -match '^\.(mp4|mov|mkv|webm|avi|m4v)$') { Add-SelectedVideo $path; return $true }
    if ($extension -match '^\.(pdf|docx|xlsx|xlsm|pptx|zip|rar)$') { Add-SelectedDocument $path; return $true }
    if ($extension -match '^\.(wav|mp3|m4a|ogg|flac|opus|aac|wma)$') { Add-SelectedAudio $path; return $true }
    return $false
}
function Save-ClipboardImage {
    $image = $null
    $copy = $null
    try {
        $image = [Windows.Forms.Clipboard]::GetImage()
        if (!$image) { return $null }
        $copy = [Drawing.Bitmap]::new($image)
        $dir = Join-Path $root 'outputs\input'
        if (!(Test-Path -LiteralPath $dir -PathType Container)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $path = Join-Path $dir ('clipboard-' + [guid]::NewGuid().ToString('N') + '.png')
        $copy.Save($path, [Drawing.Imaging.ImageFormat]::Png)
        return $path
    } catch {
        $metric.Text = 'Could not attach the clipboard image: ' + $_.Exception.Message
        return $null
    } finally {
        if ($copy) { $copy.Dispose() }
        if ($image) { $image.Dispose() }
    }
}
function Paste-ClipboardAttachments {
    try {
        if ([Windows.Forms.Clipboard]::ContainsFileDropList()) {
            $added = $false
            foreach ($path in @([Windows.Forms.Clipboard]::GetFileDropList())) {
                if (Add-SelectedFile ([string]$path)) { $added = $true }
            }
            if ($added) { Update-AttachmentBadge }
            return $added
        }
        if ([Windows.Forms.Clipboard]::ContainsImage()) {
            $path = Save-ClipboardImage
            if ($path) { Add-SelectedImage $path; Update-AttachmentBadge; return $true }
        }
    } catch {
        $metric.Text = 'Could not attach the clipboard content: ' + $_.Exception.Message
    }
    return $false
}
$clearImage.Add_Click({
    $script:selectedImages.Clear(); $script:selectedVideos.Clear(); $script:selectedDocuments.Clear(); $script:selectedAudio.Clear()
    $clearImage.Visible = $false
})

foreach ($target in @($drop, $composer, $promptBox, $chatScroll)) {
    $target.AllowDrop = $true
    $target.Add_DragEnter({ if ($_.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) { $_.Effect = [System.Windows.Forms.DragDropEffects]::Copy } })
    $target.Add_DragDrop({
        $files = @($_.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop))
        foreach ($f in $files) { [void](Add-SelectedFile $f) }
    })
}

$attachMenu = New-Object System.Windows.Forms.ContextMenuStrip
$attachImage = New-Object System.Windows.Forms.ToolStripMenuItem('Add image')
$attachVideo = New-Object System.Windows.Forms.ToolStripMenuItem('Add video')
$attachDocument = New-Object System.Windows.Forms.ToolStripMenuItem('Add document')
$attachAudio = New-Object System.Windows.Forms.ToolStripMenuItem('Add audio')
$attachFolder = New-Object System.Windows.Forms.ToolStripMenuItem('Select folder')
$attachMenu.Items.Add($attachImage) | Out-Null
$attachMenu.Items.Add($attachVideo) | Out-Null
$attachMenu.Items.Add($attachDocument) | Out-Null
$attachMenu.Items.Add($attachAudio) | Out-Null
$attachMenu.Items.Add($attachFolder) | Out-Null
$drop.Add_Click({ $attachMenu.Show($drop, 0, $drop.Height) })
$attachImage.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Filter = 'Images|*.png;*.jpg;*.jpeg;*.webp;*.bmp|All files|*.*'
    $dlg.Multiselect = [bool]$script:multiImageOcrMode
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { foreach ($f in $dlg.FileNames) { Add-SelectedFile $f | Out-Null } }
})
$attachVideo.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Filter = 'Videos|*.mp4;*.mov;*.mkv;*.webm;*.avi;*.m4v|All files|*.*'
    $dlg.Multiselect = $true
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { foreach ($f in $dlg.FileNames) { Add-SelectedFile $f | Out-Null } }
})
$attachDocument.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Filter = 'Documents and archives|*.pdf;*.docx;*.xlsx;*.xlsm;*.pptx;*.zip;*.rar|All files|*.*'
    $dlg.Multiselect = $true
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { foreach ($f in $dlg.FileNames) { Add-SelectedFile $f | Out-Null } }
})
$attachAudio.Add_Click({ Pick-AudioFile })
$attachFolder.Add_Click({ $folder.PerformClick() })

$folder.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = 'Select the folder to bind for the agent'
    if ($script:boundFolder) { $dlg.SelectedPath = $script:boundFolder }
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $script:boundFolder = [System.IO.Path]::GetFullPath($dlg.SelectedPath)
        $folder.Text = $script:boundFolder
        $folder.ForeColor = $colAccent
    }
})

function Get-PromptText { return $promptBox.Text.Trim() }

function Test-MultiImageOcrActivationIntent([string]$text) {
    if ([string]::IsNullOrWhiteSpace($text)) { return $false }
    $normalized = ($text.ToLowerInvariant().Normalize([Text.NormalizationForm]::FormD) -replace '\p{Mn}', '') -replace 'ı', 'i'
    return $normalized -match '((multi|multiple|several|many).*(image|picture|photo).*(ocr|read|reading|mode))|((image|picture|photo).*(multi|multiple|several).*(ocr|read|reading|mode))'
}

# ---------------------------------------------------------------- control tab
$ctrlTitle = New-Object System.Windows.Forms.Label
$ctrlTitle.Text = 'Model Control'
$ctrlTitle.Font = New-Object System.Drawing.Font('Segoe UI', 17, [System.Drawing.FontStyle]::Bold)
$ctrlTitle.Location = New-Object System.Drawing.Point(24, 36)
$ctrlTitle.AutoSize = $true
$controlTab.Controls.Add($ctrlTitle)

function New-CtrlButton([string]$text, [int]$x, [int]$y, [int]$w, [System.Drawing.Color]$color) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Location = New-Object System.Drawing.Point($x, $y)
    $b.Size = New-Object System.Drawing.Size($w, 46)
    $b.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
    $b.FlatStyle = 'Flat'
    $b.FlatAppearance.BorderSize = 0
    $b.BackColor = $color
    $b.ForeColor = [System.Drawing.Color]::White
    $controlTab.Controls.Add($b)
    return $b
}
$benchBtn = New-CtrlButton 'Run speed test' 24 92 250 ([System.Drawing.Color]::FromArgb(70, 90, 110))

$settingsColor = [System.Drawing.Color]::FromArgb(60, 68, 90)
$apiSettingsBtn = New-CtrlButton 'API connections…' 24 150 250 $settingsColor
$localFolderBtn = New-CtrlButton 'Add local language model (GGUF)…' 24 266 250 $settingsColor
$hfSettingsBtn = New-CtrlButton 'Hugging Face…' 24 208 250 $settingsColor
$localFolderBtn.Add_Click({ Add-LocalModelFolder })
$hfSettingsBtn.Add_Click({ Show-HuggingFaceSettings })

$apiSettingsMenu = New-Object System.Windows.Forms.ContextMenuStrip
$apiChatItem = New-Object System.Windows.Forms.ToolStripMenuItem('Chat connections…')
$apiChatItem.Add_Click({ Show-ApiSettings })
$apiMediaItem = New-Object System.Windows.Forms.ToolStripMenuItem('Image and video API…')
$apiMediaItem.Add_Click({ Show-MediaSettings })
$apiWebItem = New-Object System.Windows.Forms.ToolStripMenuItem('Web management…')
$apiWebItem.Add_Click({ Show-WebSettings })
[void]$apiSettingsMenu.Items.Add($apiChatItem)
[void]$apiSettingsMenu.Items.Add($apiMediaItem)
[void]$apiSettingsMenu.Items.Add($apiWebItem)
$apiSettingsBtn.Add_Click({ $apiSettingsMenu.Show($apiSettingsBtn, [System.Drawing.Point]::new(0, $apiSettingsBtn.Height)) })

$statusCtrl = New-Object System.Windows.Forms.Label
$statusCtrl.Location = New-Object System.Drawing.Point(24, 332)
$statusCtrl.Size = New-Object System.Drawing.Size(860, 298)
$statusCtrl.Font = New-Object System.Drawing.Font('Consolas', 10)
$statusCtrl.ForeColor = [System.Drawing.Color]::FromArgb(190, 200, 215)
$controlTab.Controls.Add($statusCtrl)

# ---------------------------------------------------------------- model management
function Invoke-Manage([string]$action, [string]$profile, [string]$modelId) {
    $managePath = Join-Path $scripts 'manage-qwen.ps1'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    foreach ($argument in @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $managePath, '-Action', $action)) {
        $psi.ArgumentList.Add($argument)
    }
    if ($profile) { $psi.ArgumentList.Add('-Profile'); $psi.ArgumentList.Add($profile) }
    if ($modelId) { $psi.ArgumentList.Add('-ModelId'); $psi.ArgumentList.Add($modelId) }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    return [System.Diagnostics.Process]::Start($psi)
}

function Update-ModelSwitch {
    # Never race two manager processes. While one exits, keep only the latest choice.
    if ($script:switchProcess) {
        if (!$script:switchProcess.HasExited) { return }
        if ($script:switchProcess.ExitCode -ne 0 -and !$script:pendingModelChange) {
            $script:modelSwitchError = 'Model operation failed. Check the model files and the logs folder, then select the model again.'
        }
        $script:switchProcess.Dispose()
        $script:switchProcess = $null
    }
    if ($script:pendingModelChange) {
        $change = $script:pendingModelChange
        $script:pendingModelChange = $null
        try { $script:switchProcess = Invoke-Manage $change.action $change.profile $change.modelId }
        catch { $script:modelSwitchError = 'Could not start the model operation: ' + $_.Exception.Message }
    }
}

# A turn with an audio attachment that is sent before the language model is ready: the in-flight
# model load is ABANDONED (the process is killed, the queued choice cleared) so the worker can free
# VRAM for ASR right away. The language model is restored in the background afterwards (by the
# worker). The fields are cleared first so the killed process's exit code is not shown as
# "Model operation failed".
function Stop-ModelSwitchForAudio {
    if ($script:pendingModelChange) { $script:pendingModelChange = $null }
    if ($script:switchProcess) {
        try {
            if (!$script:switchProcess.HasExited) {
                $script:switchProcess.Kill($true)
                $script:switchProcess.WaitForExit(5000) | Out-Null
            }
        } catch { }
        try { $script:switchProcess.Dispose() } catch { }
        $script:switchProcess = $null
    }
    $script:modelSwitchError = $null
    $script:modelReady = $false
    $script:activeModel = $null
}

# "Stop": whatever is running right now (the language-model answer, ASR/OCR/tool subprocesses,
# media generation) is stopped INSTANTLY. The ONE exception: a language-model LOAD is not cut
# (switchProcess is left alone); the load finishes in the background and chat resumes on the next
# message.
function Cancel-Generation {
    Stop-BackgroundCompaction
    if ($script:sharedMedia) {
        $script:sharedMedia.Cancel=$true
        if($script:sharedMedia.Process){try{$script:sharedMedia.Process.Kill($true)}catch{}}
        $metric.Text='Stopping generation tracking…';$send.Text='Stopping…'
    }
    if ($script:pendingSend) {
        $script:pendingSend = $false
        $send.Text = 'Send'
    }
    if ($script:chatJob -and $script:chatJob.State -in @('Running', 'NotStarted')) {
        $script:sharedRequest.Cancel = $true
        if ($script:sharedRequest.Cts) { try { $script:sharedRequest.Cts.Cancel() } catch { } }
        $send.Text = 'Stopping…'
        $metric.Text = 'Stopping the response; the model will stay in memory.'
    }
}

function Stop-LocalQwen {
    Cancel-Generation
    $script:modelReady = $false
    $script:activeModel = $null
    $script:modelSwitchError = $null
    $script:pendingModelChange = @{ action = 'stop'; profile = ''; modelId = '' }
    Update-ModelSwitch
    $statusCtrl.Text = 'Stopping the model… The interface will stay open.'
}

function Restart-LocalQwen([string]$profile) {
    $wanted = Get-SelectedLocalAlias
    if ($script:modelReady -and $script:activeModel -eq $wanted -and !$script:switchProcess -and !$script:pendingModelChange) {
        $statusCtrl.Text = "$($profile.ToUpperInvariant()) is already loaded; the model was not reloaded."
        return
    }
    Cancel-Generation
    $script:modelReady = $false
    $script:activeModel = $null
    $script:modelSwitchError = $null
    $script:pendingModelChange = @{ action = 'switch'; profile = $profile; modelId = [string]$script:apiSettings.selectedModel }
    Update-ModelSwitch
    $statusCtrl.Text = "Loading $($modelChoice.SelectedItem)… The interface will stay open."
    $metric.Text = "Loading $($modelChoice.SelectedItem)…"
}

$modelChoice.Add_SelectedIndexChanged({
    Select-RegisteredModel
})


# ---------------------------------------------------------------- past chats
Add-Type -AssemblyName System.Security.Cryptography.ProtectedData -ErrorAction SilentlyContinue
# V1 encrypted via SecureString; since SecureString can hold at most 65,536 characters,
# chats with attached images or long chats could not be saved at all (seen in a real
# UI test). V2 uses the same current-user DPAPI directly;
# V1 files can still be read.
$chatProtectionPrefix = 'MAMIDLABS-CHAT-DPAPI-V1'
$chatProtectionPrefixV2 = 'MAMIDLABS-CHAT-DPAPI-V2'

function Protect-ChatText([string]$text) {
    $cipher = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($text), $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser)
    return $chatProtectionPrefixV2 + "`n" + [Convert]::ToBase64String($cipher)
}

function Read-ChatData([string]$path) {
    try {
        $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
        if ($raw.StartsWith($chatProtectionPrefixV2 + "`n", [StringComparison]::Ordinal)) {
            $cipher = [Convert]::FromBase64String($raw.Substring($chatProtectionPrefixV2.Length + 1).Trim())
            $raw = [Text.Encoding]::UTF8.GetString([Security.Cryptography.ProtectedData]::Unprotect($cipher, $null,
                [Security.Cryptography.DataProtectionScope]::CurrentUser))
        } elseif ($raw.StartsWith($chatProtectionPrefix + "`n", [StringComparison]::Ordinal)) {
            $cipher = $raw.Substring($chatProtectionPrefix.Length + 1).Trim()
            $secure = ConvertTo-SecureString $cipher
            $raw = [Net.NetworkCredential]::new('', $secure).Password
        }
        return ($raw | ConvertFrom-Json)
    } catch { return $null }
}

function Open-LocalArtifact([string]$path) {
    if (!$path) { return }
    try { $full = [IO.Path]::GetFullPath($path) } catch { return }
    $allowedExtensions = @('.png','.jpg','.jpeg','.webp','.bmp','.gif','.mp4','.mov','.mkv','.webm','.avi','.m4v','.pdf','.docx','.xlsx','.xlsm','.pptx','.zip','.rar')
    if (!(Test-Path -LiteralPath $full -PathType Leaf) -or [IO.Path]::GetExtension($full).ToLowerInvariant() -notin $allowedExtensions) { return }
    try { Start-Process -FilePath $full } catch { }
}

function Save-Chat {
    if ($bubbles.Count -eq 0) { return }
    if (!$script:chatId) { $script:chatId = [guid]::NewGuid().ToString('N') }
    $transcript = @(foreach ($b in $bubbles) {
        @{
            role = $b.role
            text = [string]$b.body.Text
            rawText = [string]$b.rawText
            artifacts = @($b.artifacts.ToArray())
            image = $(if ($b.pic) { [string]$b.pic.Tag } else { '' })
            mediaKind = $(if ($b.pic -and [string]$b.pic.Tag -match '\.(mp4|mov|mkv|webm|avi|m4v)$') { 'video' } else { 'image' })
            thinking = $(if ($b.thinkBox) { [string]$b.thinkBox.Text } else { '' })
            seconds = $(if ($b.seconds) { [double]$b.seconds } else { 0 })
        }
    })
    $data = @{
        id = $script:chatId
        title = $script:chatTitle
        updated = (Get-Date).ToString('o')
        messages = @($chatMessages.ToArray())
        transcript = $transcript
    }
    $path = Join-Path $chatDir "$($script:chatId).json"
    $temp = $path + '.tmp'
    try {
        $json = $data | ConvertTo-Json -Depth 30
        $protected = Protect-ChatText $json
        [System.IO.File]::WriteAllText($temp, $protected, [System.Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($path)) { [IO.File]::Replace($temp, $path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $path) }
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show("The chat could not be saved. The previous save is kept.`n$($_.Exception.Message)", 'Chat save error')
    } finally {
        if ([IO.File]::Exists($temp)) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
    }
}

function Clear-ChatView {
    # If a compaction is running in the background it must not overwrite the cleared history.
    Stop-BackgroundCompaction
    foreach ($b in $bubbles) { $chatInner.Controls.Remove($b.panel); $b.panel.Dispose() }
    $bubbles.Clear()
    $chatMessages.Clear()
    $script:activeBubble = $null
    $script:stackY = 8
    $chatInner.Height = 8
    $script:lastPromptTokens = 0
    $script:lastCompletionTokens = 0
    $script:contextTokens = 0
    $script:lastSpeed = 0
    $script:streamGenerating = $false
    Update-LiveMetrics
    $script:selectedImages.Clear()
    $script:lastUserImage = $null
    $script:previousUserImage = $null
    $script:currentTurnHasImage = $false
    $script:currentTurnImagePaths = @()
    $script:currentTurnAudioPaths = @()
    $script:currentTurnMultiImageOcr = $false
    $script:multiImageOcrMode = $false
    $script:selectedVideos.Clear()
    $script:lastUserVideo = $null
    $script:selectedDocuments.Clear()
    $script:selectedAudio.Clear()
    $script:attachedDocuments.Clear()
    $clearImage.Visible = $false
}

function Load-Chat([string]$path) {
    $data = Read-ChatData $path
    if (!$data) { return }
    Cancel-Generation
    $script:pendingSend = $false
    Clear-ChatView
    $loadedChatId = [string]$data.id
    $script:chatId = if ($loadedChatId -match '^[0-9a-fA-F]{32}$') { $loadedChatId.ToLowerInvariant() } else { [guid]::NewGuid().ToString('N') }
    $script:chatTitle = [string]$data.title
    foreach ($item in @($data.transcript)) {
        $b = New-Bubble ([string]$item.role)
        $b.rawText = [string]$item.rawText
        $attachments = @($item.artifacts | Where-Object { $_ })
        foreach ($artifact in $attachments) {
            Add-BubbleArtifact $b ([string]$artifact)
            if ($item.role -eq 'user') {
                switch -Regex ([string]$artifact) {
                    '(?i)\.(png|jpe?g|webp|bmp|gif)$' { Set-LastUserImage ([string]$artifact); break }
                    '(?i)\.(mp4|mov|mkv|webm|avi|m4v)$' { $script:lastUserVideo = [string]$artifact; break }
                    '(?i)\.(pdf|docx|xlsx|xlsm|pptx|zip|rar)$' {
                        if (!$script:attachedDocuments.Contains([string]$artifact)) { $script:attachedDocuments.Add([string]$artifact) }
                    }
                }
            }
        }
        $itemText = [string]$item.text
        if ($item.role -ne 'user' -and ($itemText -match '(?m)^\s*(\||#{1,6}\s|[-*+]\s|\d+[.)]\s|```)' -or $itemText -match '\*\*.+?\*\*|`[^`]+`')) {
            Render-Markdown $b.body $itemText $colText
            $b.hasRich = $true
        } else {
            $b.body.Text = $itemText
        }
        if ($attachments.Count -gt 0 -and (!$item.image -or !(Test-Path -LiteralPath ([string]$item.image) -PathType Leaf))) {
            Set-BubbleAttachmentList $b $attachments
        } elseif ($item.image -and (Test-Path -LiteralPath ([string]$item.image) -PathType Leaf)) {
            if ([string]$item.mediaKind -eq 'video') {
                Set-BubbleVideo $b ([string]$item.image) $null
                if ($item.role -eq 'user') { $script:lastUserVideo = [string]$item.image }
            } else {
                Set-BubbleImage $b ([string]$item.image)
                if ($item.role -eq 'user') { Set-LastUserImage ([string]$item.image) }
            }
        }
        if ($item.thinking -and $b.thinkToggle) {
            $b.thinkBox.Text = [string]$item.thinking
            $b.seconds = [double]$item.seconds
            $b.thinkToggle.Text = "Thought ($($b.seconds) s) ▸"
            $b.thinkToggle.Visible = $true
            $b.thinkBox.Visible = $false
        }
    }
    foreach ($m in @($data.messages)) { [void]$chatMessages.Add($m) }
    Relayout-Chat
    Scroll-ChatToEnd
    $metric.Text = "Chat loaded: $($script:chatTitle)"
}

function Update-ApprovalDialog($ctx) {
    # The tool side gave up (timed out, Stop was pressed or the job finished): nobody is waiting for an answer.
    if ([string]$ctx.request.AskId -ne $ctx.askId -or $ctx.request.Cancel) {
        $ctx.dlg.DialogResult = [System.Windows.Forms.DialogResult]::Abort
        return
    }
    if ($ctx.deadline -is [DateTime]) {
        $left = $ctx.deadline - [DateTime]::UtcNow
        if ($left.TotalSeconds -lt 0) { $left = [TimeSpan]::Zero }
        $ctx.countdown.Text = 'If there is no answer it will count as denied in {0}:{1:00}.' -f [int][math]::Floor($left.TotalMinutes), $left.Seconds
    }
}

# Agent permission prompt. MessageBox is not used: when the tool side gave up after the timeout
# (or via Stop) the MessageBox stayed open and a later "Yes" was silently ignored
# (real UI test: create_document was denied by timeout, the window stayed open).
# This form closes itself when the request is no longer awaited. Returns: 'yes', 'no' or $null.
function Show-ApprovalDialog($request) {
    $askId = [string]$request.AskId
    $deadline = $request.AskDeadline
    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text = "Permission required  —  $([string]$request.AskTool)"
    $dlg.ClientSize = New-Object System.Drawing.Size(520, 270)
    $dlg.StartPosition = 'CenterParent'
    $dlg.FormBorderStyle = 'FixedDialog'
    $dlg.BackColor = $colPanel
    $dlg.ForeColor = $colText
    $dlg.MinimizeBox = $false
    $dlg.MaximizeBox = $false
    $dlg.ShowInTaskbar = $false
    if ($appIcon) { $dlg.Icon = $appIcon }

    $head = New-Object System.Windows.Forms.Label
    $head.Text = 'The agent wants to make a change on your computer:'
    $head.Location = New-Object System.Drawing.Point(16, 14)
    $head.Size = New-Object System.Drawing.Size(488, 22)
    $head.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
    $dlg.Controls.Add($head)

    # Read-only text box so long commands can be scrolled.
    $detailBox = New-Object System.Windows.Forms.TextBox
    $detailBox.Multiline = $true
    $detailBox.ReadOnly = $true
    $detailBox.ScrollBars = 'Vertical'
    $detailBox.Text = ([string]$request.AskDetail) -replace "(?<!`r)`n", "`r`n"
    $detailBox.Location = New-Object System.Drawing.Point(16, 42)
    $detailBox.Size = New-Object System.Drawing.Size(488, 130)
    $detailBox.BackColor = [System.Drawing.Color]::FromArgb(35, 39, 49)
    $detailBox.ForeColor = $colText
    $detailBox.BorderStyle = 'FixedSingle'
    $detailBox.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
    $dlg.Controls.Add($detailBox)

    $countdown = New-Object System.Windows.Forms.Label
    $countdown.Location = New-Object System.Drawing.Point(16, 180)
    $countdown.Size = New-Object System.Drawing.Size(488, 22)
    $countdown.ForeColor = [System.Drawing.Color]::FromArgb(170, 176, 190)
    $dlg.Controls.Add($countdown)

    $yesBtn = New-Object System.Windows.Forms.Button
    $yesBtn.Text = 'Yes, allow'
    $yesBtn.DialogResult = [System.Windows.Forms.DialogResult]::Yes
    $noBtn = New-Object System.Windows.Forms.Button
    $noBtn.Text = 'No'
    $noBtn.DialogResult = [System.Windows.Forms.DialogResult]::No
    $x = 264
    foreach ($btn in @($yesBtn, $noBtn)) {
        $btn.Location = New-Object System.Drawing.Point($x, 216)
        $btn.Size = New-Object System.Drawing.Size(116, 36)
        $btn.FlatStyle = 'Flat'
        $btn.FlatAppearance.BorderSize = 0
        $btn.ForeColor = [System.Drawing.Color]::White
        $dlg.Controls.Add($btn)
        $x += 124
    }
    $yesBtn.BackColor = [System.Drawing.Color]::FromArgb(75, 105, 185)
    $noBtn.BackColor = [System.Drawing.Color]::FromArgb(70, 72, 82)
    $dlg.CancelButton = $noBtn
    $dlg.ActiveControl = $noBtn

    # The context travels in Tag: the Tick handler reads $this.Tag without relying on function scope.
    $approvalTimer = New-Object System.Windows.Forms.Timer
    $approvalTimer.Interval = 500
    $approvalTimer.Tag = @{ request = $request; askId = $askId; deadline = $deadline; dlg = $dlg; countdown = $countdown }
    Update-ApprovalDialog $approvalTimer.Tag
    $approvalTimer.Add_Tick({ Update-ApprovalDialog $this.Tag })
    $approvalTimer.Start()
    try { $result = $dlg.ShowDialog($form) }
    finally { $approvalTimer.Stop(); $approvalTimer.Dispose(); $dlg.Dispose() }
    if ($result -eq [System.Windows.Forms.DialogResult]::Yes) { return 'yes' }
    # Abort: the tool side gave up, no answer is written. X/Esc/No: "no", like the old MessageBox.
    if ($result -eq [System.Windows.Forms.DialogResult]::Abort) { return $null }
    return 'no'
}

function Show-ChatHistory {
    Save-Chat
    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text = 'Past chats'
    $dlg.Size = New-Object System.Drawing.Size(560, 460)
    $dlg.StartPosition = 'CenterParent'
    $dlg.BackColor = $colPanel
    $dlg.ForeColor = $colText
    $dlg.MinimizeBox = $false
    $dlg.MaximizeBox = $false
    if ($appIcon) { $dlg.Icon = $appIcon }

    $list = New-Object System.Windows.Forms.ListBox
    $list.Location = New-Object System.Drawing.Point(14, 14)
    $list.Size = New-Object System.Drawing.Size(516, 340)
    $list.Anchor = 'Top,Bottom,Left,Right'
    $list.BackColor = [System.Drawing.Color]::FromArgb(35, 39, 49)
    $list.ForeColor = $colText
    $list.BorderStyle = 'FixedSingle'
    $list.Font = New-Object System.Drawing.Font('Segoe UI', 10)
    $dlg.Controls.Add($list)

    $entries = New-Object System.Collections.ArrayList
    function Refresh-List {
        $list.Items.Clear()
        $entries.Clear()
        foreach ($file in @(Get-ChildItem -LiteralPath $chatDir -Filter '*.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
            $info = Read-ChatData $file.FullName
            if (!$info) { continue }
            $title = [string]$info.title
            if (!$title) { $title = '(untitled)' }
            [void]$entries.Add($file.FullName)
            $list.Items.Add(('{0:dd.MM.yyyy HH:mm}   {1}' -f $file.LastWriteTime, $title)) | Out-Null
        }
        if ($list.Items.Count -eq 0) { $list.Items.Add('(no saved chats)') | Out-Null }
    }
    Refresh-List

    function New-DlgButton([string]$text, [int]$x, [int]$w, [System.Drawing.Color]$color) {
        $b = New-Object System.Windows.Forms.Button
        $b.Text = $text
        $b.Location = New-Object System.Drawing.Point($x, 368)
        $b.Size = New-Object System.Drawing.Size($w, 36)
        $b.Anchor = 'Bottom,Left'
        $b.FlatStyle = 'Flat'
        $b.FlatAppearance.BorderSize = 0
        $b.BackColor = $color
        $b.ForeColor = [System.Drawing.Color]::White
        $dlg.Controls.Add($b)
        return $b
    }
    $openBtn = New-DlgButton 'Open' 14 120 ([System.Drawing.Color]::FromArgb(75, 105, 185))
    $delBtn = New-DlgButton 'Delete' 140 120 ([System.Drawing.Color]::FromArgb(125, 62, 70))
    $delAllBtn = New-DlgButton 'Delete all' 266 150 ([System.Drawing.Color]::FromArgb(125, 62, 70))
    $closeBtn = New-DlgButton 'Close' 422 100 ([System.Drawing.Color]::FromArgb(70, 72, 82))

    $openBtn.Add_Click({
        if ($list.SelectedIndex -lt 0 -or $list.SelectedIndex -ge $entries.Count) { return }
        Load-Chat $entries[$list.SelectedIndex]
        $dlg.Close()
    })
    $list.Add_DoubleClick({ $openBtn.PerformClick() })
    $delBtn.Add_Click({
        if ($list.SelectedIndex -lt 0 -or $list.SelectedIndex -ge $entries.Count) { return }
        $target = $entries[$list.SelectedIndex]
        $answer = [System.Windows.Forms.MessageBox]::Show('Permanently delete this chat?', 'Delete chat',
            [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        Refresh-List
    })
    $delAllBtn.Add_Click({
        if ($entries.Count -eq 0) { return }
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "Permanently delete ALL $($entries.Count) chats? This cannot be undone.",
            'Delete all chats', [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }
        foreach ($path in @($entries)) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        Refresh-List
    })
    $closeBtn.Add_Click({ $dlg.Close() })
    $dlg.ShowDialog($form) | Out-Null
    $dlg.Dispose()
}

# ---------------------------------------------------------------- generation flow
function Get-SystemPrompt {
    $lines = @()
    $lines += 'You are the MamidLabs assistant. Reply in the language the user writes in (English by default), directly and clearly. The chat model may be local or an API; tools run on the user''s computer.'
    $lines += "Today's date: $(Get-Date -Format 'yyyy-MM-dd')."
    # LITE (small 9B model): thinking is ON and UNLIMITED (no server budget). This encourages
    # deep, step-by-step reasoning and tells the model not to cap its thinking time; it should
    # think as long as needed. The line is fixed per profile, so the prompt prefix (and thus the
    # KV cache) is not broken (added only for the LITE profile).
    if ([string]$script:apiSettings.localProfile -eq 'lite') {
        $lines += 'Think deeply and carefully: before answering, break the problem down, reason step by step, bound your assumptions and aim for the most correct conclusion. For math, logic, planning, code and multi-step problems, target the CORRECT answer instead of a fast one. Do NOT cap your thinking time: think for as long and as deeply as needed; if necessary, try more than one approach, check intermediate results and assumptions, and go back and fix mistakes. Once confident, lay out your reasoning briefly and give a clear, complete and correct answer.'
    }
    if ($netChip.Checked) { $lines += 'You have internet access: use the search_web and fetch_page tools when up-to-date information is needed.' }
    else { $lines += 'Your internet access is OFF; you cannot search the web.' }
    $consultIds = @($script:apiSettings.providers.Keys | Where-Object { $_ -in (Get-ChatProviderIds) -and $script:apiSettings.providers[$_].model -and (Get-ProviderKey $_) })
    if ($consultIds.Count) { $lines += "You can consult other models: $($consultIds -join ', '). Use the ask_model tool for a second opinion, a comparison or research where that model is stronger." }
    if ($agentChip.Checked) {
        if ($script:boundFolder) { $lines += "Agent mode is on. Bound folder: $($script:boundFolder). Use only relative_path in the file tools." }
        else { $lines += 'Agent mode is on but no folder is bound; if a file operation is needed, ask the user to bind a folder.' }
    } else { $lines += 'Agent mode is off; you have no file tools.' }
    $lines += 'Image, video and document tools are used automatically based on the intent of the message; no separate generate button or chip is needed for them.'
    $lines += 'If the user explicitly asks you to run a command and Agent is on, use the run_command tool; do not invent commands on your own or run them without the user asking.'
    $lines += 'If a tool (especially web search or page download) fails, do NOT try to work around it: do not attempt the same task with other tools or via run_command (ping, curl, wget, Invoke-WebRequest, nslookup, etc.). Report the failure to the user briefly and accurately; if you already have enough information, give a clear answer, otherwise do not make up the result.'
    $lines += 'If the user explicitly asks to generate an image, create a picture or makes a similar request, call generate_image. Do not call generate_image for requests that only describe, analyze or ask questions about an image.'
    if ([string]$script:apiSettings.localProfile -eq 'lite') {
        $lines += 'If the user wants to edit an attached photo (change clothes/hair/background, add/remove an object, put the person in another scene) call the generate_image tool with mode=regenerate; the identity_edit mode is NOT available on this profile (extremely slow on an 8 GB VRAM card, takes minutes).'
    } else {
        $lines += 'If the user wants to edit an attached photo (edit the photo, change their clothes/hair, change the background, add or remove an object, put the person in another scene) call the generate_image tool with mode=identity_edit; this mode preserves the person''s face and identity.'
    }
    $lines += 'If the user explicitly says generate a video or create a short video, call generate_video. Write the video prompt in English; detail the motion, camera movement, lighting, duration and style.'
    $lines += 'If the user says create a PDF, prepare a Word document or make a presentation, call create_document. The format value can only be pdf, docx or pptx. Write the document content as markdown.'
    $lines += 'If the user wants a chart, a plot, a bar/pie/line chart or wants you to draw the curve of a function, call generate_chart. type values: bar, line, pie, scatter, function (y=f(x)). For a single series give labels + values; use series for multiple series. Prepare the data from the numbers in the chat or from values you compute. Do not try to produce the same chart over and over; the tool is normally called ONCE, then report the result to the user briefly.'
    $lines += 'Do not call generation tools just to read/describe an image, document or video. If there is a question about an attached document use read_document; writing files and creating documents require permission.'
    return ($lines -join ' ')
}

# Per-turn hints do NOT live in the system prompt: the system prompt is the first block of
# the prompt, and whenever it changes llama-server reprocesses the whole context (first token 20+ s).
# We place them right before the last user message; that way the cached prefix
# stays intact and only the last turn is reprocessed.
function Get-TurnHints {
    $lines = @()
    foreach ($document in $script:attachedDocuments) {
        $lines += "The user attached this document: $document. If asked about its content, read this full path with read_document (for zip/rar first call it without entry to see the listing, then read the requested file with entry)."
    }
    if ($script:currentTurnHasImage) {
        $lines += 'A new image was attached in this turn. For requests to read text/writing use the OCR result; for other image understanding requests examine the image yourself. Examine the image carefully, extract the findings relevant to the user''s question and then answer in text. Do not call generate_image unless the user explicitly asks for generation/editing.'
    } elseif ($script:lastUserImage) {
        $currentText = ''
        for ($i = $chatMessages.Count - 1; $i -ge 0; $i--) {
            if ([string]$chatMessages[$i].role -eq 'user') {
                $currentText = Get-MessageText $chatMessages[$i].content
                break
            }
        }
        $normalized = $currentText.ToLowerInvariant().Normalize([Text.NormalizationForm]::FormD) -replace '\p{Mn}', ''
        if ($normalized -match '((image|picture|photo|pic|this one|from this|of this)\w*.*(generate|create|make|again|redo|style|reference|edit|change))|((generate|create|make|regenerate|redo|style|reference|edit|change)\w*.*(image|picture|photo|pic|this one|from this|of this))') {
            if ([string]$script:apiSettings.localProfile -eq 'lite') {
                $lines += 'A source image was attached in an earlier turn; the image data is not sent in this request. If the user explicitly asks for generation from this source, choose mode=regenerate or mode=style_reference for generate_image; the identity_edit mode is NOT available on this profile.'
            } else {
                $lines += 'A source image was attached in an earlier turn; the image data is not sent in this request. If the user explicitly asks for generation from this source, choose mode=regenerate or mode=style_reference for generate_image; for a photo editing request (clothes, lighting, background, add/remove an object) choose mode=identity_edit.'
            }
        }
    }
    return ($lines -join ' ')
}

function Get-MessageText($content) {
    if ($content -is [string]) { return $content }
    $parts = @()
    foreach ($item in @($content)) {
        if ($item -is [string]) { $parts += $item }
        elseif ([string]$item.type -eq 'text') { $parts += [string]$item.text }
    }
    return ($parts -join ' ')
}

function Remove-PreviousImagePayload($message) {
    if (!$message -or [string]$message.role -ne 'user' -or $message.content -is [string]) { return $message }
    $parts = @()
    $removed = $false
    foreach ($part in @($message.content)) {
        if ([string]$part.type -eq 'image_url') {
            if (!$removed) {
                $parts += @{ type = 'text'; text = '[An image was processed in an earlier turn; the image data is not resent in this request. Attach the image again if needed.]' }
                $removed = $true
            }
        } else {
            $parts += $part
        }
    }
    if (!$removed) { return $message }
    return @{ role = [string]$message.role; content = $parts }
}

# System prompt + history + per-turn hints (if any).
# Template rule: "System message must be at the beginning" -- there can be no system message
# in the middle of the history. Therefore (1) system messages accumulated in the history (like
# the compaction summary) are merged into the single system block at the start, (2) per-turn
# hints are added not as a system message but as text at the start of the last user message.
# That keeps the template happy and the prompt prefix byte-identical across turns in the KV cache.
function Build-JobMessages([switch]$NoHints) {
    $stable = @(Get-SystemPrompt)
    $history = @()
    foreach ($message in @($chatMessages.ToArray())) {
        if ([string]$message.role -eq 'system') { $stable += (Get-MessageText $message.content); continue }
        $history += $message
    }
    # We keep the image in the chat record and the UI; but after the viewing turn is over,
    # handing the same base64 payload to mmproj again with every follow-up message starts
    # a long prefill round on the llama.cpp side. Keep only the image in the last user
    # message of the request; leave an explicit marker so the model can use its earlier
    # answer instead of the old image. If the image is mentioned again the user can
    # re-attach it; for image generation the source file is kept separately as lastUserImage.
    $requestHistory = @()
    for ($i = 0; $i -lt $history.Count; $i++) {
        # Only the image newly attached in this request is sent to the server. The user message
        # from the previous turn is not kept again even if it is the last user message in history.
        $isCurrentUserMessage = ($script:currentTurnHasImage -and
            $i -eq ($history.Count - 1) -and [string]$history[$i].role -eq 'user')
        $requestHistory += $(if ($isCurrentUserMessage) { $history[$i] } else { Remove-PreviousImagePayload $history[$i] })
    }
    $messages = @(@{ role = 'system'; content = ($stable -join "`n`n") })
    $hints = if ($NoHints) { '' } else { Get-TurnHints }
    $last = if ($requestHistory.Count -gt 0) { $requestHistory[$requestHistory.Count - 1] } else { $null }
    if (!$hints -or !$last -or [string]$last.role -ne 'user') { return $messages + $requestHistory }
    if ($requestHistory.Count -ge 2) { $messages += $requestHistory[0..($requestHistory.Count - 2)] }
    # We do not change the history; we only add a note to the copy that goes to this request.
    $note = "[System note] $hints"
    $content = if ($last.content -is [string]) { $note + "`n`n" + [string]$last.content }
               else { @(@{ type = 'text'; text = $note }) + @($last.content) }
    $messages += @{ role = 'user'; content = $content }
    return $messages
}

# Context compaction used to run IN FRONT of the next message: when the user wrote a
# message the whole history was summarized first (a separate model call), and only then
# did the actual request go out -- the direct source of the "I sent it and it kept me waiting" complaint.
# Now it runs in the background as soon as the turn ends, while the user is typing.
function Start-BackgroundCompaction {
    if ($script:compactJob -or $script:busy -or $script:imageBusy -or $script:pendingSend) { return }
    if ($script:contextTokens -le $script:compactAt -or $chatMessages.Count -lt 4) { return }
    if ($script:providerId -eq 'local' -and !$script:modelReady) { return }
    $jobData = @{
        # Start-ThreadJob -FilePath reads the target file as text and runs it;
        # $PSScriptRoot is always empty in that context (verified). So the root is
        # passed explicitly here; the worker uses it inside Get-InstallRoot.
        root = $root
        messages = Build-JobMessages -NoHints
        provider = $script:providerId
        model = $script:activeModel
        compactOnly = $true
        contextLimit = $script:ctxLimit
        maxTokens = 1200
        consultProviders = @{}
        # The 'custom' base URL and provider settings are passed to the worker explicitly.
        apiSettings = $script:apiSettings
        baseUrl = (Get-CustomBaseUrl)
    }
    if ($script:providerId -ne 'local') {
        $jobData.model = if ($modelChoice.SelectedIndex -ge 0) { [string]$script:modelEntries[$modelChoice.SelectedIndex].model } else { '' }
        $jobData.apiKey = Get-ProviderKey $script:providerId
        if (!$jobData.model -or (!$jobData.apiKey -and $script:providerId -ne 'custom')) { return }
    }
    $script:sharedCompact = [hashtable]::Synchronized(@{ Cancel = $false; Cts = $null; AccessMode = 'full' })
    try {
        $script:compactJob = Start-ThreadJob -FilePath (Join-Path $root 'agent\qwen-inference.ps1') -ArgumentList $jobData, $script:sharedCompact
        $metric.Text = 'Compacting the context in the background…'
    } catch { $script:compactJob = $null }
}

function Stop-BackgroundCompaction {
    if (!$script:compactJob) { return }
    if ($script:sharedCompact) {
        $script:sharedCompact.Cancel = $true
        if ($script:sharedCompact.Cts) { try { $script:sharedCompact.Cts.Cancel() } catch { } }
    }
    Remove-Job -Job $script:compactJob -Force -ErrorAction SilentlyContinue
    $script:compactJob = $null
    $script:sharedCompact = $null
}

# After compaction only the next request measures the real token count; until then
# we update the counter with a rough estimate, otherwise the threshold stays stuck and keeps retriggering.
function Estimate-ContextTokens {
    $chars = 0
    foreach ($m in @($chatMessages.ToArray())) {
        $content = $m.content
        if ($content -is [string]) { $chars += $content.Length; continue }
        foreach ($part in @($content)) {
            if ($part -is [string]) { $chars += $part.Length }
            elseif ($part.type -eq 'text') { $chars += ([string]$part.text).Length }
            elseif ($part.type -eq 'image_url') { $chars += 4096 }
        }
    }
    return [int]($chars / 3)
}

function Begin-Generation {
    if ($script:busy -or $script:imageBusy -or !$script:pendingSend) { return }
    # A turn with an audio attachment starts IMMEDIATELY even if the language model is not ready:
    # the worker unloads the model for ASR, transcribes, then restores the model. So the in-flight
    # model load is abandoned and the audio (ASR) is handled first. Without audio the old behavior
    # is kept: wait until the model is ready.
    $turnAudio = @($script:currentTurnAudioPaths | Where-Object { $_ })
    if ($script:providerId -eq 'local' -and !$script:modelReady) {
        if ($turnAudio.Count -eq 0) { return }
        Stop-ModelSwitchForAudio
    }
    $script:pendingSend = $false
    Stop-BackgroundCompaction
    $turnHasImage = [bool]$script:currentTurnHasImage
    # While the model is loading the active alias may not exist yet (an audio turn preempted the
    # load); fall back to the SELECTED profile so the wrong profile is never started.
    $profile = Get-ProfileFromAlias $(if ($script:activeModel) { $script:activeModel } else { Get-SelectedLocalAlias })
    $jobMessages = Build-JobMessages
    # Are the components installed? Image/video generation and multi-image OCR are offered only
    # when the component is present (or image/video comes from an API provider). Otherwise the
    # model used to call a non-existent tool and behave erratically.
    $hasMedia = Test-Path -LiteralPath (Join-Path $root 'ComfyUI\main.py') -PathType Leaf
    $hasOcr = (Test-Path -LiteralPath (Join-Path $root 'venv\ocr\Scripts\python.exe')) -or (Test-Path -LiteralPath (Join-Path $root 'venv\ocr-cpu\Scripts\python.exe'))
    $imgProv = [string]$script:apiSettings.imageProvider
    $vidProv = [string]$script:apiSettings.videoProvider
    $imageApiOk = [bool]($imgProv -and $imgProv -ne 'local')
    $videoApiOk = [bool]($vidProv -and $vidProv -ne 'local')
    $jobData = @{
        # Start-ThreadJob -FilePath reads the target file as text and runs it;
        # $PSScriptRoot is always empty in that context (verified). So the root is
        # passed explicitly here; the worker uses it inside Get-InstallRoot.
        root = $root
        messages = $jobMessages
        provider = $script:providerId
        agent = [bool]$agentChip.Checked
        internet = [bool]$netChip.Checked
        # Generation/office tools are chosen by message intent and system instructions, not by chips.
        # Image/video are offered only if the component is installed (local) or an API provider is set.
        imagegen = ($hasMedia -or $imageApiOk)
        videogen = ($hasMedia -or $videoApiOk)
        office = $true
        documentCreate = $true
        chartgen = $true
        boundFolder = $script:boundFolder
        hasSourceImage = [bool]$script:lastUserImage
        hasPreviousImage = [bool]$script:previousUserImage
        hasCurrentImage = $turnHasImage
        currentImagePath = $(if ($script:currentTurnImagePaths.Count -gt 0) { [string]$script:currentTurnImagePaths[0] } else { '' })
        currentImagePaths = @($script:currentTurnImagePaths)
        currentAudioPaths = @($script:currentTurnAudioPaths)
        audioEvents = [bool]$script:audioEvents
        multiImageOcr = [bool]($script:currentTurnMultiImageOcr -and $hasOcr)
        multiImageOcrMode = [bool]$script:multiImageOcrMode
        profile = $profile
        # The image pipeline is independent of the chat provider; whether the identity_edit mode
        # is offered is decided by the LOCAL IMAGE profile here.
        imageProfileLite = ([string]$script:apiSettings.localProfile -eq 'lite')
        hasSourceVideo = [bool]$script:lastUserVideo
        attachedDocuments = @($script:attachedDocuments.ToArray())
        model = $script:activeModel
        maxTokens = 8192
        compact = ($script:contextTokens -gt $script:compactAt)
        contextLimit = $script:ctxLimit
        effort = $null
        # The 'custom' base URL and provider settings are passed to the worker explicitly.
        apiSettings = $script:apiSettings
        baseUrl = (Get-CustomBaseUrl)
    }
    if ($script:providerId -ne 'local') {
        try {
            $apiProfile=$script:apiSettings.providers[$script:providerId]
            $jobData.model=if($modelChoice.SelectedIndex -ge 0){[string]$script:modelEntries[$modelChoice.SelectedIndex].model}else{''}
            $jobData.apiKey=Get-ProviderKey $script:providerId
            if (!$jobData.model -or (!$jobData.apiKey -and $script:providerId -ne 'custom')) { throw 'Enter the API model (and its key if needed) under API connections on the Model control tab.' }
        } catch { $send.Text='Send';$metric.Text=$_.Exception.Message;return }
    }
    # Collects every API that has a key so the local model can "go and ask" other providers.
    $consultProviders = @{}
    foreach ($otherId in (Get-ChatProviderIds)) {
        $otherProfile = $script:apiSettings.providers[$otherId]
        if (!$otherProfile -or !$otherProfile.model) { continue }
        $otherKey = Get-ProviderKey $otherId
        if (!$otherKey -and $otherId -ne 'custom') { continue }
        $consultProviders[$otherId] = @{ model = [string]$otherProfile.model; apiKey = $otherKey }
    }
    $jobData.consultProviders = $consultProviders
    if($script:apiSettings.selectedLocalModel -and $script:providerId -eq 'local'){$jobData.maxTokens=4096}
    $script:sharedRequest = [hashtable]::Synchronized(@{
        Cancel = $false; Cts = $null
        AccessMode = $script:accessMode; AskTool = $null; AskDetail = $null; AskAnswer = $null
        AskId = $null; AskDeadline = $null
    })
    $script:activeBubble = New-Bubble 'assistant'
    $script:thinkStart = $null
    $script:requestStart = Get-Date
    $script:prefillWatch = $true
    Show-ThinkingStatus $script:activeBubble $(if ($turnHasImage) { 'Reading image…' } else { 'Thinking…' })
    $script:streamGenerating = $false
    $script:lastSpeed = 0
    try {
        $script:chatJob = Start-ThreadJob -FilePath (Join-Path $root 'agent\qwen-inference.ps1') -ArgumentList $jobData, $script:sharedRequest
        # This flag refers only to the turn being sent; follow-up messages must not treat
        # the old image as if it were newly attached.
        $script:currentTurnHasImage = $false
        $script:currentTurnImagePaths = @()
        $script:currentTurnAudioPaths = @()
        $script:currentTurnMultiImageOcr = $false
        $script:busy = $true
        $send.Text = 'Stop'
        $metric.Text = 'Thinking…'
    } catch {
        $script:busy = $false
        $send.Text = 'Send'
        $metric.Text = 'Could not start the request: ' + $_.Exception.Message
    }
}

# Updates the last user image; if a different image arrived, keeps the previous one (the scene).
function Set-LastUserImage([string]$path) {
    if (!$path) { return }
    if ($script:lastUserImage -and $script:lastUserImage -ne $path) { $script:previousUserImage = $script:lastUserImage }
    $script:lastUserImage = $path
}

function Start-ImageGeneration([string]$prompt, [string]$aspect, [string]$assistantText, [string]$mode, [double]$strength, $realism, [string]$referenceImage, [bool]$sceneFromPrevious = $false) {
    $profile = if ($script:providerId -eq 'local') { Get-ProfileFromAlias $script:activeModel } else { '' }
    if (!$mode) { $mode = 'new' }
    # A reference found on the internet takes precedence over the image the user attached
    $sourceImage = $script:lastUserImage
    if ($referenceImage -and (Test-Path -LiteralPath $referenceImage -PathType Leaf)) { $sourceImage = $referenceImage }
    if ($mode -ne 'new' -and !$sourceImage) { $mode = 'new' }
    # Two-image editing: the previous image is the scene (source 1), the last image is the person (source 2).
    $sourceImageB = ''
    if ($mode -eq 'identity_edit' -and $sceneFromPrevious -and $script:previousUserImage -and (Test-Path -LiteralPath $script:previousUserImage -PathType Leaf)) {
        $sourceImageB = $sourceImage
        $sourceImage = $script:previousUserImage
    }
    $b = $script:activeBubble
    if (!$b) { $b = New-Bubble 'assistant'; $script:activeBubble = $b }
    $text = $assistantText
    if (!$text) { $text = 'Generating image…' }
    $modeLabel = switch ($mode) {
        'regenerate' { "Regenerate from image (strength $strength)" }
        'style_reference' { 'Style reference' }
        'identity_edit' { if ($sourceImageB) { 'Photo editing · scene + person' } else { 'Photo editing · identity preserved' } }
        default { 'Text to image' }
    }
    if ($realism) { $modeLabel += ' + realism' }
    $b.body.Text = $text + "`r`n`r`n$modeLabel`r`nPrompt: " + $prompt
    Relayout-Chat
    Scroll-ChatToEnd

    $jobData = @{
        # Start-ThreadJob -FilePath reads the target file as text and runs it;
        # $PSScriptRoot is always empty in that context (verified). So the root is
        # passed explicitly here; the worker uses it inside Get-InstallRoot.
        root = $root
        prompt = $prompt; aspect = $aspect; restoreProfile = $profile
        mode = $mode; strength = $strength; sourceImage = $sourceImage; sourceImageB = $sourceImageB
        realism = $realism
        # On the Lite profile ComfyUI runs in low-VRAM mode with Krea-2 Turbo GGUF.
        lite = ([string]$script:apiSettings.localProfile -eq 'lite')
    }
    if (Start-CloudMedia 'image' $jobData) { return }
    $jobData.localModel=[string]$script:apiSettings.imageLocalModel
    $script:sharedMedia = [hashtable]::Synchronized(@{ Cancel = $false; Process = $null })
    try {
        $script:imageJob = Start-ThreadJob -FilePath (Join-Path $root 'agent\imagegen-run.ps1') -ArgumentList $jobData, $script:sharedMedia
        $script:imageBusy = $true
        $script:modelReady = $false
        $script:activeModel = $null
        $send.Text = 'Stop'
        $send.Enabled = $true
        $metric.Text = 'Image generation: unloading the language model…'
    } catch {
        $script:imageBusy = $false
        $metric.Text = 'Could not start image generation: ' + $_.Exception.Message
    }
}

function Start-VideoGeneration([hashtable]$spec) {
    $profile = if ($script:providerId -eq 'local') { Get-ProfileFromAlias $script:activeModel } else { '' }
    $mode = [string]$spec.mode
    if (!$mode) { $mode = 'text' }
    $sourceFile = $null
    if ($mode -eq 'image') {
        $sourceFile = $script:lastUserImage
        $found = [string]$spec.referenceImage
        if ($found -and (Test-Path -LiteralPath $found -PathType Leaf)) { $sourceFile = $found }
    }
    elseif ($mode -eq 'video') { $sourceFile = $script:lastUserVideo }
    if ($mode -ne 'text' -and !$sourceFile) { $mode = 'text' }

    $b = $script:activeBubble
    if (!$b) { $b = New-Bubble 'assistant'; $script:activeBubble = $b }
    $text = [string]$spec.assistantText
    if (!$text) { $text = 'Generating video…' }
    $modeLabel = switch ($mode) {
        'image' { 'Image to video' }
        'video'  { 'Continue from video' }
        default  { 'Text to video' }
    }
    $b.body.Text = $text + "`r`n`r`n$modeLabel  ·  $([string]$spec.seconds) s  ·  $([string]$spec.aspect)`r`nPrompt: " + [string]$spec.prompt
    Relayout-Chat
    Scroll-ChatToEnd

    $jobData = @{
        # Start-ThreadJob -FilePath reads the target file as text and runs it;
        # $PSScriptRoot is always empty in that context (verified). So the root is
        # passed explicitly here; the worker uses it inside Get-InstallRoot.
        root = $root
        prompt = [string]$spec.prompt; mode = $mode; aspect = [string]$spec.aspect
        seconds = [double]$spec.seconds; quality = [string]$spec.quality
        sourceFile = $sourceFile; restoreProfile = $profile
        # On the Lite profile resolution and duration are limited to the 8 GB VRAM budget.
        lite = ([string]$script:apiSettings.localProfile -eq 'lite')
    }
    if (Start-CloudMedia 'video' $jobData) { return }
    $jobData.localModel=[string]$script:apiSettings.videoLocalModel
    $script:sharedMedia = [hashtable]::Synchronized(@{ Cancel = $false; Process = $null })
    try {
        $script:videoJob = Start-ThreadJob -FilePath (Join-Path $root 'agent\videogen-run.ps1') -ArgumentList $jobData, $script:sharedMedia
        $script:imageBusy = $true
        $script:modelReady = $false
        $script:activeModel = $null
        $send.Text = 'Stop'
        $send.Enabled = $true
        $metric.Text = 'Video generation: unloading the language model…'
    } catch {
        $script:imageBusy = $false
        $metric.Text = 'Could not start video generation: ' + $_.Exception.Message
    }
}

# In turns where the model did not think at all (like OCR) showing the time as "Thought" would be misleading.
function Get-ThinkingLabel($bubble) {
    $verb = if ([string]::IsNullOrWhiteSpace([string]$bubble.thinkBox.Text)) { 'Done' } else { 'Thought' }
    return "$verb ($($bubble.seconds) s)"
}

function Finish-Thinking {
    $b = $script:activeBubble
    if (!$b -or !$b.thinkToggle -or !$b.thinkToggle.Visible) { return }
    if ($b.seconds) { return }
    $seconds = 0
    if ($script:thinkStart) { $seconds = [math]::Round(((Get-Date) - $script:thinkStart).TotalSeconds, 1) }
    $b.seconds = $seconds
    $b.thinkOpen = $false
    $b.thinkBox.Visible = $false
    $b.thinkToggle.Text = (Get-ThinkingLabel $b) + ' ▸'
}

function Render-JobEvent($jobEvent) {
    if (!$jobEvent -or !$jobEvent.kind) { return }
    $b = $script:activeBubble
    switch ([string]$jobEvent.kind) {
        'artifact' {
            if ($b) { Add-BubbleArtifact $b ([string]$jobEvent.text); $script:dirty = $true }
        }
        'chart' {
            # A dependency-free chart (PNG) was produced. Show it inline in the bubble; the
            # file is added to the artifacts list (inside Set-BubbleImage) and saved with the chat.
            if ($b) { Set-BubbleImage $b ([string]$jobEvent.text) }
            $script:dirty = $true
            Save-Chat
        }
        'denied' {
            # The note of a denied operation is shown independently of the model's text (added in 'done').
            if ($b) { $b.deniedNote = (@($b.deniedNote, [string]$jobEvent.text) | Where-Object { $_ }) -join "`r`n" }
            $metric.Text = [string]$jobEvent.text
        }
        'metrics' {
            $data = $jobEvent.data
            $script:streamGenerating = [bool]$data.generating
            $script:lastSpeed = [double]$data.speed
            $script:lastMetricAt = [DateTime]::UtcNow
            if ($null -ne $data.promptTokens) {
                $script:lastPromptTokens = [int]$data.promptTokens
                $script:lastCompletionTokens = [int]$data.completionTokens
                $script:contextTokens = $script:lastPromptTokens + $script:lastCompletionTokens
            }
            if ($null -ne $data.cachedPromptTokens) { $script:lastCachedTokens = [int]$data.cachedPromptTokens }
            if ($null -ne $data.processedPromptTokens) { $script:lastProcessedTokens = [int]$data.processedPromptTokens }
            if ($data.promptMs) { $script:lastPrefillMs = [double]$data.promptMs }
            if ($data.ttftMs) { $script:lastTtftMs = [double]$data.ttftMs }
            Update-LiveMetrics
        }
        'status' {
            $metric.Text = [string]$jobEvent.text
            # A new model call is starting: count the wait for the first token again.
            if ([string]$jobEvent.text -like 'Thinking*' -or [string]$jobEvent.text -like 'Agent turn*') {
                $script:requestStart = Get-Date
                $script:prefillWatch = $true
            }
            if ([string]$jobEvent.text -like '*mmproj*' -and $b) {
                Show-ThinkingStatus $b 'Reading image…'
            }
        }
        'ocr' {
            if ($b) {
                $b.ocrText = [string]$jobEvent.text
                if ($jobEvent.data -and $jobEvent.data.direct) {
                    $b.body.Text = $b.ocrText
                }
                $script:dirty = $true
            }
            if ($jobEvent.data -and $jobEvent.data.extracted) {
                $metric.Text = 'Text extraction complete'
                return
            }
            $confidence = if ($jobEvent.data -and $null -ne $jobEvent.data.confidence) {
                '{0:0%}' -f [double]$jobEvent.data.confidence
            } else { 'unknown' }
            $metric.Text = "OCR complete ($confidence confidence)"
        }
        'transcript' {
            # The attached audio was transcribed (Whisper). The transcript is shown in the
            # bubble and the language model answers over the findings (like OCR).
            if ($b) {
                $b.transcriptText = [string]$jobEvent.text
                if (![string]::IsNullOrWhiteSpace($b.transcriptText)) { $b.body.Text = $b.transcriptText }
                $script:dirty = $true
            }
            $modelLabel = if ($jobEvent.data -and $jobEvent.data.model) { [string]$jobEvent.data.model } else { 'Whisper' }
            $metric.Text = "Transcript ready ($modelLabel)"
        }
        'thinking' {
            $script:prefillWatch = $false
            if (!$b) { return }
            if (!$b.thinkToggle.Visible) {
                $b.thinkToggle.Visible = $true
                $script:thinkStart = Get-Date
            }
            $b.thinkBox.Visible = $true
            $b.thinkOpen = $true
            Append-ThinkingText $b.thinkBox ([string]$jobEvent.text)
            $script:dirty = $true
        }
        'answer' {
            $script:prefillWatch = $false
            if (!$b) { return }
            Finish-Thinking
            if (!$b.answerStarted) {
                $b.answerStarted = $true
                $b.answerStartLength = $b.body.Text.Length
            }
            $b.body.Text += [string]$jobEvent.text
            $script:dirty = $true
        }
        'answer-rewrite' {
            $script:prefillWatch = $false
            if (!$b) { return }
            Finish-Thinking
            $prefix = if ($b.answerStarted -and $b.answerStartLength -le $b.body.Text.Length) {
                $b.body.Text.Substring(0, $b.answerStartLength)
            } else { '' }
            $b.body.Text = $prefix + [string]$jobEvent.text
            $script:dirty = $true
        }
        'tool' {
            $names = @{ search_web = 'Web search'; fetch_page = 'Reading page'; list_bound_files = 'Listing folder'; read_bound_file = 'Reading file'; write_bound_file = 'Writing file'; run_command = 'Running command'; read_images_ocr = 'Multi-image reading on — reading images'; read_document = 'Reading document'; create_document = 'Creating document'; generate_image = 'Preparing image'; generate_video = 'Preparing video'; generate_chart = 'Creating chart' }
            $label = $names[[string]$jobEvent.text]
            if (!$label) { $label = [string]$jobEvent.text }
            $metric.Text = "$label…"
        }
        'compact' {
            if ($jobEvent.data) {
                $chatMessages.Clear()
                foreach ($m in @($jobEvent.data)) { [void]$chatMessages.Add($m) }
                # The compacted history is now much shorter; the counter continues with an estimate
                # until the real measurement of the next request so the threshold does not get stuck.
                $script:contextTokens = Estimate-ContextTokens
                $script:lastPromptTokens = $script:contextTokens
                $script:lastCompletionTokens = 0
                Update-LiveMetrics
            }
            $metric.Text = [string]$jobEvent.text
        }
        'videogen' {
            Finish-Thinking
            $spec = $jobEvent.data
            if ($jobEvent.usage) {
                $script:lastPromptTokens = [int]$jobEvent.usage.prompt_tokens
                $script:lastCompletionTokens = [int]$jobEvent.usage.completion_tokens
            }
            Start-VideoGeneration @{
                prompt = [string]$spec.prompt; mode = [string]$spec.mode; aspect = [string]$spec.aspect
                seconds = [double]$spec.seconds; quality = [string]$spec.quality
                assistantText = [string]$spec.assistantText
                referenceImage = [string]$spec.referenceImage
            }
        }
        'video' {
            if ($b) {
                $info = $jobEvent.data
                Set-BubbleVideo $b ([string]$jobEvent.text) ([string]$info.thumbnail)
                $b.body.Text = $b.body.Text -replace '^Generating video…', 'Video ready.'
                $b.body.Text += "`r`n`r`nSaved: " + [string]$jobEvent.text + "  (click to play)"
                [void]$chatMessages.Add(@{ role = 'assistant'; content = "[Video generated: $($info.width)x$($info.height), $($info.frames) frames / $($info.seconds) s, $($info.fps) fps, mode $($info.mode), file: $([string]$jobEvent.text)] Prompt: $($info.prompt)" })
            }
            $script:dirty = $true
            Save-Chat
        }
        'imagegen' {
            Finish-Thinking
            $spec = $jobEvent.data
            if ($jobEvent.usage) {
                $script:lastPromptTokens = [int]$jobEvent.usage.prompt_tokens
                $script:lastCompletionTokens = [int]$jobEvent.usage.completion_tokens
            }
            $strength = if ($spec.strength) { [double]$spec.strength } else { 0.6 }
            Start-ImageGeneration ([string]$spec.prompt) ([string]$spec.aspect) ([string]$spec.assistantText) ([string]$spec.mode) $strength $spec.realism ([string]$spec.referenceImage) ([bool]$spec.sceneFromPrevious)
        }
        'done' {
            Finish-Thinking
            if ($b) {
                $final = [string]$jobEvent.text
                if (!$final) { $final = $b.body.Text }
                $displayFinal = if ($b.ocrText -and $final -ne $b.ocrText) { "OCR result:`r`n$($b.ocrText)`r`n`r`n$final" }
        elseif ($b.transcriptText -and $final -ne $b.transcriptText) { "Audio transcript:`r`n$($b.transcriptText)`r`n`r`n$final" }
        else { $final }
                # Display only: the model's real text goes to $chatMessages so the KV cache match is not broken.
                if ($b.deniedNote) { $displayFinal = "$displayFinal`r`n`r`n$($b.deniedNote)".TrimStart() }
                if ($displayFinal) {
                    $b.rawText = $displayFinal
                    # During streaming we showed plain text; when finished we redraw the markdown formatted.
                    if ($displayFinal -match '(?m)^\s*(\||#{1,6}\s|[-*+]\s|\d+[.)]\s|```)' -or $displayFinal -match '\*\*.+?\*\*|`[^`]+`') {
                        Render-Markdown $b.body $displayFinal $colText
                        $b.hasRich = $true
                    } else {
                        # Turn LaTeX/math markers into readable text (see Convert-MathText).
                        $b.body.Text = (Convert-MathText $displayFinal)
                    }
                }
            }
            # If there were tool-call/tool-result turns add them first; the history the client
            # keeps must match what the server actually processed so the server's KV cache
            # can reuse it in the next turn.
            if ($jobEvent.data) {
                foreach ($m in @($jobEvent.data)) { [void]$chatMessages.Add($m) }
            }
            [void]$chatMessages.Add(@{ role = 'assistant'; content = [string]$jobEvent.text })
            if ($jobEvent.usage) {
                $script:lastPromptTokens = [int]$jobEvent.usage.prompt_tokens
                $script:lastCompletionTokens = [int]$jobEvent.usage.completion_tokens
                $script:contextTokens = $script:lastPromptTokens + $script:lastCompletionTokens
            }
            if ($b -and $b.ocrText) { $metric.Text = 'Ready — single-image mmproj mode' }
            $script:multiImageOcrMode = $false
            Reset-CurrentTurnImageState
            $script:dirty = $true
            Save-Chat
        }
        'cancelled' {
            Finish-Thinking
            if ($b) { $b.body.Text += "`r`n[Response stopped]" }
            $metric.Text = 'Response stopped; the model is in memory.'
            $script:multiImageOcrMode = $false
            Reset-CurrentTurnImageState
            $script:dirty = $true
        }
        'image' {
            if ($b) {
                Set-BubbleImage $b ([string]$jobEvent.text)
                $info = $jobEvent.data
                $b.body.Text = $b.body.Text -replace '^Generating image…', 'Image ready.'
                $b.body.Text += "`r`n`r`nSaved: " + [string]$jobEvent.text
                [void]$chatMessages.Add(@{ role = 'assistant'; content = "[Image generated: $($info.width)x$($info.height), seed $($info.seed), mode $($info.mode), file: $([string]$jobEvent.text)] Prompt: $($info.prompt)" })
            }
            $script:dirty = $true
            Save-Chat
        }
        'error' {
            Finish-Thinking
            if ($b) { $b.body.Text += "`r`n[Error] " + [string]$jobEvent.text }
            $metric.Text = 'Error: ' + [string]$jobEvent.text
            $script:multiImageOcrMode = $false
            Reset-CurrentTurnImageState
            $script:dirty = $true
        }
    }
}

function Show-MultiImageOcrNotice {
    $notice = New-Bubble 'notice'
    $notice.body.Text = 'Multi-image reading on — you can now upload several images. When done it will return to the single-image mmproj mode.'
    $metric.Text = $notice.body.Text
    $script:dirty = $true
    Relayout-Chat
    Scroll-ChatToEnd
    Save-Chat
}

$send.Add_Click({
    # Pressing Send while recording finishes the recording; the normal flow below sends the audio.
    if ($script:micRecording) {
        $micWav = Stop-MicrophoneCapture
        if ($micWav -and (Test-Path -LiteralPath $micWav -PathType Leaf)) {
            Add-SelectedAudio $micWav
            $metric.Text = 'Microphone recording attached; transcribing…'
        } else {
            $metric.Text = 'Microphone recording was not created (no device or access denied).'
        }
    }
    if ($script:imageBusy) { if($script:sharedMedia){Cancel-Generation}; return }
    if ($script:busy -or $script:pendingSend) { Cancel-Generation; return }
    $text = Get-PromptText
    $allPaths = @($script:selectedImages) + @($script:selectedVideos) + @($script:selectedDocuments) + @($script:selectedAudio)
    if (!$text -and $allPaths.Count -eq 0) { return }
    $activationIntent = Test-MultiImageOcrActivationIntent $text
    $activatingMode = $activationIntent -and !$script:multiImageOcrMode
    if ($activationIntent -and $allPaths.Count -eq 0) {
        $script:multiImageOcrMode = $true
        $userBubble = New-Bubble 'user'
        $userBubble.body.Text = $text
        $promptBox.Text = ''
        if ($activatingMode) { Show-MultiImageOcrNotice }
        else { $metric.Text = 'Multi-image reading is already on.' }
        $script:dirty = $true
        Relayout-Chat
        Scroll-ChatToEnd
        Save-Chat
        return
    }
    $multiImageRequested = $script:multiImageOcrMode -and $script:selectedImages.Count -gt 1
    if ($script:selectedImages.Count -gt 1 -and !$multiImageRequested -and !$activatingMode) {
        $metric.Text = 'First send the message "turn on multi-image reading mode"; in the normal system a single image can be used.'
        return
    }
    $script:currentTurnHasImage = $script:selectedImages.Count -gt 0
    $script:currentTurnImagePaths = @($script:selectedImages.ToArray())
    $script:currentTurnAudioPaths = @($script:selectedAudio.ToArray())
    $script:currentTurnMultiImageOcr = $multiImageRequested
    if ($script:currentTurnMultiImageOcr) {
        $metric.Text = 'Multi-image reading on — you can upload several images.'
    }
    $content = @()
    if ($text) { $content += @{ type = 'text'; text = $text } }
    foreach ($img in $script:selectedImages) {
        try { $content += @{ type = 'image_url'; image_url = @{ url = (Convert-ImageToDataUri $img) } } }
        catch { $metric.Text = 'Could not read the image: ' + $_.Exception.Message; return }
    }
    foreach ($vid in $script:selectedVideos) {
        # The video is not given to the model as an image; we add a note to the text so its first frame serves as context.
        $content += @{ type = 'text'; text = "[The user attached a video: $([IO.Path]::GetFileName($vid))]" }
    }
    foreach ($doc in $script:selectedDocuments) {
        $content += @{ type = 'text'; text = "[The user attached a document: $doc]" }
    }
    foreach ($aud in $script:selectedAudio) {
        # Audio will be transcribed with Whisper into text (the audio counterpart of OCR).
        $content += @{ type = 'text'; text = "[The user attached an audio file: $([IO.Path]::GetFileName($aud))]" }
    }
    if ($activatingMode) {
        $script:multiImageOcrMode = $true
        $script:currentTurnMultiImageOcr = $script:selectedImages.Count -gt 1
    }
    [void]$chatMessages.Add(@{ role = 'user'; content = $content })

    $bubble = New-Bubble 'user'
    $bubbleText = $text
    if (!$text -and $allPaths.Count -eq 1 -and $script:selectedDocuments.Count -eq 1) {
        $bubbleText = 'Document: ' + [IO.Path]::GetFileName($script:selectedDocuments[0])
    }
    if (!$text -and $allPaths.Count -eq 1 -and $script:selectedAudio.Count -eq 1) {
        $bubbleText = 'Audio: ' + [IO.Path]::GetFileName($script:selectedAudio[0])
    }
    $bubble.body.Text = $bubbleText
    # With several files a clickable name list (prevents overlapping); with a single file
    # a large preview as before.
    if ($allPaths.Count -gt 1) {
        Set-BubbleAttachmentList $bubble $allPaths
    } elseif ($script:selectedImages.Count -eq 1) {
        Set-BubbleImage $bubble $script:selectedImages[0]
    } elseif ($script:selectedVideos.Count -eq 1) {
        Set-BubbleVideo $bubble $script:selectedVideos[0] $null
    } elseif ($script:selectedDocuments.Count -eq 1) {
        Add-BubbleArtifact $bubble $script:selectedDocuments[0]
    } elseif ($script:selectedAudio.Count -eq 1) {
        Add-BubbleArtifact $bubble $script:selectedAudio[0]
    }
    if ($script:selectedImages.Count -gt 0) { Set-LastUserImage $script:selectedImages[$script:selectedImages.Count - 1] }
    if ($script:selectedVideos.Count -gt 0) { $script:lastUserVideo = $script:selectedVideos[$script:selectedVideos.Count - 1] }
    foreach ($document in $script:selectedDocuments) {
        if (!$script:attachedDocuments.Contains($document)) { $script:attachedDocuments.Add($document) }
    }
    if (!$script:chatTitle -and $text) {
        $script:chatTitle = $(if ($text.Length -gt 48) { $text.Substring(0, 48) + '…' } else { $text })
    }
    if ($activatingMode) { Show-MultiImageOcrNotice }
    Relayout-Chat
    Scroll-ChatToEnd

    $promptBox.Text = ''
    $script:selectedImages.Clear()
    $script:selectedVideos.Clear()
    $script:selectedDocuments.Clear()
    $script:selectedAudio.Clear()
    $clearImage.Visible = $false
    $script:pendingSend = $true
    $send.Text = 'Stop'
    # With an audio attachment start immediately even if the model is not ready (ASR preempts the load).
    if ($script:modelReady -or $script:providerId -ne 'local' -or $script:currentTurnAudioPaths.Count -gt 0) { Begin-Generation }
    else { $metric.Text = 'Loading the model… The message will be sent when it is ready.' }
})

$promptBox.Add_KeyDown({
    if ($_.Control -and $_.KeyCode -eq 'V') {
        if (Paste-ClipboardAttachments) {
            $_.SuppressKeyPress = $true
            $_.Handled = $true
            return
        }
    }
    if ($_.KeyCode -eq 'Enter' -and !$_.Shift) {
        $_.SuppressKeyPress = $true
        $_.Handled = $true
        if (!$script:busy -and !$script:pendingSend -and !$script:imageBusy) { $send.PerformClick() }
    }
})

# Right-click menu for the prompt box: paste clipboard content as media (image/document/
# audio/video) or plain text; cut/copy/select-all/clear.
$promptMenu = New-Object System.Windows.Forms.ContextMenuStrip
$pmPasteMedia = New-Object System.Windows.Forms.ToolStripMenuItem('Paste (image/document/audio)')
$pmPasteText = New-Object System.Windows.Forms.ToolStripMenuItem('Paste text')
[void]$promptMenu.Items.Add($pmPasteMedia)
[void]$promptMenu.Items.Add($pmPasteText)
[void]$promptMenu.Items.Add('-')
$pmCut = New-Object System.Windows.Forms.ToolStripMenuItem('Cut')
$pmCopy = New-Object System.Windows.Forms.ToolStripMenuItem('Copy')
$pmSelectAll = New-Object System.Windows.Forms.ToolStripMenuItem('Select all')
$pmClear = New-Object System.Windows.Forms.ToolStripMenuItem('Clear')
[void]$promptMenu.Items.Add($pmCut)
[void]$promptMenu.Items.Add($pmCopy)
[void]$promptMenu.Items.Add($pmSelectAll)
[void]$promptMenu.Items.Add($pmClear)
$pmPasteMedia.Add_Click({ [void](Paste-ClipboardAttachments) })
$pmPasteText.Add_Click({ if ([Windows.Forms.Clipboard]::ContainsText()) { $promptBox.SelectedText = [Windows.Forms.Clipboard]::GetText() } })
$pmCut.Add_Click({ $promptBox.Cut() })
$pmCopy.Add_Click({ $promptBox.Copy() })
$pmSelectAll.Add_Click({ $promptBox.SelectAll() })
$pmClear.Add_Click({ $promptBox.Clear() })
$promptBox.ContextMenuStrip = $promptMenu

$newChat = New-Object System.Windows.Forms.ToolStripMenuItem('New chat')
$fileMenu.DropDownItems.Add($newChat) | Out-Null
$exitItem = New-Object System.Windows.Forms.ToolStripMenuItem('Exit')
$fileMenu.DropDownItems.Add($exitItem) | Out-Null
$newChat.Add_Click({
    $tabs.SelectedTab = $chatTab
    Cancel-Generation
    $script:pendingSend = $false
    Save-Chat
    Clear-ChatView
    $script:chatId = $null
    $script:chatTitle = ''
    $welcome.Visible = $true
    Relayout-Chat
})
$historyItem = New-Object System.Windows.Forms.ToolStripMenuItem('Past chats…')
$fileMenu.DropDownItems.Insert(1, $historyItem)
$historyItem.Add_Click({ $tabs.SelectedTab = $chatTab; Show-ChatHistory })
$exitItem.Add_Click({ $form.Close() })

$showChat = New-Object System.Windows.Forms.ToolStripMenuItem('Chat screen')
$showControl = New-Object System.Windows.Forms.ToolStripMenuItem('Model control')
$viewMenu.DropDownItems.Add($showChat) | Out-Null
$viewMenu.DropDownItems.Add($showControl) | Out-Null
$showChat.Add_Click({ $tabs.SelectedTab = $chatTab })
$showControl.Add_Click({ $tabs.SelectedTab = $controlTab })

# The Profiles menu is rebuilt every time it opens; the installed/missing state is read
# from disk at that moment, so the menu is up to date by itself after a download finishes.
$profileMenu.Add_DropDownOpening({
    $profileMenu.DropDownItems.Clear()
    foreach ($profile in @(Get-ProfileCatalogForDisplay)) {
        $item = New-Object System.Windows.Forms.ToolStripMenuItem("$($profile.label) — $(Get-ProfileStatusLabel $profile.id)")
        $item.Tag = [string]$profile.id
        $item.Checked = ($script:providerId -eq 'local' -and [string]$script:apiSettings.localProfile -eq [string]$profile.id)
        $item.Add_Click({
            $id = [string]$this.Tag
            $state = Get-ProfileState $id
            # If files are missing, open the manager instead of switching directly.
            if (@($state.missingDownloadable).Count -or @($state.missingManual).Count) { Show-ProfileManager $id }
            else { [void](Switch-ToProfile $id) }
        })
        [void]$profileMenu.DropDownItems.Add($item)
    }
    if (!$profileMenu.DropDownItems.Count) {
        [void]$profileMenu.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripMenuItem('Could not read the catalog')))
    }
    [void]$profileMenu.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    $manager = New-Object System.Windows.Forms.ToolStripMenuItem('Profile manager…')
    $manager.Add_Click({ Show-ProfileManager ([string]$script:apiSettings.localProfile) })
    [void]$profileMenu.DropDownItems.Add($manager)
})

# ---------------------------------------------------------------- speed test
$benchBtn.Add_Click({
    if ($script:busy -or $script:imageBusy) { $statusCtrl.Text = 'Busy; wait before running the speed test.'; return }
    if (!$script:modelReady) { $statusCtrl.Text = 'The model is not loaded.'; return }
    $statusCtrl.Text = 'Speed test running…'
    $benchBtn.Enabled = $false
    $body = @{
        model = $script:activeModel
        messages = @(@{ role = 'user'; content = 'Explain the advantages of a language model running locally in 200 words.' })
        max_tokens = 400
        temperature = 0.6
        stream = $false
    } | ConvertTo-Json -Depth 10 -Compress
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-RestMethod 'http://127.0.0.1:8080/v1/chat/completions' -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 600
        $sw.Stop()
        $completion = [int]$response.usage.completion_tokens
        $promptTokens = [int]$response.usage.prompt_tokens
        $tps = if ($sw.Elapsed.TotalSeconds -gt 0) { [math]::Round($completion / $sw.Elapsed.TotalSeconds, 2) } else { 0 }
        $script:lastSpeed = $tps
        $script:benchReport = @(
            "SPEED TEST ($(Get-Date -Format 'HH:mm:ss'))"
            "model          : $script:activeModel"
            "prompt tokens  : $promptTokens"
            "output tokens  : $completion"
            "time           : $([math]::Round($sw.Elapsed.TotalSeconds,2)) s"
            "speed          : $tps token/s"
        ) -join "`r`n"
    } catch {
        $sw.Stop()
        $script:benchReport = 'Speed test failed: ' + $_.Exception.Message
    }
    $statusCtrl.Text = $script:benchReport
    $benchBtn.Enabled = $true
})

# ---------------------------------------------------------------- timers
$chatPoll = New-Object System.Windows.Forms.Timer
$chatPoll.Interval = 200
$chatPoll.Add_Tick({
    # Save the position before text append/finish operations change the control size.
    $wasNearBottom = Test-ChatNearBottom
    $oldChatScroll = -$chatScroll.AutoScrollPosition.Y
    # If the agent is waiting for permission ask that first (modal; re-entry is blocked with a flag)
    if ($script:sharedRequest -and $script:sharedRequest.AskTool -and !$script:approvalOpen) {
        $script:approvalOpen = $true
        $request = $script:sharedRequest
        $askId = [string]$request.AskId
        try {
            $answer = Show-ApprovalDialog $request
            # If the tool side gave up with a timeout/cancel (the id changed) no answer is written.
            if ($answer -and [string]$request.AskId -eq $askId) {
                $request.AskAnswer = $answer
                # We also clear it here right away so the same request is not shown a second
                # time in the short window until the tool side clears AskTool.
                $request.AskTool = $null
                $request.AskDetail = $null
            }
        } catch {
            if ([string]$request.AskId -eq $askId) { $request.AskAnswer = 'no'; $request.AskTool = $null; $request.AskDetail = $null }
        } finally { $script:approvalOpen = $false }
    }
    if ($script:chatJob) {
        try {
            foreach ($jobEvent in @(Receive-Job -Job $script:chatJob -ErrorAction SilentlyContinue)) { Render-JobEvent $jobEvent }
            if ($script:chatJob.State -in @('Completed', 'Failed', 'Stopped')) {
                # Also fetch the last events that finished between the first Receive-Job and the state check.
                foreach ($jobEvent in @(Receive-Job -Job $script:chatJob -ErrorAction SilentlyContinue)) { Render-JobEvent $jobEvent }
                if ($script:chatJob.State -eq 'Failed' -and !$script:sharedRequest.Cancel) {
                    Render-JobEvent ([pscustomobject]@{ kind = 'error'; text = [string]$script:chatJob.ChildJobs[0].JobStateInfo.Reason.Message })
                }
                Remove-Job -Job $script:chatJob -Force -ErrorAction SilentlyContinue
                $script:chatJob = $null
                $script:sharedRequest = $null
                $script:busy = $false
                $script:streamGenerating = $false
                $script:prefillWatch = $false
                if (!$script:imageBusy) { $send.Text = 'Send' }
                # If compaction is needed, take care of it while the user writes the next message.
                Start-BackgroundCompaction
            }
        } catch {
            $script:busy = $false
            $send.Text = 'Send'
        }
    }
    # Background compaction job: only its result matters, it does not interfere with the counter/status flow.
    if ($script:compactJob) {
        try {
            foreach ($jobEvent in @(Receive-Job -Job $script:compactJob -ErrorAction SilentlyContinue)) {
                if ([string]$jobEvent.kind -eq 'compact') { Render-JobEvent $jobEvent }
            }
            if ($script:compactJob.State -in @('Completed', 'Failed', 'Stopped')) {
                foreach ($jobEvent in @(Receive-Job -Job $script:compactJob -ErrorAction SilentlyContinue)) {
                    if ([string]$jobEvent.kind -eq 'compact') { Render-JobEvent $jobEvent }
                }
                Remove-Job -Job $script:compactJob -Force -ErrorAction SilentlyContinue
                $script:compactJob = $null
                $script:sharedCompact = $null
            }
        } catch {
            Remove-Job -Job $script:compactJob -Force -ErrorAction SilentlyContinue
            $script:compactJob = $null
            $script:sharedCompact = $null
        }
    }
    foreach ($slot in @('imageJob', 'videoJob')) {
        $job = Get-Variable -Name $slot -Scope Script -ValueOnly -ErrorAction SilentlyContinue
        if (!$job) { continue }
        try {
            foreach ($jobEvent in @(Receive-Job -Job $job -ErrorAction SilentlyContinue)) { Render-JobEvent $jobEvent }
            if ($job.State -in @('Completed', 'Failed', 'Stopped')) {
                foreach ($jobEvent in @(Receive-Job -Job $job -ErrorAction SilentlyContinue)) { Render-JobEvent $jobEvent }
                if ($job.State -eq 'Failed') {
                    Render-JobEvent ([pscustomobject]@{ kind = 'error'; text = [string]$job.ChildJobs[0].JobStateInfo.Reason.Message })
                }
                Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
                Set-Variable -Name $slot -Scope Script -Value $null
                $script:sharedMedia=$null
                $script:imageBusy = $false
                $send.Enabled = $true
                $send.Text = 'Send'
            }
        } catch {
            $script:imageBusy = $false
            $send.Enabled = $true
            $send.Text = 'Send'
        }
    }
    if ($script:dirty) {
        $script:dirty = $false
        Relayout-Chat
        if ($wasNearBottom) { Scroll-ChatToEnd }
        else { $chatScroll.AutoScrollPosition = New-Object System.Drawing.Point(0, $oldChatScroll) }
    }
    Update-LiveMetrics
})
$chatPoll.Start()

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({
    Update-ModelSwitch
    if ($script:providerId -ne 'local') {
        if ($script:pendingSend) { Begin-Generation }
        $statusCtrl.Text="MamidLabs · $script:providerId API`r`nModel: $($script:apiSettings.providers[$script:providerId].model)`r`nConnections are managed from the Settings menu."
        return
    }
    if ([string]$script:apiSettings.selectedModel -eq 'off') {
        $script:modelReady = $false
        $script:activeModel = $null
        if (!$script:busy) { $metric.Text = 'Model is off.' }
        $statusCtrl.Text = "Model is off`r`nYou can start one by selecting a model in the model selection box."
        return
    }
    if ($script:imageBusy) { return }
    try {
        if ($script:modelSwitchError) { throw $script:modelSwitchError }
        if ($script:switchProcess -or $script:pendingModelChange) { throw 'Loading model' }
        $health = Invoke-RestMethod 'http://127.0.0.1:8080/health' -TimeoutSec 1 -ErrorAction Stop
        if ($health.status -ne 'ok') { throw 'Loading model' }
        $active = Get-ActiveModel
        if (!$active -or $active -ne (Get-SelectedLocalAlias)) { throw 'The selected model is not ready yet' }
        $script:activeModel = $active
        $script:modelReady = $true
        if ($script:pendingSend) { Begin-Generation }
        $state = if ($script:busy) { 'Responding' } elseif ($script:multiImageOcrMode) { 'Multi-image reading on' } else { 'Ready' }
        $warn = if ($script:contextTokens -gt $script:compactAt) { '  [compaction queued]' } else { '' }
        if (!$script:busy) {
            $metric.Text = "$state$warn"
        }
        $folderState = if ($script:boundFolder) { $script:boundFolder } else { 'not bound' }
        $statusCtrl.Text = @(
            'llama health  : OK'
            "model         : $script:activeModel"
            "speed         : $([math]::Round($script:lastSpeed,2)) token/s"
            "context       : $script:contextTokens / $script:ctxLimit  (compaction threshold $script:compactAt)"
            ''
            "internet      : $(if ($netChip.Checked) { 'on' } else { 'off' })"
            "agent         : $(if ($agentChip.Checked) { 'on' } else { 'off' })"
            "multi OCR     : $(if ($script:multiImageOcrMode) { 'on — several images accepted next turn' } else { 'off — single-image mmproj' })"
            'generation tools: automatic by message'
            "bound folder  : $folderState"
            if ($script:benchReport) { ''; $script:benchReport }
        ) -join "`r`n"
    } catch {
        $script:modelReady = $false
        $script:activeModel = $null
        $loading = [bool](Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue)
        $stateText = if ($script:modelSwitchError) { $script:modelSwitchError }
            elseif ($loading -or $script:switchProcess -or $script:pendingModelChange) { "Loading $($modelChoice.SelectedItem)…" }
            else { "$($modelChoice.SelectedItem) could not be loaded or is off. You can start it by selecting it again." }
        if (!$script:busy) { $metric.Text = $stateText }
        $statusCtrl.Text = "$stateText`r`nYou can start one by selecting a model in the model selection box."
    }
})
$timer.Start()

# ---------------------------------------------------------------- shutdown
function Shutdown-All {
    if ($script:shuttingDown) { return }
    $script:shuttingDown = $true
    $chatPoll.Stop()
    $timer.Stop()
    Cancel-Generation
    Save-UiPreferences
    Save-Chat
    if ($script:sharedCompact) { $script:sharedCompact.Cancel = $true }
    foreach ($job in @($script:chatJob, $script:imageJob, $script:videoJob, $script:compactJob)) {
        if ($job) { try { Stop-Job -Job $job -ErrorAction SilentlyContinue; Remove-Job -Job $job -Force -ErrorAction SilentlyContinue } catch { } }
    }
    foreach ($stray in @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*ComfyUI\main.py*' })) {
        try { Stop-Process -Id $stray.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
    # Finish an in-flight switch before the final stop, otherwise it could start
    # a server after the window has already closed. Discard queued choices.
    $script:pendingModelChange = $null
    if ($script:switchProcess) {
        try {
            if (!$script:switchProcess.WaitForExit(55000)) { $script:switchProcess.Kill($true); $script:switchProcess.WaitForExit() }
            $script:switchProcess.Dispose()
        } catch { }
        $script:switchProcess = $null
    }
    try { $p = Invoke-Manage 'stop' $null; $p.WaitForExit(20000) | Out-Null; $p.Dispose() } catch { }
    foreach ($stray in @(Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue)) {
        try { Stop-Process -Id $stray.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}

$form.Add_FormClosing({ Shutdown-All })
$form.Add_FormClosed({
    if ($uiIconLarge) { $uiIconLarge.Dispose() }
    if ($appIcon) { $appIcon.Dispose() }
})
$form.Add_Shown({
    [QwenWindow]::ShowWindow($form.Handle, 5) | Out-Null
    [QwenWindow]::SetForegroundWindow($form.Handle) | Out-Null
    Update-WelcomeLayout
    Relayout-Chat
    # If the last choice is Off the app does not restart the model; it stops an old server that is still running.
    if ($script:providerId -eq 'local') {
        if ([string]$script:apiSettings.selectedModel -eq 'off') {
            Stop-LocalQwen
            $metric.Text = 'Model is off.'
        } else {
            $savedProfile = switch ([string]$script:apiSettings.selectedModel) {
                'builtin-flash' { 'flash'; break }
                'builtin-max' { 'max'; break }
                'builtin-lite' { 'lite'; break }
                default { if ($script:apiSettings.localProfile) { [string]$script:apiSettings.localProfile } else { 'max' } }
            }
            Restart-LocalQwen $savedProfile
            $metric.Text = 'Loading model…'
        }
    }
    # If "I will use an API" was chosen in the setup wizard, the connection window opens once on first launch.
    if ($script:apiSettings.firstRunApiSettings) {
        $script:apiSettings.Remove('firstRunApiSettings')
        Save-ApiSettings
        Show-ApiSettings
    }
})

[System.Windows.Forms.Application]::Run($form)
