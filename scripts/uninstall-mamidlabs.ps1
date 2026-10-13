#Requires -Version 5.1
<#
.SYNOPSIS
    Removes MamidLabs without leaving a trace.

.DESCRIPTION
    Deletes everything the installation left on the machine; the computer stays as if
    MamidLabs had never been installed:

      1. Processes running from the install root are stopped (the app, llama-server, ComfyUI).
      2. The install root: program, bundled PowerShell, llama.cpp, Python environments,
         ComfyUI, models, pip/Hugging Face/OCR caches, logs, chats,
         outputs and settings (including the encrypted API keys).
      3. The desktop shortcut pointing to this installation.
      4. The Windows uninstall entry (Settings > Apps > MamidLabs).
      5. The prerequisites installed by the setup wizard with winget (Python, Git, 7-Zip,
         VC++ Redistributable; only if MamidLabs installed them, settings\install-record.json).
         Ones installed beforehand are not touched.

    Files outside the install root are not touched: your own models chosen by file path
    stay where they are. If the root contains files that do not belong to MamidLabs you are asked first.

    The script runs with Windows PowerShell 5.1; since the bundled PowerShell 7 is deleted
    too, it does not use it. That is why the texts are ASCII.

    The script is run from the installation's own scripts\ folder; if -Root is not given
    the root is found from there.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-mamidlabs.ps1 -DryRun
    Lists what would be deleted, deletes nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-mamidlabs.ps1
    Shows the plan, asks for confirmation and deletes everything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-mamidlabs.ps1 -KeepUserData
    Chats, generated files and settings stay; everything else is deleted.
#>
param(
    [string]$Root,
    # Keeps the chats, outputs and settings folders.
    [switch]$KeepUserData,
    # Does not remove the prerequisites (Python, Git, 7-Zip, VC++) MamidLabs installed with winget.
    [switch]$KeepPrerequisites,
    # Also deletes the shared Python caches outside the install folder:
    # %LOCALAPPDATA%\pip\cache, ~\.cache\huggingface\xet, %TEMP%\torchinductor_<user>.
    # Other Python projects may use these too; they are deleted only on request.
    [switch]$PurgeSharedCaches,
    # Runs without asking for confirmation (used by the wizard's uninstaller).
    [switch]$Yes,
    # Only shows the plan, deletes nothing.
    [switch]$DryRun,
    # Called from the Inno Setup uninstaller: it deletes its own entry and the unins000.* files itself.
    [switch]$FromWizard,
    # For tests: another location instead of the real desktop and registry.
    [string]$DesktopPath = [Environment]::GetFolderPath('Desktop'),
    [string[]]$UninstallRegistryPaths = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    )
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# While the install root is being deleted, the script's working folder must not be there (the folder gets locked).
Set-Location -LiteralPath $env:TEMP
[Environment]::CurrentDirectory = $env:TEMP

function Write-Line([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }

if (!$Root) {
    # If the script runs from inside the installation (scripts\), the root is its parent folder.
    $parent = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { '' }
    if ($parent -and (Test-Path -LiteralPath (Join-Path $parent 'agent\profiles.json'))) { $Root = $parent }
    else { $Root = Join-Path $env:LOCALAPPDATA 'Programs\MamidLabs' }
}
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')

# ------------------------------------------------------------------ safety
function Test-ProtectedPath([string]$path) {
    $p = $path.TrimEnd('\')
    if ($p -match '^[A-Za-z]:$') { return $true }
    $protected = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData,
        $env:USERPROFILE, $env:APPDATA, $env:LOCALAPPDATA, [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('MyDocuments'), (Join-Path $env:USERPROFILE 'Downloads')) |
        Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') }
    return [bool]($protected | Where-Object { $_ -ieq $p })
}

if (!(Test-Path -LiteralPath $Root -PathType Container)) {
    Write-Line "Install folder not found: $Root" 'Yellow'
    exit 1
}
if (Test-ProtectedPath $Root) {
    Write-Line "Safety: $Root is a system or user folder; nothing was removed." 'Red'
    exit 1
}
$markers = @('agent\profiles.json', 'qwen-desktop.ps1', 'launch-qwen-desktop.vbs')
if (!($markers | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) })) {
    Write-Line "Safety: $Root does not look like a MamidLabs installation (no qwen-desktop.ps1 / agent\profiles.json); nothing was removed." 'Red'
    exit 1
}

# -------------------------------------------------------------------- plan
# Everything the installation creates in the root. Entries not listed here are asked about.
$programEntries = @('agent', 'scripts', 'assets', 'models', 'logs', 'llama.cpp', 'ComfyUI', 'venv',
    'runtime', 'tools', 'setup-source', 'requirements', 'qwen-desktop.ps1', 'launch-qwen-desktop.vbs',
    'README.md', 'LICENSE', 'EULA.txt')
$userEntries = @('chats', 'outputs', 'settings')
$wizardEntries = @('unins000.exe', 'unins000.dat', 'unins000.msg')

$existing = @(Get-ChildItem -LiteralPath $Root -Force | ForEach-Object { $_.Name })
$toDelete = @($existing | Where-Object { $programEntries -contains $_ })
if (!$KeepUserData) { $toDelete += @($existing | Where-Object { $userEntries -contains $_ }) }
if (!$FromWizard) { $toDelete += @($existing | Where-Object { $wizardEntries -contains $_ }) }
$known = $programEntries + $userEntries + $wizardEntries
$foreign = @($existing | Where-Object { $known -notcontains $_ })

# The install record is read before it is deleted.
$wingetPackages = @()
$recordPath = Join-Path $Root 'settings\install-record.json'
if (!$KeepPrerequisites -and (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
    try { $wingetPackages = @((Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json).wingetPackages | Where-Object { $_ }) } catch { }
}

# Processes running from the root. This script and the chain that started it (the wizard's
# uninstaller also carries the root path on its command line) are excluded.
function Get-AncestorIds {
    $ids = @($PID)
    $all = @(Get-CimInstance Win32_Process)
    $current = $all | Where-Object { $_.ProcessId -eq $PID }
    while ($current -and $current.ParentProcessId -and $ids -notcontains [int]$current.ParentProcessId) {
        $ids += [int]$current.ParentProcessId
        $current = $all | Where-Object { $_.ProcessId -eq $current.ParentProcessId }
    }
    return $ids
}
$skipIds = Get-AncestorIds
$rootPrefix = $Root + '\'
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $skipIds -notcontains [int]$_.ProcessId -and (
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.IndexOf($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
         $_.Name -match '^(python|pythonw|pwsh|powershell|wscript|cscript|llama-server)\.exe$'))
})

# Desktop shortcuts pointing to this installation: MamidLabs.lnk or the "MamidLabs (<folder>).lnk"
# created for a second installation. Another copy's shortcut is not touched.
$shortcuts = @()
if (Test-Path -LiteralPath $DesktopPath -PathType Container) {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($file in @(Get-ChildItem -LiteralPath $DesktopPath -Filter 'MamidLabs*.lnk' -File -ErrorAction SilentlyContinue)) {
        try {
            $link = $shell.CreateShortcut($file.FullName)
            if (($link.TargetPath + ' ' + $link.Arguments + ' ' + $link.WorkingDirectory).IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $shortcuts += $file.FullName
            }
        } catch { }
    }
}

# Uninstall entries belonging to this root. The wizard's uninstaller deletes its own entry itself.
$registryKeys = @()
foreach ($base in $UninstallRegistryPaths) {
    if (!(Test-Path -LiteralPath $base)) { continue }
    foreach ($key in @(Get-ChildItem -LiteralPath $base -ErrorAction SilentlyContinue)) {
        $props = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
        if (!$props -or [string]$props.DisplayName -notlike 'MamidLabs*') { continue }
        if (([string]$props.InstallLocation).TrimEnd('\') -ine $Root) { continue }
        if ($FromWizard -and $key.PSChildName -like '*_is1') { continue }
        $registryKeys += $key.PSPath
    }
}

$sharedCaches = @()
if ($PurgeSharedCaches) {
    $sharedCaches = @((Join-Path $env:LOCALAPPDATA 'pip\cache'), (Join-Path $env:USERPROFILE '.cache\huggingface\xet'),
        (Join-Path $env:TEMP ('torchinductor_' + $env:USERNAME))) | Where-Object { Test-Path -LiteralPath $_ }
}

# ---------------------------------------------------------------- show/confirm
Write-Line ''
Write-Line 'MamidLabs uninstall' 'White'
Write-Line "  install folder: $Root"
Write-Line ''
if ($processes) { Write-Line '  Processes to stop:'; $processes | ForEach-Object { Write-Line ("    - " + $_.Name + " (" + $_.ProcessId + ")") } }
Write-Line '  To delete:'
$toDelete | ForEach-Object { Write-Line ("    - " + (Join-Path $Root $_)) }
$shortcuts | ForEach-Object { Write-Line "    - $_" }
$registryKeys | ForEach-Object { Write-Line ("    - entry: " + ($_ -replace '^Microsoft\.PowerShell\.Core\\Registry::', '')) }
$wingetPackages | ForEach-Object { Write-Line "    - winget package (installed by MamidLabs): $_" }
$sharedCaches | ForEach-Object { Write-Line "    - shared cache: $_" }
if ($KeepUserData) { Write-Line '  Kept: chats, outputs, settings (chats, generated files, settings)' 'DarkGray' }
if ($foreign) {
    Write-Line '  The folder has entries that do not belong to MamidLabs (not deleted by default):' 'Yellow'
    $foreign | ForEach-Object { Write-Line ("    - " + (Join-Path $Root $_)) 'Yellow' }
}
Write-Line ''

if ($DryRun) { Write-Line 'Dry run (-DryRun): nothing was deleted.' 'Cyan'; exit 0 }

$deleteForeign = $false
if (!$Yes) {
    $answer = Read-Host 'Permanently delete all of it? (yes/no)'
    if ($answer -notmatch '^(y|yes)$') { Write-Line 'Cancelled; nothing was deleted.' 'Yellow'; exit 0 }
    if ($foreign) {
        $answer = Read-Host 'Delete the entries that do not belong to MamidLabs too? (yes/no)'
        $deleteForeign = $answer -match '^(y|yes)$'
    }
}
if ($deleteForeign) { $toDelete += $foreign }

# -------------------------------------------------------------------- apply
$failures = [Collections.Generic.List[string]]::new()

foreach ($process in $processes) {
    try { Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop; Write-Line ("  [stopped] " + $process.Name) 'Green' }
    catch { if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) { $failures.Add("could not stop process: $($process.Name) ($($process.ProcessId))") } }
}
if ($processes) { Start-Sleep -Seconds 2 }

# Deep venv/ComfyUI paths may exceed 260 characters; if Remove-Item is not enough rd is used.
function Remove-Entry([string]$path) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop } catch { }
        if (Test-Path -LiteralPath $path) {
            if (Test-Path -LiteralPath $path -PathType Container) { cmd.exe /d /c "rd /s /q `"\\?\$path`"" 2>$null | Out-Null }
            else { cmd.exe /d /c "del /f /q `"\\?\$path`"" 2>$null | Out-Null }
        }
        if (!(Test-Path -LiteralPath $path)) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

foreach ($name in $toDelete) {
    $path = Join-Path $Root $name
    if (Remove-Entry $path) { Write-Line "  [deleted] $path" 'Green' } else { $failures.Add("could not delete: $path") }
}
foreach ($shortcut in $shortcuts) {
    if (Remove-Entry $shortcut) { Write-Line "  [deleted] $shortcut" 'Green' } else { $failures.Add("could not delete: $shortcut") }
}
foreach ($key in $registryKeys) {
    try { Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction Stop; Write-Line '  [deleted] Windows uninstall entry' 'Green' }
    catch { $failures.Add("could not delete the entry (may need admin): $key") }
}
foreach ($cache in $sharedCaches) {
    if (Remove-Entry $cache) { Write-Line "  [deleted] $cache" 'Green' } else { $failures.Add("could not delete: $cache") }
}
foreach ($package in $wingetPackages) {
    $winget = (Get-Command winget -ErrorAction SilentlyContinue).Source
    if (!$winget) { $failures.Add("no winget; $package must be removed manually"); continue }
    Write-Line "  removing with winget: $package"
    & $winget uninstall --exact --id $package --silent --disable-interactivity --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -eq 0) { Write-Line "  [removed] $package" 'Green' } else { $failures.Add("winget could not remove $package (exit code $LASTEXITCODE)") }
}

# If the root is empty it is deleted too; if files not belonging to MamidLabs remain the folder stays.
if (!$FromWizard -and (Test-Path -LiteralPath $Root) -and !(Get-ChildItem -LiteralPath $Root -Force)) {
    if (Remove-Entry $Root) { Write-Line "  [deleted] $Root" 'Green' } else { $failures.Add("could not delete: $Root") }
}

Write-Line ''
if ($failures.Count) {
    Write-Line ("The uninstall could not be completed, " + $failures.Count + " problems:") 'Red'
    $failures | ForEach-Object { Write-Line "  - $_" 'Red' }
    Write-Line 'If a MamidLabs window is open, close it and run the script again.' 'Yellow'
} else {
    Write-Line 'MamidLabs was removed.' 'Green'
}
if (!$Yes) { [void](Read-Host 'Press Enter to close') }
if ($failures.Count) { exit 1 }
exit 0
