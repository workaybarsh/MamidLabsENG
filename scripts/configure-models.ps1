#Requires -Version 7.0
<#
.SYNOPSIS
    Writes the post-install model choices to settings\api.json.

.DESCRIPTION
    The setup wizard calls this script with the bundled PowerShell 7. It uses the same
    settings keys as the windows in the app:

      -DefaultProfile       built-in profile to load on startup (max/flash/lite)
      -NoLocalModel         no local model is loaded on startup (API or a profile to install later)
      -LlmPath              your own GGUF language model; added to the list and selected
      -ImageModelPath       your own Krea 2 Turbo file; used in place without copying
      -VideoModelPath       your own Wan 2.2 TI2V 5B file; used in place without copying
      -OpenApiSettings      the app opens the API connections window on first launch

    API keys are deliberately not taken here: a key passed on the command line would
    show up in the process list. The key is entered in the app and stored with Windows DPAPI.

.EXAMPLE
    pwsh -File scripts\configure-models.ps1 -LlmPath D:\models\qwen.gguf -OpenApiSettings
#>
param(
    [string]$Root,
    [ValidateSet('', 'max', 'flash', 'lite')][string]$DefaultProfile = '',
    [switch]$NoLocalModel,
    [string]$LlmPath,
    [string]$ImageModelPath,
    [string]$VideoModelPath,
    [switch]$OpenApiSettings
)

$ErrorActionPreference = 'Stop'
if (!$Root) { $Root = Split-Path -Parent $PSScriptRoot }
. (Join-Path $Root 'agent\media-paths.ps1')

$settingsDir = Join-Path $Root 'settings'
$settingsPath = Join-Path $settingsDir 'api.json'
$settings = @{ selected = 'local'; providers = @{} }
if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    if (!$settings.providers) { $settings.providers = @{} }
}
$changes = [Collections.Generic.List[string]]::new()

if ($DefaultProfile) {
    $settings.selected = 'local'; $settings.selectedModel = "builtin-$DefaultProfile"
    $settings.localProfile = $DefaultProfile; $settings.selectedLocalModel = ''
    $changes.Add("Startup profile: $DefaultProfile")
}
if ($NoLocalModel -and !$LlmPath) {
    $settings.selected = 'local'; $settings.selectedModel = 'off'; $settings.selectedLocalModel = ''
    $changes.Add('No local model will be loaded on startup')
}

if ($LlmPath) {
    if (!(Test-Path -LiteralPath $LlmPath -PathType Leaf) -or [IO.Path]::GetExtension($LlmPath).ToLowerInvariant() -ne '.gguf') {
        throw "GGUF language model not found: $LlmPath"
    }
    if ([IO.Path]::GetFileName($LlmPath) -match '(?i)mmproj|projector') { throw 'An image projector (mmproj) cannot be added as a language model on its own.' }
    $full = [IO.Path]::GetFullPath($LlmPath)
    $models = @($settings.localModels | Where-Object { $_ })
    $existing = @($models | Where-Object { $_.path -eq $full })[0]
    if (!$existing) {
        $existing = @{ id = ('local-' + [guid]::NewGuid().ToString('N')); name = [IO.Path]::GetFileNameWithoutExtension($full); path = $full }
        $models += $existing
    }
    $settings.localModels = $models
    $settings.selected = 'local'; $settings.selectedModel = $existing.id; $settings.selectedLocalModel = $existing.id
    $changes.Add("Language model: $full")
}

$external = @($settings.externalMediaModels | Where-Object { $_ })
foreach ($item in @(@{ kind = 'image'; path = $ImageModelPath }, @{ kind = 'video'; path = $VideoModelPath })) {
    if (!$item.path) { continue }
    $external = @(Add-ExternalMediaModel $external $item.path $item.kind)
    $settings[($item.kind + 'LocalModel')] = [IO.Path]::GetFileName($item.path)
    $settings[($item.kind + 'Provider')] = 'local'
    $changes.Add(($(if ($item.kind -eq 'image') { 'Image model: ' } else { 'Video model: ' }) + [IO.Path]::GetFullPath($item.path)))
}
if ($ImageModelPath -or $VideoModelPath) {
    $settings.externalMediaModels = $external
    [void](Write-ComfyExtraModelPaths $Root $external)
}

if ($OpenApiSettings) {
    $settings.firstRunApiSettings = $true
    $changes.Add('The API connections window will open on first launch')
}

if (!$changes.Count) { Write-Host 'No changes.'; exit 0 }
[void][IO.Directory]::CreateDirectory($settingsDir)
$temp = $settingsPath + '.tmp'
[IO.File]::WriteAllText($temp, ($settings | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temp -Destination $settingsPath -Force
foreach ($change in $changes) { Write-Host ('   [ok]   ' + $change) -ForegroundColor Green }
exit 0
