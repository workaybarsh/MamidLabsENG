#Requires -Version 5.1
<#
.SYNOPSIS
    MamidLabs install and preparation script.

.DESCRIPTION
    Makes MamidLabs runnable on a clean Windows machine: hardware
    preflight, folder layout, bundled PowerShell 7, llama.cpp CUDA build,
    Python environments and ComfyUI. Model files are not included here;
    they are downloaded from the Profiles menu in the app.

    Every stage can be rerun: an existing component is skipped, and reinstalled
    with -Force. If any step fails the script stops there and returns an
    error; it never reports a half-done installation as "complete".

.EXAMPLE
    pwsh -File scripts\install-mamidlabs.ps1 -Profile lite
    Full installation for an 8 GB VRAM machine.

.EXAMPLE
    pwsh -File scripts\install-mamidlabs.ps1 -Stage preflight
    Only the hardware and environment check; nothing is downloaded or written.

.EXAMPLE
    pwsh -File scripts\install-mamidlabs.ps1 -Profile lite -SkipMedia
    Minimal installation needed for chat; ComfyUI and the image/video environment are skipped.
#>
param(
    # ValidateSet is deliberately not used: with "pwsh -File script.ps1 -Stage a,b,c" the
    # shell passes the list as a single string and validation rejects it. Splitting and
    # checking are done manually below.
    [string[]]$Stage = @('all'),
    [ValidateSet('max', 'flash', 'lite')][string]$Profile = 'lite',
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Programs\MamidLabs'),
    [string]$Source,
    # Optional: the version written to the script-based uninstall entry (DisplayVersion),
    # read by the in-app update check. Defaults to MyAppVersion in the .iss under $Source.
    [string]$AppVersion,
    [switch]$SkipMedia,
    # The audio transcription environment (venv\asr) is separate from media: -SkipMedia skips media but not
    # audio; only when audio is not wanted is -SkipAudio given.
    [switch]$SkipAudio,
    [switch]$Force,
    # The setup wizard writes its own uninstall entry; the script does not create a second one.
    [switch]$NoUninstallEntry
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (!$Source) { $Source = Split-Path -Parent $PSScriptRoot }
# The pip cache (GBs of torch wheels) is written to the install root, not the user profile;
# when uninstall-mamidlabs.ps1 deletes the root no trace is left behind.
$env:PIP_CACHE_DIR = Join-Path $Root 'tools\pip-cache'

$script:KnownStages = @('all', 'preflight', 'layout', 'runtime', 'llama', 'python', 'asr', 'comfyui', 'verify')
$Stage = @($Stage | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (!$Stage) { $Stage = @('all') }
$unknown = @($Stage | Where-Object { $script:KnownStages -notcontains $_ })
if ($unknown) {
    throw ('Unknown stage: ' + ($unknown -join ', ') + '. Valid values: ' + ($script:KnownStages -join ', '))
}

# --------------------------------------------------------------------- versions
# Sizes are taken from the asset list of the corresponding GitHub release. Since no
# sha256 is published, integrity is verified by file length; a missing or partially
# downloaded file is caught because its size does not match.
$script:Packages = @{
    pwsh = @{
        url = 'https://github.com/PowerShell/PowerShell/releases/download/v7.6.6/PowerShell-7.6.6-win-x64.zip'
        file = 'PowerShell-7.6.6-win-x64.zip'; bytes = 106328873
    }
    llama = @{
        url = 'https://github.com/ggml-org/llama.cpp/releases/download/b10998/llama-b10998-bin-win-cuda-13.4-x64.zip'
        file = 'llama-b10998-bin-win-cuda-13.4-x64.zip'; bytes = 150116398
    }
    cudart = @{
        url = 'https://github.com/ggml-org/llama.cpp/releases/download/b10998/cudart-llama-bin-win-cuda-13.4-x64.zip'
        file = 'cudart-llama-bin-win-cuda-13.4-x64.zip'; bytes = 423535356
    }
}
# Audio transcription models (faster-whisper + AST). Prefetched into the HF cache
# (HF_HOME = root\tools\hf-home) at install time; no network needed at runtime. Whisper is
# the standard large-v3 English edition (CTranslate2 float16).
$script:AsrWhisperModel = 'Systran/faster-whisper-large-v3'
$script:AsrEventsModel = 'MIT/ast-finetuned-audioset-10-10-0.4593'
$script:LlamaBuild = 'b10998'
$script:ComfyRepo = 'https://github.com/comfyanonymous/ComfyUI.git'
# Commit verified end to end with image and video generation.
$script:ComfyCommit = 'b0f4b7b294ce482a2e071d9d762c133d38c7aa07'
# Custom node the Lite profile needs to load the Krea-2 Turbo GGUF model.
$script:ComfyGgufRepo = 'https://github.com/city96/ComfyUI-GGUF'
$script:ComfyGgufCommit = '6ea2651e7df66d7585f6ffee804b20e92fb38b8a'
# Custom node needed for photo editing (Krea 2 Identity Edit); no extra Python dependency.
$script:Krea2EditRepo = 'https://github.com/lbouaraba/comfyui-krea2edit'
$script:Krea2EditCommit = '86f886dac23013d88996e3a2e99093ba44d322fb'

# Inno Setup's AppId (must match packaging\windows\mamidlabs.iss). It identifies the
# wizard's {AppId}_is1 uninstall key; the script checks for it so a repeated "layout" run
# does not add a second, duplicate entry next to the wizard's own.
$script:MamidLabsAppId = '{BB5BDCB4-3C19-4599-88C4-5CEA1A7942CB}'

# Package lists are read from the files under requirements\ (single source of truth;
# derived from the actual imports in agent/*.py). If a file is not found the
# installation stops with a clear error; it does not silently continue with an empty list.
function Read-Requirements([string]$name) {
    $path = Join-Path (Join-Path $Source 'requirements') $name
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw ('Requirements file not found: ' + $path) }
    return @(Get-Content -LiteralPath $path | ForEach-Object { ($_ -replace '#.*', '').Trim() } | Where-Object { $_ })
}
$script:AppPackages = Read-Requirements 'app.txt'
# imagegen environment: ComfyUI and the video frame tools. The torch CUDA 13.0 wheels
# come from a separate index (not in requirements\imagegen.txt); ComfyUI's own
# requirements are installed afterwards.
$script:ImagegenTorch = @('torch', 'torchvision', 'torchaudio')
$script:ImagegenTorchIndex = 'https://download.pytorch.org/whl/cu130'
$script:ImagegenPackages = Read-Requirements 'imagegen.txt'
# Audio (ASR) environment: faster-whisper (Whisper large-v3) + AST audio events + pyannote.
# torch/torchaudio come from the CUDA 13.0 index separately (same index as imagegen).
$script:AsrTorch = @('torch', 'torchaudio')
$script:AsrPackages = Read-Requirements 'asr.txt'

# Minimum hardware per profile. On Lite it was chosen based on what is left after the
# ~1 GB of VRAM held by the Windows desktop.
$script:Requirements = @{
    lite  = @{ vramMiB = 7000;  ramGB = 14; diskGB = 58 }
    flash = @{ vramMiB = 15000; ramGB = 30; diskGB = 75 }
    max   = @{ vramMiB = 15000; ramGB = 30; diskGB = 75 }
}

# ----------------------------------------------------------------------- output
$script:Problems = [System.Collections.Generic.List[string]]::new()

function Write-Step([string]$text) { Write-Host ''; Write-Host ('== ' + $text) -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host ('   [ok]   ' + $text) -ForegroundColor Green }
function Write-Skip([string]$text) { Write-Host ('   [skip] ' + $text) -ForegroundColor DarkGray }
function Write-Note([string]$text) { Write-Host ('   [note] ' + $text) }
function Write-Warn([string]$text) {
    Write-Host ('   [warn] ' + $text) -ForegroundColor Yellow
    $script:Problems.Add('warning: ' + $text)
}

function Test-Stage([string]$name) {
    return ($Stage -contains 'all' -or $Stage -contains $name)
}

# Returns the stdout+stderr output of a native program as text.
# In Windows PowerShell 5.1, with $ErrorActionPreference 'Stop', a program whose stderr
# is redirected with 2>&1 (llama-server --version writes its version to stderr) stopped
# the script; so 'Continue' is used for the duration of the call. The exit code
# stays in $LASTEXITCODE.
function Invoke-NativeText([scriptblock]$command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return ((& $command 2>&1 | ForEach-Object { "$_" }) -join "`n").Trim() }
    finally { $ErrorActionPreference = $previous }
}

function Get-FreeSpaceGB([string]$path) {
    $qualifier = (Split-Path -Qualifier ([IO.Path]::GetFullPath($path)))
    $drive = Get-PSDrive -Name $qualifier.TrimEnd(':') -ErrorAction Stop
    return [math]::Round($drive.Free / 1GB, 1)
}

# --------------------------------------------------------------------- download
function Invoke-Download([string]$url, [string]$destination, [long]$expectedBytes) {
    $name = Split-Path -Leaf $destination
    if ((Test-Path -LiteralPath $destination -PathType Leaf) -and
        ((Get-Item -LiteralPath $destination).Length -eq $expectedBytes)) {
        Write-Skip ($name + ' already downloaded')
        return
    }
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    if (!$curl) { throw 'curl.exe not found; Windows 10 1803 or later is required.' }
    Write-Note ('Downloading ' + $name + ' (' + [math]::Round($expectedBytes / 1MB) + ' MB)...')
    # -C - resumes a partial download, --fail returns non-zero on an HTTP error.
    & $curl -L --fail --show-error --no-progress-meter --retry 5 --retry-delay 5 -C - -o $destination $url
    if ($LASTEXITCODE -ne 0) { throw ('Download failed (' + $name + '), curl exit code ' + $LASTEXITCODE) }
    if (!(Test-Path -LiteralPath $destination -PathType Leaf)) { throw ('Downloaded file missing: ' + $name) }
    $actual = (Get-Item -LiteralPath $destination).Length
    if ($actual -ne $expectedBytes) {
        Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        throw ('Size mismatch (' + $name + '): expected ' + $expectedBytes + ', got ' + $actual + '. The file was deleted, run again.')
    }
    Write-Ok ($name + ' downloaded and its size verified')
}

function Expand-Package([string]$archive, [string]$destination) {
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
}

# ------------------------------------------------------------------ preflight
function Invoke-Preflight {
    Write-Step ('Preflight - profile: ' + $Profile)
    $need = $script:Requirements[$Profile]

    $os = Get-CimInstance Win32_OperatingSystem
    Write-Note ($os.Caption + ' (' + $os.Version + ')')
    if ([version]$os.Version -lt [version]'10.0.19041') {
        Write-Warn 'Windows 10 2004 or later is recommended.'
    }

    $ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    if ($ram -lt $need.ramGB) { Write-Warn ('RAM ' + $ram + ' GB; ' + $need.ramGB + ' GB is recommended for this profile.') }
    else { Write-Ok ('RAM ' + $ram + ' GB') }

    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (!$smi) {
        Write-Warn 'nvidia-smi not found; the NVIDIA driver is not installed or is outside PATH. GPU acceleration may not work.'
    } else {
        $line = (& $smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
        $parts = $line -split '\s*,\s*'
        $vram = [int]$parts[1]
        Write-Note ('GPU: ' + $parts[0] + ', ' + $vram + ' MiB VRAM, driver ' + $parts[2])
        if ($vram -lt $need.vramMiB) {
            Write-Warn ('VRAM ' + $vram + ' MiB; the ' + $Profile + ' profile needs ' + $need.vramMiB + ' MiB. Choose a smaller profile.')
        } else {
            Write-Ok ('VRAM ' + $vram + ' MiB, enough for the ' + $Profile + ' profile')
        }
    }

    $free = Get-FreeSpaceGB $Root
    $modelNote = if ($Profile -eq 'lite') { 'model files need another ~36 GB' } else { 'model files need another ~52 GB' }
    if ($free -lt $need.diskGB) { Write-Warn ([string]$free + ' GB free on the ' + $Root + ' disk; ' + $need.diskGB + ' GB is required (' + $modelNote + ').') }
    else { Write-Ok ([string]$free + ' GB free disk (' + $modelNote + ')') }

    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (!$python) { Write-Warn 'python is not on PATH; Python 3.10+ is required.' }
    else {
        # The code is passed via stdin: Windows PowerShell 5.1 strips double quotes from
        # native arguments and Python code given with "-c" would fail with a syntax error.
        $version = Invoke-NativeText { 'import sys; print(str(sys.version_info[0]) + chr(46) + str(sys.version_info[1]))' | & $python - }
        if ($version -notmatch '^\d+\.\d+$') { Write-Warn ('Could not read the Python version: ' + $version) }
        elseif ([version]$version -lt [version]'3.10') { Write-Warn ('Python ' + $version + '; 3.10 or later is required.') }
        else { Write-Ok ('Python ' + $version + ' (' + $python + ')') }
    }

    if (!(Get-Command git -ErrorAction SilentlyContinue)) {
        if ($SkipMedia) { Write-Note 'git missing; ComfyUI will not be installed anyway because -SkipMedia was given.' }
        else { Write-Warn 'git not found; ComfyUI cannot be cloned.' }
    } else { Write-Ok 'git available' }

    # Needed for RAR extraction (office-tools.py read_rar); the wizard installs it
    # automatically via winget with -InstallPrereqs (see setup-run.ps1 Test-SevenZipReady),
    # this is informational only.
    $sevenZipFound = $false
    foreach ($base in @(${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
        if ($base -and (Test-Path -LiteralPath (Join-Path $base '7-Zip\7z.exe') -PathType Leaf)) { $sevenZipFound = $true; break }
    }
    if (!$sevenZipFound -and !(Get-Command 7z.exe -ErrorAction SilentlyContinue)) {
        Write-Warn '7-Zip not found; opening RAR files may not work (ZIP/PDF/Office are unaffected).'
    } else { Write-Ok '7-Zip available (RAR extraction works)' }

    # Needed for native builds like llama-server.exe; the wizard installs it automatically.
    $vcRedistFound = [bool]$env:WINDIR -and (Test-Path -LiteralPath (Join-Path $env:WINDIR 'System32\vcruntime140.dll') -PathType Leaf)
    if (!$vcRedistFound) {
        Write-Warn 'Visual C++ Redistributable (2015-2022, x64) not found; llama-server may not run.'
    } else { Write-Ok 'Visual C++ Redistributable available' }

    if (!(Test-Path -LiteralPath (Join-Path $Source 'mamidlabs-desktop.ps1') -PathType Leaf)) {
        throw ('mamidlabs-desktop.ps1 is missing in the source folder: ' + $Source)
    }
    Write-Ok ('Source package: ' + $Source)
}

# ------------------------------------------------------------------- folders
function Invoke-Layout {
    Write-Step 'Folder layout and program files'
    # Running from a copy of the install root itself (for example {root}\scripts\...) makes
    # the prune below delete the running script's own folder; the root program files are then
    # left untouched (they are already in place) while setup-source is still refreshed.
    $sameRoot = [IO.Path]::GetFullPath($Source).TrimEnd('\') -ieq [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $folders = @(
        'agent', 'scripts', 'assets', 'models', 'logs', 'chats', 'settings',
        'llama.cpp', 'ComfyUI', 'venv', 'runtime', 'tools', 'requirements',
        'outputs', 'outputs\images', 'outputs\videos', 'outputs\documents',
        'outputs\refs', 'outputs\thumbs', 'outputs\input'
    )
    foreach ($folder in $folders) {
        New-Item -ItemType Directory -Path (Join-Path $Root $folder) -Force | Out-Null
    }
    Write-Ok ($folders.Count.ToString() + ' folders ready')

    # If the app is open the icons and scripts are locked; copying stops halfway and the
    # installation is left in a mixed version. First check whether anything is open.
    $app = Join-Path $Root 'mamidlabs-desktop.ps1'
    $running = @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf($app, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
    if ($running) { throw 'MamidLabs is open; the program files are locked. Close the app and run the installation again.' }

    # EULA.txt is shown on the wizard's license page and the .iss [Files] embeds it into the
    # source package; a script-based install must copy it to the root (and to setup-source) too.
    # Source == Root: the files are already in place; copying a file onto itself errors.
    if (!$sameRoot) {
        foreach ($file in @('mamidlabs-desktop.ps1', 'launch-mamidlabs-desktop.vbs', 'README.md', 'LICENSE', 'EULA.txt')) {
            $from = Join-Path $Source $file
            if (Test-Path -LiteralPath $from -PathType Leaf) {
                Copy-Item -LiteralPath $from -Destination (Join-Path $Root $file) -Force
            }
        }
    }
    foreach ($folder in @('agent', 'scripts', 'assets', 'requirements')) {
        $from = Join-Path $Source $folder
        if (!(Test-Path -LiteralPath $from)) { continue }
        # Source == Root: the files are already in place; pruning would delete the running
        # script's own folder. Only an explicit source (repo or setup package) refreshes here.
        if ($sameRoot) { continue }
        $to = Join-Path $Root $folder
        # Owned program folders are pruned before copying so files removed from the source
        # (renamed/moved modules, build artifacts) do not linger in the install root. User
        # data (settings, chats, models, logs, outputs, tools, venvs, ComfyUI) is never pruned.
        if (Test-Path -LiteralPath $to) { Remove-Item -LiteralPath $to -Recurse -Force }
        Copy-Item -LiteralPath $from -Destination $Root -Recurse -Force
        Remove-ExcludedFromOwned $to $folder
    }
    Write-Ok ('Program files copied -> ' + $Root)

    # Top-level files renamed away from the old "qwen" engine names (if present) must not linger.
    Remove-LegacyTopFiles $Root

    # A wizard-installed root also keeps the program source under {root}\setup-source; Inno's
    # cancel/rollback and reinstall use it. Script-based updating (this stage) did not refresh
    # that folder, so it stayed at the previous version; if present it is refreshed with the
    # same [Files] set. It is not created for a script-based install (folder absent).
    $setupSource = Join-Path $Root 'setup-source'
    # The wizard runs this stage with -Source {root}\setup-source; refreshing the package from
    # itself would copy every top-level file/folder onto itself ("cannot overwrite itself") and
    # empty the package. Skip the refresh when the source IS the setup-source. Note that a
    # source merely INSIDE the root (this nested package) must still copy the program files
    # into the root, so the $sameRoot guard above stays equality-only.
    $setupSourceFull = [IO.Path]::GetFullPath($setupSource).TrimEnd('\')
    $sourceFull = [IO.Path]::GetFullPath($Source).TrimEnd('\')
    if ((Test-Path -LiteralPath $setupSource -PathType Container) -and ($sourceFull -ine $setupSourceFull)) {
        # The same top-level file list (including EULA.txt) is also written to the source package.
        foreach ($file in @('mamidlabs-desktop.ps1', 'launch-mamidlabs-desktop.vbs', 'README.md', 'LICENSE', 'EULA.txt')) {
            $from = Join-Path $Source $file
            if (Test-Path -LiteralPath $from -PathType Leaf) { Copy-Item -LiteralPath $from -Destination $setupSource -Force }
        }
        foreach ($folder in @('agent', 'scripts', 'requirements')) {
            $from = Join-Path $Source $folder
            if (!(Test-Path -LiteralPath $from)) { continue }
            $to = Join-Path $setupSource $folder
            # Same prune before copy as the install root, with the same .iss excludes.
            if (Test-Path -LiteralPath $to) { Remove-Item -LiteralPath $to -Recurse -Force }
            Copy-Item -LiteralPath $from -Destination $setupSource -Recurse -Force
            Remove-ExcludedFromOwned $to $folder
        }
        Remove-LegacyTopFiles $setupSource
        $assets = Join-Path $Source 'assets'
        if (Test-Path -LiteralPath $assets) {
            # assets\installer (wizard art) is copied too: the installed program files must stay
            # byte-identical to the repo.
            $assetsTarget = Join-Path $setupSource 'assets'
            if (Test-Path -LiteralPath $assetsTarget) { Remove-Item -LiteralPath $assetsTarget -Recurse -Force }
            New-Item -ItemType Directory -Path $assetsTarget -Force | Out-Null
            Get-ChildItem -LiteralPath $assets -Force | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination $assetsTarget -Recurse -Force
            }
        }
        $doc = Join-Path $Source 'docs\installation.md'
        if (Test-Path -LiteralPath $doc -PathType Leaf) {
            New-Item -ItemType Directory -Path (Join-Path $setupSource 'docs') -Force | Out-Null
            Copy-Item -LiteralPath $doc -Destination (Join-Path $setupSource 'docs\installation.md') -Force
        }
        Write-Ok ('Setup source package refreshed -> ' + $setupSource)
    }

    # Desktop shortcut: double-click to open, runs without opening a console window. If MamidLabs.lnk
    # points to an installation in another folder it is not touched; for this installation
    # "MamidLabs (<folder>).lnk" is created. The uninstaller deletes only the ones pointing to this root.
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $shell = New-Object -ComObject WScript.Shell
        $linkPath = Join-Path $desktop 'MamidLabs.lnk'
        if (Test-Path -LiteralPath $linkPath) {
            $existing = $shell.CreateShortcut($linkPath)
            $pointsHere = (($existing.TargetPath + ' ' + $existing.Arguments + ' ' + $existing.WorkingDirectory).IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0)
            if (!$pointsHere) { $linkPath = Join-Path $desktop ('MamidLabs (' + (Split-Path -Leaf $Root) + ').lnk') }
        }
        $target = Join-Path $Root 'launch-mamidlabs-desktop.vbs'
        $icon = Join-Path $Root 'assets\icons\mamidlabs.ico'
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and (!(Test-Path -LiteralPath $linkPath) -or $Force)) {
            $shortcut = $shell.CreateShortcut($linkPath)
            $shortcut.TargetPath = $target
            $shortcut.WorkingDirectory = $Root
            if (Test-Path -LiteralPath $icon -PathType Leaf) { $shortcut.IconLocation = $icon }
            $shortcut.Description = 'MamidLabs - local assistant'
            $shortcut.Save()
            Write-Ok ('Desktop shortcut created -> ' + $linkPath)
        } elseif (Test-Path -LiteralPath $linkPath) {
            Write-Skip 'Desktop shortcut already exists'
        }
    } catch {
        Write-Warn ('Could not create the desktop shortcut: ' + $_.Exception.Message)
    }

    # Shows up in the Settings > Apps list; "Uninstall" runs uninstall-mamidlabs.ps1.
    if ($NoUninstallEntry) { return }
    # The setup wizard already wrote its own ({AppId}_is1) entry for this root; writing the
    # script key too would show MamidLabs twice in the Apps list. A repeated "layout" run
    # must not add a duplicate.
    if (Test-InnoUninstallEntryExists $Root) {
        Write-Skip 'Windows uninstall entry already present (setup wizard)'
        return
    }
    try {
        $key = Join-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' (Get-UninstallKeyName $Root)
        $uninstaller = Join-Path $Root 'scripts\uninstall-mamidlabs.ps1'
        $command = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $uninstaller + '" -Root "' + $Root + '"'
        New-Item -Path $key -Force | Out-Null
        $values = @{
            DisplayName = 'MamidLabs (' + $Root + ')'
            Publisher = 'workaybarsh'; InstallLocation = $Root; UninstallString = $command
            DisplayIcon = (Join-Path $Root 'assets\icons\mamidlabs.ico'); URLInfoAbout = 'https://github.com/workaybarsh/MamidLabsENG'
        }
        # The in-app update check reads DisplayVersion; the script-based entry must carry it
        # too (from -AppVersion or MyAppVersion in the .iss under $Source, x.y.z).
        $appVersion = if ($AppVersion) { $AppVersion } else { Get-AppVersionFromSource $Source }
        if ($appVersion) { $values['DisplayVersion'] = $appVersion }
        foreach ($name in $values.Keys) { Set-ItemProperty -Path $key -Name $name -Value $values[$name] }
        foreach ($name in @('NoModify', 'NoRepair')) { Set-ItemProperty -Path $key -Name $name -Value 1 -Type DWord }
        Write-Ok 'Windows uninstall entry written (Settings > Apps > MamidLabs)'
    } catch {
        Write-Warn ('Could not write the uninstall entry: ' + $_.Exception.Message)
    }
}

# A separate entry per root: copies installed into two different folders do not overwrite
# each other's entry. uninstall-mamidlabs.ps1 generates the same name the same way.
function Get-UninstallKeyName([string]$path) {
    $normalized = [IO.Path]::GetFullPath($path).TrimEnd('\').ToLowerInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)) } finally { $sha.Dispose() }
    return 'MamidLabs-' + (($hash[0..3] | ForEach-Object { $_.ToString('x2') }) -join '')
}

# Does an Inno Setup ({AppId}_is1) uninstall entry for this root already exist? A wizard
# install writes one; the script-based entry must not be added a second time on top of it.
function Test-InnoUninstallEntryExists([string]$path) {
    $normalized = [IO.Path]::GetFullPath($path).TrimEnd('\')
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        $key = Join-Path ($hive + '\Software\Microsoft\Windows\CurrentVersion\Uninstall') ($script:MamidLabsAppId + '_is1')
        if (!(Test-Path -LiteralPath $key)) { continue }
        try {
            $props = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
            if ($props.InstallLocation -and (([string]$props.InstallLocation).TrimEnd('\') -ieq $normalized)) { return $true }
        } catch { }
    }
    return $false
}

# MyAppVersion in packaging\windows\mamidlabs.iss is the single source of the version
# (Semantic Versioning, MAJOR.MINOR.PATCH -> x.y.z). The script-based uninstall entry stores
# it as DisplayVersion so the in-app update check can read the local version even without a
# wizard install. Only a strict x.y.z value is accepted: the updater parses DisplayVersion as
# a [version] (see Test-MamidLabsUpdateAvailable), so any other value (a partial/extra part,
# a placeholder) is ignored and DisplayVersion is not written at all - a silently broken
# comparison is worse than no local version.
function Get-AppVersionFromSource([string]$source) {
    $iss = Join-Path $source 'packaging\windows\mamidlabs.iss'
    if (!(Test-Path -LiteralPath $iss -PathType Leaf)) { return $null }
    $match = [regex]::Match((Get-Content -LiteralPath $iss -Raw), '#define\s+MyAppVersion\s+"(\d+\.\d+\.\d+)"')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

# The Inno Setup [Files] section excludes build artifacts (agent\__pycache__\*, *.pyc).
# assets\installer\* is NOT excluded: the installed program files must stay byte-identical to
# the repo. `assets` is still passed, so the call shape is unchanged; it is a no-op now.
function Remove-ExcludedFromOwned([string]$path, [string]$name) {
    if (!(Test-Path -LiteralPath $path)) { return }
    if ($name -eq 'agent') {
        Get-ChildItem -LiteralPath $path -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath $path -Recurse -Force -File -Filter '*.pyc' -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

# Top-level files renamed away from the old "qwen" engine names must not linger on an update:
# Inno `ignoreversion` / `Copy-Item -Force` does not delete the old file. Renames inside folders
# are already handled when the owned folders are pruned; only the top-level files (root +
# setup-source) are removed here. The .iss [InstallDelete]/[UninstallDelete] and the uninstaller
# $programEntries target the same names.
function Remove-LegacyTopFiles([string]$dir) {
    if (!$dir -or !(Test-Path -LiteralPath $dir)) { return }
    foreach ($legacy in @('qwen-desktop.ps1', 'launch-qwen-desktop.vbs')) {
        $path = Join-Path $dir $legacy
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
    }
}

# --------------------------------------------------------- bundled PowerShell 7
function Invoke-Runtime {
    Write-Step 'Bundled PowerShell 7 runtime'
    $target = Join-Path $Root 'runtime\pwsh'
    $exe = Join-Path $target 'pwsh.exe'
    if ((Test-Path -LiteralPath $exe -PathType Leaf) -and !$Force) {
        Write-Skip 'runtime\pwsh already installed'
        return
    }
    # The app depends on Start-ThreadJob; it exists only in PowerShell 7. Since the pwsh
    # installed on the system may be a Store package (WindowsApps is locked), a portable
    # version is extracted separately.
    $cache = Join-Path $Root 'tools\downloads'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $archive = Join-Path $cache $script:Packages.pwsh.file
    Invoke-Download $script:Packages.pwsh.url $archive $script:Packages.pwsh.bytes
    Expand-Package $archive $target
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'pwsh.exe not found in the extracted archive.' }
    $reported = (& $exe -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')
    Write-Ok ('runtime\pwsh ready, version ' + $reported)
}

# ----------------------------------------------------------------- llama.cpp
function Invoke-Llama {
    Write-Step ('llama.cpp CUDA build (' + $script:LlamaBuild + ')')
    $target = Join-Path $Root ('llama.cpp\' + $script:LlamaBuild)
    $exe = Join-Path $target 'llama-server.exe'
    if ((Test-Path -LiteralPath $exe -PathType Leaf) -and !$Force) {
        Write-Skip 'llama-server.exe already installed'
        return
    }
    $cache = Join-Path $Root 'tools\downloads'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    foreach ($key in @('llama', 'cudart')) {
        $package = $script:Packages[$key]
        $archive = Join-Path $cache $package.file
        Invoke-Download $package.url $archive $package.bytes
        # Both are extracted into the same folder: llama-server.exe looks for the CUDA DLLs next to itself.
        Expand-Package $archive $target
    }
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'llama-server.exe not found in the extracted archive.' }
    Write-Ok 'llama.cpp and the CUDA runtime extracted'
}

# ------------------------------------------------------------------ audio (ASR)
# Speech transcription: faster-whisper (Whisper large-v3) + AST audio events. Models are
# prefetched into the HF cache (HF_HOME = root\tools\hf-home) at install time; no network is
# needed at runtime. venv\asr is created in the "python" stage (see Invoke-Python).
function Invoke-Asr {
    Write-Step 'Audio transcription models (Whisper large-v3 + AST)'
    if ($SkipAudio) { Write-Skip 'Audio transcription models (-SkipAudio)'; return }
    $python = Join-Path $Root 'venv\asr\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Warn 'venv\asr missing; run the "python" stage first.'
        return
    }
    $env:HF_HOME = Join-Path $Root 'tools\hf-home'
    $env:HF_HUB_DISABLE_TELEMETRY = '1'
    $env:MAMIDLABS_ASR_WHISPER = $script:AsrWhisperModel
    $env:MAMIDLABS_ASR_EVENTS = $script:AsrEventsModel
    $code = @'
import os
from huggingface_hub import snapshot_download
for repo in (os.environ["MAMIDLABS_ASR_WHISPER"], os.environ["MAMIDLABS_ASR_EVENTS"]):
    print("downloading: " + repo, flush=True)
    snapshot_download(repo_id=repo)
    print("done: " + repo, flush=True)
'@
    try {
        $output = Invoke-NativeText { $code | & $python - }
        if ($output -match 'done:') { Write-Ok 'Audio transcription models ready (HF cache)' }
        else { Write-Warn ('Audio models could not be verified: ' + $output) }
    } catch {
        Write-Warn ('Audio models could not be downloaded: ' + $_.Exception.Message)
    }
}

# ------------------------------------------------------------- Python environments
function New-Venv([string]$path, [string]$label) {
    $python = Join-Path $path 'Scripts\python.exe'
    if ((Test-Path -LiteralPath $python -PathType Leaf) -and !$Force) {
        Write-Skip ($label + ' environment already exists')
        return $python
    }
    $base = (Get-Command python -ErrorAction Stop).Source
    Write-Note ('Creating the ' + $label + ' environment...')
    & $base -m venv $path
    if ($LASTEXITCODE -ne 0) { throw ('Could not create the venv for ' + $label + '.') }
    & $python -m pip install --upgrade pip --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw ('Could not upgrade pip for ' + $label + '.') }
    return $python
}

function Install-Packages([string]$python, [string[]]$packages, [string]$label, [string]$index) {
    Write-Note ($label + ': ' + ($packages -join ', '))
    $arguments = @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check') + $packages
    if ($index) { $arguments += @('--index-url', $index) }
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw ($label + ' packages could not be installed.') }
    Write-Ok ($label + ' packages installed')
}

function Invoke-Python {
    Write-Step 'Python environments'
    $appPython = New-Venv (Join-Path $Root 'venv\app') 'app'
    Install-Packages $appPython $script:AppPackages 'app' $null

    # The audio (ASR) environment is independent of media: skipped only when audio is not wanted.
    if (!$SkipAudio) {
        $asrPython = New-Venv (Join-Path $Root 'venv\asr') 'audio (ASR)'
        Install-Packages $asrPython $script:AsrTorch 'audio torch (CUDA 13.0)' $script:ImagegenTorchIndex
        Install-Packages $asrPython $script:AsrPackages 'audio (ASR)' $null
    } else {
        Write-Skip 'audio (ASR) environment (audio not requested)'
    }

    if ($SkipMedia -and $SkipAudio) {
        Write-Skip 'imagegen environment (neither media nor audio requested)'
        return
    }
    $imagegen = New-Venv (Join-Path $Root 'venv\imagegen') 'imagegen'
    Install-Packages $imagegen $script:ImagegenTorch 'imagegen torch (CUDA 13.0)' $script:ImagegenTorchIndex
    Install-Packages $imagegen $script:ImagegenPackages 'imagegen media' $null

    Write-Note 'The OCR environments are not part of this script: scripts\install-ocr.ps1 -Warmup'
}

# --------------------------------------------------------------------- ComfyUI
# Brings an existing git copy to the pinned commit. Previously, if the folder existed it was
# skipped without looking; an old ComfyUI (7a0b5ee on a real machine, 6 days old) never
# moved to the pinned version. Local changes are backed up before being discarded.
function Sync-PinnedCheckout([string]$dir, [string]$repoUrl, [string]$commit, [string]$label) {
    # git writes warnings to stderr (for example a line ending warning); on 5.1 with 'Stop'
    # these would stop the script. Real errors are caught below by the exit code.
    $ErrorActionPreference = 'Continue'
    if (!(Test-Path -LiteralPath (Join-Path $dir '.git'))) {
        Write-Warn ($label + ' is not a git copy; its version cannot be verified. Reinstall with -Force for the pinned version.')
        return
    }
    $head = (& git -C $dir rev-parse HEAD 2>$null | Select-Object -First 1)
    if ($head -eq $commit) {
        Write-Skip ($label + ' already at the pinned version (' + $commit.Substring(0, 12) + ')')
        return
    }
    $headText = if ($head) { $head.Substring(0, [Math]::Min(12, $head.Length)) } else { 'unknown' }
    Write-Note ($label + ' is at version ' + $headText + '; moving to the pinned version ' + $commit.Substring(0, 12) + '...')
    $diff = (& git -C $dir diff 2>$null | Out-String)
    if ($diff.Trim()) {
        $backupDir = Join-Path $Root 'tools\backup'
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        $backup = Join-Path $backupDir ('{0}-{1}-{2:yyyyMMdd-HHmmss}.patch' -f ($label -replace '\W', '').ToLowerInvariant(), $headText, (Get-Date))
        [IO.File]::WriteAllText($backup, $diff, [Text.UTF8Encoding]::new($false))
        Write-Note ('Local changes backed up: ' + $backup)
    }
    if (!(& git -C $dir remote 2>$null | Where-Object { $_ -eq 'origin' })) { & git -C $dir remote add origin $repoUrl }
    & git -C $dir fetch --depth 1 origin $commit
    if ($LASTEXITCODE -ne 0) { throw ($label + ' could not be fetched (commit: ' + $commit + ').') }
    & git -C $dir checkout -q --force FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw ($label + ' could not be moved to the pinned version.') }
    Write-Ok ($label + ' brought to the pinned version (' + $commit.Substring(0, 12) + ')')
}

# Installs a ComfyUI custom node at a pinned commit, or brings an existing copy to that commit.
function Install-PinnedCustomNode([string]$comfyDir, [string]$name, [string]$repoUrl, [string]$commit) {
    $node = Join-Path $comfyDir ('custom_nodes\' + $name)
    if ((Test-Path -LiteralPath (Join-Path $node '__init__.py') -PathType Leaf) -and !$Force) {
        Sync-PinnedCheckout $node $repoUrl $commit $name
        return
    }
    if (Test-Path -LiteralPath $node -PathType Container) { Remove-Item -LiteralPath $node -Recurse -Force }
    New-Item -ItemType Directory -Path $node -Force | Out-Null
    & git -C $node init -q
    & git -C $node remote add origin $repoUrl
    & git -C $node fetch --depth 1 origin $commit
    if ($LASTEXITCODE -ne 0) { throw ($name + ' could not be fetched (commit: ' + $commit + ').') }
    & git -C $node checkout -q FETCH_HEAD
    if (!(Test-Path -LiteralPath (Join-Path $node '__init__.py') -PathType Leaf)) { throw ($name + ' __init__.py not found.') }
    Write-Ok ($name + ' cloned (commit ' + $commit.Substring(0, 12) + ')')
}

function Invoke-ComfyUI {
    Write-Step 'ComfyUI'
    if ($SkipMedia) { Write-Skip 'ComfyUI (-SkipMedia)'; return }
    $target = Join-Path $Root 'ComfyUI'
    $main = Join-Path $target 'main.py'
    # A running ComfyUI (from a previous session/open app) locks the files and makes git
    # operations hang; at the start of this stage any process belonging to this root is stopped.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -like '*ComfyUI\main.py*' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Note ('Stopped: ComfyUI (' + $_.ProcessId + ')') } catch { } }
    if ((Test-Path -LiteralPath $main -PathType Leaf) -and !$Force) {
        Sync-PinnedCheckout $target $script:ComfyRepo $script:ComfyCommit 'ComfyUI'
    } else {
        if (!(Get-Command git -ErrorAction SilentlyContinue)) { throw 'git not found; ComfyUI cannot be cloned.' }
        # Since the folder was created in the layout stage, it is cloned into an empty directory. It is
        # locked to a pinned commit: "git clone --depth 1" is tied to the default
        # branch (main), so two users installing on different dates could get different ComfyUI
        # versions and the workflows (workflow JSON, node API) could silently break.
        # Since GitHub supports shallow-fetching an arbitrary commit SHA, depth 1 is kept.
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        & git -C $target init -q
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the ComfyUI folder.' }
        & git -C $target remote add origin $script:ComfyRepo
        & git -C $target fetch --depth 1 origin $script:ComfyCommit
        if ($LASTEXITCODE -ne 0) { throw 'ComfyUI could not be fetched (commit: ' + $script:ComfyCommit + ').' }
        & git -C $target checkout -q FETCH_HEAD
        if ($LASTEXITCODE -ne 0) { throw 'ComfyUI checkout failed.' }
        if (!(Test-Path -LiteralPath $main -PathType Leaf)) { throw 'ComfyUI main.py not found.' }
        Write-Ok ('ComfyUI cloned (commit ' + $script:ComfyCommit.Substring(0, 12) + ')')
    }
    $python = Join-Path $Root 'venv\imagegen\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'No imagegen environment; run -Stage python first.' }
    Write-Note 'Installing the ComfyUI requirements (torch is already installed, it is not downloaded again)...'
    & $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $target 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'The ComfyUI requirements could not be installed.' }
    Write-Ok 'ComfyUI dependencies installed'

    # The Lite profile uses the GGUF quantized version of Krea-2 Turbo (fits in 8 GB VRAM);
    # ComfyUI does not support it natively, city96/ComfyUI-GGUF is needed.
    Install-PinnedCustomNode $target 'ComfyUI-GGUF' $script:ComfyGgufRepo $script:ComfyGgufCommit
    # Photo editing: the Krea2EditModelPatch and Krea2EditGroundedEncode nodes.
    Install-PinnedCustomNode $target 'comfyui-krea2edit' $script:Krea2EditRepo $script:Krea2EditCommit
    & $python -m pip install --quiet --disable-pip-version-check gguf
    if ($LASTEXITCODE -ne 0) { throw 'The gguf package could not be installed.' }
    Write-Ok 'ComfyUI-GGUF dependency (gguf package) installed'

    foreach ($folder in @('models\checkpoints', 'models\diffusion_models', 'models\unet', 'models\text_encoders', 'models\vae', 'models\loras')) {
        New-Item -ItemType Directory -Path (Join-Path $target $folder) -Force | Out-Null
    }
    Write-Ok 'ComfyUI model folders ready'
}

# -------------------------------------------------------------------- verification
function Invoke-Verify {
    Write-Step 'Installation verification'
    $checks = @(
        @{ label = 'Application';          path = 'mamidlabs-desktop.ps1' },
        @{ label = 'Profile catalog';      path = 'agent\profiles.json' },
        @{ label = 'Profile module';       path = 'agent\profile-catalog.ps1' },
        @{ label = 'Model manager';        path = 'scripts\manage-mamidlabs.ps1' },
        @{ label = 'Bundled PowerShell 7'; path = 'runtime\pwsh\pwsh.exe' },
        @{ label = 'llama-server';         path = ('llama.cpp\' + $script:LlamaBuild + '\llama-server.exe') },
        @{ label = 'app environment';       path = 'venv\app\Scripts\python.exe' }
    )
    if (!$SkipMedia -or !$SkipAudio) {
        $checks += @{ label = 'imagegen environment'; path = 'venv\imagegen\Scripts\python.exe' }
    }
    if (!$SkipMedia) {
        $checks += @{ label = 'ComfyUI';         path = 'ComfyUI\main.py' }
        $checks += @{ label = 'ComfyUI-GGUF';    path = 'ComfyUI\custom_nodes\ComfyUI-GGUF\__init__.py' }
        $checks += @{ label = 'Photo editing nodes'; path = 'ComfyUI\custom_nodes\comfyui-krea2edit\__init__.py' }
    }
    if (!$SkipAudio) {
        $checks += @{ label = 'audio (ASR) environment'; path = 'venv\asr\Scripts\python.exe' }
        $checks += @{ label = 'whisper-tools.py'; path = 'agent\whisper-tools.py' }
    }
    foreach ($check in $checks) {
        # The folder inside the zip (e.g. 'Release\') can vary by version; if glob is set,
        # search for the file recursively under the folder.
        if ($check.glob) {
            $hit = @(Get-ChildItem -LiteralPath (Join-Path $Root $check.path) -Recurse -Filter $check.glob -File -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($hit.Count) { Write-Ok $check.label } else { Write-Warn ($check.label + ' missing: ' + $check.path + '\' + $check.glob) }
            continue
        }
        $full = Join-Path $Root $check.path
        if (Test-Path -LiteralPath $full -PathType Leaf) { Write-Ok ($check.label + ' -> ' + $check.path) }
        else { Write-Warn ($check.label + ' missing: ' + $check.path) }
    }

    $server = Join-Path $Root ('llama.cpp\' + $script:LlamaBuild + '\llama-server.exe')
    if (Test-Path -LiteralPath $server -PathType Leaf) {
        # --version runs without allocating the GPU; missing CUDA DLLs show up here.
        $output = Invoke-NativeText { & $server --version }
        if ($LASTEXITCODE -eq 0 -or $output) { Write-Ok ('llama-server responded: ' + ($output -split "`n")[0]) }
        else { Write-Warn 'llama-server could not be run; the CUDA runtime may be missing.' }
    }

    $appPython = Join-Path $Root 'venv\app\Scripts\python.exe'
    if (Test-Path -LiteralPath $appPython -PathType Leaf) {
        $modules = 'huggingface_hub,requests,PIL,ddgs,pypdf,reportlab,docx,pptx,openpyxl,rarfile'
        $script = 'import importlib.util' + "`n" +
                  'missing=[m for m in "' + $modules + '".split(",") if not importlib.util.find_spec(m)]' + "`n" +
                  'print("MISSING:"+",".join(missing) if missing else "OK")'
        # The code contains double quotes so it is passed via stdin (5.1 strips quotes from arguments).
        $result = Invoke-NativeText { $script | & $appPython - }
        if ($result -eq 'OK') { Write-Ok 'app packages can be imported' }
        else { Write-Warn ('missing package in the app environment -> ' + $result) }
    }

    Write-Step 'Summary'
    if ($script:Problems.Count) {
        Write-Host ('   ' + $script:Problems.Count + ' problems:') -ForegroundColor Yellow
        foreach ($problem in $script:Problems) { Write-Host ('     - ' + $problem) -ForegroundColor Yellow }
        Write-Host ''
        Write-Host '   Model files are not part of this script.' -ForegroundColor DarkGray
    } else {
        Write-Host '   Installation complete. Next steps:' -ForegroundColor Green
        Write-Host ''
        Write-Host ('   1. Open the app:  ' + (Join-Path $Root 'launch-mamidlabs-desktop.vbs'))
        Write-Host '   2. Choose the profile from the Profiles menu at the top left.'
        Write-Host '   3. Fetch the missing model files with "Profile manager -> Download missing".'
        if ($Profile -eq 'lite') {
            Write-Host '      For LITE about ~36 GB is downloaded in total (9B language model, Krea-2 Turbo GGUF + support files, Wan 2.2).'
        } else {
            Write-Host '      For MAX/FLASH about ~52 GB is downloaded in total (27B language model, Krea-2 Turbo fp8 + support files, Wan 2.2).'
        }
        Write-Host '   4. If you want OCR:  pwsh -File scripts\install-ocr.ps1 -Warmup'
    }
}

# ------------------------------------------------------------------------ flow
$stages = [ordered]@{
    preflight = { Invoke-Preflight }
    layout    = { Invoke-Layout }
    runtime   = { Invoke-Runtime }
    llama     = { Invoke-Llama }
    python    = { Invoke-Python }
    asr       = { Invoke-Asr }
    comfyui   = { Invoke-ComfyUI }
    verify    = { Invoke-Verify }
}

Write-Host ''
Write-Host 'MamidLabs installation' -ForegroundColor White
Write-Host ('  target : ' + $Root)
Write-Host ('  profile: ' + $Profile)
Write-Host ('  stage  : ' + ($Stage -join ', '))

foreach ($name in $stages.Keys) {
    if (Test-Stage $name) { & $stages[$name] }
}
Write-Host ''
