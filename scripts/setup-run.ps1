#Requires -Version 5.1
<#
.SYNOPSIS
    The workload of the one-click installer: applies the wizard's choices in order and writes a summary.

.DESCRIPTION
    The setup wizard (packaging\windows\mamidlabs.iss) extracts the program files and
    runs this script in a visible console. Steps:

      1. Prerequisites: if Python 3.10+ and Git are missing they are installed with winget (-InstallPrereqs).
      2. Profile report: logs\profile-report.txt
      3. Infrastructure: install-mamidlabs.ps1 (PowerShell 7, llama.cpp, Python, ComfyUI)
      4. Models: the files of the chosen profile are downloaded (-DownloadModels)
      5. Own models / API preference are written to settings\api.json
      6. OCR (optional)
      7. Smoke test: chat, document, offline tools, image, photo editing, post-media switch
      8. Summary: logs\setup-summary.txt (shown on the wizard's last page)

    Every step is reported independently; if a step fails the following ones are still
    attempted when meaningful, and the summary lists all of them. All output is also written to logs\setup.log.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-run.ps1 -Profile lite -DownloadModels -SmokeTest
#>
param(
    # none: no local profile is installed now (API, own model or a profile to be installed later).
    [ValidateSet('max', 'flash', 'lite', 'none')][string]$Profile = 'lite',
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Programs\MamidLabs'),
    [string]$Source,
    [switch]$InstallPrereqs,
    [switch]$DownloadModels,
    [switch]$SmokeTest,
    [switch]$Ocr,
    [string]$LlmPath,
    [string]$ImageModelPath,
    [string]$VideoModelPath,
    [switch]$OpenApiSettings,
    [string]$SummaryPath,
    # Called from the wizard: the wizard writes the uninstall entry, the script does not create a second one.
    [switch]$FromWizard
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (!$Source) { $Source = Split-Path -Parent $PSScriptRoot }
$logs = Join-Path $Root 'logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
if (!$SummaryPath) { $SummaryPath = Join-Path $logs 'setup-summary.txt' }
$started = Get-Date
try { Start-Transcript -LiteralPath (Join-Path $logs 'setup.log') -Append | Out-Null } catch { }

# The wizard (mamidlabs.iss, CurStepChanged) starts this script without waiting (ewNoWait) and
# tells whether setup is still running / has finished from these two files: the PID file lets it
# terminate this process (and all its child processes, via taskkill /T) if Cancel is clicked; the
# "done" flag tells it when to stop the wait loop and read the summary. It is also written on a
# normal finish, cancelled or not - without it the wizard would wait forever.
$pidFile = Join-Path $logs 'setup.pid'
$doneFlag = Join-Path $logs 'setup-done.flag'
Remove-Item -LiteralPath $doneFlag -ErrorAction SilentlyContinue
try { [System.Diagnostics.Process]::GetCurrentProcess().Id | Out-File -LiteralPath $pidFile -Encoding ascii -Force } catch { }

# The script runs hidden; progress is shown in the wizard. At every step a 3-line file
# (step number / total / step name) is written, which the wizard reads every ~200 ms. The
# total step count is known upfront (the OCR step is not counted when OCR was not requested).
$progressFile = Join-Path $logs 'setup-step.txt'
Remove-Item -LiteralPath $progressFile -ErrorAction SilentlyContinue
$totalSteps = 6 + $(if ($Ocr) { 1 } else { 0 })
$script:stepIndex = 0
function Set-InstallProgress([string]$name) {
    $script:stepIndex++
    try { [IO.File]::WriteAllText($progressFile, "$script:stepIndex`r`n$totalSteps`r`n$name") } catch { }
}
trap {
    $trapMsg = "error: $($_.Exception.Message)"
    try { [IO.File]::WriteAllText($doneFlag, $trapMsg) } catch {
        try { $trapMsg | Out-File -LiteralPath $doneFlag -Encoding utf8 -Force } catch { }
    }
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

$steps = [Collections.Generic.List[object]]::new()
function Add-Step([string]$name, [string]$state, [string]$detail, [TimeSpan]$elapsed) {
    $steps.Add([pscustomobject]@{ name = $name; state = $state; detail = $detail; seconds = [int]$elapsed.TotalSeconds })
    $color = switch ($state) { 'ok' { 'Green' } 'skipped' { 'DarkGray' } default { 'Red' } }
    Write-Host ("   [$state] $name" + $(if ($detail) { " - $detail" } else { '' })) -ForegroundColor $color
}

# Runs the step; records the duration and the result. If the script block returns $false or
# throws an error, the step counts as failed.
function Invoke-Step([string]$name, [scriptblock]$body) {
    Set-InstallProgress $name
    Write-Host ''
    Write-Host ('== ' + $name) -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $detail = & $body
        Add-Step $name 'ok' ([string]($detail | Select-Object -Last 1)) $sw.Elapsed
        return $true
    } catch {
        Add-Step $name 'failed' $_.Exception.Message $sw.Elapsed
        return $false
    }
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Test-PythonReady {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    # python.exe under WindowsApps is an empty shortcut that redirects to the Store.
    if (!$python -or $python -like '*\WindowsApps\*') { return $false }
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $version = ('import sys; print(sys.version_info[0]*100+sys.version_info[1])' | & $python - 2>$null) } finally { $ErrorActionPreference = $previous }
    return ([int]"0$version" -ge 310)
}

function Install-WithWinget([string]$id, [string]$label) {
    $winget = (Get-Command winget -ErrorAction SilentlyContinue).Source
    if (!$winget) { throw "$label was not found and winget is not available. Install $label manually and run the installation again." }
    Write-Host "   Installing $label with winget ($id)..."
    & $winget install --exact --id $id --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    # If there is no package in user scope it tries machine scope (may ask for admin approval).
    if ($LASTEXITCODE -ne 0) {
        & $winget install --exact --id $id --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    }
    if ($LASTEXITCODE -ne 0) { throw "$label could not be installed (winget exit code $LASTEXITCODE)." }
    Update-SessionPath
    Add-InstallRecord $id
}

# Records the prerequisites MamidLabs installed; uninstall-mamidlabs.ps1 removes only these,
# it does not touch a Python/Git the user had installed before.
function Add-InstallRecord([string]$id) {
    $path = Join-Path $Root 'settings\install-record.json'
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    $record = @{ wingetPackages = @() }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try { $record.wingetPackages = @((Get-Content -LiteralPath $path -Raw | ConvertFrom-Json).wingetPackages) } catch { }
    }
    $record.wingetPackages = @(@($record.wingetPackages) + $id | Where-Object { $_ } | Select-Object -Unique)
    $record | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8
}

$pwsh = Join-Path $Root 'runtime\pwsh\pwsh.exe'
$installProfile = if ($Profile -eq 'none') { 'lite' } else { $Profile }
$infraOk = $false
$modelsReady = $false

Write-Host ''
Write-Host 'MamidLabs one-click installation' -ForegroundColor White
Write-Host ("  target : $Root")
Write-Host ("  profile: $Profile")

# Looks for the 7z.exe needed for RAR extraction to work in the standard install locations;
# checks the same locations as read_rar in agent/office-tools.py (see there).
function Test-SevenZipReady {
    foreach ($base in @(${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
        if (!$base) { continue }
        $candidate = Join-Path $base '7-Zip\7z.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $true }
    }
    return [bool](Get-Command 7z.exe -ErrorAction SilentlyContinue)
}

# Visual C++ Redistributable 2015-2022 (x64) drops vcruntime140.dll into System32;
# native builds like llama-server.exe need it.
function Test-VCRedistReady {
    if (!$env:WINDIR) { return $false }
    return (Test-Path -LiteralPath (Join-Path $env:WINDIR 'System32\vcruntime140.dll') -PathType Leaf)
}

# 1. Prerequisites
$prereqOk = Invoke-Step 'Prerequisites (Python, Git, 7-Zip, VC++ Redistributable)' {
    $notes = @()
    if (!(Test-PythonReady)) {
        if (!$InstallPrereqs) { throw 'Python 3.10+ not found.' }
        Install-WithWinget 'Python.Python.3.12' 'Python'
        if (!(Test-PythonReady)) { throw 'Python was installed but could not be found in this session; restart the computer and run the installation again.' }
        $notes += 'Python installed'
    }
    if (!(Get-Command git -ErrorAction SilentlyContinue)) {
        if (!$InstallPrereqs) { throw 'Git not found.' }
        Install-WithWinget 'Git.Git' 'Git'
        if (!(Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git was installed but could not be found in this session.' }
        $notes += 'Git installed'
    }
    # A missing 7-Zip or VC++ Redistributable does not stop setup (RAR extraction / native
    # builds may not work without them, but chat and image generation are unaffected); if
    # -InstallPrereqs is not set it is silently skipped and the user is told to install manually.
    if (!(Test-SevenZipReady)) {
        if ($InstallPrereqs) {
            try { Install-WithWinget '7zip.7zip' '7-Zip'; $notes += '7-Zip installed' }
            catch { $notes += ('7-Zip could not be installed (' + $_.Exception.Message + '); RAR extraction may not work') }
        } else { $notes += '7-Zip missing; RAR extraction may not work' }
    }
    if (!(Test-VCRedistReady)) {
        if ($InstallPrereqs) {
            try { Install-WithWinget 'Microsoft.VCRedist.2015+.x64' 'Visual C++ Redistributable'; $notes += 'VC++ Redistributable installed' }
            catch { $notes += ('VC++ Redistributable could not be installed (' + $_.Exception.Message + ')') }
        } else { $notes += 'VC++ Redistributable missing' }
    }
    if ($notes) { $notes -join ', ' } else { 'present' }
}

# 2. Profile report
[void](Invoke-Step 'Profile report' {
    & (Join-Path $Source 'scripts\profile-report.ps1') -Root $Root -OutFile (Join-Path $logs 'profile-report.txt') -RecommendFile (Join-Path $logs 'profile-recommendation.txt')
    'recommendation: ' + (Get-Content -LiteralPath (Join-Path $logs 'profile-recommendation.txt') -Raw).Trim()
})

# 3. Infrastructure
if ($prereqOk) {
    $infraOk = Invoke-Step 'Infrastructure (PowerShell 7, llama.cpp, Python, ComfyUI)' {
        # The child script writes its own output; if it throws, the step fails.
        & (Join-Path $Source 'scripts\install-mamidlabs.ps1') -Profile $installProfile -Root $Root -Source $Source -NoUninstallEntry:$FromWizard | Out-Host
        if (!(Test-Path -LiteralPath $pwsh -PathType Leaf)) { throw 'The bundled PowerShell 7 was not installed.' }
        'installed'
    }
} else {
    Set-InstallProgress 'Infrastructure (PowerShell 7, llama.cpp, Python, ComfyUI)'
    Add-Step 'Infrastructure (PowerShell 7, llama.cpp, Python, ComfyUI)' 'skipped' 'prerequisites missing' ([TimeSpan]::Zero)
}

# 4. Models
if ($Profile -eq 'none') {
    Set-InstallProgress 'Model files'
    Add-Step 'Model files' 'skipped' 'the profile will be installed later from the Profile manager' ([TimeSpan]::Zero)
} elseif (!$DownloadModels) {
    Set-InstallProgress 'Model files'
    Add-Step 'Model files' 'skipped' "later: download-profile.ps1 -Profile $Profile" ([TimeSpan]::Zero)
} elseif (!$infraOk) {
    Set-InstallProgress 'Model files'
    Add-Step 'Model files' 'skipped' 'the infrastructure could not be installed' ([TimeSpan]::Zero)
} else {
    $modelsReady = Invoke-Step "Model files ($Profile)" {
        & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\download-profile.ps1') -Profile $Profile -Root $Root | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Some files could not be downloaded; try again from the Profile manager.' }
        'downloaded and their sizes verified'
    }
}

# 5. Model choices
if ($infraOk) {
    [void](Invoke-Step 'Model choices' {
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'scripts\configure-models.ps1'), '-Root', $Root)
        if ($Profile -ne 'none') { $arguments += @('-DefaultProfile', $Profile) } else { $arguments += '-NoLocalModel' }
        if ($LlmPath) { $arguments += @('-LlmPath', $LlmPath) }
        if ($ImageModelPath) { $arguments += @('-ImageModelPath', $ImageModelPath) }
        if ($VideoModelPath) { $arguments += @('-VideoModelPath', $VideoModelPath) }
        if ($OpenApiSettings) { $arguments += '-OpenApiSettings' }
        & $pwsh @arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'The settings could not be written.' }
        'settings\api.json updated'
    })
}

# 6. OCR
if ($Ocr -and $infraOk) {
    [void](Invoke-Step 'OCR components' {
        & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\install-ocr.ps1') -Warmup | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'OCR could not be installed.' }
        'installed'
    })
}

# 7. Smoke test
$smokeReport = Join-Path $logs 'smoke-test-last.json'
if (!$SmokeTest) {
    Set-InstallProgress 'Smoke test'
    Add-Step 'Smoke test' 'skipped' 'not requested' ([TimeSpan]::Zero)
} elseif (!$modelsReady) {
    Set-InstallProgress 'Smoke test'
    Add-Step 'Smoke test' 'skipped' 'the profile files are not ready' ([TimeSpan]::Zero)
} else {
    [void](Invoke-Step 'Smoke test' {
        # Video takes a few minutes; at the end of the installation chat, document, offline tools,
        # image, editing and the post-media model switch are verified. Video: run smoke-test.ps1 manually.
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'scripts\smoke-test.ps1'),
                       '-Profile', $Profile, '-Root', $Root, '-SkipVideo', '-ReportPath', $smokeReport)
        if (!$Ocr) { $arguments += '-SkipOcr' }
        & $pwsh @arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'At least one component could not be verified; details at the end of the summary.' }
        'all components verified'
    })
}

# 8. Summary
$failed = @($steps | Where-Object { $_.state -eq 'failed' })
$lines = [Collections.Generic.List[string]]::new()
$lines.Add('MamidLabs installation summary')
$lines.Add(('Date: {0:yyyy-MM-dd HH:mm}   Duration: {1:N0} min   Target: {2}' -f $started, ((Get-Date) - $started).TotalMinutes, $Root))
$lines.Add("Profile: $Profile")
$lines.Add('')
foreach ($step in $steps) {
    # .ToUpper() uses the CURRENT CULTURE; on a Turkish-locale Windows "skipped" becomes
    # "SKİPPED" (dotted capital İ, the Turkish-I problem). .ToUpperInvariant() is always
    # correct for English text regardless of the OS locale.
    $lines.Add(('[{0}] {1}{2}' -f $step.state.ToUpperInvariant(), $step.name, $(if ($step.detail) { ' - ' + $step.detail } else { '' })))
}
if (Test-Path -LiteralPath $smokeReport -PathType Leaf) {
    try {
        $smoke = Get-Content -LiteralPath $smokeReport -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([datetime]$smoke.time -ge $started) {
            $lines.Add('')
            $lines.Add('Smoke test details:')
            foreach ($property in $smoke.results.PSObject.Properties) {
                $entry = $property.Value
                $mark = if ($entry.skipped) { 'SKIP' } elseif ($entry.ok) { 'PASS' } else { 'FAIL' }
                $lines.Add(("  [{0}] {1} - {2}" -f $mark, $property.Name, $entry.detail))
            }
        }
    } catch { }
}
$reportFile = Join-Path $logs 'profile-report.txt'
if (Test-Path -LiteralPath $reportFile -PathType Leaf) {
    $lines.Add('')
    foreach ($line in (Get-Content -LiteralPath $reportFile -Encoding UTF8)) { $lines.Add($line) }
}
$lines.Add('')
if ($failed.Count) {
    $lines.Add("$($failed.Count) steps could not be completed. Log: $(Join-Path $logs 'setup.log')")
} else {
    $lines.Add('Installation complete. MamidLabs can be opened from the desktop shortcut.')
}
if ($Profile -eq 'none' -or !$DownloadModels) {
    $lines.Add('Profile files: in the app, Profiles → Profile manager → Download missing.')
}
if ($OpenApiSettings) { $lines.Add('API key: the app opens the API connections window on first launch.') }
$summary = $lines -join "`r`n"
[IO.File]::WriteAllText($SummaryPath, $summary, [Text.UTF8Encoding]::new($true))
Write-Host ''
Write-Host $summary
try { Stop-Transcript | Out-Null } catch { }

# The wizard waits for this done flag. A single write attempt can fail silently (likely an
# AV/file lock), so it is retried; the wizard also checks whether the process is still alive
# (mamidlabs.iss ProcessIsRunning).
$doneState = if ($failed.Count) { 'error' } else { 'ok' }
$doneWritten = $false
$doneError = $null
for ($i = 0; $i -lt 5 -and !$doneWritten; $i++) {
    try {
        [IO.File]::WriteAllText($doneFlag, $doneState, [Text.UTF8Encoding]::new($false))
        $doneWritten = $true
    } catch {
        $doneError = $_.Exception.Message
        Start-Sleep -Milliseconds 300
    }
}
if (!$doneWritten) {
    try { [IO.File]::WriteAllText($doneFlag, "error: could not write done flag - $doneError") } catch { }
}
if ($failed.Count) { exit 1 }
exit 0
