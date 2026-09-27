#Requires -Version 5.1
<#
.SYNOPSIS
    Profile report: tells which MamidLabs profile will run on this computer.

.DESCRIPTION
    The graphics card (VRAM), RAM and the free space on the target disk are measured; for
    each profile a "suitable / borderline / not suitable" verdict and its reason are written,
    and a profile is recommended. The setup wizard runs this script BEFORE the installation
    with Windows PowerShell 5.1; so only commands available in 5.1 are used. It installs/downloads nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\profile-report.ps1
#>
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Programs\MamidLabs'),
    # The catalog path can be given because the setup wizard runs the script from a temporary folder.
    [string]$Catalog,
    [string]$OutFile,
    [string]$JsonFile,
    # Only the recommended profile id (lite/flash/max/api); read by the setup wizard.
    [string]$RecommendFile
)

$ErrorActionPreference = 'Stop'

# Must stay the same as $script:Requirements in install-mamidlabs.ps1
# (test_setup_wizard.py checks this). On Lite it was chosen based on what is left after
# the ~1 GB of VRAM held by the Windows desktop.
$requirements = [ordered]@{
    lite  = @{ vramMiB = 7000;  ramGB = 14; diskGB = 58; label = 'LITE  (Qwen3.5 9B, 8 GB card)' }
    flash = @{ vramMiB = 15000; ramGB = 30; diskGB = 75; label = 'FLASH (Qwen3.8 27B, short thinking)' }
    max   = @{ vramMiB = 15000; ramGB = 30; diskGB = 75; label = 'MAX   (Qwen3.8 27B, deep thinking)' }
}

function Get-GpuInfo {
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (!$smi -and $env:SystemRoot) {
        # Some driver installations do not add nvidia-smi to PATH.
        $default = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
        if (Test-Path -LiteralPath $default) { $smi = $default }
    }
    if ($smi) {
        try {
            $line = (& $smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
            $parts = $line -split '\s*,\s*'
            return @{ name = $parts[0]; vramMiB = [int]$parts[1]; driver = $parts[2]; nvidia = $true }
        } catch { }
    }
    # Non-NVIDIA card or no driver: only the name is reported; the VRAM is unknown.
    $controller = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object -First 1
    $name = if ($controller) { [string]$controller.Name } else { 'not found' }
    return @{ name = $name; vramMiB = 0; driver = ''; nvidia = $false }
}

function Get-FreeDiskGB([string]$path) {
    try {
        $qualifier = Split-Path -Qualifier ([IO.Path]::GetFullPath($path))
        $drive = Get-PSDrive -Name $qualifier.TrimEnd(':') -ErrorAction Stop
        return [math]::Round($drive.Free / 1GB, 1)
    } catch { return 0 }
}

# Total size of the downloadable files in the catalog (the same target is counted once) and
# the names of the files with no download source that must be installed manually.
function Get-ProfileDownload([string]$id) {
    $result = @{ gb = 0; manual = @() }
    $catalog = if ($Catalog) { $Catalog } else { Join-Path (Split-Path -Parent $PSScriptRoot) 'agent\profiles.json' }
    if (!(Test-Path -LiteralPath $catalog -PathType Leaf)) { return $result }
    $profiles = (Get-Content -LiteralPath $catalog -Raw -Encoding UTF8 | ConvertFrom-Json).profiles
    $entry = @($profiles | Where-Object { $_.id -eq $id })[0]
    if (!$entry) { return $result }
    $bytes = 0
    $seen = @{}
    foreach ($file in @($entry.files)) {
        if ($seen.ContainsKey([string]$file.target)) { continue }
        $seen[[string]$file.target] = $true
        if ([string]$file.repo) { $bytes += [long]$file.bytes } else { $result.manual += [string]$file.label }
    }
    $result.gb = [math]::Round($bytes / 1GB, 1)
    return $result
}

$os = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
$gpu = Get-GpuInfo
$diskGB = Get-FreeDiskGB $Root

$verdicts = [ordered]@{}
foreach ($id in $requirements.Keys) {
    $need = $requirements[$id]
    $notes = [Collections.Generic.List[string]]::new()
    $status = 'suitable'
    if (!$gpu.nvidia) { $status = 'not suitable'; $notes.Add('NVIDIA graphics card/driver not found') }
    elseif ($gpu.vramMiB -lt $need.vramMiB) { $status = 'not suitable'; $notes.Add("VRAM $($gpu.vramMiB) MiB < $($need.vramMiB) MiB") }
    if ($ramGB -lt $need.ramGB) {
        # Low RAM does not prevent running but slows down model switches.
        if ($status -eq 'suitable') { $status = 'borderline' }
        $notes.Add("RAM $ramGB GB < recommended $($need.ramGB) GB")
    }
    if ($diskGB -lt $need.diskGB) {
        if ($status -eq 'suitable') { $status = 'borderline' }
        $notes.Add("free disk $diskGB GB < $($need.diskGB) GB")
    }
    $download = Get-ProfileDownload $id
    $verdicts[$id] = @{ status = $status; notes = @($notes); downloadGB = $download.gb; manualFiles = @($download.manual) }
}

# FLASH uses the same files as MAX but answers faster; it is recommended for daily
# use. If no local profile fits, an API or your own model is recommended.
$recommended = 'api'
foreach ($id in @('flash', 'lite')) {
    if ($verdicts[$id].status -ne 'not suitable') { $recommended = $id; break }
}

$lines = [Collections.Generic.List[string]]::new()
$lines.Add('MamidLabs profile report')
$lines.Add('')
$lines.Add("System   : $($os.Caption) ($($os.Version))")
$lines.Add("Processor: $cpu")
$lines.Add("RAM      : $ramGB GB")
$gpuText = if ($gpu.nvidia) { "$($gpu.name), $($gpu.vramMiB) MiB VRAM, driver $($gpu.driver)" } else { "$($gpu.name) (not NVIDIA or no driver)" }
$lines.Add("Graphics : $gpuText")
$lines.Add("Disk     : $diskGB GB free for $Root")
$lines.Add('')
foreach ($id in $verdicts.Keys) {
    $verdict = $verdicts[$id]
    $detail = if ($verdict.notes.Count) { ' - ' + ($verdict.notes -join '; ') } else { '' }
    # .ToUpper() uses the CURRENT CULTURE; on a Turkish-locale Windows this turns "suitable"
    # into "SUİTABLE" (dotted capital İ, the Turkish-I problem) instead of "SUITABLE".
    # .ToUpperInvariant() is culture-independent and always correct for English text.
    $lines.Add(('{0}: {1}{2}' -f $requirements[$id].label, $verdict.status.ToUpperInvariant(), $detail))
    if ($verdict.downloadGB -gt 0) { $lines.Add("        model files to download ~$($verdict.downloadGB) GB") }
    foreach ($manual in $verdict.manualFiles) { $lines.Add("        to install manually: $manual") }
}
$lines.Add('')
$lines.Add($(switch ($recommended) {
    'flash' { 'Recommendation: FLASH. The fastest daily use on a 16 GB card; for deep reasoning MAX also runs with the same files.' }
    'lite'  { 'Recommendation: LITE. The profile prepared for 8 GB cards.' }
    default { 'Recommendation: no local profile fits this hardware. Use an API (OpenAI, Claude, Gemini, DeepSeek) or your own small model.' }
}))
$text = $lines -join "`r`n"

if ($OutFile) { [IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($true)) }
if ($JsonFile) {
    $data = [ordered]@{
        recommended = $recommended; ramGB = $ramGB; diskGB = $diskGB
        gpu = $gpu.name; vramMiB = $gpu.vramMiB; nvidia = $gpu.nvidia; profiles = $verdicts
    }
    [IO.File]::WriteAllText($JsonFile, ($data | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
}
if ($RecommendFile) { [IO.File]::WriteAllText($RecommendFile, $recommended, [Text.Encoding]::ASCII) }
# If -OutFile was not given the report is printed to the screen.
if (!$OutFile) { $text }
