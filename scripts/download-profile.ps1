#Requires -Version 5.1
<#
.SYNOPSIS
    Downloads a profile's missing model files without opening the app.

.DESCRIPTION
    Does the same job as "Profiles -> Profile manager -> Download missing" in the app:
    reads the agent\profiles.json catalog, downloads the files that are not on disk and
    are downloadable (repo field set) from a pinned commit with agent\hf-hub.py and
    verifies their sizes. The setup wizard calls this script at the end of the installation;
    a user who chose "install later" can run the same command any time.

    The HF_TOKEN environment variable is used for gated repositories; the repositories in
    the catalog are public.

.EXAMPLE
    runtime\pwsh\pwsh.exe -File scripts\download-profile.ps1 -Profile lite
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('max', 'flash', 'lite')][string]$Profile,
    [string]$Root
)

$ErrorActionPreference = 'Stop'
if (!$Root) { $Root = Split-Path -Parent $PSScriptRoot }
# Windows PowerShell 5.1 writes ASCII to native programs by default; both directions are
# switched to UTF-8 so non-ASCII labels are not garbled.
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$catalogPath = Join-Path $Root 'agent\profiles.json'
$python = Join-Path $Root 'venv\app\Scripts\python.exe'
$hub = Join-Path $Root 'agent\hf-hub.py'
foreach ($required in @($catalogPath, $python, $hub)) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw "Not found: $required. Run install-mamidlabs.ps1 first." }
}

# Does the GGUF header contain an MTP/nextn block? Used to detect that an existing
# (MTP-less) model still needs the head added without re-downloading (see agent/mtp_inject.py).
function Test-GgufHasNextn([string]$Path) {
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $buffer = [byte[]]::new(16777216)
            $read = $fs.Read($buffer, 0, $buffer.Length)
        }
        finally { $fs.Dispose() }
        return ([Text.Encoding]::ASCII.GetString($buffer, 0, $read)).Contains('nextn')
    }
    catch { return $false }
}

$entry = @((Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json).profiles | Where-Object { $_.id -eq $Profile })[0]
if (!$entry) { throw "No such profile in the catalog: $Profile" }

$files = [Collections.Generic.List[object]]::new()
$manual = [Collections.Generic.List[string]]::new()
foreach ($file in @($entry.files)) {
    $target = Join-Path $Root ([string]$file.target)
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        # A file with an MTP config whose head is missing is still processed (added without re-downloading).
        if (!($file.mtp -and !(Test-GgufHasNextn $target))) { continue }
    }
    if (![string]$file.repo) {
        $hint = if ([string]$file.source) { " (source: $($file.source))" } else { '' }
        $manual.Add("$($file.label) -> $($file.target)$hint")
        continue
    }
    $files.Add([ordered]@{
        repo = [string]$file.repo; filename = [string]$file.filename; revision = [string]$file.revision
        target = $target; label = [string]$file.label; bytes = [long]$file.bytes; mtp = $file.mtp
    })
}

$totalGB = [math]::Round((($files | ForEach-Object { $_.bytes }) | Measure-Object -Sum).Sum / 1GB, 1)
Write-Host "Profile $($entry.label): $($files.Count) files to download (~$totalGB GB)."
$failed = $false
if ($files.Count) {
    $job = @{ action = 'download'; files = @($files.ToArray()); apiKey = $env:HF_TOKEN } | ConvertTo-Json -Depth 6 -Compress
    $job | & $python $hub | ForEach-Object {
        $message = $null
        try { $message = $_ | ConvertFrom-Json } catch { Write-Host $_; return }
        switch ([string]$message.kind) {
            'error' { $failed = $true; Write-Host ('   [error] ' + $message.text) -ForegroundColor Red }
            'status' { Write-Host ('   ' + $message.text) }
        }
    }
    if ($LASTEXITCODE -ne 0) { $failed = $true }
}
if ($manual.Count) {
    Write-Host ''
    Write-Host 'Files to install manually (no download source pinned in the catalog):' -ForegroundColor Yellow
    foreach ($line in $manual) { Write-Host ('   - ' + $line) -ForegroundColor Yellow }
}
if ($failed) { exit 1 }
Write-Host 'Download complete.' -ForegroundColor Green
exit 0
